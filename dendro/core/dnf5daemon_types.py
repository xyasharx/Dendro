# dendro/core/dnf5daemon_types.py
"""Typed constants, exceptions, DTOs, and validation helpers for DNF5 D-Bus."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cmp_to_key
from typing import Any, Final

from dendro.core.i18n import format_byte_size, tr
from dendro.core.package_types import (
    AvailableUpdateInfo,
    HistoryPackageChange,
    _normalise_rpm_epoch,
    compare_rpm_evr,
    split_rpm_nevra,
)

SERVICE_NAME = "org.rpm.dnf.v0"
ROOT_PATH = "/org/rpm/dnf/v0"
SESSION_MANAGER_INTERFACE = "org.rpm.dnf.v0.SessionManager"
BASE_INTERFACE = "org.rpm.dnf.v0.Base"
GOAL_INTERFACE = "org.rpm.dnf.v0.Goal"
RPM_INTERFACE = "org.rpm.dnf.v0.rpm.Rpm"
REPOSITORY_INTERFACE = "org.rpm.dnf.v0.rpm.Repo"
GROUP_INTERFACE = "org.rpm.dnf.v0.comps.Group"
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
        super().__init__(tr("Unable to resolve transaction"), detail)
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


def _signed_size(size_bytes: int) -> str:
    """Format a signed byte count for a disk-change summary."""
    if size_bytes == 0:
        return tr("no change")
    sign = "+" if size_bytes > 0 else "-"
    return f"{sign}{format_byte_size(abs(size_bytes))}"


def _normalise_specs(values: Sequence[str], action: str) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    if len(values) > 10_000:
        raise Dnf5DaemonValidationError(
            tr("Too many package changes"),
            tr("The {action} request contains more than 10,000 entries.").format(
                action=action
            ),
        )
    for raw in values:
        if not isinstance(raw, str):
            raise Dnf5DaemonValidationError(
                tr("Invalid package specification"),
                tr("A {action} request is not text.").format(action=action),
            )
        if not raw or raw != raw.strip():
            raise Dnf5DaemonValidationError(
                tr("Invalid package specification"),
                tr(
                    "Package specifications cannot be empty or have leading/trailing whitespace: {value}"
                ).format(value=repr(raw)),
            )
        if len(raw) > 4096:
            raise Dnf5DaemonValidationError(
                tr("Invalid package specification"),
                tr("A package specification exceeds 4096 characters."),
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in raw):
            raise Dnf5DaemonValidationError(
                tr("Invalid package specification"),
                tr("Package specifications cannot contain control characters."),
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
            tr("Invalid repository ID"),
            tr("Repository IDs must be non-empty text no longer than 255 characters."),
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise Dnf5DaemonValidationError(
            tr("Invalid repository ID"),
            tr("Repository IDs cannot contain control characters."),
        )
    return value


def _looks_like_local_rpm(spec: str) -> bool:
    return os.path.isabs(spec) and spec.casefold().endswith(".rpm")


def _require_sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise Dnf5DaemonCallError(
            tr("DNF5 daemon returned invalid data"),
            tr("Expected {label} to be an array.").format(label=label),
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
    reinstalls: tuple[str, ...] = ()
    downgrades: tuple[str, ...] = ()
    distro_syncs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        installs = _normalise_specs(self.installs, "install")
        removals = _normalise_specs(self.removals, "remove")
        upgrades = _normalise_specs(self.upgrades, "upgrade")
        reinstalls = _normalise_specs(self.reinstalls, "reinstall")
        downgrades = _normalise_specs(self.downgrades, "downgrade")
        distro_syncs = _normalise_specs(self.distro_syncs, "distro-sync")

        if (
            not installs
            and not removals
            and not upgrades
            and not self.upgrade_all
            and not reinstalls
            and not downgrades
            and not distro_syncs
        ):
            raise Dnf5DaemonValidationError(
                tr("No package changes requested"),
                tr(
                    "Select at least one package action before resolving a transaction."
                ),
            )
        if self.upgrade_all and upgrades:
            raise Dnf5DaemonValidationError(
                tr("Conflicting upgrade request"),
                tr(
                    "A full system upgrade cannot be combined with a selective upgrade list."
                ),
            )

        action_sets = (
            set(installs),
            set(removals),
            set(upgrades),
            set(reinstalls),
            set(downgrades),
            set(distro_syncs),
        )
        seen_specs: set[str] = set()
        conflict_set: set[str] = set()
        for bucket in action_sets:
            conflict_set.update(seen_specs & bucket)
            seen_specs.update(bucket)
        conflicts = sorted(conflict_set)
        if conflicts:
            raise Dnf5DaemonValidationError(
                tr("Conflicting package actions"),
                tr(
                    "The same package specification appears in incompatible actions: {conflicts}"
                ).format(conflicts=", ".join(conflicts)),
            )

        object.__setattr__(self, "installs", installs)
        object.__setattr__(self, "removals", removals)
        object.__setattr__(self, "upgrades", upgrades)
        object.__setattr__(self, "reinstalls", reinstalls)
        object.__setattr__(self, "downgrades", downgrades)
        object.__setattr__(self, "distro_syncs", distro_syncs)

    @classmethod
    def from_values(
        cls,
        *,
        installs: Sequence[str] = (),
        removals: Sequence[str] = (),
        upgrades: Sequence[str] = (),
        upgrade_all: bool = False,
        reinstalls: Sequence[str] = (),
        downgrades: Sequence[str] = (),
        distro_syncs: Sequence[str] = (),
    ) -> TransactionRequest:
        return cls(
            tuple(installs),
            tuple(removals),
            tuple(upgrades),
            upgrade_all,
            tuple(reinstalls),
            tuple(downgrades),
            tuple(distro_syncs),
        )


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
            "install": tr("Install"),
            "upgrade": tr("Upgrade"),
            "downgrade": tr("Downgrade"),
            "remove": tr("Remove"),
            "reinstall": tr("Reinstall"),
            "replaced": tr("Replace"),
            "reason_change": tr("Change reason"),
            "reason change": tr("Change reason"),
            "skipped": tr("Skipped"),
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

    def grouped_names(
        self,
    ) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
        """Return ``(installs, removals, upgrades, reinstalls, downgrades)``."""
        installs: list[str] = []
        removals: list[str] = []
        upgrades: list[str] = []
        reinstalls: list[str] = []
        downgrades: list[str] = []
        inbound_names = _inbound_package_names(self.items)
        for item in self.items:
            if item.is_skipped:
                continue
            action = item.action.casefold()
            if action == "install":
                installs.append(item.nevra)
            elif action == "reinstall":
                reinstalls.append(item.nevra)
            elif action == "upgrade":
                upgrades.append(item.nevra)
            elif action == "downgrade":
                downgrades.append(item.nevra)
            elif _is_effective_removal(item, inbound_names):
                removals.append(item.nevra)
        return installs, removals, upgrades, reinstalls, downgrades

    def preview_text(self) -> str:
        """Stable, locale-safe display text derived from DNF's typed output."""
        lines = [
            tr(
                "DNF5 daemon resolved this transaction. No change has been executed yet."
            ),
            "",
        ]
        if self.warnings:
            lines.append(tr("Resolver notices:"))
            lines.extend(f"  - {warning}" for warning in self.warnings)
            lines.append("")
        active_items = tuple(item for item in self.items if not item.is_skipped)
        skipped_items = tuple(item for item in self.items if item.is_skipped)
        if not active_items:
            lines.append(tr("No package changes are required."))
        else:
            lines.append(tr("Resolved package changes:"))
            lines.extend(
                f"  {item.action_label}: {item.nevra} ({item.reason})"
                for item in active_items
            )
            lines.append("")
            lines.append(
                "  "
                + tr("Download: {download}  Disk change: {disk}").format(
                    download=format_byte_size(self.download_size_total_bytes),
                    disk=_signed_size(self.install_size_delta_bytes),
                )
            )
        if skipped_items:
            lines.append("")
            lines.append(tr("Skipped items:"))
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
    is_installed: bool = False
    from_repository: str = ""
    download_size_bytes: int = 0
    build_time: int = 0

    @property
    def name_arch(self) -> str:
        """Return DNF's architecture-qualified selector for this package."""
        return f"{self.name}.{self.arch}" if self.name and self.arch else self.name

    @property
    def evr_tuple(self) -> tuple[str, str, str]:
        """Return normalized ``(epoch, version, release)``."""
        return (_normalise_rpm_epoch(self.epoch), self.version, self.release)

    @property
    def evr(self) -> str:
        """Return formatted ``[epoch:]version-release``."""
        epoch = _normalise_rpm_epoch(self.epoch)
        vr = f"{self.version}-{self.release}" if self.release else self.version
        return f"{epoch}:{vr}" if epoch != "0" and vr else vr

    @property
    def full_nevra(self) -> str:
        """Return exact ``name-[epoch:]version-release.arch`` selector."""
        if not all((self.name, self.version, self.release, self.arch)):
            return self.name_arch
        return f"{self.name}-{self.evr}.{self.arch}"


