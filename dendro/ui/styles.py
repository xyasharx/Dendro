# dendro/ui/styles.py
"""
Theme resolution and desktop portal color scheme detection for Dendro.

Color palettes live in :mod:`dendro.ui.theme_palettes` and the QSS template lives in
:mod:`dendro.ui.stylesheet_builder`. This module keeps the public theme API and
in-process desktop dark-mode detection (portal D-Bus, Qt ``QStyleHints``,
``settings.ini``, ``kdeglobals``, and a tightly gated ``gsettings`` fallback).
"""

from __future__ import annotations

import configparser
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QGuiApplication

from dendro.core.process_utils import is_running_in_flatpak
from dendro.ui.stylesheet_builder import _control_assets, build_stylesheet
from dendro.ui.theme_palettes import (
    THEME_DISPLAY_OPTIONS,
    THEMES_CONFIG,
    build_delegate_palette,
)

try:
    from PyQt6.QtDBus import QDBusConnection, QDBusMessage

    HAS_QTDBUS = True
except ImportError:
    HAS_QTDBUS = False

LOGGER = logging.getLogger(__name__)


def _debug_log(msg: str) -> None:
    """Print timestamped diagnostic logs to stderr only when DENDRO_DEBUG is set."""
    if os.environ.get("DENDRO_DEBUG"):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        print(f"[{ts}] [Dendro:debug] {msg}", file=sys.stderr)


# This query runs on the GUI thread during theme resolution. Qt's default
# blocking D-Bus timeout is about 25 seconds, so a portal that accepts but never
# answers would freeze the window. Each bounded call keeps the worst case short;
# the caller falls through to the deterministic non-D-Bus fallbacks.
PORTAL_SETTINGS_TIMEOUT_MS = 1000
PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_SETTINGS_INTERFACE = "org.freedesktop.portal.Settings"

_GNOME_GTK_DESKTOPS = frozenset(
    {"gnome", "ubuntu", "pop", "budgie", "cinnamon", "mate", "pantheon", "unity", "gtk"}
)
_NON_GSETTINGS_DESKTOPS = frozenset(
    {"kde", "plasma", "lxqt", "sway", "hyprland", "river", "wlroots"}
)
_PORTAL_CONTRAST_CACHE_TTL_S = 30.0
_portal_contrast_cache: tuple[str, str, float, bool] | None = None


def _set_portal_contrast_cache(value: bool | None) -> None:
    """Update or clear the process-local FreeDesktop portal contrast cache."""
    global _portal_contrast_cache
    if value is None:
        _portal_contrast_cache = None
        return
    if not HAS_QTDBUS:
        _portal_contrast_cache = None
        return
    try:
        bus = QDBusConnection.sessionBus()
        name_fn = getattr(bus, "name", None)
        bus_name = name_fn() if callable(name_fn) else ""
    except (RuntimeError, TypeError):
        bus_name = ""
    if isinstance(bus_name, str) and bus_name:
        _portal_contrast_cache = (
            bus_name,
            os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
            time.monotonic() + _PORTAL_CONTRAST_CACHE_TTL_S,
            bool(value),
        )
    else:
        _portal_contrast_cache = None


def portal_color_scheme_to_theme_key(value: object) -> str | None:
    """Translate a portal Settings color-scheme value to ``dark`` or ``light``.

    The portal sends the setting in a D-Bus variant.  ``Read`` is also known to
    add an extra variant layer, so unwrap defensively for both the startup read
    and the live ``SettingChanged`` signal.
    """
    for _ in range(2):
        unwrap = getattr(value, "variant", None)
        if not callable(unwrap):
            break
        unwrapped = unwrap()
        if unwrapped is value:
            break
        value = unwrapped

    try:
        preference = int(value)
    except (TypeError, ValueError):
        return None

    if preference == 1:
        return "dark"
    if preference == 2:
        return "light"
    return None


