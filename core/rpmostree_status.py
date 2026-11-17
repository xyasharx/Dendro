"""Unprivileged, read-only ``org.projectatomic.rpmostree1`` deployment status.

On Fedora Atomic and OSTree-managed hosts (Silverblue, Kinoite, Sway Atomic,
Budgie Atomic, COSMIC Atomic, IoT, CoreOS, and OCI container-native images),
Dendro performs a single bounded ``org.freedesktop.DBus.Properties.Get`` call
on ``/org/projectatomic/rpmostree1/Sysroot`` to read ``Deployments``.

Security invariants:
- Strictly read-only ``Properties.Get``: never invokes a mutating method, never
  connects to ``/run/rpm-ostree-transaction.sock``, never triggers Polkit, and
  never spawns ``rpm-ostree`` or ``bootc`` CLI processes.
- Never called from inside a Flatpak sandbox (preserving Dendro's sole
  ``--system-talk-name=org.rpm.dnf.v0`` Flatpak system-bus permission).
- Strict schema and type validation (including exact ``bool`` checks) with
  control-character stripping and bounded collection sizes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import (
    QDBusArgument,
    QDBusConnection,
    QDBusMessage,
    QDBusObjectPath,
    QDBusSignature,
    QDBusVariant,
)

from core.host_environment import (
    HostEnvironment,
    detect_host_environment,
    sanitize_display_text,
)

RPMOSTREE_SERVICE_NAME: Final[str] = "org.projectatomic.rpmostree1"
RPMOSTREE_SYSROOT_PATH: Final[str] = "/org/projectatomic/rpmostree1/Sysroot"
RPMOSTREE_SYSROOT_INTERFACE: Final[str] = "org.projectatomic.rpmostree1.Sysroot"
DBUS_PROPERTIES_INTERFACE: Final[str] = "org.freedesktop.DBus.Properties"
DEPLOYMENTS_PROPERTY: Final[str] = "Deployments"

PROPERTY_READ_TIMEOUT_MS: Final[int] = 15_000
_MAX_DEPLOYMENTS: Final[int] = 64
_MAX_PACKAGE_SELECTORS: Final[int] = 16_384
_MAX_SELECTOR_LENGTH: Final[int] = 512
_MAX_UNWRAP_DEPTH: Final[int] = 12


class RpmOstreeOriginKind:
    """Installed-package provenance tags for OSTree-managed hosts."""

    NONE: Final[str] = ""
    BASE: Final[str] = "base"
    LAYERED: Final[str] = "layered"
    LOCAL: Final[str] = "local"
    OVERRIDE: Final[str] = "override"


class RpmOstreeStatusError(RuntimeError):
    """Typed, user-presentable failure while querying ``rpm-ostreed`` state."""

    def __init__(self, title: str, detail: str, *, dbus_name: str = "") -> None:
        super().__init__(f"{title}: {detail}" if detail else title)
        self.title = title
        self.detail = detail
        self.dbus_name = dbus_name


class RpmOstreePropertyTransport(Protocol):
    """Minimal read-only D-Bus property transport for testability."""

    def get_property(self, path: str, interface: str, property_name: str) -> Any:
        """Read and unwrap one ``org.freedesktop.DBus.Properties.Get`` value."""


def _unwrap_dbus_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively unwrap QtDBus containers with a hard recursion ceiling.

    Unused commit-metadata fields (such as ``base-commit-meta`` or
    ``signatures``) may contain arbitrarily nested variants; capping recursion
    to ``None`` bounds CPU/stack work without rejecting valid deployments.
    """
    if depth > _MAX_UNWRAP_DEPTH:
        return None
    if isinstance(value, QDBusVariant):
        return _unwrap_dbus_value(value.variant(), depth=depth + 1)
    if isinstance(value, (QDBusObjectPath, QDBusSignature)):
        return str(
            value.path() if isinstance(value, QDBusObjectPath) else value.signature()
        )
    if isinstance(value, QDBusArgument):
        # PyQt6 decodes standard array/map/struct signatures through value()
        # when available; treat undecodable opaque payloads conservatively.
        if hasattr(value, "asVariant"):
            return _unwrap_dbus_value(value.asVariant(), depth=depth + 1)
        return None
    if isinstance(value, Mapping):
        return {
            _unwrap_dbus_value(key, depth=depth + 1): _unwrap_dbus_value(
                item, depth=depth + 1
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_unwrap_dbus_value(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_unwrap_dbus_value(item, depth=depth + 1) for item in value)
    return value


class QtRpmOstreePropertyTransport:
    """Bounded read-only ``QtDBus`` property reader for ``rpm-ostreed``."""

    def __init__(
        self,
        connection: QDBusConnection | None = None,
        *,
        timeout_ms: int = PROPERTY_READ_TIMEOUT_MS,
    ) -> None:
        self._connection = connection or QDBusConnection.systemBus()
        self._timeout_ms = max(1_000, int(timeout_ms))
        if not self._connection.isConnected():
            error = self._connection.lastError()
            detail = (
                error.message()
                or "The system D-Bus is unavailable to inspect 'rpm-ostree' state."
            )
            raise RpmOstreeStatusError(
                "rpm-ostree status is unavailable",
                sanitize_display_text(detail, max_length=240),
                dbus_name=error.name(),
            )

    def get_property(self, path: str, interface: str, property_name: str) -> Any:
        message: QDBusMessage = QDBusMessage.createMethodCall(
            RPMOSTREE_SERVICE_NAME,
            path,
            DBUS_PROPERTIES_INTERFACE,
            "Get",
        )
        message.setArguments([interface, property_name])
        reply = self._connection.call(message, timeout=self._timeout_ms)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            error_name = reply.errorName()
            raw_detail = (
                reply.errorMessage()
                or f"'{interface}.{property_name}' did not return a value."
            )
            raise RpmOstreeStatusError(
                "rpm-ostree status is unavailable",
                sanitize_display_text(raw_detail, max_length=240),
                dbus_name=error_name,
            )
        if reply.type() != QDBusMessage.MessageType.ReplyMessage:
            raise RpmOstreeStatusError(
                "rpm-ostree status is unavailable",
                f"Unexpected D-Bus reply type while reading '{interface}.{property_name}'.",
            )
        arguments = reply.arguments()
        if len(arguments) != 1:
            raise RpmOstreeStatusError(
                "Invalid rpm-ostree reply",
                f"Expected one property variant for '{interface}.{property_name}', "
                f"received {len(arguments)}.",
            )
        return _unwrap_dbus_value(arguments[0])


def _clean_selector(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or len(value) > _MAX_SELECTOR_LENGTH:
        return ""
    if any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        return ""
    return value


def _extract_string_set(mapping: Mapping[str, Any], key: str) -> frozenset[str]:
    raw = mapping.get(key)
    if raw is None:
        return frozenset()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise RpmOstreeStatusError(
            "Invalid rpm-ostree reply",
            f"Deployment field '{key}' must be a string list.",
        )
    if len(raw) > _MAX_PACKAGE_SELECTORS:
        raise RpmOstreeStatusError(
            "Invalid rpm-ostree reply",
            f"Deployment field '{key}' exceeded {_MAX_PACKAGE_SELECTORS} entries.",
        )
    cleaned: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise RpmOstreeStatusError(
                "Invalid rpm-ostree reply",
                f"Deployment field '{key}' contained a non-string entry.",
            )
        selector = _clean_selector(entry)
        if selector:
            cleaned.add(selector)
    return frozenset(cleaned)


def _extract_bool(
    mapping: Mapping[str, Any], key: str, *, default: bool = False
) -> bool:
    raw = mapping.get(key, default)
    # In Python bool subclasses int; require an exact boolean wire value.
    if not isinstance(raw, bool):
        raise RpmOstreeStatusError(
            "Invalid rpm-ostree reply",
            f"Deployment field '{key}' must be a boolean.",
        )
    return raw


def _extract_text(
    mapping: Mapping[str, Any], key: str, *, max_length: int = 160
) -> str:
    raw = mapping.get(key, "")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise RpmOstreeStatusError(
            "Invalid rpm-ostree reply",
            f"Deployment field '{key}' must be text.",
        )
    return sanitize_display_text(raw, max_length=max_length)


def _format_rpm_tuple(entry: Sequence[Any]) -> tuple[str, ...]:
    """Convert an ``rpm-ostreed`` ``(name, epoch, version, release, arch)`` tuple."""
    if len(entry) != 5 or not all(isinstance(part, str) for part in entry):
        return ()
    name, epoch, version, release, arch = (_clean_selector(part) for part in entry)
    if not all((name, version, release, arch)):
        return ()
    normalized_epoch = epoch if epoch and epoch != "(none)" else "0"
    nevra = f"{name}-{version}-{release}.{arch}"
    full_nevra = f"{name}-{normalized_epoch}:{version}-{release}.{arch}"
    return (name, f"{name}.{arch}", nevra, full_nevra)


def _extract_override_selectors(mapping: Mapping[str, Any]) -> frozenset[str]:
    """Collect base-override package selectors across ``rpm-ostreed`` fields."""
    selectors = set(_extract_string_set(mapping, "requested-base-local-replacements"))

    # requested-base-remote-replacements has signature a(sas): [(source, [pkgs]), ...]
    remote_requested = mapping.get("requested-base-remote-replacements")
    if isinstance(remote_requested, Sequence) and not isinstance(
        remote_requested, (str, bytes)
    ):
        for group in remote_requested[:_MAX_PACKAGE_SELECTORS]:
            if (
                isinstance(group, Sequence)
                and not isinstance(group, (str, bytes))
                and len(group) == 2
                and isinstance(group[1], Sequence)
                and not isinstance(group[1], (str, bytes))
            ):
                for raw_pkg in group[1][:_MAX_PACKAGE_SELECTORS]:
                    selector = _clean_selector(raw_pkg)
                    if selector:
                        selectors.add(selector)
                    if len(selectors) >= _MAX_PACKAGE_SELECTORS:
                        return frozenset(selectors)

    # base-local-replacements and base-remote-replacements are a(vv) of
    # ((new_name, epoch, ver, rel, arch), (old_name, epoch, ver, rel, arch)).
    for resolved_key in ("base-local-replacements", "base-remote-replacements"):
        resolved = mapping.get(resolved_key)
        if not isinstance(resolved, Sequence) or isinstance(resolved, (str, bytes)):
            continue
        for pair in resolved[:_MAX_PACKAGE_SELECTORS]:
            if (
                isinstance(pair, Sequence)
                and not isinstance(pair, (str, bytes))
                and len(pair) >= 1
                and isinstance(pair[0], Sequence)
                and not isinstance(pair[0], (str, bytes))
            ):
                selectors.update(_format_rpm_tuple(pair[0]))
                if len(selectors) >= _MAX_PACKAGE_SELECTORS:
                    return frozenset(selectors)

    return frozenset(selectors)


def _extract_removed_base_selectors(mapping: Mapping[str, Any]) -> frozenset[str]:
    selectors = set(_extract_string_set(mapping, "requested-base-removals"))
    resolved = mapping.get("base-removals")
    if isinstance(resolved, Sequence) and not isinstance(resolved, (str, bytes)):
        for entry in resolved[:_MAX_PACKAGE_SELECTORS]:
            if isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)):
                selectors.update(_format_rpm_tuple(entry))
                if len(selectors) >= _MAX_PACKAGE_SELECTORS:
                    break
    return frozenset(selectors)


@dataclass(frozen=True, slots=True)
class RpmOstreeDeployment:
    """Validated read-only summary of a single OSTree / bootc deployment."""

    deployment_id: str
    osname: str = ""
    checksum: str = ""
    version: str = ""
    origin: str = ""
    container_image_reference: str = ""
    booted: bool = False
    staged: bool = False
    pinned: bool = False
    unlocked: str = "none"
    layered_packages: frozenset[str] = frozenset()
    requested_packages: frozenset[str] = frozenset()
    requested_local_packages: frozenset[str] = frozenset()
    overridden_packages: frozenset[str] = frozenset()
    removed_base_packages: frozenset[str] = frozenset()

    @property
    def is_container_image(self) -> bool:
        return bool(self.container_image_reference)

    @property
    def has_unlocked_overlay(self) -> bool:
        normalized = self.unlocked.strip().casefold()
        return bool(normalized) and normalized != "none"

    @property
    def display_version(self) -> str:
        if self.version:
            return self.version
        if self.checksum:
            return self.checksum[:12]
        return self.deployment_id[:24]


@dataclass(frozen=True, slots=True)
class RpmOstreeHostStatus:
    """Read-only snapshot of booted, pending, and rollback OSTree deployments."""

    booted: RpmOstreeDeployment
    pending: RpmOstreeDeployment | None = None
    rollback: RpmOstreeDeployment | None = None
    deployment_count: int = 1

    @property
    def has_pending_deployment(self) -> bool:
        return self.pending is not None

    def classify_installed_package(
        self,
        *,
        name: str,
        arch: str,
        nevra: str,
        full_nevra: str,
    ) -> str:
        """Return ``RpmOstreeOriginKind`` for an installed RPM in ``booted``."""
        candidates = {
            selector
            for selector in (
                full_nevra,
                nevra,
                f"{name}.{arch}" if name and arch else "",
                name,
            )
            if selector
        }
        if not candidates:
            return RpmOstreeOriginKind.BASE
        if candidates & self.booted.overridden_packages:
            return RpmOstreeOriginKind.OVERRIDE
        if candidates & self.booted.requested_local_packages:
            return RpmOstreeOriginKind.LOCAL
        if candidates & (self.booted.requested_packages | self.booted.layered_packages):
            return RpmOstreeOriginKind.LAYERED
        return RpmOstreeOriginKind.BASE

    def is_user_layered(
        self,
        *,
        name: str,
        arch: str,
        nevra: str,
        full_nevra: str,
    ) -> bool:
        """Return whether an installed RPM was layered or overridden by the user."""
        return self.classify_installed_package(
            name=name,
            arch=arch,
            nevra=nevra,
            full_nevra=full_nevra,
        ) in {
            RpmOstreeOriginKind.LAYERED,
            RpmOstreeOriginKind.LOCAL,
            RpmOstreeOriginKind.OVERRIDE,
        }

    def pending_reboot_notice(self) -> str:
        """Describe a staged or queued deployment awaiting reboot, if any."""
        if self.pending is None:
            return ""
        pending_label = self.pending.display_version
        booted_label = self.booted.display_version
        kind = "staged" if self.pending.staged else "pending"
        if pending_label and booted_label and pending_label != booted_label:
            return (
                f"A {kind} deployment ({pending_label}) is awaiting reboot; "
                f"showing the currently booted deployment ({booted_label})."
            )
        return (
            f"A {kind} OS deployment is awaiting reboot; showing packages from "
            "the currently booted deployment."
        )

    def unlocked_overlay_warning(self) -> str:
        """Warn when the booted deployment has an active unlocked ``/usr`` mode."""
        if not self.booted.has_unlocked_overlay:
            return ""
        mode = sanitize_display_text(self.booted.unlocked, max_length=40)
        return (
            f"Booted deployment has an unlocked '/usr' overlay ('{mode}'); "
            "DNF5 mutations remain disabled to prevent '/usr' and '/etc' drift."
        )

    def summary_line(self) -> str:
        """Compact deployment status summary suitable for a banner or status bar."""
        parts: list[str] = []
        if self.booted.display_version:
            parts.append(f"Booted deployment: {self.booted.display_version}.")
        pending_notice = self.pending_reboot_notice()
        if pending_notice:
            parts.append(pending_notice)
        overlay_notice = self.unlocked_overlay_warning()
        if overlay_notice:
            parts.append(overlay_notice)
        return " ".join(parts)


def _parse_deployment(raw: Any) -> RpmOstreeDeployment:
    if not isinstance(raw, Mapping):
        raise RpmOstreeStatusError(
            "Invalid rpm-ostree reply",
            "Each deployment entry must be a key-value dictionary.",
        )
    string_keyed: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise RpmOstreeStatusError(
                "Invalid rpm-ostree reply",
                "Deployment dictionary contained a non-string property key.",
            )
        string_keyed[key] = value

    deployment_id = _extract_text(string_keyed, "id", max_length=200)
    checksum = _extract_text(string_keyed, "checksum", max_length=128)
    if not deployment_id and not checksum:
        raise RpmOstreeStatusError(
            "Invalid rpm-ostree reply",
            "Deployment entry is missing both 'id' and 'checksum'.",
        )

    local_packages = _extract_string_set(
        string_keyed, "requested-local-packages"
    ) | _extract_string_set(string_keyed, "requested-local-fileoverride-packages")

    return RpmOstreeDeployment(
        deployment_id=deployment_id or checksum,
        osname=_extract_text(string_keyed, "osname", max_length=120),
        checksum=checksum,
        version=_extract_text(string_keyed, "version", max_length=120),
        origin=_extract_text(string_keyed, "origin", max_length=240),
        container_image_reference=_extract_text(
            string_keyed, "container-image-reference", max_length=240
        ),
        booted=_extract_bool(string_keyed, "booted", default=False),
        staged=_extract_bool(string_keyed, "staged", default=False),
        pinned=_extract_bool(string_keyed, "pinned", default=False),
        unlocked=_extract_text(string_keyed, "unlocked", max_length=40) or "none",
        layered_packages=_extract_string_set(string_keyed, "packages"),
        requested_packages=_extract_string_set(string_keyed, "requested-packages"),
        requested_local_packages=local_packages,
        overridden_packages=_extract_override_selectors(string_keyed),
        removed_base_packages=_extract_removed_base_selectors(string_keyed),
    )


def parse_deployments_variant(raw_deployments: Any) -> RpmOstreeHostStatus:
    """Validate an unwrapped ``Sysroot.Deployments`` (``aa{sv}``) payload."""
    if isinstance(raw_deployments, (str, bytes)) or not isinstance(
        raw_deployments, Sequence
    ):
        raise RpmOstreeStatusError(
            "Invalid rpm-ostree reply",
            "'Sysroot.Deployments' did not return a deployment list.",
        )
    if not raw_deployments:
        raise RpmOstreeStatusError(
            "rpm-ostree status is unavailable",
            "'Sysroot.Deployments' returned no active deployments.",
        )
    if len(raw_deployments) > _MAX_DEPLOYMENTS:
        raise RpmOstreeStatusError(
            "Invalid rpm-ostree reply",
            f"'Sysroot.Deployments' returned more than {_MAX_DEPLOYMENTS} entries.",
        )

    deployments = [_parse_deployment(item) for item in raw_deployments]
    booted = next((dep for dep in deployments if dep.booted), None)
    if booted is None:
        raise RpmOstreeStatusError(
            "rpm-ostree status is unavailable",
            "No deployment in 'Sysroot.Deployments' is marked as booted.",
        )

    booted_index = deployments.index(booted)
    pending = next(
        (
            dep
            for dep in deployments
            if dep.staged and dep.deployment_id != booted.deployment_id
        ),
        None,
    )
    if pending is None and booted_index > 0:
        # Deployments are ordered in boot priority: index 0 is the next-boot
        # default deployment when an update or package change awaits reboot.
        pending = deployments[0]

    rollback = next(
        (
            dep
            for dep in deployments[booted_index + 1 :]
            if not dep.staged and dep.deployment_id != booted.deployment_id
        ),
        None,
    )

    return RpmOstreeHostStatus(
        booted=booted,
        pending=pending,
        rollback=rollback,
        deployment_count=len(deployments),
    )


def query_rpmostree_status(
    transport: RpmOstreePropertyTransport | None = None,
    *,
    environment_detector: Callable[[], HostEnvironment] = detect_host_environment,
) -> RpmOstreeHostStatus:
    """Read and validate host deployment state from ``org.projectatomic.rpmostree1``."""
    environment = environment_detector()
    if not environment.supports_rpmostree_readonly:
        raise RpmOstreeStatusError(
            "rpm-ostree status is unavailable",
            "Read-only 'rpm-ostree' inspection is enabled only on Fedora Atomic "
            "and OSTree hosts outside the Flatpak sandbox.",
        )
    active_transport = (
        transport if transport is not None else QtRpmOstreePropertyTransport()
    )
    raw_deployments = active_transport.get_property(
        RPMOSTREE_SYSROOT_PATH,
        RPMOSTREE_SYSROOT_INTERFACE,
        DEPLOYMENTS_PROPERTY,
    )
    return parse_deployments_variant(raw_deployments)


class RpmOstreeStatusSignals(QObject):
    """Terminal signals emitted by ``RpmOstreeStatusWorker``."""

    status_update = pyqtSignal(str)
    loaded = pyqtSignal(object)  # RpmOstreeHostStatus
    failed = pyqtSignal(str)


class RpmOstreeStatusWorker(QRunnable):
    """Background worker reading unprivileged ``rpm-ostreed`` deployment state."""

    def __init__(
        self,
        *,
        loader: Callable[[], RpmOstreeHostStatus] = query_rpmostree_status,
    ) -> None:
        super().__init__()
        self.signals = RpmOstreeStatusSignals()
        self._loader = loader
        self._is_cancelled = threading.Event()

    def cancel(self) -> None:
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self) -> None:
        if self._is_cancelled.is_set():
            return
        self.signals.status_update.emit(
            "Inspecting booted deployment metadata via 'org.projectatomic.rpmostree1'…"
        )
        try:
            status = self._loader()
        except RpmOstreeStatusError as exc:
            if not self._is_cancelled.is_set():
                self.signals.failed.emit(f"{exc.title}: {exc.detail}")
            return
        except Exception as exc:  # Defensive worker boundary.
            if not self._is_cancelled.is_set():
                self.signals.failed.emit(
                    f"Unexpected rpm-ostree query error: {type(exc).__name__}: {exc}"
                )
            return
        if not self._is_cancelled.is_set():
            self.signals.loaded.emit(status)


__all__ = [
    "DBUS_PROPERTIES_INTERFACE",
    "DEPLOYMENTS_PROPERTY",
    "PROPERTY_READ_TIMEOUT_MS",
    "RPMOSTREE_SERVICE_NAME",
    "RPMOSTREE_SYSROOT_INTERFACE",
    "RPMOSTREE_SYSROOT_PATH",
    "QtRpmOstreePropertyTransport",
    "RpmOstreeDeployment",
    "RpmOstreeHostStatus",
    "RpmOstreeOriginKind",
    "RpmOstreePropertyTransport",
    "RpmOstreeStatusError",
    "RpmOstreeStatusSignals",
    "RpmOstreeStatusWorker",
    "parse_deployments_variant",
    "query_rpmostree_status",
]
