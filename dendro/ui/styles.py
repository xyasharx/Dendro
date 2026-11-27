# dendro/ui/styles.py
"""
Theme resolution and desktop portal color scheme detection for Dendro.

Color palettes live in :mod:`dendro.ui.theme_palettes` and the QSS template lives in
:mod:`dendro.ui.stylesheet_builder`. This module keeps the public theme API and
in-process desktop dark-mode detection (portal D-Bus, Qt ``QStyleHints``,
``settings.ini``, XFCE ``xsettings.xml``, ``kdeglobals``, and a tightly gated
``gsettings`` fallback).
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

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
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
    {
        "gnome",
        "ubuntu",
        "pop",
        "budgie",
        "cinnamon",
        "mate",
        "pantheon",
        "unity",
        "xfce",
        "gtk",
    }
)
_NON_GSETTINGS_DESKTOPS = frozenset(
    {"kde", "plasma", "lxqt", "sway", "hyprland", "river", "wlroots"}
)
_PORTAL_CACHE_TTL_S = 30.0
_portal_color_scheme_cache: tuple[str, str, float, bool] | None = None
_portal_contrast_cache: tuple[str, str, float, bool] | None = None
_portal_reduced_motion_cache: tuple[str, str, float, bool] | None = None
_gsettings_dark_mode_cache: tuple[str, str, str, float, bool] | None = None


def _current_session_bus_identity() -> tuple[str, str] | None:
    """Return ``(bus_name, bus_addr)`` when a named session bus is connected."""
    if not HAS_QTDBUS:
        return None
    try:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return None
        name_fn = getattr(bus, "name", None)
        bus_name = name_fn() if callable(name_fn) else ""
    except (RuntimeError, TypeError):
        bus_name = ""
    if isinstance(bus_name, str) and bus_name:
        return bus_name, os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    return None


def _read_portal_cache(
    cache: tuple[str, str, float, bool] | None,
) -> bool | None:
    """Return cached boolean if valid for the active session bus and within TTL."""
    if cache is None:
        return None
    identity = _current_session_bus_identity()
    if identity is None:
        return None
    bus_name, bus_addr = identity
    if cache[0] == bus_name and cache[1] == bus_addr and time.monotonic() < cache[2]:
        return cache[3]
    return None


def _build_portal_cache(
    value: bool | None,
) -> tuple[str, str, float, bool] | None:
    if value is None:
        return None
    identity = _current_session_bus_identity()
    if identity is None:
        return None
    bus_name, bus_addr = identity
    return (
        bus_name,
        bus_addr,
        time.monotonic() + _PORTAL_CACHE_TTL_S,
        bool(value),
    )


def _set_portal_color_scheme_cache(value: bool | None) -> None:
    """Update or clear the process-local FreeDesktop portal dark-mode cache."""
    global _portal_color_scheme_cache
    _portal_color_scheme_cache = _build_portal_cache(value)


def _set_portal_contrast_cache(value: bool | None) -> None:
    """Update or clear the process-local FreeDesktop portal contrast cache."""
    global _portal_contrast_cache
    _portal_contrast_cache = _build_portal_cache(value)


def _set_portal_reduced_motion_cache(value: bool | None) -> None:
    """Update or clear the process-local FreeDesktop portal reduced-motion cache."""
    global _portal_reduced_motion_cache
    _portal_reduced_motion_cache = _build_portal_cache(value)


def _gsettings_cache_identity() -> tuple[str, str, str]:
    """Return session descriptors that scope the async ``gsettings`` cache."""
    return (
        os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
        os.environ.get("XDG_CURRENT_DESKTOP", ""),
        os.environ.get("DESKTOP_SESSION", ""),
    )


def _set_gsettings_dark_mode_cache(value: bool | None) -> None:
    """Update or clear the process-local async ``gsettings`` dark-mode cache."""
    global _gsettings_dark_mode_cache
    if value is None:
        _gsettings_dark_mode_cache = None
        return
    bus_addr, current_desktop, desktop_session = _gsettings_cache_identity()
    _gsettings_dark_mode_cache = (
        bus_addr,
        current_desktop,
        desktop_session,
        time.monotonic() + _PORTAL_CACHE_TTL_S,
        bool(value),
    )


def _read_gsettings_dark_mode_cache() -> bool | None:
    """Return cached async ``gsettings`` dark mode if valid for the active session."""
    if _gsettings_dark_mode_cache is None:
        return None
    bus_addr, current_desktop, desktop_session = _gsettings_cache_identity()
    if (
        _gsettings_dark_mode_cache[0] == bus_addr
        and _gsettings_dark_mode_cache[1] == current_desktop
        and _gsettings_dark_mode_cache[2] == desktop_session
        and time.monotonic() < _gsettings_dark_mode_cache[3]
    ):
        return _gsettings_dark_mode_cache[4]
    return None


def _unwrap_dbus_variant(value: object) -> object:
    """Unwrap up to two nested D-Bus ``QDBusVariant`` layers defensively."""
    for _ in range(2):
        unwrap = getattr(value, "variant", None)
        if not callable(unwrap):
            break
        unwrapped = unwrap()
        if unwrapped is value:
            break
        value = unwrapped
    return value


def portal_color_scheme_to_theme_key(value: object) -> str | None:
    """Translate a portal Settings color-scheme value to ``dark`` or ``light``.

    The portal sends the setting in a D-Bus variant.  ``Read`` is also known to
    add an extra variant layer, so unwrap defensively for both the startup read
    and the live ``SettingChanged`` signal.
    """
    value = _unwrap_dbus_variant(value)

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
    value = _unwrap_dbus_variant(value)

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


def portal_reduced_motion_to_bool(value: object) -> bool | None:
    """Translate a portal Settings ``reduced-motion`` value to ``bool`` or ``None``.

    The FreeDesktop appearance portal defines ``reduced-motion`` as a
    ``uint32``: ``0`` = no preference, ``1`` = reduced motion.
    """
    value = _unwrap_dbus_variant(value)

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
    """Read GTK 3/4 theme preferences in-process from standard INI files."""
    gtk_ini_paths = (
        os.path.join(_config_home_dir(), "gtk-3.0", "settings.ini"),
        os.path.join(_config_home_dir(), "gtk-4.0", "settings.ini"),
        "/etc/gtk-3.0/settings.ini",
    )
    for ini_path in gtk_ini_paths:
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


def _read_gtk_reduced_motion() -> bool | None:
    """Read ``gtk-enable-animations`` from GTK 3/4 INI files without blocking."""
    gtk_ini_paths = (
        os.path.join(_config_home_dir(), "gtk-4.0", "settings.ini"),
        os.path.join(_config_home_dir(), "gtk-3.0", "settings.ini"),
        "/etc/gtk-3.0/settings.ini",
    )
    for ini_path in gtk_ini_paths:
        if not os.path.isfile(ini_path):
            continue
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(ini_path, encoding="utf-8")
            if cp.has_section("Settings"):
                enable_anim = (
                    cp.get("Settings", "gtk-enable-animations", fallback="")
                    .strip()
                    .lower()
                )
                if enable_anim in ("0", "false", "no"):
                    return True
                if enable_anim in ("1", "true", "yes"):
                    return False
        except (OSError, configparser.Error, UnicodeError):
            LOGGER.debug(
                "Could not parse GTK animation settings from %s.",
                ini_path,
                exc_info=True,
            )
    return None


def _read_xfce_dark_mode() -> bool | None:
    """Read XFCE ``xsettings.xml`` theme preferences in-process."""
    xfce_xml_paths = (
        os.path.join(
            _config_home_dir(),
            "xfce4",
            "xfconf",
            "xfce-perchannel-xml",
            "xsettings.xml",
        ),
        "/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml",
    )
    for xml_path in xfce_xml_paths:
        if not os.path.isfile(xml_path):
            continue
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            if root is None:
                continue
            found_theme_name = False
            for net_prop in root.findall("./property[@name='Net']"):
                for theme_prop in net_prop.findall("./property[@name='ThemeName']"):
                    theme_name = (theme_prop.attrib.get("value") or "").strip().lower()
                    if not theme_name:
                        continue
                    found_theme_name = True
                    if "dark" in theme_name:
                        return True
            if found_theme_name:
                break
        except (OSError, ET.ParseError, DefusedXmlException, UnicodeError):
            LOGGER.debug(
                "Could not parse XFCE theme settings from %s.",
                xml_path,
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


def _read_kde_reduced_motion() -> bool | None:
    """Read KDE Plasma ``AnimationDurationFactor`` from ``kdeglobals``."""
    kde_globals = os.path.join(_config_home_dir(), "kdeglobals")
    if not os.path.isfile(kde_globals):
        return None
    try:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(kde_globals, encoding="utf-8")
        if cp.has_section("KDE"):
            raw = cp.get("KDE", "AnimationDurationFactor", fallback="").strip()
            if raw:
                try:
                    return float(raw) <= 0.0
                except ValueError:
                    return None
    except (OSError, configparser.Error, UnicodeError):
        LOGGER.debug(
            "Could not parse KDE animation settings from %s.",
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


def _query_portal_appearance_setting(key: str) -> object | None:
    """Read one ``org.freedesktop.appearance`` key via bounded D-Bus ``ReadOne``/``Read``."""
    if not HAS_QTDBUS:
        return None
    try:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return None
        for method in ("ReadOne", "Read"):
            message = QDBusMessage.createMethodCall(
                PORTAL_SERVICE,
                PORTAL_PATH,
                PORTAL_SETTINGS_INTERFACE,
                method,
            )
            message.setArguments(["org.freedesktop.appearance", key])
            reply = bus.call(message, timeout=PORTAL_SETTINGS_TIMEOUT_MS)
            if (
                reply
                and reply.type() == QDBusMessage.MessageType.ReplyMessage
                and reply.arguments()
            ):
                return reply.arguments()[0]
    except (RuntimeError, TypeError, ValueError):
        LOGGER.debug("Could not read portal appearance key %r.", key, exc_info=True)
    return None


def _env_gtk_theme_dark_mode() -> bool | None:
    """Return explicit dark/light preference from ``GTK_THEME``, or ``None``."""
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
    return None


def has_non_gsettings_color_scheme() -> bool:
    """Return True if a higher-priority non-``gsettings`` source resolved the theme."""
    if _env_gtk_theme_dark_mode() is not None:
        return True
    app = QGuiApplication.instance()
    if app and hasattr(app, "styleHints"):
        style_hints = app.styleHints()
        color_scheme_fn = getattr(style_hints, "colorScheme", None)
        if callable(color_scheme_fn) and color_scheme_fn() in (
            Qt.ColorScheme.Dark,
            Qt.ColorScheme.Light,
        ):
            return True
    if _read_portal_cache(_portal_color_scheme_cache) is not None:
        return True
    if _read_gtk3_dark_mode() is not None:
        return True
    if _read_xfce_dark_mode() is not None:
        return True
    return _read_kde_dark_mode() is not None


def is_system_dark_mode(*, allow_blocking: bool = True) -> bool:
    """Detect whether the desktop environment is currently using a dark appearance.

    Prioritises non-blocking in-process checks (``GTK_THEME``, Qt 6.5+
    ``QStyleHints.colorScheme()``, cached portal results, and desktop INI/XML
    configs). When ``allow_blocking`` is true and Qt reports
    ``Qt.ColorScheme.Unknown``, falls back to bounded FreeDesktop portal D-Bus
    and tightly gated GNOME/GTK ``gsettings`` queries.
    """
    # 1. Explicit GTK_THEME environment variable override
    env_result = _env_gtk_theme_dark_mode()
    if env_result is not None:
        return env_result

    # 2. Qt 6.5+/6.8+ QStyleHints (in-process native Wayland / X11 colorScheme)
    app = QGuiApplication.instance()
    if app and hasattr(app, "styleHints"):
        style_hints = app.styleHints()
        color_scheme_fn = getattr(style_hints, "colorScheme", None)
        if callable(color_scheme_fn):
            scheme = color_scheme_fn()
            if scheme == Qt.ColorScheme.Dark:
                return True
            if scheme == Qt.ColorScheme.Light:
                return False

    # 3. Cached or bounded FreeDesktop Portal Query via QtDBus (1=dark, 2=light)
    cached_dark = _read_portal_cache(_portal_color_scheme_cache)
    if cached_dark is not None:
        return cached_dark

    if allow_blocking and HAS_QTDBUS:
        raw_value = _query_portal_appearance_setting("color-scheme")
        if raw_value is not None:
            theme_key = portal_color_scheme_to_theme_key(raw_value)
            if theme_key == "dark":
                _set_portal_color_scheme_cache(True)
                return True
            if theme_key == "light":
                _set_portal_color_scheme_cache(False)
                return False

    # 4. In-process GTK 3/4 Configuration File Read
    gtk3_result = _read_gtk3_dark_mode()
    if gtk3_result is not None:
        return gtk3_result

    # 5. In-process XFCE Configuration File Read (xfconf xsettings.xml)
    xfce_result = _read_xfce_dark_mode()
    if xfce_result is not None:
        return xfce_result

    # 6. In-process KDE Plasma Configuration Read (~/.config/kdeglobals)
    kde_result = _read_kde_dark_mode()
    if kde_result is not None:
        return kde_result

    # 7. Cached or synchronous fallback: gsettings only in live GNOME/GTK sessions
    if _should_query_gsettings():
        cached_gsettings = _read_gsettings_dark_mode_cache()
        if cached_gsettings is not None:
            return cached_gsettings
        if allow_blocking:
            gsettings_result = _query_gsettings_dark_mode()
            if gsettings_result is not None:
                return gsettings_result

    return False


def is_system_high_contrast(*, allow_blocking: bool = True) -> bool:
    """Detect whether the desktop environment requests high-contrast rendering.

    Checks, in priority order:
    1. ``GTK_THEME`` environment overrides naming a high-contrast theme.
    2. Qt 6.10+ ``QGuiApplication.styleHints().accessibility().contrastPreference()``.
    3. Cached or bounded FreeDesktop portal D-Bus ``org.freedesktop.appearance``
       ``contrast`` setting (when ``allow_blocking`` is true).
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

    cached_hc = _read_portal_cache(_portal_contrast_cache)
    if cached_hc is not None:
        return cached_hc

    if allow_blocking and HAS_QTDBUS:
        raw_value = _query_portal_appearance_setting("contrast")
        resolved_hc = (
            portal_contrast_to_bool(raw_value) if raw_value is not None else None
        )
        if _current_session_bus_identity() is not None:
            _set_portal_contrast_cache(False if resolved_hc is None else resolved_hc)
        if resolved_hc is not None:
            return resolved_hc

    return False