def portal_contrast_to_bool(value: object) -> bool | None:
    """Translate a portal Settings ``contrast`` value to ``bool`` or ``None``.

    The FreeDesktop appearance portal defines ``contrast`` as a ``uint32``:
    ``0`` = normal preference, ``1`` = high contrast. Unwraps nested D-Bus
    variants defensively for both ``Read``/``ReadOne`` and ``SettingChanged``.
    """
    for _ in range(2):
        unwrap = getattr(value, "variant", None)
        if not callable(unwrap):
            break
        unwrapped = unwrap()
        if unwrapped is value:
            break
        value = unwrapped

    if isinstance(value, bool):
        return value
    try:
        preference = int(value)
    except (TypeError, ValueError):
        return None

    if preference == 1:
        return True
    if preference == 0:
        return False
    return None


def _config_home_dir() -> str:
    """Return the active XDG config directory, honoring XDG_CONFIG_HOME."""
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def _read_gtk3_dark_mode() -> bool | None:
    """Read GTK 3 theme preferences in-process from standard INI files."""
    gtk3_ini_paths = (
        os.path.join(_config_home_dir(), "gtk-3.0", "settings.ini"),
        "/etc/gtk-3.0/settings.ini",
    )
    for ini_path in gtk3_ini_paths:
        if not os.path.isfile(ini_path):
            continue
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(ini_path, encoding="utf-8")
            if cp.has_section("Settings"):
                prefer_dark = cp.get(
                    "Settings", "gtk-application-prefer-dark-theme", fallback=""
                ).lower()
                if prefer_dark in ("1", "true", "yes"):
                    return True
                gtk_theme = cp.get("Settings", "gtk-theme-name", fallback="").lower()
                if (
                    "-dark" in gtk_theme
                    or gtk_theme.endswith("dark")
                    or "dark" in gtk_theme
                ):
                    return True
        except (OSError, configparser.Error, UnicodeError):
            LOGGER.debug(
                "Could not parse GTK theme settings from %s.",
                ini_path,
                exc_info=True,
            )
    return None


def _read_kde_dark_mode() -> bool | None:
    """Read KDE Plasma color scheme preferences in-process from ``kdeglobals``."""
    kde_globals = os.path.join(_config_home_dir(), "kdeglobals")
    if not os.path.isfile(kde_globals):
        return None
    try:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(kde_globals, encoding="utf-8")
        if cp.has_section("General"):
            color_scheme = cp.get("General", "ColorScheme", fallback="").strip().lower()
            if color_scheme:
                return "dark" in color_scheme
    except (OSError, configparser.Error, UnicodeError):
        LOGGER.debug(
            "Could not parse KDE theme settings from %s.",
            kde_globals,
            exc_info=True,
        )
    return None


def _should_query_gsettings() -> bool:
    """Gate the synchronous ``gsettings`` fallback to live GNOME/GTK desktop sessions."""
    if is_running_in_flatpak():
        return False
    desktop_tokens = {
        part.strip().lower()
        for var in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION")
        for part in os.environ.get(var, "").split(":")
        if part.strip()
    }
    if desktop_tokens & _NON_GSETTINGS_DESKTOPS:
        return False
    if desktop_tokens and not (desktop_tokens & _GNOME_GTK_DESKTOPS):
        return False
    if not (
        desktop_tokens
        or os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        or os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("DISPLAY")
    ):
        return False
    return shutil.which("gsettings") is not None


def _query_gsettings_dark_mode() -> bool | None:
    """Last-resort synchronous GNOME ``gsettings`` color-scheme query."""
    try:
        res = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True,
            text=True,
            check=False,
            timeout=1,
        )
        if res.returncode == 0:
            val = res.stdout.strip().strip("'\"").lower()
            if "prefer-dark" in val or val == "dark":
                return True
            if val in ("prefer-light", "light"):
                return False
            if val == "default":
                res_theme = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=1,
                )
                if res_theme.returncode == 0:
                    t_val = res_theme.stdout.strip().strip("'\"").lower()
                    if "-dark" in t_val or t_val.endswith("dark"):
                        return True
                return False
    except (OSError, subprocess.SubprocessError, UnicodeError):
        LOGGER.debug("Could not query the GNOME color-scheme setting.", exc_info=True)
    return None


