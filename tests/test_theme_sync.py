"""Regression coverage for portal and Qt theme synchronization."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt

import ui.main_window as main_window
from ui import settings_dialog
from ui.header import HeaderBar
from ui.main_window import MainWindow
from ui.settings_dialog import DendroSettingsDialog
from ui.styles import portal_color_scheme_to_theme_key


class _Variant:
    def __init__(self, value: object) -> None:
        self._value = value

    def variant(self) -> object:
        return self._value


class _Settings:
    """In-memory QSettings substitute that keeps UI tests out of user config."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = dict(values or {})

    def value(
        self, key: str, default: object = None, *, type: type | None = None
    ) -> object:
        value = self.values.get(key, default)
        return type(value) if type is not None and value is not None else value

    def setValue(self, key: str, value: object) -> None:
        self.values[key] = value


def test_portal_color_scheme_parser_handles_portal_variants() -> None:
    assert portal_color_scheme_to_theme_key(_Variant(1)) == "dark"
    assert portal_color_scheme_to_theme_key(_Variant(_Variant(2))) == "light"
    assert portal_color_scheme_to_theme_key(0) is None
    assert portal_color_scheme_to_theme_key("invalid") is None


def test_portal_dbus_receiver_is_a_typed_qt_slot() -> None:
    signature = b"_on_portal_settings_message(QDBusMessage)"
    assert MainWindow.staticMetaObject.indexOfSlot(signature) >= 0


def test_portal_subscription_uses_the_typed_message_receiver(monkeypatch) -> None:
    class _Bus:
        def __init__(self) -> None:
            self.subscription: tuple[object, ...] | None = None

        def isConnected(self) -> bool:
            return True

        def connect(self, *arguments: object) -> bool:
            self.subscription = arguments
            return True

    bus = _Bus()
    monkeypatch.setattr(
        main_window,
        "QDBusConnection",
        SimpleNamespace(sessionBus=lambda: bus),
    )

    def receiver(_message: object) -> None:
        return None

    window = SimpleNamespace(_on_portal_settings_message=receiver)

    MainWindow._subscribe_to_portal_color_scheme_changes(window)

    assert bus.subscription is not None
    assert bus.subscription[-1] is receiver


class _PortalMessage:
    def __init__(self, arguments: list[object]) -> None:
        self._arguments = arguments

    def path(self) -> str:
        return "/org/freedesktop/portal/desktop"

    def interface(self) -> str:
        return "org.freedesktop.portal.Settings"

    def member(self) -> str:
        return "SettingChanged"

    def arguments(self) -> list[object]:
        return self._arguments


def test_portal_dbus_message_forwards_its_new_value() -> None:
    received: list[tuple[str, str, object]] = []
    window = SimpleNamespace(
        _on_portal_setting_changed=lambda namespace, key, value: received.append(
            (namespace, key, value)
        )
    )

    MainWindow._on_portal_settings_message(
        window,
        _PortalMessage(["org.freedesktop.appearance", "color-scheme", 2]),
    )

    assert received == [("org.freedesktop.appearance", "color-scheme", 2)]


def test_portal_signal_uses_its_new_value_for_auto_mode() -> None:
    queued: list[str | None] = []
    window = SimpleNamespace(
        current_theme="auto",
        _applying_theme=False,
        _queue_system_theme_update=queued.append,
    )

    MainWindow._on_portal_setting_changed(
        window,
        "org.freedesktop.appearance",
        "color-scheme",
        _Variant(2),
    )

    assert queued == ["light"]


def test_portal_signal_leaves_pinned_theme_untouched() -> None:
    queued: list[str | None] = []
    window = SimpleNamespace(
        current_theme="dark",
        _applying_theme=False,
        _queue_system_theme_update=queued.append,
    )

    MainWindow._on_portal_setting_changed(
        window,
        "org.freedesktop.appearance",
        "color-scheme",
        _Variant(2),
    )

    assert queued == []


def test_qt_signal_queues_a_deferred_system_reresolution() -> None:
    queued: list[str | None] = []
    window = SimpleNamespace(
        current_theme="auto",
        _applying_theme=False,
        _queue_system_theme_update=lambda key=None: queued.append(key),
    )

    MainWindow._on_system_color_scheme_changed(window, Qt.ColorScheme.Dark)

    assert queued == [None]