@dataclass(frozen=True, slots=True)
class PackageRepoVersions:
    """Installed and repository package versions resolved for one package."""

    package_name: str
    arch: str
    installed: tuple[DnfPackageRecord, ...] = ()
    available: tuple[DnfPackageRecord, ...] = ()
    downgrades: tuple[DnfPackageRecord, ...] = ()
    sync_target: DnfPackageRecord | None = None
    sync_needed: bool = False
    sync_direction: str = ""


def _compare_records_desc(a: DnfPackageRecord, b: DnfPackageRecord) -> int:
    cmp_evr = compare_rpm_evr(a.evr_tuple, b.evr_tuple)
    if cmp_evr != 0:
        return -cmp_evr
    if a.repository != b.repository:
        return -1 if a.repository < b.repository else 1
    return 0


def build_package_repo_versions(
    records: Sequence[DnfPackageRecord],
    *,
    package_name: str,
    arch: str = "",
    installed_evr: tuple[str, str, str] | None = None,
) -> PackageRepoVersions:
    """Build sorted downgrade candidates and distro-sync state from daemon records."""
    exact_name = [record for record in records if record.name == package_name]
    normalized_arch = arch.strip()

    installed_records: list[DnfPackageRecord] = []
    available_records: list[DnfPackageRecord] = []
    for record in exact_name:
        if normalized_arch and record.arch != normalized_arch:
            continue
        repo_norm = record.repository.lstrip("@").casefold()
        if record.is_installed or repo_norm == "system":
            installed_records.append(record)
        else:
            available_records.append(record)

    # Rare packaging transitions change an RPM between native arch and noarch.
    if not available_records and normalized_arch and normalized_arch != "noarch":
        available_records = [
            record
            for record in exact_name
            if record.arch == "noarch"
            and not (
                record.is_installed
                or record.repository.lstrip("@").casefold() == "system"
            )
        ]

    available_sorted = sorted(available_records, key=cmp_to_key(_compare_records_desc))

    deduped_available: list[DnfPackageRecord] = []
    seen_nevras: set[str] = set()
    for record in available_sorted:
        if record.full_nevra not in seen_nevras:
            seen_nevras.add(record.full_nevra)
            deduped_available.append(record)

    ref_evr: tuple[str, str, str] | None = None
    if installed_evr is not None:
        ref_evr = (
            _normalise_rpm_epoch(installed_evr[0]),
            installed_evr[1],
            installed_evr[2],
        )
    elif installed_records:
        ref_evr = installed_records[0].evr_tuple

    downgrades = tuple(
        record
        for record in deduped_available
        if ref_evr is not None and compare_rpm_evr(record.evr_tuple, ref_evr) < 0
    )

    sync_target = deduped_available[0] if deduped_available else None
    sync_needed = False
    sync_direction = ""
    if sync_target is not None and ref_evr is not None:
        cmp_sync = compare_rpm_evr(sync_target.evr_tuple, ref_evr)
        if cmp_sync > 0:
            sync_needed = True
            sync_direction = "upgrade"
        elif cmp_sync < 0:
            sync_needed = True
            sync_direction = "downgrade"
        else:
            sync_direction = "match"

    return PackageRepoVersions(
        package_name=package_name,
        arch=normalized_arch,
        installed=tuple(installed_records),
        available=tuple(deduped_available),
        downgrades=downgrades,
        sync_target=sync_target,
        sync_needed=sync_needed,
        sync_direction=sync_direction,
    )


