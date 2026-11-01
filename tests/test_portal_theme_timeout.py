"""Regression tests for the bounded GUI-thread desktop-portal colour-scheme query.

The fake bus mirrors PyQt6's ``QDBusConnection.call`` signature. Passing the
timeout as the second positional argument raises ``TypeError`` there, and the
production code catches broad errors on this path, so the test checks the
actual argument shape rather than only the returned theme.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtDBus import QDBus, QDBusMessage

from ui import styles

PORTAL_PATH = "/org/freedesktop/portal/desktop"
SETTINGS_INTERFACE = "org.freedesktop.portal.Settings"


class _Reply:
    def __init__(self, values=None, error_name: str = "") -> None:
        self._values = list(values or [])
        self._error_name = error_name

    def type(self):
        if self._error_name:
            return QDBusMessage.MessageType.ErrorMessage
        return QDBusMessage.MessageType.ReplyMessage

    def arguments(self):
        return list(self._values)

    def errorName(self) -> str:
        return self._error_name

    def errorMessage(self) -> str:
        return self._error_name


class _Bus:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls: list[tuple[str, str, str, int]] = []

    def isConnected(self) -> bool:
        return True

    def call(self, message, *args, timeout: int = -1):
        if args and not isinstance(args[0], QDBus.CallMode):
            raise TypeError("argument 2 has unexpected type 'int'")
        method = message.member()
        self.calls.append((message.interface(), method, message.path(), timeout))
        return self._responder(method)


@pytest.fixture
def isolated_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove environment and process fallbacks so only the portal answers."""
    monkeypatch.delenv("GTK_THEME", raising=False)
    monkeypatch.setattr(
        styles.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no gsettings")),
    )
    monkeypatch.setattr(styles.os.path, "isfile", lambda _path: False)


def _install_bus(monkeypatch: pytest.MonkeyPatch, bus: _Bus) -> None:
    monkeypatch.setattr(styles, "HAS_QTDBUS", True)
    monkeypatch.setattr(
        styles,
        "QDBusConnection",
        SimpleNamespace(sessionBus=lambda: bus),
    )


def test_portal_timeout_is_short_enough_for_a_gui_thread_query() -> None:
    assert 0 < styles.PORTAL_SETTINGS_TIMEOUT_MS <= 2000


def test_dark_answer_from_read_one_is_used_without_a_second_query(
    monkeypatch: pytest.MonkeyPatch, isolated_fallbacks: None
) -> None:
    bus = _Bus(lambda method: _Reply(values=[1]))
    _install_bus(monkeypatch, bus)

    assert styles.is_system_dark_mode() is True
    assert bus.calls == [
        (SETTINGS_INTERFACE, "ReadOne", PORTAL_PATH, styles.PORTAL_SETTINGS_TIMEOUT_MS)
    ]


def test_light_answer_falls_back_to_read_after_a_read_one_error(
    monkeypatch: pytest.MonkeyPatch, isolated_fallbacks: None
) -> None:
    def responder(method: str) -> _Reply:
        if method == "ReadOne":
            return _Reply(error_name="org.freedesktop.DBus.Error.UnknownMethod")
        return _Reply(values=[2])

    bus = _Bus(responder)
    _install_bus(monkeypatch, bus)

    assert styles.is_system_dark_mode() is False
    assert [call[1] for call in bus.calls] == ["ReadOne", "Read"]
    assert all(call[3] == styles.PORTAL_SETTINGS_TIMEOUT_MS for call in bus.calls)


def test_unanswered_portal_is_bounded_and_returns_the_deterministic_fallback(
    monkeypatch: pytest.MonkeyPatch, isolated_fallbacks: None
) -> None:
    """A portal that never replies must not block for Qt's default 25 s."""
    bus = _Bus(
        lambda _method: _Reply(
            error_name="org.freedesktop.DBus.Error.NoReply",
        )
    )
    _install_bus(monkeypatch, bus)

    assert styles.is_system_dark_mode() is False
    assert [call[1] for call in bus.calls] == ["ReadOne", "Read"]
    assert all(call[3] == styles.PORTAL_SETTINGS_TIMEOUT_MS for call in bus.calls)
