"""Regression tests for the bounded GUI-thread desktop-portal colour-scheme query.

The fake bus mirrors PyQt6's ``QDBusConnection.call`` signature. Passing the
timeout as the second positional argument raises ``TypeError`` there, and the
production code catches broad errors on this path, so the test checks the
actual argument shape rather than only the returned theme.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from PyQt6.QtDBus import QDBus, QDBusMessage

from dendro.ui import styles

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


def test_in_process_ini_theme_detection_avoids_gsettings_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(styles, "HAS_QTDBUS", False)
    monkeypatch.setattr(
        styles, "QGuiApplication", SimpleNamespace(instance=lambda: None)
    )
    monkeypatch.delenv("GTK_THEME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        styles.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "In-process INI detection must not invoke gsettings subprocess"
        ),
    )

    gtk_ini = tmp_path / "gtk-3.0" / "settings.ini"
    gtk_ini.parent.mkdir(parents=True)
    gtk_ini.write_text("[Settings]\ngtk-application-prefer-dark-theme = 1\n")
    assert styles.is_system_dark_mode() is True

    gtk_ini.unlink()
    kde_ini = tmp_path / "kdeglobals"
    kde_ini.write_text("[General]\nColorScheme=BreezeDark\n")
    assert styles.is_system_dark_mode() is True


def test_gsettings_fallback_skips_non_gtk_desktops_and_flatpak(
    monkeypatch: pytest.MonkeyPatch, isolated_fallbacks: None, tmp_path
) -> None:
    monkeypatch.setattr(styles, "HAS_QTDBUS", False)
    monkeypatch.setattr(
        styles, "QGuiApplication", SimpleNamespace(instance=lambda: None)
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    called: list[object] = []
    monkeypatch.setattr(
        styles.subprocess,
        "run",
        lambda cmd, **_kwargs: (
            called.append(cmd) or SimpleNamespace(returncode=1, stdout="")
        ),
    )

    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr(styles, "is_running_in_flatpak", lambda: False)
    assert styles.is_system_dark_mode() is False
    assert called == []

    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setattr(styles, "is_running_in_flatpak", lambda: True)
    assert styles.is_system_dark_mode() is False
    assert called == []


def test_theme_palettes_and_stylesheet_builder_render_all_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from dendro.ui.stylesheet_builder import build_stylesheet
    from dendro.ui.theme_palettes import THEMES_CONFIG, build_delegate_palette

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for key in ("dark", "light", "dark_hc", "light_hc"):
        assert key in THEMES_CONFIG
        qss = build_stylesheet(THEMES_CONFIG[key])
        assert "QMainWindow" in qss
        assert THEMES_CONFIG[key]["bg_base"] in qss
        assert styles.get_theme_stylesheet(key) == qss
        palette = build_delegate_palette(key)
        assert "accent" in palette


def test_portal_contrast_query_uses_bounded_timeout_and_caches_named_bus(
    monkeypatch: pytest.MonkeyPatch, isolated_fallbacks: None
) -> None:
    monkeypatch.setattr(
        styles, "QGuiApplication", SimpleNamespace(instance=lambda: None)
    )
    styles._set_portal_contrast_cache(None)

    class _NamedBus(_Bus):
        def name(self) -> str:
            return ":1.test"

    def responder(method: str) -> _Reply:
        if method == "ReadOne":
            return _Reply(error_name="org.freedesktop.DBus.Error.UnknownMethod")
        return _Reply(values=[1])

    bus = _NamedBus(responder)
    _install_bus(monkeypatch, bus)
    try:
        assert styles.is_system_high_contrast() is True
        assert styles.is_system_high_contrast() is True
        assert [call[1] for call in bus.calls] == ["ReadOne", "Read"]
        assert all(call[3] == styles.PORTAL_SETTINGS_TIMEOUT_MS for call in bus.calls)

        styles._set_portal_contrast_cache(False)
        assert styles.is_system_high_contrast() is False
    finally:
        styles._set_portal_contrast_cache(None)


def test_style_hints_color_scheme_is_checked_before_dbus_and_gsettings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native Qt QStyleHints.colorScheme() resolves without blocking D-Bus/gsettings."""
    from PyQt6.QtCore import Qt

    monkeypatch.delenv("GTK_THEME", raising=False)
    styles._set_portal_color_scheme_cache(None)
    monkeypatch.setattr(
        styles,
        "QDBusConnection",
        SimpleNamespace(
            sessionBus=lambda: pytest.fail(
                "Portal D-Bus must not be queried when QStyleHints.colorScheme() is known"
            )
        ),
    )
    monkeypatch.setattr(
        styles.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "gsettings subprocess must not run when QStyleHints.colorScheme() is known"
        ),
    )

    for scheme, expected in (
        (Qt.ColorScheme.Dark, True),
        (Qt.ColorScheme.Light, False),
    ):
        fake_app = SimpleNamespace(
            styleHints=lambda s=scheme: SimpleNamespace(colorScheme=lambda: s)
        )
        monkeypatch.setattr(
            styles,
            "QGuiApplication",
            SimpleNamespace(instance=lambda app=fake_app: app),
        )
        assert styles.is_system_dark_mode() is expected