@dataclass(frozen=True, slots=True)
class DnfCompsPackage:
    """One package member returned inside ``org.rpm.dnf.v0.comps.Group.list``."""

    name: str
    package_type: int = 0
    condition: str = ""


@dataclass(frozen=True, slots=True)
class DnfCompsGroup:
    """One Fedora Comps group returned by ``org.rpm.dnf.v0.comps.Group.list``."""

    groupid: str
    name: str = ""
    installed: bool = False
    uservisible: bool = False
    repos: tuple[str, ...] = ()
    packages: tuple[DnfCompsPackage, ...] = ()


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
    packages: tuple[HistoryPackageChange, ...] = ()
    user_id: int | None = None
    releasever: str = ""
    comment: str = ""


@dataclass(frozen=True, slots=True)
class RecentPackageChange:
    """One package entry returned by ``org.rpm.dnf.v0.History.recent_changes``."""

    change_type: str  # "installed" | "removed" | "upgraded" | "downgraded"
    name: str
    arch: str = ""
    evr: str = ""
    original_evr: str = ""
    original_arch: str = ""
    summary: str = ""
    repo_id: str = ""
    reason: str = ""
    transaction_time: int = 0
    advisories: tuple[str, ...] = ()

    @property
    def name_arch(self) -> str:
        if self.name and self.arch:
            return f"{self.name}.{self.arch}"
        return self.name


