"""Typed client for Fedora's ``dnf5daemon-server`` system D-Bus API.

Dendro deliberately delegates package authority to Fedora's daemon instead of
constructing shell commands or a second general-purpose root service.  The
server owns resolution, RPM locking, transaction execution, and Polkit
authorization.  This module owns only unprivileged request validation, typed
D-Bus marshalling, an in-memory resolved-plan capability, and presentation
progress.

The D-Bus API uses arrays and variant maps that PyQt cannot safely infer from
plain Python lists.  ``QtDbusTransport`` therefore serializes every method
argument from the API signature rather than relying on heuristic conversion.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from PyQt6.QtCore import QObject, QMetaType, QRunnable, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import (
    QDBusArgument,
    QDBusConnection,
    QDBusMessage,
    QDBusObjectPath,
    QDBusVariant,
)


SERVICE_NAME = "org.rpm.dnf.v0"
ROOT_PATH = "/org/rpm/dnf/v0"
SESSION_MANAGER_INTERFACE = "org.rpm.dnf.v0.SessionManager"
BASE_INTERFACE = "org.rpm.dnf.v0.Base"
GOAL_INTERFACE = "org.rpm.dnf.v0.Goal"
RPM_INTERFACE = "org.rpm.dnf.v0.rpm.Rpm"
REPOSITORY_INTERFACE = "org.rpm.dnf.v0.rpm.Repo"

# Qt's default D-Bus call timeout is generally 25 seconds.  A package
# transaction can legitimately download or process packages for much longer.
# Qt requires a finite signed integer, so this is the largest accepted value
# and is intentionally a practical "no client-side timeout", not a retry or
# polling policy.  Server errors, Polkit cancellation, and DNF transaction
# completion still provide the operation's terminal state.
NO_CLIENT_SIDE_TIMEOUT_MS = 2_147_483_647
LOGGER = logging.getLogger(__name__)


class Dnf5DaemonError(RuntimeError):
    """Base error with a user-facing title and actionable detail."""

    def __init__(self, title: str, detail: str, *, dbus_name: str = "") -> None:
        super().__init__(detail)
        self.title = title
        self.detail = detail
        self.dbus_name = dbus_name


class Dnf5DaemonUnavailable(Dnf5DaemonError):
    """The Fedora service or the permitted system-bus connection is absent."""


class Dnf5DaemonCallError(Dnf5DaemonError):
    """A structured D-Bus method call failed."""


class Dnf5DaemonValidationError(Dnf5DaemonError):
    """The frontend rejected an invalid request before contacting the daemon."""


class Dnf5DaemonPlanError(Dnf5DaemonError):
    """The caller attempted an invalid or stale resolved-plan transition."""


class Dnf5DaemonResolutionError(Dnf5DaemonError):
    """DNF's solver rejected a requested package transaction."""

    def __init__(self, detail: str, problems: Sequence[str]) -> None:
        super().__init__("Unable to resolve transaction", detail)
        self.problems = tuple(problems)


class DbusTransport(Protocol):
    """Small transport seam that enables deterministic client tests."""

    def call(
        self,
        path: str,
        interface: str,
        method: str,
        signatures: Sequence[str],
        arguments: Sequence[Any],
    ) -> tuple[Any, ...]:
        """Call a typed D-Bus method and return its decoded output arguments."""

    def subscribe(
        self,
        path: str,
        interface: str,
        signal: str,
        callback: Callable[[tuple[Any, ...]], None],
    ) -> object:
        """Subscribe to one signal and return an opaque unsubscription token."""

    def unsubscribe(self, token: object) -> None:
        """Remove a subscription returned by :meth:`subscribe`."""


@dataclass(frozen=True, slots=True)
class TransactionRequest:
    """The complete intent presented to DNF for one atomic resolution.

    No string is ever interpreted by a shell.  Strings are sent as values in
    typed D-Bus arrays only after they pass UTF-8/control-character validation.
    """

    installs: tuple[str, ...] = ()
    removals: tuple[str, ...] = ()
    upgrades: tuple[str, ...] = ()
    upgrade_all: bool = False

    def __post_init__(self) -> None:
        installs = _normalise_specs(self.installs, "install")
        removals = _normalise_specs(self.removals, "remove")
        upgrades = _normalise_specs(self.upgrades, "upgrade")

        if not installs and not removals and not upgrades and not self.upgrade_all:
            raise Dnf5DaemonValidationError(
                "No package changes requested",
                "Select at least one package action before resolving a transaction.",
            )
        if self.upgrade_all and upgrades:
            raise Dnf5DaemonValidationError(
                "Conflicting upgrade request",
                "A full system upgrade cannot be combined with a selective upgrade list.",
            )

        install_set = set(installs)
        removal_set = set(removals)
        upgrade_set = set(upgrades)
        conflicts = sorted(
            (install_set & removal_set)
            | (install_set & upgrade_set)
            | (removal_set & upgrade_set)
        )
        if conflicts:
            raise Dnf5DaemonValidationError(
                "Conflicting package actions",
                "The same package specification appears in incompatible actions: "
                + ", ".join(conflicts),
            )

        object.__setattr__(self, "installs", installs)
        object.__setattr__(self, "removals", removals)
        object.__setattr__(self, "upgrades", upgrades)

    @classmethod
    def from_values(
        cls,
        *,
        installs: Sequence[str] = (),
        removals: Sequence[str] = (),
        upgrades: Sequence[str] = (),
        upgrade_all: bool = False,
    ) -> "TransactionRequest":
        return cls(tuple(installs), tuple(removals), tuple(upgrades), upgrade_all)


