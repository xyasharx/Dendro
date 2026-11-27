"""Pure-Python and QtCore tests for systemd-logind reboot control & offline status types."""

from __future__ import annotations

import sys
import types

import pytest

from dendro.core.dnf5daemon_types import (
    Dnf5DaemonValidationError,
    OfflineTransactionPhase,
    OfflineTransactionStatus,
    _normalise_offline_finish_action,
)
from dendro.core.host_environment import HostEnvironment, HostEnvironmentKind
from dendro.core.system_reboot import (
    LOGIND_MANAGER_INTERFACE,
    LOGIND_MANAGER_PATH,
    LOGIND_SERVICE_NAME,
    QtLogindTransport,
    RebootCapability,
    SystemRebootClient,
    SystemRebootError,
)


class FakeLogindTransport:
    """In-memory deterministic systemd-logind D-Bus transport."""

    def __init__(
        self,
        *,
        can_reboot_result: object = ("yes",),
        can_reboot_error: SystemRebootError | None = None,
        reboot_error: SystemRebootError | None = None,
    ) -> None:
        self.can_reboot_result = can_reboot_result
        self.can_reboot_error = can_reboot_error
        self.reboot_error = reboot_error
        self.calls: list[tuple[str, tuple[str, ...], tuple[object, ...], int]] = []

    def call_method(
        self,
        method: str,
        signatures: tuple[str, ...],
        arguments: tuple[object, ...],
        *,
        timeout_ms: int,
    ) -> tuple[object, ...]:
        self.calls.append((method, signatures, arguments, timeout_ms))
        if method == "CanReboot":
            if self.can_reboot_error is not None:
                raise self.can_reboot_error
            return self.can_reboot_result  # type: ignore[return-value]
        if method == "Reboot":
            if self.reboot_error is not None:
                raise self.reboot_error
            return ()
        raise AssertionError(f"Unexpected logind method call: {method}")


def _client_for(
    transport: FakeLogindTransport, env: HostEnvironment
) -> SystemRebootClient:
    return SystemRebootClient(
        transport=transport,
        environment_detector=lambda: env,
        flatpak_detector=lambda: env.is_flatpak,
    )


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (HostEnvironment.traditional(), True),
        (
            HostEnvironment(
                kind=HostEnvironmentKind.TRADITIONAL_FEDORA,
                in_container=True,
                container_booted_systemd=True,
            ),
            True,
        ),
        (HostEnvironment.flatpak(), False),
        (
            HostEnvironment(kind=HostEnvironmentKind.ATOMIC_OSTREE, ostree_booted=True),
            False,
        ),
        (
            HostEnvironment(
                kind=HostEnvironmentKind.READ_ONLY_USR, bootc_composefs=True
            ),
            False,
        ),
        (
            HostEnvironment(kind=HostEnvironmentKind.READ_ONLY_USR, usr_read_only=True),
            False,
        ),
        (
            HostEnvironment(
                kind=HostEnvironmentKind.UNBOOTED_CONTAINER, in_container=True
            ),
            False,
        ),
    ],
)
def test_supports_host_reboot_control_strictly_requires_traditional_fedora(
    env: HostEnvironment, expected: bool
) -> None:
    assert env.supports_host_reboot_control is expected


@pytest.mark.parametrize(
    ("raw_state", "expected", "can_trigger", "inhibited"),
    [
        ("yes", RebootCapability.YES, True, False),
        ("challenge", RebootCapability.CHALLENGE, True, False),
        ("no", RebootCapability.NO, False, False),
        ("na", RebootCapability.NA, False, False),
        ("inhibited", RebootCapability.INHIBITED, False, True),
        ("inhibitor-blocked", RebootCapability.INHIBITOR_BLOCKED, False, True),
        (
            "challenge-inhibitor-blocked",
            RebootCapability.CHALLENGE_INHIBITOR_BLOCKED,
            False,
            True,
        ),
        ("something-else", RebootCapability.UNAVAILABLE, False, False),
    ],
)
def test_query_reboot_capability_maps_logind_states(
    raw_state: str,
    expected: RebootCapability,
    can_trigger: bool,
    inhibited: bool,
) -> None:
    transport = FakeLogindTransport(can_reboot_result=(raw_state,))
    client = _client_for(transport, HostEnvironment.traditional())

    cap = client.query_reboot_capability()

    assert cap is expected
    assert cap.can_trigger_immediately is can_trigger
    assert cap.is_inhibited is inhibited
    assert transport.calls == [("CanReboot", (), (), 10_000)]