@dataclass(frozen=True, slots=True)
class RecentChangesSummary:
    """Typed changeset returned by ``org.rpm.dnf.v0.History.recent_changes``."""

    installed: tuple[RecentPackageChange, ...] = ()
    removed: tuple[RecentPackageChange, ...] = ()
    upgraded: tuple[RecentPackageChange, ...] = ()
    downgraded: tuple[RecentPackageChange, ...] = ()

    @property
    def all_changes(self) -> tuple[RecentPackageChange, ...]:
        combined = [
            *self.upgraded,
            *self.installed,
            *self.downgraded,
            *self.removed,
        ]
        combined.sort(
            key=lambda item: (-item.transaction_time, item.name.casefold(), item.arch)
        )
        return tuple(combined)

    @property
    def total_count(self) -> int:
        return (
            len(self.installed)
            + len(self.removed)
            + len(self.upgraded)
            + len(self.downgraded)
        )

    @property
    def advisory_package_count(self) -> int:
        return sum(1 for item in self.all_changes if item.advisories)


@dataclass(frozen=True, slots=True)
class ReverseTransactionPreview:
    """Approximate inverse transaction request derived from a recorded diff."""

    request: TransactionRequest
    reversible_count: int
    skipped_items: tuple[str, ...] = ()


def _is_installonly_kernel_name(name: str) -> bool:
    normalized = name.strip().casefold()
    return normalized == "kernel" or normalized.startswith("kernel-")