@dataclass(frozen=True, slots=True)
class TransactionItem:
    """One structured item returned by ``Goal.resolve``."""

    object_type: str
    action: str
    reason: str
    attributes: Mapping[str, Any]
    package: Mapping[str, Any]

    @property
    def name(self) -> str:
        candidate = (
            self.package.get("name")
            or self.package.get("groupid")
            or self.package.get("id")
        )
        return str(candidate) if candidate is not None else "unknown item"

    @property
    def nevra(self) -> str:
        """Return the best complete identity available in the documented fields."""
        name = self.name
        version = str(self.package.get("version") or "")
        release = str(self.package.get("release") or "")
        arch = str(self.package.get("arch") or "")
        epoch = str(self.package.get("epoch") or "")
        if not version:
            return name
        evr = f"{epoch}:" if epoch and epoch != "0" else ""
        evr += version
        if release:
            evr += f"-{release}"
        return f"{name}-{evr}.{arch}" if arch else f"{name}-{evr}"

    @property
    def is_removal(self) -> bool:
        return self.action.casefold() in {"remove", "replaced"}

    @property
    def action_label(self) -> str:
        labels = {
            "install": "Install",
            "upgrade": "Upgrade",
            "downgrade": "Downgrade",
            "remove": "Remove",
            "reinstall": "Reinstall",
            "replaced": "Replace",
            "reason_change": "Change reason",
        }
        return labels.get(self.action.casefold(), self.action.replace("_", " ").title())


@dataclass(slots=True)
class TransactionPlan:
    """An unforgeable-in-process capability for one daemon-backed plan."""

    token: str
    session_path: str
    request: TransactionRequest
    items: tuple[TransactionItem, ...]
    resolver_result: int
    warnings: tuple[str, ...]
    critical_removals: tuple[str, ...]
    _closed: bool = field(default=False, repr=False)

    @property
    def has_critical_system_removal(self) -> bool:
        return bool(self.critical_removals)

    @property
    def has_changes(self) -> bool:
        return bool(self.items)

    def categorized_names(self) -> tuple[list[str], list[str], list[str]]:
        installs: list[str] = []
        removals: list[str] = []
        upgrades: list[str] = []
        for item in self.items:
            action = item.action.casefold()
            if action in {"install", "reinstall"}:
                installs.append(item.nevra)
            elif action in {"upgrade", "downgrade"}:
                upgrades.append(item.nevra)
            elif item.is_removal:
                removals.append(item.nevra)
        return installs, removals, upgrades

    def preview_text(self) -> str:
        """Stable, locale-safe display text derived from DNF's typed output."""
        lines = [
            "DNF5 daemon resolved this transaction. No change has been executed yet.",
            "",
        ]
        if self.warnings:
            lines.append("Resolver notices:")
            lines.extend(f"  - {warning}" for warning in self.warnings)
            lines.append("")
        if not self.items:
            lines.append("No package changes are required.")
            return "\n".join(lines)
        lines.append("Resolved package changes:")
        for item in self.items:
            lines.append(f"  {item.action_label}: {item.nevra} ({item.reason})")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class TransactionExecutionResult:
    """Terminal result reported after ``Goal.do_transaction`` returns."""

    completed_signal_received: bool
    completed_successfully: bool


@dataclass(frozen=True, slots=True)
class DnfRepository:
    """Repository information requested from the daemon, not parsed from host files."""

    identifier: str
    name: str
    enabled: bool
    baseurl: str
    repo_file: str


@dataclass(frozen=True, slots=True)
class DnfPackageRecord:
    """A bounded package record returned by the daemon's read-only RPM API."""

    name: str
    version: str
    release: str
    arch: str
    repository: str
    summary: str = ""
    size_bytes: int = 0
    epoch: str = "0"

    @property
    def name_arch(self) -> str:
        """Return DNF's architecture-qualified selector for this package."""
        return f"{self.name}.{self.arch}" if self.name and self.arch else self.name


@dataclass(frozen=True, slots=True)
class _Subscription:
    path: str
    interface: str
    signal: str
    callback_id: int


class _QtDbusSignalSink(QObject):
    """Receives all subscribed D-Bus signals through Qt's generic message slot."""

    def __init__(self) -> None:
        super().__init__()
        self.callbacks: dict[
            tuple[str, str, str], dict[int, Callable[[tuple[Any, ...]], None]]
        ] = {}

    @pyqtSlot(QDBusMessage)
    def receive(self, message: QDBusMessage) -> None:
        key = (message.path(), message.interface(), message.member())
        callbacks = tuple(self.callbacks.get(key, {}).values())
        if not callbacks:
            return
        arguments = tuple(_unwrap_dbus_value(value) for value in message.arguments())
        for callback in callbacks:
            try:
                callback(arguments)
            except Exception:
                # A malformed daemon signal or presentation callback must not
                # escape Qt's event dispatcher and destabilize the GUI thread.
                LOGGER.exception(
                    "DNF5 progress callback failed for %s.%s.", key[1], key[2]
                )