def is_system_reduced_motion(*, allow_blocking: bool = True) -> bool:
    """Detect whether the desktop environment prefers reduced UI motion.

    Checks, in priority order:
    1. Qt 6.12+ ``QGuiApplication.styleHints().accessibility().motionPreference()``.
    2. Cached or bounded FreeDesktop portal D-Bus ``org.freedesktop.appearance``
       ``reduced-motion`` setting (when ``allow_blocking`` is true).
    3. In-process GTK 3/4 ``gtk-enable-animations`` and KDE ``AnimationDurationFactor``.
    """
    app = QGuiApplication.instance()
    if app and hasattr(app, "styleHints"):
        style_hints = app.styleHints()
        accessibility_fn = getattr(style_hints, "accessibility", None)
        if callable(accessibility_fn):
            accessibility = accessibility_fn()
            motion_fn = getattr(accessibility, "motionPreference", None)
            reduced_motion_enum = getattr(
                getattr(Qt, "MotionPreference", None),
                "ReducedMotion",
                None,
            )
            if (
                callable(motion_fn)
                and reduced_motion_enum is not None
                and motion_fn() == reduced_motion_enum
            ):
                return True

    cached_rm = _read_portal_cache(_portal_reduced_motion_cache)
    if cached_rm is not None:
        return cached_rm

    if allow_blocking and HAS_QTDBUS:
        raw_value = _query_portal_appearance_setting("reduced-motion")
        resolved_rm = (
            portal_reduced_motion_to_bool(raw_value) if raw_value is not None else None
        )
        if _current_session_bus_identity() is not None:
            _set_portal_reduced_motion_cache(
                False if resolved_rm is None else resolved_rm
            )
        if resolved_rm is not None:
            return resolved_rm

    gtk_rm = _read_gtk_reduced_motion()
    if gtk_rm is not None:
        return gtk_rm

    kde_rm = _read_kde_reduced_motion()
    if kde_rm is not None:
        return kde_rm

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
    allow_blocking: bool = False,
) -> str:
    """Resolve theme choice to ``dark``, ``light``, ``dark_hc``, or ``light_hc``."""
    choice = (theme_choice or "auto").lower()
    if choice in ("dark_hc", "light_hc"):
        return choice
    hc = (
        is_system_high_contrast(allow_blocking=allow_blocking)
        if high_contrast is None
        else high_contrast
    )
    if choice in ("dark", "mocha", "tokyo_night", "nord", "gruvbox"):
        return resolve_theme_key("dark", system_dark=True, high_contrast=hc)
    if choice in ("light", "latte", "solarized_light"):
        return resolve_theme_key("light", system_dark=False, high_contrast=hc)
    return resolve_theme_key(
        "auto",
        system_dark=is_system_dark_mode(allow_blocking=allow_blocking),
        high_contrast=hc,
    )


def get_theme_stylesheet(theme_choice: str, *, allow_blocking: bool = False) -> str:
    """Retrieve the full stylesheet for a given theme choice."""
    key = get_resolved_theme_key(theme_choice, allow_blocking=allow_blocking)
    return build_stylesheet(THEMES_CONFIG[key])


def get_delegate_palette(
    theme_choice: str, *, allow_blocking: bool = False
) -> dict[str, QColor]:
    """Generate ``QColor`` palette objects used by delegates and custom widgets."""
    return build_delegate_palette(
        get_resolved_theme_key(theme_choice, allow_blocking=allow_blocking)
    )


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
    "is_system_reduced_motion",
    "portal_color_scheme_to_theme_key",
    "portal_contrast_to_bool",
    "portal_reduced_motion_to_bool",
    "resolve_theme_key",
)
