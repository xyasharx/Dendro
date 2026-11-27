"""Typed client for Fedora's ``dnf5daemon-server`` system D-Bus API.

Dendro delegates package authority to Fedora's daemon instead of constructing
shell commands or a second general-purpose root service.  The server owns
resolution, RPM locking, transaction execution, and Polkit authorization.  This
module owns session-backed plan ownership, progress tracking, key prompt
coordination, and daemon queries (including ``Rpm.list_fd`` and ``History.list``).

Typed constants, exceptions, and DTOs live in :mod:`dendro.core.dnf5daemon_types`;
QtDBus marshalling lives in :mod:`dendro.core.dnf5daemon_transport`; response parsers live
in :mod:`dendro.core.dnf5daemon_parsers`.  Public and test-visible symbols remain
re-exported here.
"""

from __future__ import annotations

import logging
import os
import secrets
import select
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QSettings, pyqtSignal, pyqtSlot

from dendro.core.daemon_locale import daemon_locale
from dendro.core.dnf5daemon_parsers import (
    _advisory_affected_packages,
    _advisory_references,
    _extract_repository_urls,
    _format_offline_transaction_error,
    _key_import_arguments,
    _key_import_prompt,
    _parse_advisories,
    _parse_comps_groups,
    _parse_fd_package_json_line,
    _parse_history_transactions,
    _parse_package_records,
    _parse_transaction_item,
)
from dendro.core.dnf5daemon_transport import (
    DbusTransport,
    QtDbusTransport,
    _marshal_argument,
    _marshal_string_array,
    _marshal_string_map,
    _marshal_variant_map,
    _marshal_variant_value,
    _QtDbusSignalSink,
    _Subscription,
    _unwrap_dbus_value,
)
from dendro.core.dnf5daemon_types import (
    ADVISORY_INTERFACE,
    AUTOMATIC_KEY_IMPORT_PREFERENCE,
    BASE_INTERFACE,
    GOAL_INTERFACE,
    GROUP_INTERFACE,
    HISTORY_INTERFACE,
    KEY_IMPORT_REQUEST_SIGNAL,
    KEY_IMPORTED_SIGNAL,
    NO_CLIENT_SIDE_TIMEOUT_MS,
    OFFLINE_INTERFACE,
    REPOSITORY_INTERFACE,
    ROOT_PATH,
    RPM_INTERFACE,
    SERVICE_NAME,
    SESSION_MANAGER_INTERFACE,
    TRANSACTION_ERROR,
    WRITE_TO_FD_FINISHED_SIGNAL,
    DbusInt64,
    Dnf5DaemonCallError,
    Dnf5DaemonError,
    Dnf5DaemonPlanError,
    Dnf5DaemonResolutionError,
    Dnf5DaemonUnavailable,
    Dnf5DaemonUnsupportedError,
    Dnf5DaemonValidationError,
    DnfCompsGroup,
    DnfCompsPackage,
    DnfHistoryTransaction,
    DnfPackageRecord,
    DnfRepository,
    RepositoryKeyPrompt,
    SecurityAdvisory,
    TransactionExecutionResult,
    TransactionItem,
    TransactionPlan,
    TransactionRequest,
    _as_text,
    _inbound_package_names,
    _is_effective_removal,
    _looks_like_local_rpm,
    _normalise_repository_id,
    _normalise_specs,
    _require_sequence,
    _signed_size,
    _single_line,
    _size_attribute,
    format_byte_size,
)
from dendro.core.host_environment import HostEnvironment, detect_host_environment
from dendro.core.settings import get_app_settings
from dendro.core.worker_lifecycle import ShutdownSafeRunnable

LOGGER = logging.getLogger(__name__)


def repository_key_import_is_automatic() -> bool:
    """Report whether the user opted into trusting new repository keys silently.

    The default is ``False``: importing a repository signing key is the moment a
    third-party repository becomes trusted for every package it ships, so it
    stays an explicit decision unless the user asks for it.
    """
    settings = get_app_settings(QSettings)
    return bool(settings.value(AUTOMATIC_KEY_IMPORT_PREFERENCE, False, type=bool))


def _create_pipe() -> tuple[int, int]:
    """Create a non-inheritable OS pipe."""
    if hasattr(os, "pipe2"):
        return os.pipe2(os.O_CLOEXEC)
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, False)
    os.set_inheritable(write_fd, False)
    return read_fd, write_fd


_UPDATE_PACKAGE_ATTRS: tuple[str, ...] = (
    "name",
    "epoch",
    "version",
    "release",
    "arch",
    "repo_id",
    "summary",
    "install_size",
    "download_size",
)