def test_query_reboot_capability_returns_unavailable_on_transport_error() -> None:
    transport = FakeLogindTransport(
        can_reboot_error=SystemRebootError("unavailable", "no bus")
    )
    client = _client_for(transport, HostEnvironment.traditional())
    assert client.query_reboot_capability() is RebootCapability.UNAVAILABLE


@pytest.mark.parametrize(
    "env",
    [
        HostEnvironment.flatpak(),
        HostEnvironment(kind=HostEnvironmentKind.ATOMIC_OSTREE, ostree_booted=True),
        HostEnvironment(kind=HostEnvironmentKind.READ_ONLY_USR, bootc_composefs=True),
        HostEnvironment(kind=HostEnvironmentKind.READ_ONLY_USR, usr_read_only=True),
        HostEnvironment(kind=HostEnvironmentKind.UNBOOTED_CONTAINER, in_container=True),
    ],
)
def test_reboot_client_never_touches_logind_outside_traditional_host(
    env: HostEnvironment,
) -> None:
    transport = FakeLogindTransport(can_reboot_result=("yes",))
    client = _client_for(transport, env)

    assert client.query_reboot_capability() is RebootCapability.UNAVAILABLE
    with pytest.raises(SystemRebootError) as exc_info:
        client.trigger_reboot()

    assert transport.calls == []
    if env.is_flatpak:
        assert "Flatpak" in exc_info.value.title
    else:
        assert "unavailable" in exc_info.value.title.lower()


def test_flatpak_detector_overrides_host_environment_detector() -> None:
    transport = FakeLogindTransport(can_reboot_result=("yes",))
    client = SystemRebootClient(
        transport=transport,
        environment_detector=HostEnvironment.traditional,
        flatpak_detector=lambda: True,
    )

    assert client.query_reboot_capability() is RebootCapability.UNAVAILABLE
    with pytest.raises(SystemRebootError) as exc_info:
        client.trigger_reboot()
    assert "Flatpak" in exc_info.value.title
    assert transport.calls == []


@pytest.mark.parametrize("state", ["yes", "challenge"])
def test_trigger_reboot_invokes_logind_reboot_with_interactive_true(state: str) -> None:
    transport = FakeLogindTransport(can_reboot_result=(state,))
    client = _client_for(transport, HostEnvironment.traditional())

    client.trigger_reboot()

    assert transport.calls == [
        ("CanReboot", (), (), 10_000),
        ("Reboot", ("b",), (True,), 120_000),
    ]


@pytest.mark.parametrize(
    "state", ["inhibited", "inhibitor-blocked", "challenge-inhibitor-blocked"]
)
def test_trigger_reboot_reports_inhibitor_locks_without_sending_reboot(
    state: str,
) -> None:
    transport = FakeLogindTransport(can_reboot_result=(state,))
    client = _client_for(transport, HostEnvironment.traditional())

    with pytest.raises(SystemRebootError) as exc_info:
        client.trigger_reboot()

    assert "inhibited" in exc_info.value.title.lower()
    assert transport.calls == [("CanReboot", (), (), 10_000)]


@pytest.mark.parametrize("state", ["no", "na"])
def test_trigger_reboot_reports_disallowed_session_without_sending_reboot(
    state: str,
) -> None:
    transport = FakeLogindTransport(can_reboot_result=(state,))
    client = _client_for(transport, HostEnvironment.traditional())

    with pytest.raises(SystemRebootError) as exc_info:
        client.trigger_reboot()

    assert state in exc_info.value.detail
    assert transport.calls == [("CanReboot", (), (), 10_000)]