def is_system_dark_mode() -> bool:
    """Detect whether the desktop environment is currently using a dark appearance.

    Prioritises in-process environment, FreeDesktop portal D-Bus, Qt
    ``QStyleHints``, and desktop config file reads before falling back to
    ``gsettings`` only in non-sandboxed GNOME/GTK desktop sessions.
    """
    # 1. Explicit GTK_THEME environment variable override
    env_gtk_theme = os.environ.get("GTK_THEME", "").lower()
    if env_gtk_theme:
        if (
            ":dark" in env_gtk_theme
            or "-dark" in env_gtk_theme
            or env_gtk_theme.endswith("dark")
        ):
            return True
        if ":light" in env_gtk_theme or "-light" in env_gtk_theme:
            return False

    # 2. In-process FreeDesktop Portal Query via QtDBus (0=default, 1=dark, 2=light)
    if HAS_QTDBUS:
        try:
            bus = QDBusConnection.sessionBus()
            if bus.isConnected():
                for method in ("ReadOne", "Read"):
                    message = QDBusMessage.createMethodCall(
                        PORTAL_SERVICE,
                        PORTAL_PATH,
                        PORTAL_SETTINGS_INTERFACE,
                        method,
                    )
                    message.setArguments(["org.freedesktop.appearance", "color-scheme"])
                    reply = bus.call(message, timeout=PORTAL_SETTINGS_TIMEOUT_MS)
                    if (
                        reply
                        and reply.type() == QDBusMessage.MessageType.ReplyMessage
                        and reply.arguments()
                    ):
                        theme_key = portal_color_scheme_to_theme_key(
                            reply.arguments()[0]
                        )
                        if theme_key == "dark":
                            return True
                        if theme_key == "light":
                            return False
        except (RuntimeError, TypeError, ValueError):
            LOGGER.debug(
                "Could not read the portal color-scheme setting.", exc_info=True
            )

    # 3. Qt 6 QStyleHints (native Wayland / X11 colorScheme)
    app = QGuiApplication.instance()
    if app and hasattr(app, "styleHints"):
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False

    # 4. In-process GTK 3 Configuration File Read (~/.config/gtk-3.0/settings.ini)
    gtk3_result = _read_gtk3_dark_mode()
    if gtk3_result is not None:
        return gtk3_result

    # 5. In-process KDE Plasma Configuration Read (~/.config/kdeglobals)
    kde_result = _read_kde_dark_mode()
    if kde_result is not None:
        return kde_result

    # 6. Conditional fallback: GNOME gsettings only in live GNOME/GTK desktop sessions
    if _should_query_gsettings():
        gsettings_result = _query_gsettings_dark_mode()
        if gsettings_result is not None:
            return gsettings_result

    return False


def is_system_high_contrast() -> bool:
    """Detect whether the desktop environment requests high-contrast rendering.

    Checks, in priority order:
    1. ``GTK_THEME`` environment overrides naming a high-contrast theme.
    2. Qt 6.8+ ``QGuiApplication.styleHints().accessibility().contrastPreference()``.
    3. FreeDesktop portal D-Bus ``org.freedesktop.appearance`` ``contrast`` setting.
    """
    env_gtk_theme = os.environ.get("GTK_THEME", "").strip().lower()
    if env_gtk_theme and (
        "highcontrast" in env_gtk_theme
        or "high-contrast" in env_gtk_theme
        or ":hc" in env_gtk_theme
        or env_gtk_theme.endswith("-hc")
    ):
        return True

    app = QGuiApplication.instance()
    if app and hasattr(app, "styleHints"):
        style_hints = app.styleHints()
        accessibility_fn = getattr(style_hints, "accessibility", None)
        if callable(accessibility_fn):
            accessibility = accessibility_fn()
            contrast_fn = getattr(accessibility, "contrastPreference", None)
            high_contrast_enum = getattr(
                getattr(Qt, "ContrastPreference", None),
                "HighContrast",
                None,
            )
            if (
                callable(contrast_fn)
                and high_contrast_enum is not None
                and contrast_fn() == high_contrast_enum
            ):
                return True

    if HAS_QTDBUS:
        try:
            bus = QDBusConnection.sessionBus()
            if bus.isConnected():
                name_fn = getattr(bus, "name", None)
                bus_name = name_fn() if callable(name_fn) else ""
                bus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
                now = time.monotonic()
                if (
                    isinstance(bus_name, str)
                    and bus_name
                    and _portal_contrast_cache is not None
                    and _portal_contrast_cache[0] == bus_name
                    and _portal_contrast_cache[1] == bus_addr
                    and now < _portal_contrast_cache[2]
                ):
                    return _portal_contrast_cache[3]

                resolved_hc: bool | None = None
                for method in ("ReadOne", "Read"):
                    message = QDBusMessage.createMethodCall(
                        PORTAL_SERVICE,
                        PORTAL_PATH,
                        PORTAL_SETTINGS_INTERFACE,
                        method,
                    )
                    message.setArguments(["org.freedesktop.appearance", "contrast"])
                    reply = bus.call(message, timeout=PORTAL_SETTINGS_TIMEOUT_MS)
                    if (
                        reply
                        and reply.type() == QDBusMessage.MessageType.ReplyMessage
                        and reply.arguments()
                    ):
                        hc = portal_contrast_to_bool(reply.arguments()[0])
                        if hc is not None:
                            resolved_hc = hc
                            break
                if isinstance(bus_name, str) and bus_name:
                    _set_portal_contrast_cache(
                        False if resolved_hc is None else resolved_hc
                    )
                if resolved_hc is not None:
                    return resolved_hc
        except (RuntimeError, TypeError, ValueError):
            LOGGER.debug("Could not read the portal contrast setting.", exc_info=True)

    return False