class QtDbusTransport:
    """QtDBus implementation with explicit D-Bus signatures for every call."""

    def __init__(self, connection: QDBusConnection | None = None) -> None:
        self._connection = connection or QDBusConnection.systemBus()
        if not self._connection.isConnected():
            error = self._connection.lastError()
            detail = (
                error.message()
                or "The system D-Bus is unavailable to this application."
            )
            raise Dnf5DaemonUnavailable(
                "DNF5 daemon is unavailable", detail, dbus_name=error.name()
            )
        self._sink = _QtDbusSignalSink()
        self._next_callback_id = 0
        self._subscriptions: dict[int, _Subscription] = {}
        self._lock = threading.RLock()

    def call(
        self,
        path: str,
        interface: str,
        method: str,
        signatures: Sequence[str],
        arguments: Sequence[Any],
    ) -> tuple[Any, ...]:
        if len(signatures) != len(arguments):
            raise Dnf5DaemonValidationError(
                "Internal D-Bus contract error",
                f"{interface}.{method} was given {len(arguments)} argument(s), expected {len(signatures)}.",
            )

        message = QDBusMessage.createMethodCall(SERVICE_NAME, path, interface, method)
        try:
            message.setArguments(
                [
                    _marshal_argument(signature, argument)
                    for signature, argument in zip(signatures, arguments, strict=True)
                ]
            )
        except (TypeError, ValueError) as exc:
            raise Dnf5DaemonValidationError("Invalid D-Bus request", str(exc)) from exc

        reply = self._connection.call(message, timeout=NO_CLIENT_SIDE_TIMEOUT_MS)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            error_name = reply.errorName()
            detail = (
                reply.errorMessage() or f"{interface}.{method} did not return a result."
            )
            if error_name in {
                "org.freedesktop.DBus.Error.ServiceUnknown",
                "org.freedesktop.DBus.Error.AccessDenied",
                "org.freedesktop.DBus.Error.Disconnected",
            }:
                raise Dnf5DaemonUnavailable(
                    "DNF5 daemon is unavailable", detail, dbus_name=error_name
                )
            raise Dnf5DaemonCallError(
                "DNF5 daemon request failed", detail, dbus_name=error_name
            )
        if reply.type() != QDBusMessage.MessageType.ReplyMessage:
            raise Dnf5DaemonCallError(
                "DNF5 daemon request failed",
                f"{interface}.{method} returned an unexpected D-Bus message type.",
            )
        return tuple(_unwrap_dbus_value(value) for value in reply.arguments())

    def subscribe(
        self,
        path: str,
        interface: str,
        signal: str,
        callback: Callable[[tuple[Any, ...]], None],
    ) -> object:
        key = (path, interface, signal)
        with self._lock:
            callbacks = self._sink.callbacks.setdefault(key, {})
            if not callbacks:
                connected = self._connection.connect(
                    SERVICE_NAME, path, interface, signal, self._sink.receive
                )
                if not connected:
                    error = self._connection.lastError()
                    self._sink.callbacks.pop(key, None)
                    raise Dnf5DaemonCallError(
                        "Could not subscribe to DNF5 progress",
                        error.message()
                        or f"D-Bus refused subscription to {interface}.{signal}.",
                        dbus_name=error.name(),
                    )
            self._next_callback_id += 1
            callback_id = self._next_callback_id
            callbacks[callback_id] = callback
            token = _Subscription(path, interface, signal, callback_id)
            self._subscriptions[callback_id] = token
            return token

    def unsubscribe(self, token: object) -> None:
        if not isinstance(token, _Subscription):
            return
        with self._lock:
            subscription = self._subscriptions.pop(token.callback_id, None)
            if subscription is None:
                return
            key = (subscription.path, subscription.interface, subscription.signal)
            callbacks = self._sink.callbacks.get(key)
            if callbacks is None:
                return
            callbacks.pop(subscription.callback_id, None)
            if callbacks:
                return
            self._sink.callbacks.pop(key, None)
            self._connection.disconnect(
                SERVICE_NAME,
                subscription.path,
                subscription.interface,
                subscription.signal,
                self._sink.receive,
            )


class _PlanProgress:
    """Converts daemon callback signals into monotonic presentation progress.

    Qt delivers subscribed signals on the GUI thread while ``Goal.do_transaction``
    runs in a worker.  A small lock protects the terminal completion snapshot
    read by that worker; presentation callbacks are always made after releasing
    the lock so they cannot re-enter mutable tracker state.
    """

    def __init__(
        self,
        session_path: str,
        emit_log: Callable[[str], None],
        emit_progress: Callable[[int], None],
    ) -> None:
        self.session_path = session_path
        self.emit_log = emit_log
        self.emit_progress = emit_progress
        self.downloads: dict[str, tuple[int, int]] = {}
        self.action_total = 0
        self.completed_actions: set[str] = set()
        self.last_percent = 0
        self.completed_signal_received = False
        self.completed_successfully = True
        self._lock = threading.RLock()

    def completion_status(self) -> tuple[bool, bool]:
        """Return a coherent completion-signal snapshot to the worker."""
        with self._lock:
            return self.completed_signal_received, self.completed_successfully

    def _belongs_to_session(self, values: tuple[Any, ...]) -> bool:
        return bool(values) and str(values[0]) == self.session_path

    @staticmethod
    def _non_negative_integer(value: Any, field: str) -> int | None:
        # D-Bus declares these fields as unsigned integers.  Do not let a
        # malformed external signal turn into an exception from Qt's dispatcher.
        if isinstance(value, bool):
            LOGGER.warning(
                "Ignoring malformed DNF5 progress signal: %s is boolean, not an integer.",
                field,
            )
            return None
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            LOGGER.warning(
                "Ignoring malformed DNF5 progress signal: %s is not an integer.", field
            )
            return None
        if number < 0:
            LOGGER.warning(
                "Ignoring malformed DNF5 progress signal: %s is negative.", field
            )
            return None
        return number

    def _set_progress(self, value: int) -> None:
        with self._lock:
            bounded = max(self.last_percent, min(100, value))
            if bounded == self.last_percent:
                return
            self.last_percent = bounded
        self.emit_progress(bounded)

    def download_add_new(self, values: tuple[Any, ...]) -> None:
        if not self._belongs_to_session(values) or len(values) < 4:
            return
        total = self._non_negative_integer(values[3], "download total")
        if total is None:
            return
        download_id, description = str(values[1]), str(values[2])
        with self._lock:
            self.downloads[download_id] = (total, 0)
        self.emit_log(f"Downloading: {description}\n")

    def download_progress(self, values: tuple[Any, ...]) -> None:
        if not self._belongs_to_session(values) or len(values) < 4:
            return
        total = self._non_negative_integer(values[2], "download total")
        downloaded = self._non_negative_integer(values[3], "downloaded bytes")
        if total is None or downloaded is None:
            return
        download_id = str(values[1])
        with self._lock:
            self.downloads[download_id] = (
                total,
                min(downloaded, total) if total else downloaded,
            )
            known_total = sum(
                item_total for item_total, _item_done in self.downloads.values()
            )
            known_done = sum(
                item_done for _item_total, item_done in self.downloads.values()
            )
        if known_total:
            self._set_progress(int((known_done / known_total) * 30))

    def download_end(self, values: tuple[Any, ...]) -> None:
        if not self._belongs_to_session(values) or len(values) < 4:
            return
        status = self._non_negative_integer(values[2], "download transfer status")
        if status is None:
            return
        if status != 0:
            self.emit_log(f"Download failed: {values[3]}\n")

    def transaction_before_begin(self, values: tuple[Any, ...]) -> None:
        if not self._belongs_to_session(values) or len(values) < 2:
            return
        action_total = self._non_negative_integer(values[1], "transaction action total")
        if action_total is None:
            return
        with self._lock:
            self.action_total = action_total
        self._set_progress(30)
        self.emit_log("Applying RPM transaction…\n")

    def transaction_action_start(self, values: tuple[Any, ...]) -> None:
        if not self._belongs_to_session(values) or len(values) < 4:
            return
        self.emit_log(f"Processing: {values[1]}\n")

    def transaction_action_stop(self, values: tuple[Any, ...]) -> None:
        if not self._belongs_to_session(values) or len(values) < 3:
            return
        action = str(values[1])
        with self._lock:
            self.completed_actions.add(action)
            action_total = self.action_total
            completed_actions = len(self.completed_actions)
        if action_total:
            self._set_progress(30 + int((completed_actions / action_total) * 60))

    def transaction_verify_progress(self, values: tuple[Any, ...]) -> None:
        if not self._belongs_to_session(values) or len(values) < 3:
            return
        processed = self._non_negative_integer(values[1], "verification processed")
        total = self._non_negative_integer(values[2], "verification total")
        if processed is None or total is None:
            return
        if total:
            self._set_progress(90 + int((min(processed, total) / total) * 10))

    def transaction_script_error(self, values: tuple[Any, ...]) -> None:
        if self._belongs_to_session(values) and len(values) >= 4:
            self.emit_log(f"Scriptlet error for {values[1]} (exit {values[3]}).\n")

    def transaction_unpack_error(self, values: tuple[Any, ...]) -> None:
        if self._belongs_to_session(values) and len(values) >= 2:
            self.emit_log(f"Package unpack error: {values[1]}\n")

    def transaction_after_complete(self, values: tuple[Any, ...]) -> None:
        if not self._belongs_to_session(values) or len(values) < 2:
            return
        if not isinstance(values[1], bool):
            LOGGER.warning(
                "Ignoring malformed DNF5 completion signal: success is not boolean."
            )
            return
        with self._lock:
            self.completed_signal_received = True
            self.completed_successfully = values[1]
        if values[1]:
            self._set_progress(100)
            self.emit_log("RPM transaction completed.\n")
        else:
            self.emit_log("RPM transaction reported a failure.\n")