class _RepositoryKeyResponder:
    """Answer repository key-import prompts the daemon is blocking on."""

    def __init__(
        self,
        *,
        confirm: Callable[[str, str, bool], None],
        automatic: Callable[[], bool],
        ask: Callable[[RepositoryKeyPrompt], bool | None] | None = None,
        imported: Callable[[tuple[Any, ...]], None] | None = None,
        declined: Callable[[RepositoryKeyPrompt], None] | None = None,
    ) -> None:
        self._confirm = confirm
        self._automatic = automatic
        self._ask = ask
        self._imported_callback = imported
        self._declined_callback = declined
        self._lock = threading.RLock()
        self._tokens: list[tuple[DbusTransport, object]] = []

    def watch(self, transport: DbusTransport, session_path: str) -> None:
        """Subscribe to this session's key prompts, best effort."""
        wanted = [(KEY_IMPORT_REQUEST_SIGNAL, self._requested)]
        if self._imported_callback is not None:
            wanted.append((KEY_IMPORTED_SIGNAL, self._reported_imported))
        for signal, callback in wanted:
            try:
                token = transport.subscribe(
                    session_path, BASE_INTERFACE, signal, callback
                )
            except Exception:
                LOGGER.debug(
                    "Could not watch %s on %s.", signal, session_path, exc_info=True
                )
                continue
            with self._lock:
                self._tokens.append((transport, token))

    def stop(self) -> None:
        """Drop every subscription installed by :meth:`watch`."""
        with self._lock:
            tokens = tuple(self._tokens)
            self._tokens.clear()
        for transport, token in tokens:
            try:
                transport.unsubscribe(token)
            except Exception:
                LOGGER.debug(
                    "Could not stop watching repository key prompts.", exc_info=True
                )

    def _reported_imported(self, arguments: tuple[Any, ...]) -> None:
        callback = self._imported_callback
        if callback is None:
            return
        try:
            callback(arguments)
        except Exception:
            LOGGER.debug("Imported-key callback failed.", exc_info=True)

    def _report_declined(self, prompt: RepositoryKeyPrompt) -> None:
        callback = self._declined_callback
        if callback is None:
            return
        try:
            callback(prompt)
        except Exception:
            LOGGER.debug("Declined-key callback failed.", exc_info=True)

    def _decide(self, prompt: RepositoryKeyPrompt) -> bool:
        try:
            if self._automatic():
                return True
        except Exception:
            LOGGER.exception("Could not read the repository key import preference.")
        ask = self._ask
        if ask is None:
            return False
        try:
            answer = ask(prompt)
        except Exception:
            LOGGER.exception(
                "Could not ask about the repository key %s.", prompt.key_id
            )
            return False
        return bool(answer)

    def _requested(self, arguments: tuple[Any, ...]) -> None:
        try:
            prompt = _key_import_prompt(arguments)
        except Exception:
            LOGGER.exception("Could not interpret a repository key import request.")
            return
        decided = self._decide(prompt)
        try:
            self._confirm(prompt.session_path, prompt.key_id, decided)
        except Exception as exc:
            LOGGER.exception(
                "Could not answer the key import prompt for %s.", prompt.key_id
            )
            prompt = replace(
                prompt, key_url=f"{prompt.key_url} ({type(exc).__name__}: {exc})"
            )
            self._report_declined(prompt)
            return
        if not decided:
            self._report_declined(prompt)


class _PlanProgress:
    """Converts daemon callback signals into monotonic presentation progress."""

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
        download_id, description = str(values[1]), _single_line(values[2])
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
        self.emit_log(f"Processing: {_single_line(values[1])}\n")

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
            package = _single_line(values[1])
            code = _single_line(values[3], max_length=32)
            self.emit_log(f"Scriptlet error for {package} (exit {code}).\n")

    def transaction_unpack_error(self, values: tuple[Any, ...]) -> None:
        if self._belongs_to_session(values) and len(values) >= 2:
            self.emit_log(f"Package unpack error: {_single_line(values[1])}\n")

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


def _is_protected_transaction_item(item: TransactionItem, protected: set[str]) -> bool:
    """Match protected package names as well as running-kernel NEVRA specs."""
    name_cf = item.name.casefold()
    if name_cf in protected:
        return True
    version = str(item.package.get("version") or "").strip()
    release = str(item.package.get("release") or "").strip()
    arch = str(item.package.get("arch") or "").strip()
    if version and release:
        vr = f"{version}-{release}".casefold()
        if f"{name_cf}-{vr}" in protected:
            return True
        if arch and f"{name_cf}-{vr}.{arch.casefold()}" in protected:
            return True
    return False


