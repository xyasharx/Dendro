# dendro/core/dnf5daemon_types.py
"""Typed constants, exceptions, DTOs, and validation helpers for DNF5 D-Bus."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SERVICE_NAME = "org.rpm.dnf.v0"
ROOT_PATH = "/org/rpm/dnf/v0"
SESSION_MANAGER_INTERFACE = "org.rpm.dnf.v0.SessionManager"
BASE_INTERFACE = "org.rpm.dnf.v0.Base"
GOAL_INTERFACE = "org.rpm.dnf.v0.Goal"
RPM_INTERFACE = "org.rpm.dnf.v0.rpm.Rpm"
REPOSITORY_INTERFACE = "org.rpm.dnf.v0.rpm.Repo"
ADVISORY_INTERFACE = "org.rpm.dnf.v0.Advisory"
OFFLINE_INTERFACE = "org.rpm.dnf.v0.Offline"
HISTORY_INTERFACE = "org.rpm.dnf.v0.History"

TRANSACTION_ERROR = "org.rpm.dnf.v0.rpm.Rpm.TransactionError"

WRITE_TO_FD_FINISHED_SIGNAL = "write_to_fd_finished"

# The daemon asks its client whether an OpenPGP repository key may be imported
# before repository metadata is trusted.  ``Session::wait_for_key_confirmation``
# blocks the calling D-Bus method for up to five minutes waiting for that
# answer and then fails the whole repository load, so a client that ignores the
# signal leaves the user watching a spinner until the daemon gives up.
KEY_IMPORT_REQUEST_SIGNAL = "repo_key_import_request"
KEY_IMPORTED_SIGNAL = "repo_key_imported"
AUTOMATIC_KEY_IMPORT_PREFERENCE = "security/import_repository_keys_automatically"

# Qt's default D-Bus call timeout is generally 25 seconds.  A package
# transaction can legitimately download or process packages for much longer.
# Qt requires a finite signed integer, so this is the largest accepted value
# and is intentionally a practical "no client-side timeout", not a retry or
# polling policy.  Server errors, Polkit cancellation, and DNF transaction
# completion still provide the operation's terminal state.
NO_CLIENT_SIDE_TIMEOUT_MS = 2_147_483_647


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


class Dnf5DaemonUnsupportedError(Dnf5DaemonCallError):
    """The installed daemon does not expose the requested D-Bus interface or method."""


class Dnf5DaemonValidationError(Dnf5DaemonError):
    """The frontend rejected an invalid request before contacting the daemon."""


class Dnf5DaemonPlanError(Dnf5DaemonError):
    """The caller attempted an invalid or stale resolved-plan transition."""


class Dnf5DaemonResolutionError(Dnf5DaemonError):
    """DNF's solver rejected a requested package transaction."""

    def __init__(
        self,
        detail: str,
        problems: Sequence[str],
        *,
        allow_erasing_attempted: bool = False,
    ) -> None:
        super().__init__("Unable to resolve transaction", detail)
        self.problems = tuple(problems)
        self.allow_erasing_attempted = bool(allow_erasing_attempted)


@dataclass(frozen=True, slots=True)
class DbusInt64:
    """Explicit signed 64-bit integer (``x``) for D-Bus ``a{sv}`` variants.

    PyQt6 converts bare Python ``int`` values inside ``QDBusVariant`` as
    32-bit ``INT32`` (``i``).  Options declared as ``int64`` in
    ``dnf5daemon-server`` (such as ``History.list``'s ``limit`` and ``since``)
    strictly require ``INT64`` (``x``) and fail otherwise with
    ``Map item "<key>" has incorrect type.``.
    """

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError(
                f"DbusInt64 requires an integer value, got {type(self.value).__name__}."
            )
        normalized = int(self.value)
        if normalized < -(2**63) or normalized >= 2**63:
            raise ValueError(
                f"DbusInt64 value {normalized} is out of signed 64-bit range."
            )
        object.__setattr__(self, "value", normalized)


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else str(value) if value is not None else ""