class Dnf5DaemonClient:
    """Typed DNF5 daemon client with session-backed plan ownership."""

    def __init__(
        self,
        transport: DbusTransport | None = None,
        *,
        flatpak_detector: Callable[[], bool] | None = None,
    ) -> None:
        # Creating the window must not require a live Fedora system bus: read-only
        # views remain useful in a sandbox, source checkout, or degraded desktop.
        # The real transport is constructed on the first daemon operation, where
        # a missing bus/service becomes a normal, user-facing capability error.
        self._transport: DbusTransport | None = transport
        self._flatpak_detector = flatpak_detector or (
            lambda: Path("/.flatpak-info").exists()
        )
        self._plans: dict[str, TransactionPlan] = {}
        self._subscriptions: dict[str, list[object]] = {}
        self._progress: dict[str, _PlanProgress] = {}
        self._lock = threading.RLock()

    def ensure_available(self) -> None:
        """Initialize the D-Bus transport in the caller's Qt thread.

        GUI callers invoke this before handing a daemon method to a worker so
        the Qt D-Bus signal receiver has a running event loop in the GUI thread.
        A missing system bus remains a recoverable capability error.
        """
        self._get_transport()

    def resolve(
        self,
        request: TransactionRequest,
        protected_packages: set[str] | frozenset[str],
    ) -> TransactionPlan:
        """Open a daemon session, submit all intent, and retain its resolved goal."""
        install_specs = self._prepare_install_specs(request.installs)
        session_path = self._open_session(
            load_system_repo=True, load_available_repos=True
        )
        try:
            if request.upgrade_all:
                self._call(
                    session_path, RPM_INTERFACE, "upgrade", ("as", "a{sv}"), ((), {})
                )
            elif request.upgrades:
                self._call(
                    session_path,
                    RPM_INTERFACE,
                    "upgrade",
                    ("as", "a{sv}"),
                    (request.upgrades, {}),
                )
            if install_specs:
                self._call(
                    session_path,
                    RPM_INTERFACE,
                    "install",
                    ("as", "a{sv}"),
                    (install_specs, {}),
                )
            if request.removals:
                self._call(
                    session_path,
                    RPM_INTERFACE,
                    "remove",
                    ("as", "a{sv}"),
                    (request.removals, {}),
                )

            response = self._call(
                session_path,
                GOAL_INTERFACE,
                "resolve",
                ("a{sv}",),
                ({"allow_erasing": False, "interactive": True},),
            )
            if len(response) != 2:
                raise Dnf5DaemonCallError(
                    "DNF5 daemon returned an invalid plan",
                    "Goal.resolve did not return transaction items and a result code.",
                )
            raw_items, result = response
            try:
                result_code = int(result)
            except (TypeError, ValueError) as exc:
                raise Dnf5DaemonCallError(
                    "DNF5 daemon returned an invalid plan",
                    "Goal.resolve returned a non-numeric result code.",
                ) from exc
            if result_code == 2:
                problems = self._problem_strings(session_path)
                raise Dnf5DaemonResolutionError(
                    "DNF could not find a safe dependency solution. "
                    + " ".join(problems),
                    problems,
                )
            if result_code not in {0, 1}:
                raise Dnf5DaemonCallError(
                    "DNF5 daemon returned an invalid plan",
                    f"Goal.resolve returned unsupported result code {result_code}.",
                )

            items = tuple(
                _parse_transaction_item(value)
                for value in _require_sequence(raw_items, "transaction items")
            )
            warnings = (
                tuple(self._problem_strings(session_path)) if result_code == 1 else ()
            )
            protected = {name.casefold() for name in protected_packages}
            critical_removals = tuple(
                sorted(
                    {
                        item.name
                        for item in items
                        if item.is_removal and item.name.casefold() in protected
                    }
                )
            )
            plan = TransactionPlan(
                token=secrets.token_urlsafe(32),
                session_path=session_path,
                request=request,
                items=items,
                resolver_result=result_code,
                warnings=warnings,
                critical_removals=critical_removals,
            )
            with self._lock:
                self._plans[plan.token] = plan
            return plan
        except Exception as operation_error:
            try:
                self._close_session_path(session_path)
            except Exception as cleanup_error:
                cleanup_detail = (
                    cleanup_error.detail
                    if isinstance(cleanup_error, Dnf5DaemonError)
                    else f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
                raise Dnf5DaemonCallError(
                    "DNF5 daemon cleanup failed",
                    f"{operation_error}\n\nThe failed resolve session could not be closed: {cleanup_detail}",
                ) from cleanup_error
            raise

    def execute(self, plan: TransactionPlan) -> TransactionExecutionResult:
        """Execute exactly the retained plan; never rebuild intent from the UI."""
        self._assert_active_plan(plan)
        if not plan.has_changes:
            raise Dnf5DaemonPlanError(
                "Nothing to execute",
                "DNF resolved this request to no package changes, so no transaction was started.",
            )
        self._call(
            plan.session_path,
            GOAL_INTERFACE,
            "do_transaction",
            ("a{sv}",),
            ({"interactive": True, "comment": "Dendro"},),
        )
        with self._lock:
            progress = self._progress.get(plan.token)
        completed_signal_received, completed_successfully = (
            progress.completion_status() if progress is not None else (False, True)
        )
        if completed_signal_received and not completed_successfully:
            raise Dnf5DaemonCallError(
                "RPM transaction failed",
                "The daemon completed the transaction with a failure status.",
            )
        return TransactionExecutionResult(
            completed_signal_received=completed_signal_received,
            completed_successfully=completed_successfully,
        )

    def request_cancellation(self, plan: TransactionPlan) -> tuple[bool, str]:
        """Request cancellation only through the daemon-supported download phase."""
        self._assert_active_plan(plan)
        response = self._call(plan.session_path, GOAL_INTERFACE, "cancel", (), ())
        if len(response) != 2:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned an invalid cancellation result",
                "Goal.cancel did not return a success flag and diagnostic message.",
            )
        return bool(response[0]), str(response[1])

    def attach_progress(
        self,
        plan: TransactionPlan,
        emit_log: Callable[[str], None],
        emit_progress: Callable[[int], None],
    ) -> None:
        """Subscribe before execution so no daemon progress event is lost.

        Any partially registered subscriptions remain owned by the plan until
        their unsubscription succeeds.  This prevents a failed setup cleanup
        from leaking callbacks or allowing a retry to register duplicate signal
        handlers for the same transaction.
        """
        self._assert_active_plan(plan)
        self.detach_progress(plan)
        tracker = _PlanProgress(plan.session_path, emit_log, emit_progress)
        handlers: tuple[tuple[str, Callable[[tuple[Any, ...]], None]], ...] = (
            ("download_add_new", tracker.download_add_new),
            ("download_progress", tracker.download_progress),
            ("download_end", tracker.download_end),
            ("transaction_before_begin", tracker.transaction_before_begin),
            ("transaction_action_start", tracker.transaction_action_start),
            ("transaction_action_stop", tracker.transaction_action_stop),
            ("transaction_verify_progress", tracker.transaction_verify_progress),
            ("transaction_script_error", tracker.transaction_script_error),
            ("transaction_unpack_error", tracker.transaction_unpack_error),
            ("transaction_after_complete", tracker.transaction_after_complete),
        )
        subscriptions: list[object] = []
        try:
            for signal, callback in handlers:
                interface = (
                    BASE_INTERFACE if signal.startswith("download_") else RPM_INTERFACE
                )
                subscriptions.append(
                    self._get_transport().subscribe(
                        plan.session_path, interface, signal, callback
                    )
                )
        except Exception as subscribe_error:
            # Store partial ownership before cleanup so a failed unsubscribe can
            # be retried through detach_progress() or final session cleanup.
            with self._lock:
                self._subscriptions[plan.token] = subscriptions
                self._progress[plan.token] = tracker
            try:
                self.detach_progress(plan)
            except Exception as cleanup_error:
                raise Dnf5DaemonCallError(
                    "Could not subscribe to DNF5 progress",
                    f"{type(subscribe_error).__name__}: {subscribe_error}\n"
                    "Cleanup of partially registered subscriptions also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}",
                ) from subscribe_error
            raise Dnf5DaemonCallError(
                "Could not subscribe to DNF5 progress",
                f"{type(subscribe_error).__name__}: {subscribe_error}",
            ) from subscribe_error
        with self._lock:
            self._subscriptions[plan.token] = subscriptions
            self._progress[plan.token] = tracker

    def detach_progress(self, plan: TransactionPlan) -> None:
        """Unsubscribe every tracker callback, retaining tokens that need retry."""
        with self._lock:
            subscriptions = tuple(self._subscriptions.get(plan.token, ()))
        if not subscriptions:
            with self._lock:
                self._subscriptions.pop(plan.token, None)
                self._progress.pop(plan.token, None)
            return

        failures: list[str] = []
        remaining: list[object] = []
        for subscription in subscriptions:
            try:
                self._get_transport().unsubscribe(subscription)
            except Exception as exc:
                remaining.append(subscription)
                failures.append(f"{type(exc).__name__}: {exc}")

        with self._lock:
            if remaining:
                self._subscriptions[plan.token] = remaining
            else:
                self._subscriptions.pop(plan.token, None)
                self._progress.pop(plan.token, None)
        if failures:
            raise Dnf5DaemonCallError(
                "Could not unsubscribe from DNF5 progress",
                "\n".join(failures),
            )

    def discard(self, plan: TransactionPlan) -> None:
        """Close the daemon session so a preview can never execute later."""
        self._assert_active_plan(plan)
        self._finalize_plan(plan)

    def close_after_execution(self, plan: TransactionPlan) -> None:
        """Release subscriptions and the daemon session after a terminal result."""
        self._finalize_plan(plan)

    def _finalize_plan(self, plan: TransactionPlan) -> None:
        """Attempt every cleanup step while irrevocably invalidating the plan."""
        failures: list[str] = []
        try:
            self.detach_progress(plan)
        except Exception as exc:
            failures.append(f"progress subscriptions: {type(exc).__name__}: {exc}")
        try:
            self._close_plan(plan)
        except Exception as exc:
            failures.append(f"session: {type(exc).__name__}: {exc}")
        if failures:
            raise Dnf5DaemonCallError(
                "DNF5 daemon cleanup failed",
                "\n".join(failures),
            )

    def set_repository_enabled(self, repository_id: str, enabled: bool) -> None:
        repo_id = _normalise_repository_id(repository_id)
        self._with_session(
            lambda session_path: self._call(
                session_path,
                REPOSITORY_INTERFACE,
                "enable_with_options" if enabled else "disable_with_options",
                ("as", "a{sv}"),
                ((repo_id,), {"interactive": True}),
            ),
            load_system_repo=False,
            load_available_repos=False,
        )

    def clean_cache(self) -> None:
        result = self._with_session(
            lambda session_path: self._call(
                session_path,
                BASE_INTERFACE,
                "clean_with_options",
                ("s", "a{sv}"),
                ("all", {"interactive": True}),
            ),
            load_system_repo=False,
            load_available_repos=False,
        )
        if len(result) != 2 or not bool(result[0]):
            detail = (
                str(result[1])
                if len(result) >= 2
                else "The daemon did not provide a cache-cleaning result."
            )
            raise Dnf5DaemonCallError("DNF cache clean failed", detail)

    def list_available_updates(
        self, *, refresh: bool = False
    ) -> tuple[DnfPackageRecord, ...]:
        """Return upgrade candidates through the daemon's read-only RPM API.

        ``Rpm.list`` returns a small, explicitly requested attribute set here;
        unlike a full installed-package inventory, upgrade candidates fit in one
        bounded D-Bus reply.  A manual refresh first expires metadata through
        the daemon, then opening the query session reloads it.  No DNF command
        or host-process bridge is involved.
        """
        if refresh:
            result = self._with_session(
                lambda session_path: self._call(
                    session_path,
                    BASE_INTERFACE,
                    "clean_with_options",
                    ("s", "a{sv}"),
                    ("expire-cache", {"interactive": False}),
                ),
                load_system_repo=False,
                load_available_repos=False,
            )
            if len(result) != 2 or not bool(result[0]):
                detail = (
                    str(result[1])
                    if len(result) >= 2
                    else "The daemon did not provide an expiry result."
                )
                raise Dnf5DaemonCallError("Could not refresh DNF metadata", detail)

        response = self._with_session(
            lambda session_path: self._call(
                session_path,
                RPM_INTERFACE,
                "list",
                ("a{sv}",),
                (
                    {
                        "scope": "upgrades",
                        "latest-limit": 1,
                        "package_attrs": (
                            "name",
                            "epoch",
                            "version",
                            "release",
                            "arch",
                            "repo_id",
                        ),
                        "interactive": False,
                    },
                ),
            ),
            load_system_repo=True,
            load_available_repos=True,
        )
        return _parse_package_records(response, "upgrade candidates")

    def list_repositories(self) -> tuple[DnfRepository, ...]:
        response = self._with_session(
            lambda session_path: self._call(
                session_path,
                REPOSITORY_INTERFACE,
                "list",
                ("a{sv}",),
                (
                    {
                        "enable_disable": "all",
                        "repo_attrs": (
                            "name",
                            "enabled",
                            "baseurl",
                            "metalink",
                            "mirrorlist",
                            "repofile",
                        ),
                        "interactive": False,
                    },
                ),
            ),
            load_system_repo=False,
            load_available_repos=False,
        )
        if len(response) != 1:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid repositories",
                "Repository.list did not return exactly one repository array.",
            )
        repositories: list[DnfRepository] = []
        for raw in _require_sequence(response[0], "repositories"):
            if not isinstance(raw, Mapping):
                raise Dnf5DaemonCallError(
                    "DNF5 daemon returned invalid repositories",
                    "Repository.list returned an item that is not a map.",
                )
            raw_repo_id = raw.get("id", "")
            if not isinstance(raw_repo_id, str):
                raise Dnf5DaemonCallError(
                    "DNF5 daemon returned invalid repositories",
                    "Repository.list returned a repository identifier that is not text.",
                )
            try:
                repo_id = _normalise_repository_id(raw_repo_id)
            except Dnf5DaemonValidationError as exc:
                raise Dnf5DaemonCallError(
                    "DNF5 daemon returned invalid repositories", exc.detail
                ) from exc
            raw_enabled = raw.get("enabled")
            if not isinstance(raw_enabled, bool):
                raise Dnf5DaemonCallError(
                    "DNF5 daemon returned invalid repositories",
                    "Repository.list returned an enabled state that is not boolean.",
                )
            baseurl = _as_text(raw.get("baseurl", ""))
            if not baseurl:
                baseurl = _as_text(raw.get("metalink", "")) or _as_text(
                    raw.get("mirrorlist", "")
                )
            repositories.append(
                DnfRepository(
                    identifier=repo_id,
                    name=_as_text(raw.get("name", repo_id)) or repo_id,
                    enabled=raw_enabled,
                    baseurl=baseurl,
                    repo_file=_as_text(raw.get("repofile", "")),
                )
            )
        return tuple(
            sorted(
                repositories, key=lambda repository: repository.identifier.casefold()
            )
        )

    def _prepare_install_specs(self, installs: Sequence[str]) -> tuple[str, ...]:
        prepared: list[str] = []
        for spec in installs:
            if _looks_like_local_rpm(spec):
                if self._flatpak_detector():
                    raise Dnf5DaemonValidationError(
                        "Local RPM installation is unavailable in Flatpak",
                        "Dendro does not expose the host filesystem or run host commands from Flatpak. "
                        "Install this RPM from an RPM-host Dendro installation instead.",
                    )
                path = os.path.realpath(spec)
                if not os.path.isfile(path):
                    raise Dnf5DaemonValidationError("Local RPM file not found", path)
                if not os.access(path, os.R_OK):
                    raise Dnf5DaemonValidationError(
                        "Local RPM file is not readable", path
                    )
                prepared.append(path)
            else:
                prepared.append(spec)
        return tuple(prepared)

    def _open_session(
        self, *, load_system_repo: bool, load_available_repos: bool
    ) -> str:
        response = self._call(
            ROOT_PATH,
            SESSION_MANAGER_INTERFACE,
            "open_session",
            ("a{sv}",),
            (
                {
                    "load_system_repo": load_system_repo,
                    "load_available_repos": load_available_repos,
                },
            ),
        )
        if (
            len(response) != 1
            or not isinstance(response[0], str)
            or not response[0].startswith(ROOT_PATH + "/")
        ):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned an invalid session",
                "SessionManager.open_session did not return a valid session object path.",
            )
        return response[0]

    def _with_session(
        self,
        operation: Callable[[str], tuple[Any, ...]],
        *,
        load_system_repo: bool,
        load_available_repos: bool,
    ) -> tuple[Any, ...]:
        session_path = self._open_session(
            load_system_repo=load_system_repo,
            load_available_repos=load_available_repos,
        )
        operation_error: Exception | None = None
        try:
            return operation(session_path)
        except Exception as exc:
            operation_error = exc
            raise
        finally:
            try:
                self._close_session_path(session_path)
            except Exception as close_error:
                if operation_error is None:
                    raise
                raise Dnf5DaemonCallError(
                    "DNF5 daemon cleanup failed",
                    f"{operation_error}; additionally, closing the daemon session failed: {close_error}",
                ) from close_error

    def _problem_strings(self, session_path: str) -> tuple[str, ...]:
        response = self._call(
            session_path, GOAL_INTERFACE, "get_transaction_problems_string", (), ()
        )
        if len(response) != 1:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid diagnostics",
                "Goal.get_transaction_problems_string did not return one string array.",
            )
        return tuple(
            str(problem)
            for problem in _require_sequence(response[0], "transaction problems")
            if str(problem)
        )

    def _assert_active_plan(self, plan: TransactionPlan) -> None:
        with self._lock:
            known = self._plans.get(plan.token)
            if known is not plan or plan._closed:
                raise Dnf5DaemonPlanError(
                    "Transaction preview expired",
                    "This resolved plan has already been discarded, executed, or replaced. Resolve it again before continuing.",
                )

    def _close_plan(self, plan: TransactionPlan) -> None:
        # Remove the capability before doing I/O. A transport failure must never
        # leave a resolved plan callable from another UI path or callback.
        with self._lock:
            known = self._plans.get(plan.token)
            if known is not plan or plan._closed:
                return
            plan._closed = True
            self._plans.pop(plan.token, None)
        self._close_session_path(plan.session_path)

    def _close_session_path(self, session_path: str) -> None:
        response = self._call(
            ROOT_PATH,
            SESSION_MANAGER_INTERFACE,
            "close_session",
            ("o",),
            (session_path,),
        )
        if len(response) != 1 or not bool(response[0]):
            raise Dnf5DaemonCallError(
                "DNF5 daemon cleanup failed",
                f"SessionManager.close_session refused to close {session_path}.",
            )

    def _get_transport(self) -> DbusTransport:
        """Create the system-bus transport only when a daemon feature is used."""
        with self._lock:
            if self._transport is None:
                try:
                    self._transport = QtDbusTransport()
                except Dnf5DaemonError:
                    raise
                except Exception as exc:
                    raise Dnf5DaemonUnavailable(
                        "DNF5 daemon is unavailable",
                        f"Could not initialize the system D-Bus transport: {type(exc).__name__}: {exc}",
                    ) from exc
            return self._transport

    def _call(
        self,
        path: str,
        interface: str,
        method: str,
        signatures: Sequence[str],
        arguments: Sequence[Any],
    ) -> tuple[Any, ...]:
        try:
            return self._get_transport().call(
                path, interface, method, signatures, arguments
            )
        except Dnf5DaemonError:
            raise
        except Exception as exc:
            raise Dnf5DaemonCallError(
                "DNF5 daemon communication failed",
                f"{interface}.{method}: {type(exc).__name__}: {exc}",
            ) from exc