def resolve_theme_key(
    theme_choice: str,
    *,
    system_dark: bool,
    high_contrast: bool = False,
) -> str:
    """Resolve a theme choice + desktop flags into a concrete palette key."""
    choice = (theme_choice or "auto").lower()
    if choice in ("dark_hc", "light_hc"):
        return choice
    if choice in ("dark", "mocha", "tokyo_night", "nord", "gruvbox"):
        base = "dark"
    elif choice in ("light", "latte", "solarized_light"):
        base = "light"
    else:
        base = "dark" if system_dark else "light"
    if high_contrast:
        return f"{base}_hc"
    return base


def get_resolved_theme_key(
    theme_choice: str,
    *,
    high_contrast: bool | None = None,
) -> str:
    """Resolve theme choice to ``dark``, ``light``, ``dark_hc``, or ``light_hc``."""
    choice = (theme_choice or "auto").lower()
    if choice in ("dark_hc", "light_hc"):
        return choice
    hc = is_system_high_contrast() if high_contrast is None else high_contrast
    if choice in ("dark", "mocha", "tokyo_night", "nord", "gruvbox"):
        return resolve_theme_key("dark", system_dark=True, high_contrast=hc)
    if choice in ("light", "latte", "solarized_light"):
        return resolve_theme_key("light", system_dark=False, high_contrast=hc)
    return resolve_theme_key(
        "auto", system_dark=is_system_dark_mode(), high_contrast=hc
    )


def get_theme_stylesheet(theme_choice: str) -> str:
    """Retrieve the full stylesheet for a given theme choice."""
    key = get_resolved_theme_key(theme_choice)
    return build_stylesheet(THEMES_CONFIG[key])


def get_delegate_palette(theme_choice: str) -> dict[str, QColor]:
    """Generate ``QColor`` palette objects used by delegates and custom widgets."""
    return build_delegate_palette(get_resolved_theme_key(theme_choice))


__all__ = (
    "HAS_QTDBUS",
    "PORTAL_PATH",
    "PORTAL_SERVICE",
    "PORTAL_SETTINGS_INTERFACE",
    "PORTAL_SETTINGS_TIMEOUT_MS",
    "THEMES_CONFIG",
    "THEME_DISPLAY_OPTIONS",
    "_control_assets",
    "_debug_log",
    "build_stylesheet",
    "get_delegate_palette",
    "get_resolved_theme_key",
    "get_theme_stylesheet",
    "is_system_dark_mode",
    "is_system_high_contrast",
    "portal_color_scheme_to_theme_key",
    "portal_contrast_to_bool",
    "resolve_theme_key",
)