def _size_attribute(value: Any) -> int:
    """Read one D-Bus size field, treating anything unusable as "unknown"."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        size = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, size)


def format_byte_size(size_bytes: int) -> str:
    """Render a byte count for plan summaries."""
    if size_bytes < 0:
        return "unknown size"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _signed_size(size_bytes: int) -> str:
    """Format a signed byte count for a disk-change summary."""
    if size_bytes == 0:
        return "no change"
    sign = "+" if size_bytes > 0 else "-"
    return f"{sign}{format_byte_size(abs(size_bytes))}"


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


def _single_line(value: Any, *, max_length: int = 1024) -> str:
    """Flatten an external D-Bus string into one bounded, printable line."""
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = "".join(character if character.isprintable() else " " for character in text)
    text = " ".join(text.split())
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "\u2026"
    return text


def _inbound_package_names(items: Sequence[TransactionItem]) -> set[str]:
    """Names that this transaction installs or re-installs."""
    return {
        item.name.casefold()
        for item in items
        if item.action.casefold() in {"install", "reinstall", "upgrade", "downgrade"}
        and item.name
    }


def _is_effective_removal(item: TransactionItem, inbound_names: set[str]) -> bool:
    """Report whether an item really leaves its package name uninstalled."""
    if not item.is_removal:
        return False
    if item.action.casefold() == "remove":
        return True
    return item.name.casefold() not in inbound_names


@dataclass(frozen=True, slots=True)
class TransactionRequest:
    """The complete intent presented to DNF for one atomic resolution."""

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
    ) -> TransactionRequest:
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
    def install_size_bytes(self) -> int:
        """Return the installed size the daemon reported, or 0 when absent."""
        return _size_attribute(self.package.get("install_size"))

    @property
    def download_size_bytes(self) -> int:
        """Return the download size the daemon reported, or 0 when absent."""
        return _size_attribute(self.package.get("download_size"))

    @property
    def is_removal(self) -> bool:
        return self.action.casefold() in {"remove", "replaced"}

    @property
    def is_skipped(self) -> bool:
        return (
            self.object_type.strip().casefold() == "skipped"
            or self.action.strip().casefold() == "skipped"
        )

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
            "reason change": "Change reason",
            "skipped": "Skipped",
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
        return any(not item.is_skipped for item in self.items)

    @property
    def install_size_delta_bytes(self) -> int:
        """Net change in installed size, in bytes, across the active items."""
        total = 0
        for item in self.items:
            if item.is_skipped:
                continue
            size = item.install_size_bytes
            total += -size if item.is_removal else size
        return total

    @property
    def download_size_total_bytes(self) -> int:
        """Bytes the daemon reported it must download for the active items."""
        return sum(
            item.download_size_bytes for item in self.items if not item.is_skipped
        )

    def categorized_names(self) -> tuple[list[str], list[str], list[str]]:
        installs: list[str] = []
        removals: list[str] = []
        upgrades: list[str] = []
        inbound_names = _inbound_package_names(self.items)
        for item in self.items:
            if item.is_skipped:
                continue
            action = item.action.casefold()
            if action in {"install", "reinstall"}:
                installs.append(item.nevra)
            elif action in {"upgrade", "downgrade"}:
                upgrades.append(item.nevra)
            elif _is_effective_removal(item, inbound_names):
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
        active_items = tuple(item for item in self.items if not item.is_skipped)
        skipped_items = tuple(item for item in self.items if item.is_skipped)
        if not active_items:
            lines.append("No package changes are required.")
        else:
            lines.append("Resolved package changes:")
            lines.extend(
                f"  {item.action_label}: {item.nevra} ({item.reason})"
                for item in active_items
            )
            lines.append("")
            lines.append(
                f"Download: {format_byte_size(self.download_size_total_bytes)}  "
                f"Disk change: {_signed_size(self.install_size_delta_bytes)}"
            )
        if skipped_items:
            lines.append("")
            lines.append("Skipped items:")
            lines.extend(
                f"  {item.action_label}: {item.nevra} ({item.reason})"
                for item in skipped_items
            )
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
    urls: tuple[str, ...] = ()


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
class DnfHistoryTransaction:
    """One transaction record returned by ``org.rpm.dnf.v0.History.list``."""

    transaction_id: int
    command_line: str
    start_time: int
    end_time: int
    status: str
    altered_count: int
    actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SecurityAdvisory:
    """One security advisory as the daemon reports it."""

    advisory_id: str
    title: str
    advisory_type: str
    severity: str
    affected_packages: tuple[str, ...] = ()
    cve_ids: tuple[str, ...] = ()
    bugzilla_ids: tuple[str, ...] = ()
    description: str = ""

    @property
    def affects_installed_packages(self) -> bool:
        """True when at least one affected package is installed and outdated."""
        return bool(self.affected_packages)

    @property
    def severity_key(self) -> int:
        """Normalised severity for sorting: critical first, unknown last."""
        order = {
            "critical": 0,
            "important": 1,
            "high": 2,
            "moderate": 3,
            "medium": 3,
            "low": 4,
            "bugfix": 6,
            "enhancement": 6,
            "newpackage": 6,
            "none": 7,
        }
        return order.get(self.severity.casefold(), 5)


@dataclass(frozen=True, slots=True)
class RepositoryKeyPrompt:
    """One OpenPGP key the daemon wants to trust, before any decision is made."""

    session_path: str
    key_id: str
    fingerprint: str
    key_url: str
    user_ids: tuple[str, ...] = ()