class RepositoryListSignals(QObject):
    """Terminal signals for one unprivileged daemon repository-list request."""

    loaded = pyqtSignal(object)
    failed = pyqtSignal(str, str)


class RepositoryListWorker(QRunnable):
    """Fetch repository metadata without blocking the repository dialog's event loop."""

    def __init__(self, client: Dnf5DaemonClient) -> None:
        super().__init__()
        self.client = client
        self.signals = RepositoryListSignals()
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self) -> None:
        try:
            repositories = self.client.list_repositories()
        except Dnf5DaemonError as exc:
            self.signals.failed.emit(exc.title, exc.detail)
        except Exception as exc:  # Defensive terminal result for every worker path.
            self.signals.failed.emit(
                "Unable to load repositories", f"{type(exc).__name__}: {exc}"
            )
        else:
            self.signals.loaded.emit(repositories)


def _normalise_specs(values: Sequence[str], action: str) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    if len(values) > 10_000:
        raise Dnf5DaemonValidationError(
            "Too many package changes",
            f"The {action} request contains more than 10,000 entries.",
        )
    for raw in values:
        if not isinstance(raw, str):
            raise Dnf5DaemonValidationError(
                "Invalid package specification", f"A {action} request is not text."
            )
        if not raw or raw != raw.strip():
            raise Dnf5DaemonValidationError(
                "Invalid package specification",
                f"Package specifications cannot be empty or have leading/trailing whitespace: {raw!r}",
            )
        if len(raw) > 4096:
            raise Dnf5DaemonValidationError(
                "Invalid package specification",
                "A package specification exceeds 4096 characters.",
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in raw):
            raise Dnf5DaemonValidationError(
                "Invalid package specification",
                "Package specifications cannot contain control characters.",
            )
        if raw not in seen:
            seen.add(raw)
            result.append(raw)
    return tuple(result)


