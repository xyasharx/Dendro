"""Regression coverage for portal and Qt theme synchronization."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt

import dendro.ui.theme_coordinator as theme_coordinator
from dendro.ui import settings_dialog
from dendro.ui.header import HeaderBar
from dendro.ui.main_window import MainWindow
from dendro.ui.settings_dialog import DendroSettingsDialog
from dendro.ui.styles import (
    portal_color_scheme_to_theme_key,
    portal_contrast_to_bool,
    resolve_theme_key,
)
from dendro.ui.theme_coordinator import ThemeState


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
        theme_coordinator,
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
        _theme_state=ThemeState(applying=False),
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
        _theme_state=ThemeState(applying=False),
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
        _theme_state=ThemeState(applying=False),
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
        theme_coordinator, "QGuiApplication", SimpleNamespace(instance=lambda: fake_app)
    )
    window = SimpleNamespace(_theme_state=ThemeState())

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
        theme_coordinator, "QGuiApplication", SimpleNamespace(instance=lambda: fake_app)
    )
    window = SimpleNamespace(
        _theme_state=ThemeState(native_override=Qt.ColorScheme.Dark)
    )

    MainWindow._sync_native_color_scheme(window, "auto", "dark")

    assert hints.calls == [Qt.ColorScheme.Unknown]
    assert window._theme_state.native_override is None


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
        theme_coordinator.QTimer,
        "singleShot",
        staticmethod(lambda _delay, callback: callbacks.append(callback)),
    )
    window = SimpleNamespace(
        current_theme="auto",
        _theme_state=ThemeState(applying=False),
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
    # Both signals arrived, the later (portal) value won, and the queue is
    # drained: one callback, one applied theme, no leftover state.
    assert window._theme_state.pending_key is None
    assert window._theme_state.queued is False


def test_portal_contrast_parser_and_resolution_cover_high_contrast_modes() -> None:
    assert portal_contrast_to_bool(_Variant(0)) is False
    assert portal_contrast_to_bool(_Variant(_Variant(1))) is True
    assert portal_contrast_to_bool("invalid") is None
    assert portal_contrast_to_bool(99) is None

    assert resolve_theme_key("auto", system_dark=True, high_contrast=False) == "dark"
    assert resolve_theme_key("auto", system_dark=True, high_contrast=True) == "dark_hc"
    assert (
        resolve_theme_key("auto", system_dark=False, high_contrast=True) == "light_hc"
    )
    assert resolve_theme_key("dark", system_dark=False, high_contrast=True) == "dark_hc"
    assert (
        resolve_theme_key("light", system_dark=True, high_contrast=True) == "light_hc"
    )


def test_portal_contrast_signal_updates_even_a_pinned_theme(monkeypatch) -> None:
    callbacks = []
    applied: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        theme_coordinator.QTimer,
        "singleShot",
        staticmethod(lambda _delay, callback: callbacks.append(callback)),
    )
    window = SimpleNamespace(
        current_theme="dark",
        _theme_state=ThemeState(choice="dark", applying=False),
        _apply_theme=lambda choice, *, resolved_key=None: applied.append(
            (choice, resolved_key)
        ),
    )
    window._queue_contrast_theme_update = lambda key=None: (
        MainWindow._queue_contrast_theme_update(window, key)
    )
    window._apply_queued_system_theme = lambda: MainWindow._apply_queued_system_theme(
        window
    )

    MainWindow._on_portal_setting_changed(
        window,
        "org.freedesktop.appearance",
        "contrast",
        _Variant(1),
    )

    assert len(callbacks) == 1
    callbacks[0]()
    assert applied == [("dark", "dark_hc")]
    assert window._theme_state.force_contrast is False


def test_qt_contrast_signal_queues_contrast_refresh() -> None:
    queued: list[str | None] = []
    window = SimpleNamespace(
        current_theme="light",
        _theme_state=ThemeState(choice="light", applying=False),
        _queue_contrast_theme_update=lambda key=None: queued.append(key),
    )

    MainWindow._on_system_contrast_changed(window, 1)
    assert queued == [None]


def test_init_theming_ignores_non_signal_contrast_attribute(monkeypatch) -> None:
    class _Signal:
        def __init__(self) -> None:
            self.slots: list[object] = []

        def connect(self, slot: object) -> None:
            self.slots.append(slot)

    def _not_a_signal(*_args):
        return None

    class _Accessibility:
        contrastPreferenceChanged = _not_a_signal

    class _Hints:
        colorSchemeChanged = _Signal()

        def accessibility(self):
            return _Accessibility()

    hints = _Hints()
    fake_app = SimpleNamespace(styleHints=lambda: hints)
    monkeypatch.setattr(
        theme_coordinator, "QGuiApplication", SimpleNamespace(instance=lambda: fake_app)
    )
    window = SimpleNamespace(
        current_theme="auto",
        _apply_theme=lambda *_args, **_kwargs: None,
        _on_system_color_scheme_changed=lambda *_args: None,
        _on_system_contrast_changed=lambda *_args: None,
        _subscribe_to_portal_color_scheme_changes=lambda: None,
    )

    MainWindow._init_theming(window)
    assert len(hints.colorSchemeChanged.slots) == 1


def test_portal_and_qt_reduced_motion_toggle_tree_view_animation() -> None:
    animated_calls: list[bool] = []
    window = SimpleNamespace(
        current_theme="auto",
        _theme_state=ThemeState(),
        tree_view=SimpleNamespace(setAnimated=animated_calls.append),
    )
    window._sync_motion_preference = lambda **kwargs: (
        MainWindow._sync_motion_preference(window, **kwargs)
    )

    MainWindow._on_portal_setting_changed(
        window,
        "org.freedesktop.appearance",
        "reduced-motion",
        _Variant(1),
    )
    MainWindow._on_portal_setting_changed(
        window,
        "org.freedesktop.appearance",
        "reduced-motion",
        _Variant(0),
    )
    assert animated_calls == [False, True]

    motion_enum = getattr(Qt, "MotionPreference", None)
    if motion_enum is not None:
        MainWindow._on_system_motion_changed(window, motion_enum.ReducedMotion)
        MainWindow._on_system_motion_changed(window, motion_enum.NoPreference)
        assert animated_calls == [False, True, False, True]


def test_async_portal_startup_uses_pending_call_watcher_without_blocking(
    qapp, monkeypatch
) -> None:
    """_start_async_portal_sync queries portal keys via asyncCall + watcher."""
    from PyQt6.QtDBus import QDBusMessage, QDBusPendingCall

    sent_methods: list[tuple[str, list[object]]] = []
    queued_themes: list[str | None] = []
    queued_contrasts: list[str | None] = []
    synced_motions: list[bool] = []

    class _AsyncBus:
        def isConnected(self) -> bool:
            return True

        def asyncCall(self, message, timeout: int = -1):
            assert timeout > 0
            method = message.member()
            args = list(message.arguments())
            sent_methods.append((method, args))
            key = args[1]
            # Simulate ReadOne error on color-scheme so Read fallback is exercised.
            if key == "color-scheme" and method == "ReadOne":
                err = QDBusMessage.createError(
                    "org.freedesktop.DBus.Error.UnknownMethod", "UnknownMethod"
                )
                return QDBusPendingCall.fromCompletedCall(err)
            val = 1  # dark / high-contrast / reduced-motion
            reply = message.createReply([val])
            return QDBusPendingCall.fromCompletedCall(reply)

    monkeypatch.setattr(theme_coordinator, "HAS_QTDBUS", True)
    monkeypatch.setattr(
        theme_coordinator,
        "QDBusConnection",
        SimpleNamespace(sessionBus=lambda: _AsyncBus()),
    )
    fake_app = SimpleNamespace(
        styleHints=lambda: SimpleNamespace(colorScheme=lambda: Qt.ColorScheme.Unknown)
    )
    monkeypatch.setattr(
        theme_coordinator,
        "QGuiApplication",
        SimpleNamespace(instance=lambda: fake_app),
    )
    monkeypatch.setattr(
        theme_coordinator,
        "is_system_high_contrast",
        lambda **_kwargs: True,
    )

    window = SimpleNamespace(
        current_theme="auto",
        _theme_state=ThemeState(applied_key="light"),
        _portal_watchers=set(),
        _queue_system_theme_update=queued_themes.append,
        _queue_contrast_theme_update=queued_contrasts.append,
        _sync_motion_preference=lambda *, reduced_motion=None, **_kwargs: (
            synced_motions.append(bool(reduced_motion))
        ),
    )
    window._query_portal_setting_async = lambda key, method="ReadOne": (
        MainWindow._query_portal_setting_async(window, key, method)
    )
    window._on_async_portal_reply = lambda watcher, key, method: (
        MainWindow._on_async_portal_reply(window, watcher, key, method)
    )
    window._apply_async_portal_value = lambda key, raw_value: (
        MainWindow._apply_async_portal_value(window, key, raw_value)
    )

    MainWindow._start_async_portal_sync(window)
    for _ in range(6):
        qapp.processEvents()

    assert ("ReadOne", ["org.freedesktop.appearance", "color-scheme"]) in sent_methods
    assert ("Read", ["org.freedesktop.appearance", "color-scheme"]) in sent_methods
    assert ("ReadOne", ["org.freedesktop.appearance", "contrast"]) in sent_methods
    assert ("ReadOne", ["org.freedesktop.appearance", "reduced-motion"]) in sent_methods
    assert queued_themes == ["dark_hc"]
    assert len(queued_contrasts) == 1
    assert isinstance(queued_contrasts[0], str) and queued_contrasts[0].endswith("_hc")
    assert synced_motions == [True]
    assert window._portal_watchers == set()


def test_async_portal_failure_in_container_falls_back_to_gsettings(
    qapp, monkeypatch, tmp_path
) -> None:
    """When portal.Settings is unavailable (e.g. XFCE container), gsettings runs off-thread."""
    from PyQt6.QtDBus import QDBusMessage, QDBusPendingCall

    orig_isfile = os.path.isfile
    sent_methods: list[tuple[str, list[object]]] = []
    queued_themes: list[str | None] = []
    started_workers: list[object] = []

    class _NoPortalBus:
        def isConnected(self) -> bool:
            return True

        def asyncCall(self, message, timeout: int = -1):
            assert timeout > 0
            method = message.member()
            args = list(message.arguments())
            sent_methods.append((method, args))
            err = QDBusMessage.createError(
                "org.freedesktop.DBus.Error.UnknownMethod",
                'No such interface "org.freedesktop.portal.Settings"',
            )
            return QDBusPendingCall.fromCompletedCall(err)

    monkeypatch.delenv("GTK_THEME", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/container-bus")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        theme_coordinator.styles.os.path,
        "isfile",
        lambda path, _orig=orig_isfile: (
            False
            if os.fspath(path).startswith(("/etc/gtk-3.0/", "/etc/xdg/xfce4/"))
            else _orig(path)
        ),
    )
    theme_coordinator._set_portal_color_scheme_cache(None)
    theme_coordinator._set_gsettings_dark_mode_cache(None)

    monkeypatch.setattr(theme_coordinator, "HAS_QTDBUS", True)
    monkeypatch.setattr(
        theme_coordinator,
        "QDBusConnection",
        SimpleNamespace(sessionBus=lambda: _NoPortalBus()),
    )
    fake_app = SimpleNamespace(
        styleHints=lambda: SimpleNamespace(colorScheme=lambda: Qt.ColorScheme.Unknown)
    )
    monkeypatch.setattr(
        theme_coordinator,
        "QGuiApplication",
        SimpleNamespace(instance=lambda: fake_app),
    )
    monkeypatch.setattr(
        theme_coordinator.styles,
        "QGuiApplication",
        SimpleNamespace(instance=lambda: fake_app),
    )
    monkeypatch.setattr(
        theme_coordinator.styles, "is_running_in_flatpak", lambda: False
    )
    monkeypatch.setattr(
        theme_coordinator.styles.shutil,
        "which",
        lambda name: "/usr/bin/gsettings" if name == "gsettings" else None,
    )
    monkeypatch.setattr(
        theme_coordinator,
        "_query_gsettings_dark_mode",
        lambda: True,
    )
    monkeypatch.setattr(
        theme_coordinator,
        "is_system_high_contrast",
        lambda **_kwargs: False,
    )

    class _SyncPool:
        def start(self, worker) -> None:
            started_workers.append(worker)
            worker.run()

    window = SimpleNamespace(
        current_theme="auto",
        _theme_state=ThemeState(applied_key="light"),
        _portal_watchers=set(),
        _gsettings_theme_worker=None,
        _gsettings_request_serial=0,
        thread_pool=_SyncPool(),
        _queue_system_theme_update=queued_themes.append,
    )
    window._query_portal_setting_async = lambda key, method="ReadOne": (
        MainWindow._query_portal_setting_async(window, key, method)
    )
    window._on_async_portal_reply = lambda watcher, key, method: (
        MainWindow._on_async_portal_reply(window, watcher, key, method)
    )
    window._apply_async_portal_value = lambda key, raw_value: (
        MainWindow._apply_async_portal_value(window, key, raw_value)
    )
    window._trigger_gsettings_fallback_if_available = lambda: (
        MainWindow._trigger_gsettings_fallback_if_available(window)
    )
    window._maybe_start_async_gsettings_fallback = lambda: (
        MainWindow._maybe_start_async_gsettings_fallback(window)
    )
    window._on_async_gsettings_dark_mode_result = lambda serial, is_dark: (
        MainWindow._on_async_gsettings_dark_mode_result(window, serial, is_dark)
    )

    try:
        MainWindow._start_async_portal_sync(window)
        for _ in range(6):
            qapp.processEvents()

        assert (
            "ReadOne",
            ["org.freedesktop.appearance", "color-scheme"],
        ) in sent_methods
        assert ("Read", ["org.freedesktop.appearance", "color-scheme"]) in sent_methods
        assert len(started_workers) == 1
        assert queued_themes == ["dark"]
        assert (
            theme_coordinator.styles.is_system_dark_mode(allow_blocking=False) is True
        )
    finally:
        theme_coordinator._set_gsettings_dark_mode_cache(None)
        theme_coordinator._set_portal_color_scheme_cache(None)


def test_async_portal_zero_no_preference_and_disconnected_bus_run_gsettings_fallback(
    qapp, monkeypatch, tmp_path
) -> None:
    """Both portal '0' (no preference) and a disconnected D-Bus trigger gsettings."""
    from PyQt6.QtDBus import QDBusPendingCall

    orig_isfile = os.path.isfile
    monkeypatch.delenv("GTK_THEME", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/xfce-bus")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        theme_coordinator.styles.os.path,
        "isfile",
        lambda path, _orig=orig_isfile: (
            False
            if os.fspath(path).startswith(("/etc/gtk-3.0/", "/etc/xdg/xfce4/"))
            else _orig(path)
        ),
    )
    fake_app = SimpleNamespace(
        styleHints=lambda: SimpleNamespace(colorScheme=lambda: Qt.ColorScheme.Unknown)
    )
    monkeypatch.setattr(
        theme_coordinator,
        "QGuiApplication",
        SimpleNamespace(instance=lambda: fake_app),
    )
    monkeypatch.setattr(
        theme_coordinator.styles,
        "QGuiApplication",
        SimpleNamespace(instance=lambda: fake_app),
    )
    monkeypatch.setattr(
        theme_coordinator.styles, "is_running_in_flatpak", lambda: False
    )
    monkeypatch.setattr(
        theme_coordinator.styles.shutil,
        "which",
        lambda name: "/usr/bin/gsettings" if name == "gsettings" else None,
    )
    monkeypatch.setattr(theme_coordinator, "_query_gsettings_dark_mode", lambda: True)
    monkeypatch.setattr(
        theme_coordinator, "is_system_high_contrast", lambda **_kwargs: False
    )

    class _SyncPool:
        def start(self, worker) -> None:
            worker.run()

    def _make_window():
        queued: list[str | None] = []
        win = SimpleNamespace(
            current_theme="auto",
            _theme_state=ThemeState(applied_key="light"),
            _portal_watchers=set(),
            _gsettings_theme_worker=None,
            _gsettings_request_serial=0,
            thread_pool=_SyncPool(),
            _queue_system_theme_update=queued.append,
        )
        win._query_portal_setting_async = lambda key, method="ReadOne": (
            MainWindow._query_portal_setting_async(win, key, method)
        )
        win._on_async_portal_reply = lambda watcher, key, method: (
            MainWindow._on_async_portal_reply(win, watcher, key, method)
        )
        win._apply_async_portal_value = lambda key, raw_value: (
            MainWindow._apply_async_portal_value(win, key, raw_value)
        )
        win._trigger_gsettings_fallback_if_available = lambda: (
            MainWindow._trigger_gsettings_fallback_if_available(win)
        )
        win._maybe_start_async_gsettings_fallback = lambda: (
            MainWindow._maybe_start_async_gsettings_fallback(win)
        )
        win._on_async_gsettings_dark_mode_result = lambda serial, is_dark: (
            MainWindow._on_async_gsettings_dark_mode_result(win, serial, is_dark)
        )
        return win, queued

    try:
        # 1. Portal responds 0 ("no preference").
        class _ZeroBus:
            def isConnected(self) -> bool:
                return True

            def asyncCall(self, message, timeout: int = -1):
                return QDBusPendingCall.fromCompletedCall(message.createReply([0]))

        theme_coordinator._set_portal_color_scheme_cache(None)
        theme_coordinator._set_gsettings_dark_mode_cache(None)
        monkeypatch.setattr(theme_coordinator, "HAS_QTDBUS", True)
        monkeypatch.setattr(
            theme_coordinator,
            "QDBusConnection",
            SimpleNamespace(sessionBus=lambda: _ZeroBus()),
        )
        w1, q1 = _make_window()
        MainWindow._start_async_portal_sync(w1)
        for _ in range(6):
            qapp.processEvents()
        assert q1 == ["dark"]

        # 2. D-Bus unavailable entirely.
        theme_coordinator._set_portal_color_scheme_cache(None)
        theme_coordinator._set_gsettings_dark_mode_cache(None)
        monkeypatch.setattr(theme_coordinator, "HAS_QTDBUS", False)
        w2, q2 = _make_window()
        MainWindow._start_async_portal_sync(w2)
        assert q2 == ["dark"]

        # 3. Stale serial and XFCE xsettings.xml dark preference are respected.
        xsettings = (
            tmp_path / "xfce4" / "xfconf" / "xfce-perchannel-xml" / "xsettings.xml"
        )
        xsettings.parent.mkdir(parents=True)
        xsettings.write_text(
            '<channel name="xsettings" version="1.0">'
            '<property name="Net" type="empty">'
            '<property name="ThemeName" type="string" value="Adwaita-dark"/>'
            "</property></channel>",
            encoding="utf-8",
        )
        w3, q3 = _make_window()
        MainWindow._on_async_gsettings_dark_mode_result(w3, 999, False)
        assert q3 == []
        MainWindow._maybe_start_async_gsettings_fallback(w3)
        assert w3._gsettings_theme_worker is None
        assert q3 == []
    finally:
        theme_coordinator._set_gsettings_dark_mode_cache(None)
        theme_coordinator._set_portal_color_scheme_cache(None)


def test_main_window_real_threadpool_gsettings_fallback_applies_dark_theme(
    qtbot, monkeypatch, tmp_path
) -> None:
    """End-to-end real QThreadPool + MainWindow @pyqtSlot delivery switches to dark."""
    from PyQt6.QtWidgets import QApplication

    from dendro.core.host_environment import HostEnvironment

    app = QApplication.instance()
    previous_stylesheet = app.styleSheet() if app is not None else ""
    previous_palette = app.palette() if app is not None else None

    orig_isfile = os.path.isfile
    monkeypatch.delenv("GTK_THEME", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/container-bus")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        theme_coordinator.styles.os.path,
        "isfile",
        lambda path, _orig=orig_isfile: (
            False
            if os.fspath(path).startswith(("/etc/gtk-3.0/", "/etc/xdg/xfce4/"))
            else _orig(path)
        ),
    )
    monkeypatch.setattr(theme_coordinator, "HAS_QTDBUS", False)
    monkeypatch.setattr(theme_coordinator.styles, "HAS_QTDBUS", False)
    monkeypatch.setattr(
        theme_coordinator.styles, "is_running_in_flatpak", lambda: False
    )
    monkeypatch.setattr(
        theme_coordinator.styles.shutil,
        "which",
        lambda name: "/usr/bin/gsettings" if name == "gsettings" else None,
    )
    monkeypatch.setattr(theme_coordinator, "_query_gsettings_dark_mode", lambda: True)
    monkeypatch.setattr(
        theme_coordinator, "has_non_gsettings_color_scheme", lambda: False
    )
    monkeypatch.setattr(
        theme_coordinator, "is_system_high_contrast", lambda **_kwargs: False
    )
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)

    theme_coordinator._set_portal_color_scheme_cache(None)
    theme_coordinator._set_gsettings_dark_mode_cache(None)

    window = MainWindow(host_environment=HostEnvironment.traditional())
    qtbot.addWidget(window)
    try:
        window._cancel_async_gsettings_fallback()
        theme_coordinator._set_gsettings_dark_mode_cache(None)
        window.current_theme = "auto"
        window._apply_theme("auto", resolved_key="light")
        assert window._theme_state.applied_key == "light"

        window._maybe_start_async_gsettings_fallback()
        qtbot.waitUntil(
            lambda: window._theme_state.applied_key == "dark",
            timeout=3000,
        )
        assert window._theme_state.applied_key == "dark"
    finally:
        window.close()
        window.thread_pool.waitForDone(2000)
        if app is not None:
            app.setStyleSheet(previous_stylesheet)
            if previous_palette is not None:
                app.setPalette(previous_palette)
        theme_coordinator._set_gsettings_dark_mode_cache(None)
        theme_coordinator._set_portal_color_scheme_cache(None)