def test_auto_theme_does_not_reset_an_already_automatic_native_scheme(
    monkeypatch,
) -> None:
    class _Hints:
        def __init__(self) -> None:
            self.calls: list[Qt.ColorScheme] = []

        def setColorScheme(self, scheme: Qt.ColorScheme) -> None:
            self.calls.append(scheme)

    hints = _Hints()
    fake_app = SimpleNamespace(styleHints=lambda: hints)
    monkeypatch.setattr(
        main_window, "QGuiApplication", SimpleNamespace(instance=lambda: fake_app)
    )
    window = SimpleNamespace(_native_color_scheme_override=None)

    MainWindow._sync_native_color_scheme(window, "auto", "dark")

    assert hints.calls == []


def test_auto_theme_clears_only_a_previous_dendro_override(monkeypatch) -> None:
    class _Hints:
        def __init__(self) -> None:
            self.calls: list[Qt.ColorScheme] = []

        def setColorScheme(self, scheme: Qt.ColorScheme) -> None:
            self.calls.append(scheme)

    hints = _Hints()
    fake_app = SimpleNamespace(styleHints=lambda: hints)
    monkeypatch.setattr(
        main_window, "QGuiApplication", SimpleNamespace(instance=lambda: fake_app)
    )
    window = SimpleNamespace(_native_color_scheme_override=Qt.ColorScheme.Dark)

    MainWindow._sync_native_color_scheme(window, "auto", "dark")

    assert hints.calls == [Qt.ColorScheme.Unknown]
    assert window._native_color_scheme_override is None


def test_header_has_no_duplicate_theme_override_control(qtbot) -> None:
    """Appearance remains an explicit three-state Preferences setting."""
    header = HeaderBar()
    qtbot.addWidget(header)

    assert not hasattr(header, "theme_toggle_btn")
    assert not hasattr(HeaderBar, "theme_toggle_clicked")
    assert not hasattr(MainWindow, "_on_theme_toggle_clicked")


def test_settings_theme_choice_updates_the_main_window_preference() -> None:
    applied: list[str] = []
    window = SimpleNamespace(
        current_theme="auto",
        _apply_theme=applied.append,
    )

    MainWindow._set_theme_choice(window, "light")
    MainWindow._set_theme_choice(window, "unsupported")

    assert window.current_theme == "light"
    assert applied == ["light"]


def test_theme_selector_persists_and_emits_the_selected_choice(
    qtbot, monkeypatch
) -> None:
    settings = _Settings({"theme": "auto"})
    monkeypatch.setattr(settings_dialog, "QSettings", lambda *_args: settings)
    dialog = DendroSettingsDialog()
    qtbot.addWidget(dialog)

    emitted: list[str] = []
    dialog.theme_changed.connect(emitted.append)
    light_index = dialog.theme_selector.findData("light")
    assert light_index >= 0

    dialog.theme_selector.setCurrentIndex(light_index)

    assert settings.values["theme"] == "light"
    assert emitted == ["light"]


def test_system_theme_events_are_coalesced_and_keep_the_latest_portal_value(
    monkeypatch,
) -> None:
    callbacks = []
    applied: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        main_window.QTimer,
        "singleShot",
        staticmethod(lambda _delay, callback: callbacks.append(callback)),
    )
    window = SimpleNamespace(
        current_theme="auto",
        _applying_theme=False,
        _pending_system_theme_key=None,
        _system_theme_update_queued=False,
        _apply_theme=lambda choice, *, resolved_key=None: applied.append(
            (choice, resolved_key)
        ),
    )
    window._apply_queued_system_theme = lambda: MainWindow._apply_queued_system_theme(
        window
    )

    MainWindow._queue_system_theme_update(window, "dark")
    MainWindow._queue_system_theme_update(window, "light")

    assert len(callbacks) == 1
    callbacks[0]()
    assert applied == [("auto", "light")]
    assert window._pending_system_theme_key is None
    assert window._system_theme_update_queued is False