def _normalise_repository_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 255
    ):
        raise Dnf5DaemonValidationError(
            "Invalid repository ID",
            "Repository IDs must be non-empty text no longer than 255 characters.",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise Dnf5DaemonValidationError(
            "Invalid repository ID", "Repository IDs cannot contain control characters."
        )
    return value


def _looks_like_local_rpm(spec: str) -> bool:
    return os.path.isabs(spec) and spec.casefold().endswith(".rpm")


def _require_sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid data", f"Expected {label} to be an array."
        )
    return value


def _parse_transaction_item(raw: Any) -> TransactionItem:
    values = _require_sequence(raw, "transaction item")
    if len(values) != 5:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid data",
            "A transaction item does not have five fields.",
        )
    object_type, action, reason, attributes, package = values
    if not isinstance(attributes, Mapping) or not isinstance(package, Mapping):
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid data",
            "A transaction item has invalid attributes or package metadata.",
        )
    if not all(
        isinstance(value, str) and value.strip()
        for value in (object_type, action, reason)
    ):
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid data",
            "A transaction item has a missing object type, action, or reason.",
        )
    package_data = {
        str(key): _unwrap_dbus_value(value) for key, value in package.items()
    }
    identity = next(
        (
            package_data.get(field)
            for field in ("name", "groupid", "id")
            if package_data.get(field)
        ),
        None,
    )
    if not isinstance(identity, str) or not identity.strip():
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid data",
            "A transaction item is missing its package or group identity.",
        )
    return TransactionItem(
        object_type=object_type,
        action=action,
        reason=reason,
        attributes={
            str(key): _unwrap_dbus_value(value) for key, value in attributes.items()
        },
        package=package_data,
    )


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else str(value) if value is not None else ""


