"""Bounded ``org.freedesktop.login1`` reboot capability and trigger helper.

Dendro allows a user who just staged an offline boot update to restart right
from the application on traditional mutable Fedora hosts.

Security and sandbox invariants:
- Never queried or invoked inside a Flatpak sandbox: Dendro's Flatpak
  manifest (``io.github.xyasharx.Dendro.yml``) intentionally limits system-bus
  access to ``--system-talk-name=org.rpm.dnf.v0`` alone.
- Never queried or invoked on Fedora Atomic / OSTree, read-only ``/usr``, or
  unbooted container environments.
- Delegates authorization and systemd inhibition checks directly to
  ``org.freedesktop.login1.Manager.CanReboot()`` and ``Reboot(True)``, never
  spawning privileged reboot commands or bypassing inhibitors.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from enum import StrEnum
from itertools import count
from typing import Any, Final, Protocol

from dendro.core.host_environment import (
    HostEnvironment,
    detect_host_environment,
    sanitize_display_text,
)
from dendro.core.i18n import tr
from dendro.core.process_utils import is_running_in_flatpak

LOGIND_SERVICE_NAME: Final[str] = "org.freedesktop.login1"
LOGIND_MANAGER_PATH: Final[str] = "/org/freedesktop/login1"
LOGIND_MANAGER_INTERFACE: Final[str] = "org.freedesktop.login1.Manager"

LOGIND_CAN_REBOOT_TIMEOUT_MS: Final[int] = 10_000
LOGIND_REBOOT_TIMEOUT_MS: Final[int] = 120_000

_CONNECTION_SEQ = count(1)


class RebootCapability(StrEnum):
    """Documented ``org.freedesktop.login1.Manager.CanReboot()`` states."""

    YES = "yes"
    CHALLENGE = "challenge"
    INHIBITED = "inhibited"
    INHIBITOR_BLOCKED = "inhibitor-blocked"
    CHALLENGE_INHIBITOR_BLOCKED = "challenge-inhibitor-blocked"
    NO = "no"
    NA = "na"
    UNAVAILABLE = "unavailable"

    @property
    def can_trigger_immediately(self) -> bool:
        """Whether ``Reboot(True)`` can run directly or after Polkit prompt."""
        return self in {RebootCapability.YES, RebootCapability.CHALLENGE}

    @property
    def is_inhibited(self) -> bool:
        """Whether an active systemd inhibitor currently blocks restart."""
        return self in {
            RebootCapability.INHIBITED,
            RebootCapability.INHIBITOR_BLOCKED,
            RebootCapability.CHALLENGE_INHIBITOR_BLOCKED,
        }

    @property
    def requires_authorization(self) -> bool:
        """Whether ``logind`` reports Polkit authentication will be required."""
        return self is RebootCapability.CHALLENGE


_CAN_REBOOT_MAP: Final[dict[str, RebootCapability]] = {
    cap.value: cap
    for cap in RebootCapability
    if cap is not RebootCapability.UNAVAILABLE
}


class SystemRebootError(RuntimeError):
    """Typed, user-presentable error from ``org.freedesktop.login1`` reboot control."""

    def __init__(self, title: str, detail: str, *, dbus_name: str = "") -> None:
        super().__init__(f"{title}: {detail}" if detail else title)
        self.title = title
        self.detail = detail
        self.dbus_name = dbus_name


class SystemRebootTransport(Protocol):
    """Minimal ``org.freedesktop.login1.Manager`` D-Bus transport seam for tests."""

    def call_method(
        self,
        method: str,
        signatures: tuple[str, ...],
        arguments: tuple[Any, ...],
        *,
        timeout_ms: int,
    ) -> tuple[Any, ...]:
        """Invoke one method on ``org.freedesktop.login1.Manager``."""


class QtLogindTransport:
    """One-shot private system-bus transport for ``org.freedesktop.login1.Manager``."""

    def call_method(
        self,
        method: str,
        signatures: tuple[str, ...],
        arguments: tuple[Any, ...],
        *,
        timeout_ms: int,
    ) -> tuple[Any, ...]:
        from PyQt6.QtDBus import QDBusConnection, QDBusMessage

        if len(signatures) != len(arguments):
            raise SystemRebootError(
                tr("System restart failed"),
                tr("Internal logind D-Bus argument mismatch."),
            )

        connection_name = f"dendro-logind-{os.getpid()}-{threading.get_ident()}-{next(_CONNECTION_SEQ)}"
        conn = QDBusConnection.connectToBus(
            QDBusConnection.BusType.SystemBus, connection_name
        )
        try:
            if not conn.isConnected():
                err = conn.lastError()
                raise SystemRebootError(
                    tr("System restart unavailable"),
                    sanitize_display_text(err.message(), max_length=256)
                    or tr("The system D-Bus connection is unavailable."),
                    dbus_name=err.name(),
                )
            msg = QDBusMessage.createMethodCall(
                LOGIND_SERVICE_NAME,
                LOGIND_MANAGER_PATH,
                LOGIND_MANAGER_INTERFACE,
                method,
            )
            if arguments:
                marshalled: list[Any] = []
                for signature, value in zip(signatures, arguments, strict=True):
                    if signature == "b":
                        marshalled.append(bool(value))
                    else:
                        marshalled.append(value)
                msg.setArguments(marshalled)
            reply = conn.call(msg, timeout=timeout_ms)
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                detail = sanitize_display_text(reply.errorMessage(), max_length=512)
                raise SystemRebootError(
                    tr("System restart failed"),
                    detail
                    or tr("systemd-logind rejected the {method} request.").format(
                        method=method
                    ),
                    dbus_name=reply.errorName(),
                )
            if reply.type() != QDBusMessage.MessageType.ReplyMessage:
                raise SystemRebootError(
                    tr("System restart failed"),
                    tr(
                        "systemd-logind returned an unexpected D-Bus reply type for {method}."
                    ).format(method=method),
                )
            return tuple(reply.arguments())
        finally:
            QDBusConnection.disconnectFromBus(connection_name)


class SystemRebootClient:
    """Safe capability checker and restart trigger backed by ``systemd-logind``."""

    def __init__(
        self,
        *,
        transport: SystemRebootTransport | None = None,
        environment_detector: Callable[[], HostEnvironment] = detect_host_environment,
        flatpak_detector: Callable[[], bool] = is_running_in_flatpak,
    ) -> None:
        self._transport = transport
        self._environment_detector = environment_detector
        self._flatpak_detector = flatpak_detector

    def _effective_environment(self) -> HostEnvironment:
        if self._flatpak_detector():
            return HostEnvironment.flatpak()
        environment = self._environment_detector()
        if environment.is_flatpak:
            return HostEnvironment.flatpak()
        return environment

    def _get_transport(self) -> SystemRebootTransport:
        if self._transport is None:
            self._transport = QtLogindTransport()
        return self._transport

    def _require_reboot_support(self) -> None:
        environment = self._effective_environment()
        if not environment.supports_host_reboot_control:
            if environment.is_flatpak:
                raise SystemRebootError(
                    tr("Restart from Flatpak is unavailable"),
                    tr(
                        "Dendro's Flatpak sandbox does not access systemd-logind. "
                        "Restart your computer from your desktop menu to apply the staged update."
                    ),
                )
            raise SystemRebootError(
                tr("System restart control is unavailable"),
                tr(
                    "Dendro only triggers system restarts on traditional mutable Fedora hosts."
                ),
            )

    def query_reboot_capability(self) -> RebootCapability:
        """Query ``CanReboot()`` when permitted by the runtime environment."""
        if not self._effective_environment().supports_host_reboot_control:
            return RebootCapability.UNAVAILABLE
        try:
            reply = self._get_transport().call_method(
                "CanReboot",
                (),
                (),
                timeout_ms=LOGIND_CAN_REBOOT_TIMEOUT_MS,
            )
        except SystemRebootError:
            return RebootCapability.UNAVAILABLE
        if len(reply) != 1 or not isinstance(reply[0], str):
            return RebootCapability.UNAVAILABLE
        normalized = sanitize_display_text(reply[0], max_length=64).strip().casefold()
        return _CAN_REBOOT_MAP.get(normalized, RebootCapability.UNAVAILABLE)

    def trigger_reboot(self, *, interactive: bool = True) -> None:
        """Check ``CanReboot()`` and invoke ``org.freedesktop.login1.Manager.Reboot``."""
        self._require_reboot_support()
        capability = self.query_reboot_capability()
        if capability.is_inhibited:
            raise SystemRebootError(
                tr("System restart is currently inhibited"),
                tr(
                    "Another application or active session holds a shutdown inhibitor lock. "
                    "Close blocking tasks or restart from your desktop session menu."
                ),
            )
        if not capability.can_trigger_immediately:
            raise SystemRebootError(
                tr("System restart unavailable"),
                tr(
                    "systemd-logind reports that restarting this system is not currently permitted ({state}). "
                    "Restart manually from your desktop menu to apply the staged update."
                ).format(state=capability.value),
            )
        self._get_transport().call_method(
            "Reboot",
            ("b",),
            (bool(interactive),),
            timeout_ms=LOGIND_REBOOT_TIMEOUT_MS,
        )


__all__ = (
    "LOGIND_CAN_REBOOT_TIMEOUT_MS",
    "LOGIND_MANAGER_INTERFACE",
    "LOGIND_MANAGER_PATH",
    "LOGIND_REBOOT_TIMEOUT_MS",
    "LOGIND_SERVICE_NAME",
    "QtLogindTransport",
    "RebootCapability",
    "SystemRebootClient",
    "SystemRebootError",
    "SystemRebootTransport",
)
