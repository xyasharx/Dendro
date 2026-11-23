# dendro/ui/styles.py
"""
Theme resolution and desktop portal color scheme detection for Dendro.

Color palettes live in :mod:`ui.theme_palettes` and the QSS template lives in
:mod:`ui.stylesheet_builder`. This module keeps the public theme API and
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
from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QGuiApplication

from core.process_utils import is_running_in_flatpak
from ui.stylesheet_builder import _control_assets, build_stylesheet
from ui.theme_palettes import (
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


def _read_gtk3_dark_mode() -> bool | None:
    """Read GTK 3 theme preferences in-process from standard INI files."""
    gtk3_ini_paths = (
        os.path.expanduser("~/.config/gtk-3.0/settings.ini"),
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
    kde_globals = os.path.expanduser("~/.config/kdeglobals")
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


def get_resolved_theme_key(theme_choice: str) -> str:
    """Resolve theme choice to either ``dark`` or ``light``, aliasing legacy keys."""
    choice = (theme_choice or "auto").lower()
    if choice == "auto":
        return "dark" if is_system_dark_mode() else "light"
    if choice in ("dark", "mocha", "tokyo_night", "nord", "gruvbox"):
        return "dark"
    if choice in ("light", "latte", "solarized_light"):
        return "light"
    return "dark" if is_system_dark_mode() else "light"


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
    "portal_color_scheme_to_theme_key",
)