def _parse_package_records(
    response: tuple[Any, ...], label: str
) -> tuple[DnfPackageRecord, ...]:
    if len(response) != 1:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid package data",
            f"Rpm.list did not return exactly one array for {label}.",
        )

    records: list[DnfPackageRecord] = []
    for raw in _require_sequence(response[0], label):
        if not isinstance(raw, Mapping):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                f"Rpm.list returned a non-map item for {label}.",
            )
        name = _as_text(raw.get("name", "")).strip()
        epoch = _as_text(raw.get("epoch", "")).strip()
        if epoch in {"", "(none)"}:
            epoch = "0"
        version = _as_text(raw.get("version", "")).strip()
        release = _as_text(raw.get("release", "")).strip()
        arch = _as_text(raw.get("arch", "")).strip()
        if not name or not version or not release or not arch:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                f"An {label} item is missing its name, version, release, or architecture.",
            )
        raw_size = raw.get("install_size", raw.get("download_size", 0))
        if isinstance(raw_size, bool):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                f"An {label} item has an invalid package size.",
            )
        try:
            size_bytes = int(raw_size or 0)
        except (TypeError, ValueError) as exc:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                f"An {label} item has an invalid package size.",
            ) from exc
        if size_bytes < 0:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                f"An {label} item has a negative package size.",
            )
        records.append(
            DnfPackageRecord(
                name=name,
                version=version,
                release=release,
                arch=arch,
                repository=_as_text(
                    raw.get("repo_id", raw.get("repository", ""))
                ).strip(),
                epoch=epoch,
                summary=_as_text(raw.get("summary", "")).strip(),
                size_bytes=size_bytes,
            )
        )
    return tuple(records)