class Dnf5DaemonClient:
    """Typed DNF5 daemon client with session-backed plan ownership."""

    def __init__(
        self,
        transport: DbusTransport | None = None,
        *,
        flatpak_detector: Callable[[], bool] | None = None,
        environment_detector: Callable[[], HostEnvironment] | None = None,
        key_import_policy: Callable[[], bool] | None = None,
        key_import_prompt: Callable[[RepositoryKeyPrompt], bool | None] | None = None,
    ) -> None:
        self._transport: DbusTransport | None = transport
        self._key_import_policy = key_import_policy
        self._key_import_prompt = key_import_prompt
        self._explicit_flatpak_detector = flatpak_detector
        self._flatpak_detector = flatpak_detector or (
            lambda: Path("/.flatpak-info").exists()
        )
        if environment_detector is not None:
            self._environment_detector: Callable[[], HostEnvironment] | None = (
                environment_detector
            )
        elif transport is None:
            self._environment_detector = detect_host_environment
        else:
            self._environment_detector = None
        self._plans: dict[str, TransactionPlan] = {}
        self._subscriptions: dict[str, list[object]] = {}
        self._progress: dict[str, _PlanProgress] = {}
        self._key_responders: dict[str, _RepositoryKeyResponder] = {}
        self._declined_keys: list[str] = []
        self._lock = threading.RLock()

    def _current_environment(self) -> HostEnvironment:
        if self._environment_detector is not None:
            environment = self._environment_detector()
            if (
                self._explicit_flatpak_detector is not None
                and self._explicit_flatpak_detector()
                and not environment.is_flatpak
            ):
                return HostEnvironment.flatpak()
            return environment
        if self._flatpak_detector():
            return HostEnvironment.flatpak()
        return HostEnvironment.traditional()

    def _is_flatpak(self) -> bool:
        if self._explicit_flatpak_detector is not None:
            return self._explicit_flatpak_detector()
        return self._current_environment().is_flatpak

    def _require_daemon_queries(self, action: str) -> None:
        environment = self._current_environment()
        if not environment.supports_dnf5_daemon_queries:
            title, detail = environment.dnf5_daemon_unavailable(action)
            raise Dnf5DaemonUnavailable(title, detail)

    def _require_daemon_mutations(self, action: str) -> None:
        environment = self._current_environment()
        if not environment.supports_dnf5_daemon_mutations:
            title, detail = environment.dnf5_daemon_unavailable(action)
            raise Dnf5DaemonUnavailable(title, detail)

    def ensure_available(self) -> None:
        """Initialize the D-Bus transport in the caller's Qt thread."""
        self._require_daemon_queries("connect to the DNF5 system daemon")
        self._get_transport()

    def close(self) -> None:
        """Release any owned D-Bus transport and background signal resources."""
        with self._lock:
            transport = self._transport
            self._transport = None
        if transport is not None:
            close_fn = getattr(transport, "close", None)
            if callable(close_fn):
                close_fn()

    def _expire_metadata_cache(self, error_title: str) -> None:
        """Expire cached DNF5 repository metadata and validate the daemon reply."""
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
        if len(result) != 2 or not isinstance(result[0], bool) or not result[0]:
            detail = (
                _as_text(result[1])
                if len(result) == 2 and isinstance(result[0], bool)
                else "The daemon did not provide a valid expiry result."
            )
            raise Dnf5DaemonCallError(error_title, detail)

    def resolve(
        self,
        request: TransactionRequest,
        protected_packages: set[str] | frozenset[str],
        *,
        allow_erasing: bool = False,
    ) -> TransactionPlan:
        """Open a daemon session, submit all intent, and retain its resolved goal."""
        self._require_daemon_mutations("resolve package transactions")
        install_specs = self._prepare_install_specs(request.installs)
        with self._lock:
            self._declined_keys.clear()
        session_path, key_responder = self._open_session_answering_key_prompts(
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
                ({"allow_erasing": bool(allow_erasing), "interactive": True},),
            )
            if len(response) != 2:
                raise Dnf5DaemonCallError(
                    "DNF5 daemon returned an invalid plan",
                    "Goal.resolve did not return transaction items and a result code.",
                )
            raw_items, result = response
            if isinstance(result, bool):
                raise Dnf5DaemonCallError(
                    "DNF5 daemon returned an invalid plan",
                    "Goal.resolve returned a boolean instead of a numeric result code.",
                )
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
                    allow_erasing_attempted=bool(allow_erasing),
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
            inbound_names = _inbound_package_names(items)
            critical_removals = tuple(
                sorted(
                    {
                        item.name
                        for item in items
                        if _is_effective_removal(item, inbound_names)
                        and _is_protected_transaction_item(item, protected)
                    }
                )
            )
            plan = TransactionPlan(
                token=secrets.token_urlsafe(32),
                session_path=session_path,
                request=request,
                items=items,
                resolver_result=result_code,
                warnings=warnings + self.take_declined_key_notices(),
                critical_removals=critical_removals,
            )
            with self._lock:
                self._plans[plan.token] = plan
                if key_responder is not None:
                    self._key_responders[plan.token] = key_responder
            return plan
        except Exception as operation_error:
            if key_responder is not None:
                key_responder.stop()
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
        self._require_daemon_mutations("execute package transactions")
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

    def execute_offline(self, plan: TransactionPlan) -> TransactionExecutionResult:
        """Download the retained plan and apply it during the next boot."""
        self._require_daemon_mutations("stage package transactions for the next boot")
        self._assert_active_plan(plan)
        if not plan.has_changes:
            raise Dnf5DaemonPlanError(
                "Nothing to stage",
                "DNF resolved this request to no package changes, so nothing was staged.",
            )
        try:
            self._call(
                plan.session_path,
                GOAL_INTERFACE,
                "do_transaction",
                ("a{sv}",),
                ({"interactive": True, "offline": True, "comment": "Dendro"},),
            )
        except Dnf5DaemonCallError as exc:
            raise _format_offline_transaction_error(exc, plan.request) from exc
        return TransactionExecutionResult(
            completed_signal_received=False, completed_successfully=True
        )

    def offline_transaction_pending(self) -> bool:
        """Report whether an offline transaction is already queued for boot."""
        self._require_daemon_queries("read the pending offline transaction")
        response = self._with_session(
            lambda session_path: self._call(
                session_path, OFFLINE_INTERFACE, "get_status", (), ()
            ),
            load_system_repo=False,
            load_available_repos=False,
        )
        if len(response) != 2 or not isinstance(response[0], bool):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid offline status",
                "Offline.get_status did not return a pending flag and a status map.",
            )
        return response[0]

    def request_cancellation(self, plan: TransactionPlan) -> tuple[bool, str]:
        """Request cancellation only through the daemon-supported download phase."""
        self._assert_active_plan(plan)
        response = self._call(plan.session_path, GOAL_INTERFACE, "cancel", (), ())
        if len(response) != 2:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned an invalid cancellation result",
                "Goal.cancel did not return a success flag and diagnostic message.",
            )
        if not isinstance(response[0], bool):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned an invalid cancellation result",
                "Goal.cancel returned a success flag that is not boolean.",
            )
        return response[0], _as_text(response[1])

    def attach_progress(
        self,
        plan: TransactionPlan,
        emit_log: Callable[[str], None],
        emit_progress: Callable[[int], None],
    ) -> None:
        """Subscribe before execution so no daemon progress event is lost."""
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
        self._require_daemon_mutations(
            "enable repositories" if enabled else "disable repositories"
        )
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
        self._require_daemon_mutations("clean DNF5 metadata and package caches")
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
        if len(result) != 2 or not isinstance(result[0], bool) or not result[0]:
            detail = (
                _as_text(result[1])
                if len(result) == 2 and isinstance(result[0], bool)
                else "The daemon did not provide a valid cache-cleaning result."
            )
            raise Dnf5DaemonCallError("DNF cache clean failed", detail)

    def list_available_updates(
        self, *, refresh: bool = False
    ) -> tuple[DnfPackageRecord, ...]:
        """Return upgrade candidates through the daemon's read-only RPM API."""
        self._require_daemon_queries("check for DNF5 package updates")
        if refresh:
            self._expire_metadata_cache("Could not refresh DNF metadata")

        def _query(session_path: str) -> tuple[DnfPackageRecord, ...]:
            fd_options: dict[str, Any] = {
                "scope": "upgrades",
                "latest-limit": 1,
                "package_attrs": _UPDATE_PACKAGE_ATTRS,
                "interactive": False,
                "with_src": True,
                "with_nevra": True,
                "with_provides": False,
                "with_filenames": False,
                "with_binaries": False,
                "icase": True,
            }
            try:
                raw_records = self._stream_packages_fd_in_session(
                    session_path, fd_options, cancel_event=None
                )
            except Dnf5DaemonUnsupportedError:
                response = self._call(
                    session_path,
                    RPM_INTERFACE,
                    "list",
                    ("a{sv}",),
                    (
                        {
                            "scope": "upgrades",
                            "latest-limit": 1,
                            "package_attrs": _UPDATE_PACKAGE_ATTRS,
                            "interactive": False,
                        },
                    ),
                )
                return _parse_package_records(response, "upgrade candidates")
            return _parse_package_records((raw_records,), "upgrade candidates")

        return self._with_session(
            _query,
            load_system_repo=True,
            load_available_repos=True,
        )

    def _stream_packages_fd_in_session(
        self,
        session_path: str,
        options: dict[str, Any],
        *,
        cancel_event: threading.Event | None,
    ) -> tuple[dict[str, Any], ...]:
        transport = self._get_transport()
        condition = threading.Condition()
        finished: dict[str, tuple[bool, str]] = {}

        def _on_finished(arguments: tuple[Any, ...]) -> None:
            if len(arguments) < 2:
                return
            ok = bool(arguments[0])
            sig_transfer_id = str(arguments[1] or "")
            error_msg = _as_text(arguments[2] if len(arguments) > 2 else "").strip()
            if not sig_transfer_id:
                return
            with condition:
                finished[sig_transfer_id] = (ok, error_msg)
                condition.notify_all()

        subscription: object | None = None
        subscribe_fn = getattr(transport, "subscribe_background", transport.subscribe)
        try:
            subscription = subscribe_fn(
                session_path,
                RPM_INTERFACE,
                WRITE_TO_FD_FINISHED_SIGNAL,
                _on_finished,
            )
        except Exception:
            LOGGER.debug(
                "Could not subscribe to %s.%s on %s.",
                RPM_INTERFACE,
                WRITE_TO_FD_FINISHED_SIGNAL,
                session_path,
                exc_info=True,
            )

        read_fd, write_fd = _create_pipe()
        records: list[dict[str, Any]] = []
        reader_errors: list[str] = []

        def _read_stream() -> None:
            buffer = bytearray()
            poller = select.poll() if cancel_event is not None else None
            if poller is not None:
                poller.register(read_fd, select.POLLIN | select.POLLHUP)
            try:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    if poller is not None:
                        events = poller.poll(50)
                        if not events:
                            continue
                    chunk = os.read(read_fd, 65536)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    while True:
                        newline = buffer.find(b"\n")
                        if newline < 0:
                            break
                        raw_line = bytes(buffer[:newline]).rstrip(b"\r")
                        del buffer[: newline + 1]
                        if not raw_line.strip():
                            continue
                        text = raw_line.decode("utf-8")
                        records.append(_parse_fd_package_json_line(text))
                if buffer.strip() and not (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    text = bytes(buffer).decode("utf-8")
                    records.append(_parse_fd_package_json_line(text))
            except (UnicodeDecodeError, ValueError, OSError) as exc:
                reader_errors.append(str(exc))
            finally:
                os.close(read_fd)

        reader_thread = threading.Thread(
            target=_read_stream,
            name="dendro-rpm-list-fd-reader",
            daemon=True,
        )
        reader_thread.start()
        transfer_id = ""
        try:
            try:
                try:
                    response = self._call(
                        session_path,
                        RPM_INTERFACE,
                        "list_fd",
                        ("a{sv}", "h"),
                        (options, write_fd),
                    )
                finally:
                    os.close(write_fd)

                if (
                    len(response) != 1
                    or not isinstance(response[0], str)
                    or not response[0]
                ):
                    raise Dnf5DaemonCallError(
                        "DNF5 daemon returned invalid package stream",
                        "Rpm.list_fd did not return a transfer identifier.",
                    )
                transfer_id = response[0]
            finally:
                reader_thread.join(timeout=30.0)

            if cancel_event is not None and cancel_event.is_set():
                return ()

            if reader_thread.is_alive():
                raise Dnf5DaemonCallError(
                    "DNF5 daemon package stream stalled",
                    "The DNF5 daemon did not finish streaming package data in time.",
                )

            with condition:
                result = finished.get(transfer_id)
                if result is None and subscription is not None:
                    condition.wait_for(lambda: transfer_id in finished, timeout=0.5)
                    result = finished.get(transfer_id)
        finally:
            if subscription is not None:
                try:
                    transport.unsubscribe(subscription)
                except Exception:
                    LOGGER.debug(
                        "Could not unsubscribe from %s.",
                        WRITE_TO_FD_FINISHED_SIGNAL,
                        exc_info=True,
                    )

        if result is not None and not result[0]:
            raise Dnf5DaemonCallError(
                "DNF5 daemon package query failed",
                result[1] or "Rpm.list_fd failed while streaming packages.",
            )
        if reader_errors:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                reader_errors[0],
            )
        return tuple(records)

    def list_packages_fd(
        self,
        *,
        scope: str = "all",
        patterns: Sequence[str] = (),
        package_attrs: Sequence[str] = (),
        latest_limit: int | None = None,
        with_src: bool = True,
        with_nevra: bool = True,
        with_provides: bool = True,
        with_filenames: bool = True,
        with_binaries: bool = True,
        icase: bool = True,
        load_system_repo: bool = True,
        load_available_repos: bool = False,
        optional_metadata_types: Sequence[str] = (),
        cancel_event: threading.Event | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Stream JSON package records from ``org.rpm.dnf.v0.rpm.Rpm.list_fd``."""
        self._require_daemon_queries("query DNF5 packages")
        if cancel_event is not None and cancel_event.is_set():
            return ()
        options: dict[str, Any] = {
            "scope": scope,
            "interactive": False,
            "with_src": bool(with_src),
            "with_nevra": bool(with_nevra),
            "with_provides": bool(with_provides),
            "with_filenames": bool(with_filenames),
            "with_binaries": bool(with_binaries),
            "icase": bool(icase),
        }
        if patterns:
            options["patterns"] = tuple(patterns)
        if package_attrs:
            options["package_attrs"] = tuple(package_attrs)
        if latest_limit is not None:
            options["latest-limit"] = int(latest_limit)

        return self._with_session(
            lambda session_path: self._stream_packages_fd_in_session(
                session_path, options, cancel_event=cancel_event
            ),
            load_system_repo=load_system_repo,
            load_available_repos=load_available_repos,
            optional_metadata_types=optional_metadata_types,
        )

    def list_groups(
        self,
        *,
        scope: str = "all",
        with_hidden: bool = True,
        attributes: Sequence[str] = (
            "groupid",
            "name",
            "installed",
            "uservisible",
            "repos",
            "packages",
        ),
        patterns: Sequence[str] = (),
        contains_pkgs: Sequence[str] = (),
        load_system_repo: bool = True,
        load_available_repos: bool = True,
    ) -> tuple[DnfCompsGroup, ...]:
        """Return Fedora Comps groups via ``org.rpm.dnf.v0.comps.Group.list``."""
        self._require_daemon_queries("list DNF5 Comps groups")
        normalized_scope = scope.strip().lower()
        if normalized_scope not in {"all", "installed", "available"}:
            raise Dnf5DaemonValidationError(
                "Invalid group query scope",
                f"Unsupported Comps group scope {scope!r}.",
            )
        options: dict[str, Any] = {
            "scope": normalized_scope,
            "with_hidden": bool(with_hidden),
            "interactive": False,
        }
        if attributes:
            options["attributes"] = tuple(attributes)
        if patterns:
            options["patterns"] = tuple(patterns)
        if contains_pkgs:
            options["contains_pkgs"] = tuple(contains_pkgs)

        response = self._with_session(
            lambda session_path: self._call(
                session_path,
                GROUP_INTERFACE,
                "list",
                ("a{sv}",),
                (options,),
            ),
            load_system_repo=load_system_repo,
            load_available_repos=load_available_repos,
        )
        return _parse_comps_groups(response)

    def list_history(
        self,
        *,
        limit: int = 200,
        reverse: bool = False,
        include_packages: bool = True,
    ) -> tuple[DnfHistoryTransaction, ...]:
        """Return transaction history entries via ``org.rpm.dnf.v0.History.list``."""
        self._require_daemon_queries("read DNF5 transaction history")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError(
                f"list_history limit must be an integer, got {type(limit).__name__}."
            )
        options: dict[str, Any] = {
            "reverse": bool(reverse),
            "include_packages": bool(include_packages),
            "transaction_attrs": ("id", "start", "end", "description", "status"),
        }
        if include_packages:
            options["package_attrs"] = ("name",)
        if limit > 0:
            options["limit"] = DbusInt64(limit)

        response = self._with_session(
            lambda session_path: self._call(
                session_path,
                HISTORY_INTERFACE,
                "list",
                ("a{sv}",),
                (options,),
            ),
            load_system_repo=False,
            load_available_repos=False,
        )
        return _parse_history_transactions(response)

    def list_security_advisories(
        self, *, refresh: bool = False
    ) -> tuple[SecurityAdvisory, ...]:
        """Return the security advisories that affect this system."""
        self._require_daemon_queries("read DNF5 security advisories")
        if refresh:
            self._expire_metadata_cache("Could not refresh DNF security metadata")
        response = self._with_session(
            lambda session_path: self._call(
                session_path,
                ADVISORY_INTERFACE,
                "list",
                ("a{sv}",),
                (
                    {
                        "advisory_attrs": (
                            "advisoryid",
                            "title",
                            "type",
                            "severity",
                            "description",
                            "references",
                            "collections",
                        ),
                        "availability": "available",
                        "with_cve": False,
                        "with_bz": False,
                        "interactive": False,
                    },
                ),
            ),
            load_system_repo=True,
            load_available_repos=True,
        )
        return _parse_advisories(response)

    def list_repositories(self) -> tuple[DnfRepository, ...]:
        self._require_daemon_queries("list DNF5 repositories")
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
            baseurl, repo_urls = _extract_repository_urls(raw)
            repositories.append(
                DnfRepository(
                    identifier=repo_id,
                    name=_as_text(raw.get("name", repo_id)) or repo_id,
                    enabled=raw_enabled,
                    baseurl=baseurl,
                    repo_file=_as_text(raw.get("repofile", "")),
                    urls=repo_urls,
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
                if self._is_flatpak():
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
        self,
        *,
        load_system_repo: bool,
        load_available_repos: bool,
        optional_metadata_types: Sequence[str] = (),
    ) -> str:
        session_config: dict[str, Any] = {
            "load_system_repo": load_system_repo,
            "load_available_repos": load_available_repos,
            "locale": daemon_locale(),
        }
        normalized_metadata_types = tuple(
            item.strip() for item in optional_metadata_types if str(item).strip()
        )
        if normalized_metadata_types:
            session_config["optional_metadata_types"] = normalized_metadata_types
        response = self._call(
            ROOT_PATH,
            SESSION_MANAGER_INTERFACE,
            "open_session",
            ("a{sv}",),
            (session_config,),
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
        operation: Callable[[str], Any],
        *,
        load_system_repo: bool,
        load_available_repos: bool,
        optional_metadata_types: Sequence[str] = (),
    ) -> Any:
        session_path, key_responder = self._open_session_answering_key_prompts(
            load_system_repo=load_system_repo,
            load_available_repos=load_available_repos,
            optional_metadata_types=optional_metadata_types,
        )
        operation_error: Exception | None = None
        try:
            return operation(session_path)
        except Exception as exc:
            operation_error = exc
            raise
        finally:
            if key_responder is not None:
                key_responder.stop()
            try:
                self._close_session_path(session_path)
            except Exception as close_error:
                if operation_error is None:
                    raise
                raise Dnf5DaemonCallError(
                    "DNF5 daemon cleanup failed",
                    f"{operation_error}; additionally, closing the daemon session failed: {close_error}",
                ) from close_error

    def _open_session_answering_key_prompts(
        self,
        *,
        load_system_repo: bool,
        load_available_repos: bool,
        optional_metadata_types: Sequence[str] = (),
    ) -> tuple[str, _RepositoryKeyResponder | None]:
        session_path = self._open_session(
            load_system_repo=load_system_repo,
            load_available_repos=load_available_repos,
            optional_metadata_types=optional_metadata_types,
        )
        if not load_available_repos:
            return session_path, None
        responder = _RepositoryKeyResponder(
            confirm=self._confirm_repository_key,
            automatic=self._key_import_is_automatic,
            ask=self._ask_about_repository_key,
            imported=self._report_imported_key,
            declined=self._report_declined_key,
        )
        responder.watch(self._get_transport(), session_path)
        return session_path, responder

    def _key_import_is_automatic(self) -> bool:
        provider = self._key_import_policy
        if provider is not None:
            return bool(provider())
        return repository_key_import_is_automatic()

    def _ask_about_repository_key(self, prompt: RepositoryKeyPrompt) -> bool:
        provider = self._key_import_prompt
        if provider is None:
            return False
        return bool(provider(prompt))

    def _confirm_repository_key(
        self, session_path: str, key_id: str, confirmed: bool
    ) -> None:
        self._get_transport().call(
            session_path,
            REPOSITORY_INTERFACE,
            "confirm_key_with_options",
            ("s", "b", "a{sv}"),
            (key_id, confirmed, {"interactive": True}),
        )

    def _report_imported_key(self, arguments: tuple[Any, ...]) -> None:
        try:
            _session_path, key_id, fingerprint = _key_import_arguments(arguments)
        except ValueError:
            LOGGER.debug("Ignoring an unreadable key-imported signal.", exc_info=True)
            return
        LOGGER.info(
            "The DNF5 daemon imported a repository OpenPGP key (%s, %s).",
            key_id,
            fingerprint or "unknown fingerprint",
        )

    def _report_declined_key(self, prompt: RepositoryKeyPrompt) -> None:
        key = prompt.key_id or "unknown key"
        fingerprint = prompt.fingerprint or "unknown fingerprint"
        message = (
            f"Repository signing key {key} ({fingerprint}) was not trusted, so the "
            "repository that needs it was left out of this transaction. Import the "
            "key with 'sudo dnf' or enable automatic repository key import in "
            "Settings if you trust this repository."
        )
        LOGGER.warning(
            "Declined to import a repository signing key in session %s: %s",
            prompt.session_path,
            message,
        )
        with self._lock:
            if message not in self._declined_keys:
                self._declined_keys.append(message)

    def take_declined_key_notices(self) -> tuple[str, ...]:
        """Return and clear notices about repositories skipped for a missing key."""
        with self._lock:
            notices = tuple(self._declined_keys)
            self._declined_keys.clear()
        return notices

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
        with self._lock:
            known = self._plans.get(plan.token)
            if known is not plan or plan._closed:
                return
            plan._closed = True
            self._plans.pop(plan.token, None)
            key_responder = self._key_responders.pop(plan.token, None)
        if key_responder is not None:
            key_responder.stop()
        self._close_session_path(plan.session_path)

    def _close_session_path(self, session_path: str) -> None:
        response = self._call(
            ROOT_PATH,
            SESSION_MANAGER_INTERFACE,
            "close_session",
            ("o",),
            (session_path,),
        )
        if len(response) != 1 or not isinstance(response[0], bool) or not response[0]:
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


class RepositoryListWorker(ShutdownSafeRunnable):
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
        except Exception as exc:
            self.signals.failed.emit(
                "Unable to load repositories", f"{type(exc).__name__}: {exc}"
            )
        else:
            self.signals.loaded.emit(repositories)


__all__ = (
    "ADVISORY_INTERFACE",
    "AUTOMATIC_KEY_IMPORT_PREFERENCE",
    "BASE_INTERFACE",
    "GOAL_INTERFACE",
    "GROUP_INTERFACE",
    "HISTORY_INTERFACE",
    "KEY_IMPORTED_SIGNAL",
    "KEY_IMPORT_REQUEST_SIGNAL",
    "NO_CLIENT_SIDE_TIMEOUT_MS",
    "OFFLINE_INTERFACE",
    "REPOSITORY_INTERFACE",
    "ROOT_PATH",
    "RPM_INTERFACE",
    "SERVICE_NAME",
    "SESSION_MANAGER_INTERFACE",
    "TRANSACTION_ERROR",
    "WRITE_TO_FD_FINISHED_SIGNAL",
    "DbusInt64",
    "DbusTransport",
    "Dnf5DaemonCallError",
    "Dnf5DaemonClient",
    "Dnf5DaemonError",
    "Dnf5DaemonPlanError",
    "Dnf5DaemonResolutionError",
    "Dnf5DaemonUnavailable",
    "Dnf5DaemonUnsupportedError",
    "Dnf5DaemonValidationError",
    "DnfCompsGroup",
    "DnfCompsPackage",
    "DnfHistoryTransaction",
    "DnfPackageRecord",
    "DnfRepository",
    "QtDbusTransport",
    "RepositoryKeyPrompt",
    "RepositoryListSignals",
    "RepositoryListWorker",
    "SecurityAdvisory",
    "TransactionExecutionResult",
    "TransactionItem",
    "TransactionPlan",
    "TransactionRequest",
    "_PlanProgress",
    "_QtDbusSignalSink",
    "_RepositoryKeyResponder",
    "_Subscription",
    "_advisory_affected_packages",
    "_advisory_references",
    "_as_text",
    "_extract_repository_urls",
    "_format_offline_transaction_error",
    "_inbound_package_names",
    "_is_effective_removal",
    "_key_import_arguments",
    "_key_import_prompt",
    "_looks_like_local_rpm",
    "_marshal_argument",
    "_marshal_string_array",
    "_marshal_string_map",
    "_marshal_variant_map",
    "_marshal_variant_value",
    "_normalise_repository_id",
    "_normalise_specs",
    "_parse_advisories",
    "_parse_comps_groups",
    "_parse_fd_package_json_line",
    "_parse_history_transactions",
    "_parse_package_records",
    "_parse_transaction_item",
    "_require_sequence",
    "_signed_size",
    "_single_line",
    "_size_attribute",
    "_unwrap_dbus_value",
    "format_byte_size",
    "repository_key_import_is_automatic",
)