def test_non_blocking_startup_skips_dbus_and_gsettings_fallbacks(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """allow_blocking=False never invokes blocking D-Bus call() or subprocess.run."""
    from PyQt6.QtCore import Qt

    monkeypatch.delenv("GTK_THEME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    styles._set_portal_color_scheme_cache(None)
    styles._set_portal_contrast_cache(None)
    styles._set_portal_reduced_motion_cache(None)

    fake_app = SimpleNamespace(
        styleHints=lambda: SimpleNamespace(
            colorScheme=lambda: Qt.ColorScheme.Unknown,
            accessibility=lambda: SimpleNamespace(),
        )
    )
    monkeypatch.setattr(
        styles, "QGuiApplication", SimpleNamespace(instance=lambda: fake_app)
    )
    monkeypatch.setattr(
        styles,
        "QDBusConnection",
        SimpleNamespace(
            sessionBus=lambda: SimpleNamespace(
                isConnected=lambda: True,
                call=lambda *_args, **_kwargs: pytest.fail(
                    "Non-blocking startup must not call synchronous D-Bus bus.call()"
                ),
            )
        ),
    )
    monkeypatch.setattr(
        styles.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "Non-blocking startup must not invoke synchronous gsettings subprocess"
        ),
    )

    assert styles.is_system_dark_mode(allow_blocking=False) is False
    assert styles.is_system_high_contrast(allow_blocking=False) is False
    assert styles.is_system_reduced_motion(allow_blocking=False) is False


def test_reduced_motion_reads_qt_accessibility_portal_and_ini_safely(
    monkeypatch: pytest.MonkeyPatch, isolated_fallbacks: None, tmp_path
) -> None:
    """Reduced motion supports Qt 6.12 hints, pre-6.12 absence, portal, and INI."""
    from PyQt6.QtCore import Qt

    styles._set_portal_reduced_motion_cache(None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    # 1. Qt 6.12+ QAccessibilityHints.motionPreference() when present.
    motion_pref_enum = getattr(Qt, "MotionPreference", None)
    if motion_pref_enum is not None:
        fake_acc = SimpleNamespace(
            motionPreference=lambda: motion_pref_enum.ReducedMotion
        )
        fake_app = SimpleNamespace(
            styleHints=lambda: SimpleNamespace(accessibility=lambda: fake_acc)
        )
        monkeypatch.setattr(
            styles, "QGuiApplication", SimpleNamespace(instance=lambda: fake_app)
        )
        assert styles.is_system_reduced_motion(allow_blocking=False) is True

    # 2. PyQt6 < 6.12 fallback (no accessibility() or no motionPreference()) + INI.
    fake_app_old = SimpleNamespace(styleHints=lambda: SimpleNamespace())
    monkeypatch.setattr(
        styles, "QGuiApplication", SimpleNamespace(instance=lambda: fake_app_old)
    )
    monkeypatch.setattr(styles, "HAS_QTDBUS", False)
    assert styles.is_system_reduced_motion(allow_blocking=False) is False

    # Undo isolated_fallbacks isfile patch so real temp INI files are read.
    monkeypatch.setattr(styles.os.path, "isfile", os.path.isfile)
    gtk4_ini = tmp_path / "gtk-4.0" / "settings.ini"
    gtk4_ini.parent.mkdir(parents=True)
    gtk4_ini.write_text("[Settings]\ngtk-enable-animations = false\n")
    assert styles.is_system_reduced_motion(allow_blocking=False) is True

    gtk4_ini.unlink()
    kde_ini = tmp_path / "kdeglobals"
    kde_ini.write_text("[KDE]\nAnimationDurationFactor=0\n")
    assert styles.is_system_reduced_motion(allow_blocking=False) is True

    # 3. Portal ReadOne fallback when Qt & INI do not specify reduced motion.
    kde_ini.unlink()
    bus = _Bus(lambda _method: _Reply(values=[1]))
    _install_bus(monkeypatch, bus)
    assert styles.is_system_reduced_motion(allow_blocking=True) is True
    assert [call[1] for call in bus.calls] == ["ReadOne"]