def _marshal_argument(signature: str, value: Any) -> Any:
    if signature == "s":
        if not isinstance(value, str):
            raise TypeError("D-Bus string argument must be text.")
        return value
    if signature == "b":
        if not isinstance(value, bool):
            raise TypeError("D-Bus boolean argument must be bool.")
        return value
    if signature == "o":
        if not isinstance(value, str) or not value.startswith("/"):
            raise TypeError(
                "D-Bus object-path argument must be an absolute object path."
            )
        return QDBusObjectPath(value)
    if signature == "as":
        return _marshal_string_array(value)
    if signature == "a{sv}":
        if not isinstance(value, Mapping):
            raise TypeError("D-Bus options argument must be a mapping.")
        return _marshal_variant_map(value)
    raise TypeError(f"Unsupported typed D-Bus signature: {signature}")


def _marshal_string_array(values: Any) -> QDBusArgument:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("D-Bus string array must be a sequence of text values.")
    argument = QDBusArgument()
    argument.beginArray(QMetaType.Type.QString.value)
    for value in values:
        if not isinstance(value, str):
            raise TypeError("D-Bus string arrays cannot contain non-text values.")
        argument.add(value)
    argument.endArray()
    return argument


def _marshal_string_map(values: Mapping[str, Any]) -> QDBusArgument:
    argument = QDBusArgument()
    argument.beginMap(QMetaType.Type.QString.value, QMetaType.Type.QString.value)
    for key in sorted(values):
        value = values[key]
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("D-Bus string maps require text keys and values.")
        argument.beginMapEntry()
        argument.add(key)
        argument.add(value)
        argument.endMapEntry()
    argument.endMap()
    return argument


def _marshal_variant_value(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, str) or isinstance(value, int):
        return value
    if isinstance(value, Mapping):
        return _marshal_string_map(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return _marshal_string_array(value)
    raise TypeError(f"Unsupported D-Bus variant value type: {type(value).__name__}")


def _marshal_variant_map(values: Mapping[str, Any]) -> QDBusArgument:
    argument = QDBusArgument()
    variant_meta_type = QMetaType.fromName(b"QDBusVariant")
    if not variant_meta_type.isValid():
        raise TypeError("QtDBus did not register QDBusVariant support.")
    argument.beginMap(QMetaType.Type.QString.value, variant_meta_type.id())
    for key in sorted(values):
        if not isinstance(key, str):
            raise TypeError("D-Bus option keys must be text.")
        argument.beginMapEntry()
        argument.add(key)
        argument.add(QDBusVariant(_marshal_variant_value(values[key])))
        argument.endMapEntry()
    argument.endMap()
    return argument


def _unwrap_dbus_value(value: Any) -> Any:
    if isinstance(value, QDBusVariant):
        return _unwrap_dbus_value(value.variant())
    if isinstance(value, QDBusObjectPath):
        return value.path()
    if isinstance(value, Mapping):
        return {str(key): _unwrap_dbus_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_unwrap_dbus_value(item) for item in value)
    if isinstance(value, list):
        return [_unwrap_dbus_value(item) for item in value]
    return value