def _collect_reverse_specs(
    packages: Sequence[HistoryPackageChange],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    installs: list[str] = []
    removals: list[str] = []
    upgrades: list[str] = []
    downgrades: list[str] = []
    skipped: list[str] = []
    assigned_specs: dict[str, str] = {}

    def _add(bucket_name: str, bucket: list[str], spec: str, pkg_label: str) -> None:
        cleaned = spec.strip()
        if not cleaned:
            return
        existing_bucket = assigned_specs.get(cleaned)
        if existing_bucket is not None:
            if existing_bucket != bucket_name:
                skipped.append(pkg_label)
            return
        assigned_specs[cleaned] = bucket_name
        bucket.append(cleaned)

    for pkg in packages:
        name = pkg.name.strip()
        if not name:
            continue
        label = pkg.name_arch or name
        if name.casefold() == "gpg-pubkey":
            skipped.append(f"{label} ({pkg.action})" if pkg.action else label)
            continue
        action_cf = pkg.action.strip().casefold()
        if action_cf == "install":
            spec = (
                (pkg.nevra or pkg.name_arch or name)
                if _is_installonly_kernel_name(name) and pkg.evr
                else (pkg.name_arch or name)
            )
            _add("remove", removals, spec, f"{label} ({pkg.action})")
        elif action_cf in {"remove", "erase"}:
            spec = pkg.nevra or pkg.name_arch or name
            _add("install", installs, spec, f"{label} ({pkg.action})")
        elif action_cf in {"upgrade", "update"}:
            spec = pkg.original_nevra if pkg.original_evr else (pkg.name_arch or name)
            _add("downgrade", downgrades, spec, f"{label} ({pkg.action})")
        elif action_cf == "downgrade":
            spec = pkg.original_nevra if pkg.original_evr else (pkg.name_arch or name)
            _add("upgrade", upgrades, spec, f"{label} ({pkg.action})")
        else:
            skipped.append(f"{label} ({pkg.action})" if pkg.action else label)

    return (
        tuple(installs),
        tuple(removals),
        tuple(upgrades),
        tuple(downgrades),
        tuple(skipped),
    )


def can_build_reverse_transaction(
    packages: Sequence[HistoryPackageChange],
) -> bool:
    """Return True when ``packages`` has at least one reversible package delta."""
    installs, removals, upgrades, downgrades, _skipped = _collect_reverse_specs(
        packages
    )
    return bool(installs or removals or upgrades or downgrades)


def build_reverse_transaction_preview(
    packages: Sequence[HistoryPackageChange],
) -> ReverseTransactionPreview:
    """Construct an approximate inverse ``TransactionRequest`` from history deltas."""
    installs, removals, upgrades, downgrades, skipped = _collect_reverse_specs(packages)
    total = len(installs) + len(removals) + len(upgrades) + len(downgrades)
    if total == 0:
        raise Dnf5DaemonValidationError(
            tr("No reversible package actions"),
            tr(
                "This transaction does not contain any reversible install, remove, upgrade, or downgrade package changes."
            ),
        )
    request = TransactionRequest.from_values(
        installs=installs,
        removals=removals,
        upgrades=upgrades,
        downgrades=downgrades,
    )
    return ReverseTransactionPreview(
        request=request,
        reversible_count=total,
        skipped_items=skipped,
    )


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
    def is_security(self) -> bool:
        """True when this advisory represents a security update."""
        return self.advisory_type.strip().casefold() in {"", "security"}

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


def _format_evr_string(epoch: str, version: str, release: str) -> str:
    evr = f"{version}-{release}" if version and release else version or release
    norm_epoch = _normalise_rpm_epoch(epoch)
    return f"{norm_epoch}:{evr}" if evr and norm_epoch != "0" else evr


def _record_full_nevra(record: Any) -> str:
    existing = getattr(record, "full_nevra", "")
    if isinstance(existing, str) and existing:
        return existing
    name = str(getattr(record, "name", "") or "").strip()
    version = str(getattr(record, "version", "") or "").strip()
    release = str(getattr(record, "release", "") or "").strip()
    arch = str(getattr(record, "arch", "") or "").strip()
    if not all((name, version, release, arch)):
        return str(
            getattr(record, "name_arch", "")
            or (f"{name}.{arch}" if name and arch else name)
        )
    epoch = _normalise_rpm_epoch(getattr(record, "epoch", "0"))
    evr = f"{epoch}:{version}" if epoch != "0" else version
    return f"{name}-{evr}-{release}.{arch}"


def build_available_updates_map(
    updates: Sequence[Any],
    advisories: Sequence[SecurityAdvisory] = (),
) -> dict[str, AvailableUpdateInfo]:
    """Cross-reference upgrade candidates with security advisories."""
    security_advisories = [
        adv for adv in advisories if adv.is_security and adv.affected_packages
    ]
    exact_nevra_map: dict[str, list[SecurityAdvisory]] = {}
    name_arch_map: dict[
        tuple[str, str], list[tuple[SecurityAdvisory, tuple[str, str, str]]]
    ] = {}
    name_only_map: dict[str, list[tuple[SecurityAdvisory, tuple[str, str, str]]]] = {}

    for adv in security_advisories:
        for nevra in adv.affected_packages:
            cleaned_nevra = nevra.strip()
            if not cleaned_nevra:
                continue
            exact_nevra_map.setdefault(cleaned_nevra, []).append(adv)
            parsed = split_rpm_nevra(cleaned_nevra)
            if parsed is not None:
                pkg_name, pkg_epoch, pkg_ver, pkg_rel, pkg_arch = parsed
                evr = (pkg_epoch, pkg_ver, pkg_rel)
                name_arch_map.setdefault((pkg_name, pkg_arch), []).append((adv, evr))
                name_only_map.setdefault(pkg_name, []).append((adv, evr))

    updates_map: dict[str, AvailableUpdateInfo] = {}
    for update in updates:
        name = str(getattr(update, "name", "") or "").strip()
        version = str(getattr(update, "version", "") or "").strip()
        release = str(getattr(update, "release", "") or "").strip()
        arch = str(getattr(update, "arch", "") or "").strip()
        epoch = str(getattr(update, "epoch", "0") or "0").strip()
        repository = str(getattr(update, "repository", "") or "") or "Updates"
        name_arch = str(
            getattr(update, "name_arch", "")
            or (f"{name}.{arch}" if name and arch else name)
        )
        full_nevra = _record_full_nevra(update)
        update_evr = getattr(update, "evr_tuple", None) or (
            _normalise_rpm_epoch(epoch),
            version,
            release,
        )

        matched: dict[str, SecurityAdvisory] = {}
        for adv in exact_nevra_map.get(full_nevra, ()):
            matched[adv.advisory_id] = adv

        candidates = (
            name_arch_map.get((name, arch), ()) if arch else name_only_map.get(name, ())
        )
        for adv, adv_evr in candidates:
            if compare_rpm_evr(update_evr, adv_evr) >= 0:
                matched[adv.advisory_id] = adv

        best = (
            min(matched.values(), key=lambda adv: (adv.severity_key, adv.advisory_id))
            if matched
            else None
        )
        updates_map[name_arch] = AvailableUpdateInfo(
            name=name,
            new_version=version,
            new_release=release,
            arch=arch,
            epoch=epoch,
            repository=repository,
            is_security=best is not None,
            advisory_id=best.advisory_id if best is not None else "",
            advisory_severity=best.severity.strip() if best is not None else "",
        )
    return updates_map


def advisory_affected_targets(
    advisories: Sequence[SecurityAdvisory],
) -> dict[str, tuple[SecurityAdvisory, tuple[str, str, str], str]]:
    """Map affected ``name.arch`` keys to ``(best_advisory, evr_tuple, display_ver)``."""
    targets: dict[str, tuple[SecurityAdvisory, tuple[str, str, str], str]] = {}
    for advisory in advisories:
        for nevra in advisory.affected_packages:
            parsed = split_rpm_nevra(nevra)
            if parsed is None:
                continue
            pkg_name, pkg_epoch, pkg_ver, pkg_rel, pkg_arch = parsed
            name_arch = f"{pkg_name}.{pkg_arch}" if pkg_arch else pkg_name
            evr = (pkg_epoch, pkg_ver, pkg_rel)
            display_ver = _format_evr_string(pkg_epoch, pkg_ver, pkg_rel)
            existing = targets.get(name_arch)
            if existing is None:
                targets[name_arch] = (advisory, evr, display_ver)
                continue
            prev_adv, prev_evr, prev_disp = existing
            cmp_evr = compare_rpm_evr(evr, prev_evr)
            best_adv = min(
                (prev_adv, advisory),
                key=lambda item: (item.severity_key, item.advisory_id),
            )
            best_evr = evr if cmp_evr > 0 else prev_evr
            best_disp = display_ver if cmp_evr > 0 else prev_disp
            targets[name_arch] = (best_adv, best_evr, best_disp)
    return targets


@dataclass(frozen=True, slots=True)
class RepositoryKeyPrompt:
    """One OpenPGP key the daemon wants to trust, before any decision is made."""

    session_path: str
    key_id: str
    fingerprint: str
    key_url: str
    user_ids: tuple[str, ...] = ()


VALID_OFFLINE_FINISH_ACTIONS: Final[frozenset[str]] = frozenset({"reboot", "poweroff"})


class OfflineTransactionPhase(StrEnum):
    """Normalized ``org.rpm.dnf.v0.Offline`` transaction lifecycle phases."""

    NONE = "none"
    READY = "ready"
    DOWNLOAD_COMPLETE = "download-complete"
    DOWNLOAD_INCOMPLETE = "download-incomplete"
    TRANSACTION_INCOMPLETE = "transaction-incomplete"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OfflineTransactionStatus:
    """Typed snapshot returned by ``org.rpm.dnf.v0.Offline.get_status``."""

    pending: bool
    status: str = ""
    phase: OfflineTransactionPhase = OfflineTransactionPhase.NONE
    cachedir: str = ""
    target_releasever: str = ""
    system_releasever: str = ""
    verb: str = ""
    cmd_line: str = ""
    poweroff_after: bool = False
    module_platform_id: str = ""

    @property
    def has_state(self) -> bool:
        """Whether the daemon holds a scheduled, pre-staged, or incomplete state."""
        return self.pending or self.phase is not OfflineTransactionPhase.NONE

    @property
    def is_scheduled_for_boot(self) -> bool:
        """Whether ``/system-update`` is currently armed for the next boot."""
        return self.pending or self.phase is OfflineTransactionPhase.READY

    @property
    def is_pre_staged(self) -> bool:
        """Whether packages are downloaded and tested but not yet scheduled."""
        return (
            not self.pending and self.phase is OfflineTransactionPhase.DOWNLOAD_COMPLETE
        )

    @property
    def is_incomplete(self) -> bool:
        """Whether a prior download or boot transaction ended incompletely."""
        return self.phase in {
            OfflineTransactionPhase.DOWNLOAD_INCOMPLETE,
            OfflineTransactionPhase.TRANSACTION_INCOMPLETE,
        }

    @property
    def finish_action(self) -> str:
        """Post-update finish action (``'poweroff'`` or ``'reboot'``)."""
        return "poweroff" if self.poweroff_after else "reboot"


def _normalise_offline_finish_action(action: str) -> str:
    if not isinstance(action, str):
        raise Dnf5DaemonValidationError(
            tr("Invalid finish action"),
            tr("Offline finish action must be text ('reboot' or 'poweroff')."),
        )
    normalized = action.strip().casefold()
    if normalized not in VALID_OFFLINE_FINISH_ACTIONS:
        raise Dnf5DaemonValidationError(
            tr("Invalid finish action"),
            tr(
                "Unsupported offline finish action: {action!r}. Expected 'reboot' or 'poweroff'."
            ).format(action=action),
        )
    return normalized