def test_qt_logind_transport_uses_private_bus_and_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeMsgType:
        ReplyMessage = 1
        ErrorMessage = 2

    class _FakeMessage:
        MessageType = _FakeMsgType

        def __init__(
            self, args: list[object], *, msg_type: int = 1, err: str = ""
        ) -> None:
            self._args = args
            self._msg_type = msg_type
            self._err = err
            self.target: tuple[str, str, str] | None = None
            self.set_args: list[object] = []

        @classmethod
        def createMethodCall(cls, service: str, path: str, iface: str, member: str):
            msg = cls([])
            msg.target = (service, path, f"{iface}.{member}")
            return msg

        def setArguments(self, args: list[object]) -> None:
            self.set_args = list(args)

        def type(self) -> int:
            return self._msg_type

        def arguments(self) -> list[object]:
            return self._args

        def errorMessage(self) -> str:
            return self._err

        def errorName(self) -> str:
            return "org.freedesktop.DBus.Error.AccessDenied"

    class _FakeBusType:
        SystemBus = 1

    class _FakeBus:
        def __init__(self) -> None:
            self.calls: list[tuple[object, int]] = []
            self.disconnected: list[str] = []

        def isConnected(self) -> bool:
            return True

        def call(self, message, timeout: int = -1):
            self.calls.append((message, timeout))
            return _FakeMessage(["yes"], msg_type=_FakeMsgType.ReplyMessage)

    bus = _FakeBus()

    fake_qtdbus = types.ModuleType("PyQt6.QtDBus")

    class _Conn:
        BusType = _FakeBusType

        @staticmethod
        def connectToBus(_bus_type, _name: str):
            return bus

        @staticmethod
        def disconnectFromBus(name: str) -> None:
            bus.disconnected.append(name)

    fake_qtdbus.QDBusConnection = _Conn  # type: ignore[attr-defined]
    fake_qtdbus.QDBusMessage = _FakeMessage  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PyQt6.QtDBus", fake_qtdbus)

    transport = QtLogindTransport()
    result = transport.call_method("CanReboot", (), (), timeout_ms=10_000)

    assert result == ("yes",)
    assert len(bus.calls) == 1
    sent_msg, timeout = bus.calls[0]
    assert sent_msg.target == (
        LOGIND_SERVICE_NAME,
        LOGIND_MANAGER_PATH,
        f"{LOGIND_MANAGER_INTERFACE}.CanReboot",
    )
    assert timeout == 10_000
    assert len(bus.disconnected) == 1


def test_offline_status_properties_and_finish_action_normalization() -> None:
    idle = OfflineTransactionStatus(
        pending=False,
        status="",
        phase=OfflineTransactionPhase.NONE,
    )
    assert idle.has_state is False
    assert idle.is_scheduled_for_boot is False
    assert idle.is_pre_staged is False
    assert idle.finish_action == "reboot"

    scheduled = OfflineTransactionStatus(
        pending=True,
        status="ready",
        phase=OfflineTransactionPhase.READY,
        poweroff_after=True,
        target_releasever="45",
        system_releasever="44",
    )
    assert scheduled.has_state is True
    assert scheduled.is_scheduled_for_boot is True
    assert scheduled.is_pre_staged is False
    assert scheduled.finish_action == "poweroff"

    pre_staged = OfflineTransactionStatus(
        pending=False,
        status="download-complete",
        phase=OfflineTransactionPhase.DOWNLOAD_COMPLETE,
    )
    assert pre_staged.has_state is True
    assert pre_staged.is_pre_staged is True
    assert pre_staged.is_scheduled_for_boot is False

    incomplete = OfflineTransactionStatus(
        pending=False,
        status="transaction-incomplete",
        phase=OfflineTransactionPhase.TRANSACTION_INCOMPLETE,
    )
    assert incomplete.has_state is True
    assert incomplete.is_incomplete is True

    assert _normalise_offline_finish_action(" PowerOff ") == "poweroff"
    assert _normalise_offline_finish_action("reboot") == "reboot"
    with pytest.raises(Dnf5DaemonValidationError):
        _normalise_offline_finish_action("halt")
    with pytest.raises(Dnf5DaemonValidationError):
        _normalise_offline_finish_action(123)  # type: ignore[arg-type]
