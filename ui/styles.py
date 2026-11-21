# dendro/ui/styles.py
"""
Theme palettes, dynamic QSS builder, and desktop portal color scheme detection for Dendro.
Provides curated high-contrast dark and light color palettes with a modern, elevated UI design system.
Explicitly styles tree views, viewports, headers, sidebars, and cards to prevent host theme bleed.
Zero emoji glyphs and zero color emoji font chains to prevent Fontconfig crashes.
"""

from __future__ import annotations

import configparser
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Final

LOGGER = logging.getLogger(__name__)


def _debug_log(msg: str):
    """Prints timestamped diagnostic logs to stderr only when DENDRO_DEBUG is set."""
    if os.environ.get("DENDRO_DEBUG"):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        print(f"[{ts}] [Dendro:debug] {msg}", file=sys.stderr)


from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QGuiApplication

try:
    from PyQt6.QtDBus import QDBusConnection, QDBusMessage

    HAS_QTDBUS = True
except ImportError:
    HAS_QTDBUS = False

# This query runs on the GUI thread during theme resolution. Qt's default
# blocking D-Bus timeout is about 25 seconds, so a portal that accepts but never
# answers would freeze the window. Each bounded call keeps the worst case short;
# the caller falls through to the deterministic non-D-Bus fallbacks.
PORTAL_SETTINGS_TIMEOUT_MS = 1000
PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_SETTINGS_INTERFACE = "org.freedesktop.portal.Settings"

# =============================================================================
# Theme Color Palettes (Curated High-Contrast Dark & Light Themes)
# =============================================================================

THEMES_CONFIG: Final[dict[str, dict[str, str]]] = {
    # 1. Dark Theme (Modern High-Contrast Dark)
    "dark": {
        "name": "Dark",
        "is_dark": "true",
        "bg_base": "#181825",
        "bg_surface": "#1e1e2e",
        "bg_card": "#24273a",
        "bg_input": "#11111b",
        "bg_hover": "#313244",
        "bg_selected": "#45475a",
        "text_primary": "#cdd6f4",
        "text_secondary": "#a6adc8",
        "text_dim": "#888ea8",
        "border": "#313244",
        "border_subtle": "#252739",
        "border_strong": "#585b70",
        "accent": "#89b4fa",
        "accent_hover": "#b4befe",
        "accent_text": "#11111b",
        "badge_bg_installed": "#1e3a2f",
        "badge_fg_installed": "#a6e3a1",
        "badge_border_installed": "#2d5a47",
        "badge_bg_update": "#1d2d44",
        "badge_fg_update": "#89b4fa",
        "badge_border_update": "#2e4a70",
        "badge_bg_missing": "#45232e",
        "badge_fg_missing": "#f38ba8",
        "badge_border_missing": "#6a2e3f",
        "badge_bg_queued_in": "#453322",
        "badge_fg_queued_in": "#fab387",
        "badge_border_queued_in": "#6e4b2d",
        "badge_bg_queued_rm": "#45252b",
        "badge_fg_queued_rm": "#eba0ac",
        "badge_border_queued_rm": "#6d303b",
        "badge_bg_tag": "#3d2f47",
        "badge_fg_tag": "#cba6f7",
        "badge_border_tag": "#573d69",
    },
    # 2. Light Theme (WCAG AA High-Contrast Light)
    "light": {
        "name": "Light",
        "is_dark": "false",
        "bg_base": "#eff1f5",
        "bg_surface": "#ffffff",
        "bg_card": "#f8f9fc",
        "bg_input": "#e6e9ef",
        "bg_hover": "#e2e5ec",
        "bg_selected": "#dce0e8",
        "text_primary": "#383b52",
        "text_secondary": "#4e5268",
        "text_dim": "#595f72",
        "border": "#ccd0da",
        "border_subtle": "#e6e9ef",
        "border_strong": "#9aa0b0",
        "accent": "#1d5fd6",
        "accent_hover": "#174eb3",
        "accent_text": "#ffffff",
        "badge_bg_installed": "#eaf5ed",
        "badge_fg_installed": "#256d1b",
        "badge_border_installed": "#b5dfc0",
        "badge_bg_update": "#e8f0fe",
        "badge_fg_update": "#1856c9",
        "badge_border_update": "#b8d0fa",
        "badge_bg_missing": "#fdecee",
        "badge_fg_missing": "#c41235",
        "badge_border_missing": "#f5b3bf",
        "badge_bg_queued_in": "#fff4e5",
        "badge_fg_queued_in": "#9a5200",
        "badge_border_queued_in": "#f5cf9b",
        "badge_bg_queued_rm": "#fdecee",
        "badge_fg_queued_rm": "#b4232c",
        "badge_border_queued_rm": "#f5b3bf",
        "badge_bg_tag": "#f3effa",
        "badge_fg_tag": "#6d28d9",
        "badge_border_tag": "#d9caf5",
    },
}

THEME_DISPLAY_OPTIONS: Final[list[tuple[str, str]]] = [
    ("auto", "Follow System"),
    ("dark", "Dark Mode"),
    ("light", "Light Mode"),
]


# =============================================================================
# System Color Scheme Detection (Fedora FreeDesktop Portal, GTK 3 & QStyleHints)
# =============================================================================


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


def is_system_dark_mode() -> bool:
    """
    Detects if the desktop environment is currently in dark mode.
    Prioritises in-process Qt/D-Bus checks without spawning child processes.
    """
    # 1. Check explicit GTK_THEME environment variable override
    env_gtk_theme = os.environ.get("GTK_THEME", "").lower()
    if env_gtk_theme:
        if (
            ":dark" in env_gtk_theme
            or "-dark" in env_gtk_theme
            or env_gtk_theme.endswith("dark")
        ):
            return True
        elif ":light" in env_gtk_theme or "-light" in env_gtk_theme:
            return False

    # 2. In-Process FreeDesktop Portal Query via QtDBus (0=default, 1=dark, 2=light)
    if HAS_QTDBUS:
        try:
            bus = QDBusConnection.sessionBus()
            if bus.isConnected():
                # Attempt modern ReadOne, then fall back to standard Read. Each
                # call has an explicit deadline and no introspection round-trip.
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
            # Desktop portals are optional and may reject a settings query while
            # starting. Continue through the deterministic fallback chain, but
            # retain a diagnostic for support investigations.
            LOGGER.debug(
                "Could not read the portal color-scheme setting.", exc_info=True
            )

    # 3. Qt 6 QStyleHints (native Wayland / X11 colorScheme)
    app = QGuiApplication.instance()
    if app and hasattr(app, "styleHints"):
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        elif scheme == Qt.ColorScheme.Light:
            return False

    # 4. Synchronous one-shot fallback: GNOME gsettings
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
            elif val in ("prefer-light", "light"):
                return False
            elif val == "default":
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

    # 5. In-process GTK 3 Configuration File Read (~/.config/gtk-3.0/settings.ini)
    gtk3_ini_paths = [
        os.path.expanduser("~/.config/gtk-3.0/settings.ini"),
        "/etc/gtk-3.0/settings.ini",
    ]
    for ini_path in gtk3_ini_paths:
        if os.path.isfile(ini_path):
            try:
                cp = configparser.ConfigParser(interpolation=None)
                cp.read(ini_path, encoding="utf-8")
                if cp.has_section("Settings"):
                    prefer_dark = cp.get(
                        "Settings", "gtk-application-prefer-dark-theme", fallback=""
                    ).lower()
                    if prefer_dark in ("1", "true", "yes"):
                        return True
                    gtk_theme = cp.get(
                        "Settings", "gtk-theme-name", fallback=""
                    ).lower()
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

    # 6. In-process KDE Plasma Configuration Read (~/.config/kdeglobals)
    kde_globals = os.path.expanduser("~/.config/kdeglobals")
    if os.path.isfile(kde_globals):
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(kde_globals, encoding="utf-8")
            if cp.has_section("General"):
                color_scheme = cp.get("General", "ColorScheme", fallback="").lower()
                if "dark" in color_scheme:
                    return True
        except (OSError, configparser.Error, UnicodeError):
            LOGGER.debug(
                "Could not parse KDE theme settings from %s.",
                kde_globals,
                exc_info=True,
            )

    return False


# =============================================================================
# Dynamic QSS Stylesheet Builder (Elevated Modern Desktop Theme)
# =============================================================================

# QSS can only paint a ::indicator marker from an image file, so the two glyphs
# the form controls need are written into a per-user cache directory once and
# referenced by path. They are generated rather than shipped so the marker
# colour always matches the resolved palette.
_CHECK_MARKER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
    'viewBox="0 0 16 16"><path d="M3.5 8.4 L6.4 11.2 L12.5 4.9" '
    'fill="none" stroke="{stroke}" stroke-width="2.1" stroke-linecap="round" '
    'stroke-linejoin="round"/></svg>'
)
_DOT_MARKER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
    'viewBox="0 0 16 16"><circle cx="8" cy="8" r="3.4" fill="{stroke}"/></svg>'
)
_CHEVRON_MARKER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" '
    'viewBox="0 0 12 12"><path d="M2.5 4.4 L6 8 L9.5 4.4" '
    'fill="none" stroke="{stroke}" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"/></svg>'
)


def _control_assets(
    on_accent_colour: str,
    muted_colour: str,
    on_input_colour: str,
    accent_colour: str,
) -> tuple[str, str, str, str, str]:
    """Return the marker paths ``(check, muted_check, radio, muted_radio, chevron)``.

    The three markers cannot share a colour: the checkmark sits on the accent
    fill, the disabled checkmark sits on the muted well, and the drop-down
    chevron sits on the input background. Using one stroke colour made the
    chevron vanish into the input on the dark palette.

    Written atomically under the user cache directory so repeated theme
    switches reuse the same small files instead of leaving a trail of them.
    Any failure returns five empty strings, which simply drops the marker
    images and leaves the solid indicator fills in place; callers can rely on
    the shape of the result no matter how hostile the cache directory is.
    """
    base = (
        Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        / "dendro"
        / "control-assets"
    )
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "", "", "", "", ""
    written: list[str] = []
    for name, template, stroke_colour in (
        ("check", _CHECK_MARKER_SVG, on_accent_colour),
        ("check-muted", _CHECK_MARKER_SVG, muted_colour),
        # The radio dot sits inside an input-coloured well, so it is drawn in
        # the accent itself; accent_text would disappear into that well.
        ("radio", _DOT_MARKER_SVG, accent_colour),
        ("radio-muted", _DOT_MARKER_SVG, muted_colour),
        ("chevron", _CHEVRON_MARKER_SVG, on_input_colour),
    ):
        target = base / f"{name}-{stroke_colour.lstrip('#')}.svg"
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            try:
                temporary.write_text(
                    template.format(stroke=stroke_colour), encoding="utf-8"
                )
                temporary.replace(target)
            except OSError:
                continue
        written.append(target.as_posix())
    if len(written) != 5:
        return "", "", "", "", ""
    return tuple(written)  # type: ignore[return-value]


def build_stylesheet(c: dict[str, str]) -> str:
    """Generates the polished application-wide Qt stylesheet."""
    (
        check_svg,
        check_muted_svg,
        radio_svg,
        radio_muted_svg,
        chevron_svg,
    ) = _control_assets(
        c["accent_text"], c["text_dim"], c["text_secondary"], c["accent"]
    )

    # An ``image: url()`` with no path is invalid QSS and can stop the whole
    # indicator block applying, so each marker declaration is emitted as an
    # empty string when its asset could not be written.
    def marker(path: str) -> str:
        """Return a complete ``image:`` declaration, or nothing at all."""
        return f"    image: url({path});\n" if path else ""

    check_image = marker(check_svg)
    check_muted_image = marker(check_muted_svg)
    radio_image = marker(radio_svg)
    radio_muted_image = marker(radio_muted_svg)
    chevron_image = marker(chevron_svg)
    return f"""
/* Global Reset & Base Typography

   This rule must NOT set a background. A universal `QWidget` background makes
   Qt paint a filled rectangle behind every child label, checkbox and inline
   widget, including ones that should be transparent on a card. The result was
   text sitting on visibly darker "navy" patched rectangles wherever a card
   surface differed from the window surface. Containers opt into a background
   through their own named rules below. */
QWidget {{
    background: transparent;
    color: {c["text_primary"]};
    font-family: "Cantarell", "Inter", "Segoe UI", "system-ui", sans-serif;
    selection-background-color: {c["bg_selected"]};
    selection-color: {c["accent"]};
}}

/* Top-level surfaces paint the window colour; children stay transparent. */
QMainWindow, QDialog {{
    background-color: {c["bg_base"]};
}}

/* Dialog Windows */
QDialog {{
    background-color: {c["bg_base"]};
    color: {c["text_primary"]};
}}

/* Tooltips (Synchronized with Active Theme) */
QToolTip {{
    background-color: {c["bg_card"]};
    color: {c["text_primary"]};
    border: 1px solid {c["border"]};
    border-radius: 4px;
    padding: 5px 9px;
}}

/* --------------------------------------------------------------------------
   Global Button Sizing & Appearance (Dialogs, Drawers & Windows)
   -------------------------------------------------------------------------- */
QPushButton {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    color: {c["text_primary"]};
    padding: 6px 14px;
    min-height: 28px;
    font-weight: 600;
}}

QPushButton:hover {{
    background-color: {c["bg_hover"]};
    border-color: {c["border"]};
    color: {c["text_primary"]};
}}

/* Keyboard focus must be visible, including on the default button of a
   confirmation dialog: a user who tabs to an answer has to be able to see
   which button Enter will press. */
QPushButton:focus {{
    border: 2px solid {c["accent"]};
    padding: 5px 13px;
}}

QPushButton:pressed {{
    background-color: {c["bg_selected"]};
}}

QPushButton:disabled {{
    background-color: {c["bg_input"]};
    border-color: {c["border_subtle"]};
    color: {c["text_dim"]};
}}

/* Primary Accent Action Button (Apply, Commit, Proceed, Undo) */
QPushButton#ApplyButton {{
    background-color: {c["accent"]};
    color: {c["accent_text"]};
    border: 1px solid {c["accent"]};
    border-radius: 6px;
    padding: 5px 16px;
    min-height: 26px;
    font-weight: 700;
}}

/* A destructive confirmation must not wear the same blue as a routine
   "apply". Used for a DNF5 plan that removes protected system packages. */
QPushButton#DangerButton {{
    background-color: {c["badge_fg_missing"]};
    color: {c["bg_surface"]};
    border: 1px solid {c["badge_fg_missing"]};
    border-radius: 6px;
    padding: 5px 16px;
    min-height: 26px;
    font-weight: 700;
}}

QPushButton#DangerButton:hover {{
    background-color: {c["badge_border_missing"]};
    border-color: {c["badge_border_missing"]};
    color: {c["bg_surface"]};
}}

QPushButton#DangerButton:disabled {{
    background-color: {c["bg_hover"]};
    border-color: {c["border_subtle"]};
    color: {c["text_dim"]};
}}

QPushButton#ApplyButton:hover {{
    background-color: {c["accent_hover"]};
    border-color: {c["accent_hover"]};
    color: {c["accent_text"]};
}}

QPushButton#ApplyButton:disabled {{
    background-color: {c["bg_hover"]};
    border-color: {c["border_subtle"]};
    color: {c["text_dim"]};
}}

/* --------------------------------------------------------------------------
   Top Header & Control Toolbar
   -------------------------------------------------------------------------- */
QWidget#HeaderContainer {{
    background-color: {c["bg_surface"]};
    border-bottom: 1px solid {c["border"]};
    min-height: 48px;
}}

/* Compact Toolbar Sizing Specifically for Header Bar */
QWidget#HeaderContainer QPushButton {{
    min-height: 26px;
    max-height: 26px;
    border-radius: 6px;
    padding: 0px 10px;
    font-weight: 600;
}}

QWidget#HeaderContainer QPushButton#ApplyButton {{
    padding: 0px 14px;
    font-weight: 700;
}}

QWidget#HeaderContainer QPushButton#HeaderIconBtn {{
    padding: 0px;
}}

QLineEdit#SearchBar {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    padding: 0px 10px;
    min-height: 26px;
    max-height: 26px;
    color: {c["text_primary"]};
    selection-background-color: {c["accent"]};
    selection-color: {c["accent_text"]};
}}

QLineEdit#SearchBar:hover {{
    border: 1px solid {c["text_dim"]};
}}

QLineEdit#SearchBar:focus {{
    border: 1px solid {c["accent"]};
    background-color: {c["bg_input"]};
}}

/* Real-Time Filter Syntax Validation Feedback */
QLineEdit#SearchBar[filterState="valid"] {{
    border: 1px solid {c["badge_fg_installed"]};
}}

QLineEdit#SearchBar[filterState="invalid"] {{
    border: 1px solid {c["badge_fg_tag"]};
}}

/* Header Secondary Tool Buttons (Grouped Cluster) */
QPushButton#HeaderToolBtn {{
    background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    color: {c["text_secondary"]};
    padding: 0px 12px;
    font-weight: 600;
}}

/* Header Compact Icon-Only Buttons (Sidebar Toggle & Theme Toggle) */
QPushButton#HeaderIconBtn {{
    background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    color: {c["text_secondary"]};
    padding: 0px;
}}

QPushButton#HeaderToolBtn:hover,
QPushButton#HeaderIconBtn:hover {{
    background-color: {c["bg_hover"]};
    border: 1px solid {c["border"]};
    color: {c["text_primary"]};
}}

QPushButton#HeaderToolBtn:pressed,
QPushButton#HeaderIconBtn:pressed {{
    background-color: {c["bg_selected"]};
}}

/* Dynamic Update Indicator Button */
QPushButton#UpdatesIndicatorBtn {{
    background-color: {c["badge_bg_queued_in"]};
    border: 1px solid {c["badge_border_queued_in"]};
    color: {c["badge_fg_queued_in"]};
    font-weight: 700;
    padding: 0px 12px;
}}

QPushButton#UpdatesIndicatorBtn:hover {{
    background-color: {c["badge_fg_queued_in"]};
    color: {c["bg_base"]};
}}

/* --------------------------------------------------------------------------
   Host Mode Safety Banner (Fedora Atomic / Read-Only /usr / Container)
   -------------------------------------------------------------------------- */
QFrame#HostModeBanner {{
    background-color: {c["bg_card"]};
    border: none;
    border-bottom: 1px solid {c["border"]};
}}

QLabel#HostModeBannerTitle {{
    background: transparent;
    color: {c["text_primary"]};
    font-weight: 700;
}}

QLabel#HostModeBannerDetail {{
    background: transparent;
    color: {c["text_secondary"]};
}}

QLabel#HostModeBannerStatus {{
    background: transparent;
    color: {c["accent"]};
    font-weight: 600;
}}

/* --------------------------------------------------------------------------
   Sidebar Navigation
   -------------------------------------------------------------------------- */
QListWidget#SidebarList {{
    background-color: {c["bg_surface"]};
    border: none;
    border-right: 1px solid {c["border"]};
    padding: 8px 6px;
    outline: none;
}}

QListWidget#SidebarList::item {{
    border: none;
    border-radius: 6px;
}}

/* --------------------------------------------------------------------------
   Package Tree View (Explicit Viewport & Item Styling)
   -------------------------------------------------------------------------- */
QTreeView, QTreeView#PackageTreeView {{
    background-color: {c["bg_base"]};
    alternate-background-color: {c["bg_base"]};
    color: {c["text_primary"]};
    border: none;
    outline: none;
    padding: 0px;
    show-decoration-selected: 1;
}}

QTreeView::item {{
    background-color: transparent;
    color: {c["text_primary"]};
    border: none;
    border-radius: 4px;
}}

QTreeView::item:hover {{
    background-color: {c["bg_hover"]};
    color: {c["text_primary"]};
}}

QTreeView::item:selected {{
    background-color: {c["bg_selected"]};
    color: {c["accent"]};
}}

QHeaderView::section {{
    background-color: {c["bg_surface"]};
    color: {c["text_secondary"]};
    padding: 8px 12px;
    border: none;
    border-bottom: 1px solid {c["border"]};
    border-right: 1px solid {c["border_subtle"]};
    font-weight: 700;
    letter-spacing: 0.3px;
    text-transform: uppercase;
}}

QHeaderView::section:hover {{
    background-color: {c["bg_hover"]};
    color: {c["accent"]};
}}

/* --------------------------------------------------------------------------
   Inspector Side Panel & Detail Dashboard
   -------------------------------------------------------------------------- */
QWidget#InspectorPanel {{
    background-color: {c["bg_surface"]};
    border-left: 1px solid {c["border"]};
}}

/* The transaction drawer is a plain QWidget inside the workspace splitter, so
   it is not a window and does not inherit a window surface. Without a rule of
   its own its background stayed unpainted. */
QWidget#TransactionDrawer {{
    background-color: {c["bg_surface"]};
    border-left: 1px solid {c["border"]};
}}

QLabel#InspectorPkgTitle {{
    font-weight: 800;
    color: {c["text_primary"]};
}}

/* --------------------------------------------------------------------------
   Inspector metadata card

   Three labelled groups of field/value cells. The field name is small, muted
   and uppercase; the value carries full contrast, so the card reads as a table
   rather than as one run of "Label: value" sentences.
   -------------------------------------------------------------------------- */
QFrame#StatGridCard {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QLabel#StatGroupTitle {{
    color: {c["text_dim"]};
    font-weight: 700;
    letter-spacing: 0.6px;
    text-transform: uppercase;
}}

QLabel#StatFieldName {{
    color: {c["text_dim"]};
    font-weight: 600;
    letter-spacing: 0.3px;
}}

QLabel#StatFieldValue {{
    color: {c["text_primary"]};
    font-weight: 600;
}}

/* A value the host did not report is muted, so an absent fact does not read as
   an emphatic one. */
QLabel#StatFieldValue[muted="true"] {{
    color: {c["text_dim"]};
    font-weight: 500;
}}

/* The update cell doubles as a status chip. The frame carries the fill and the
   outline; the text label inside it stays transparent. The chip's padding is
   layout margin in code, not stylesheet padding: padding here would not feed the
   label's size hint, so the text would measure one line and paint two. */
QFrame#StatStatusChip {{
    border: 1px solid transparent;
    border-radius: 6px;
    background: transparent;
}}

QFrame#StatStatusChip[tone="current"] {{
    background-color: {c["badge_bg_installed"]};
    border: 1px solid {c["badge_border_installed"]};
}}

QFrame#StatStatusChip[tone="current"] QLabel#StatFieldValue {{
    color: {c["badge_fg_installed"]};
}}

QFrame#StatStatusChip[tone="update"] {{
    background-color: {c["badge_bg_update"]};
    border: 1px solid {c["badge_border_update"]};
}}

QFrame#StatStatusChip[tone="update"] QLabel#StatFieldValue {{
    color: {c["badge_fg_update"]};
}}

/* The chip must stay outlined even when the tone resolves to no fill, or an
   unknown state would read as floating text rather than as a status. */
QFrame#StatStatusChip[tone="unknown"] {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
}}

QFrame#StatStatusChip[tone="unknown"] QLabel#StatFieldValue {{
    color: {c["text_dim"]};
}}

/* Rule between the card's groups. Matches InspectorCardDivider above: the
   subtle border is within 1.0 contrast of the card surface on dark and reads as
   nothing at all, so the regular border carries these hairlines. */
QFrame#StatSeparator {{
    background-color: {c["border"]};
    border: none;
}}

/* --------------------------------------------------------------------------
   Inspector section cards
   -------------------------------------------------------------------------- */
QFrame#InspectorSectionCard {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QLabel#InspectorSectionTitle {{
    color: {c["text_dim"]};
    font-weight: 700;
    letter-spacing: 0.5px;
    text-transform: uppercase;
}}

/* A hairline separator inside a card, used to set the assessment apart from
   the justification beneath it without introducing a second bold heading. */
QFrame#InspectorCardDivider {{
    background-color: {c["border"]};
    border: none;
}}

QLabel#InspectorBasisLabel {{
    color: {c["text_secondary"]};
    font-weight: 600;
}}

QLabel#InspectorPackagerLabel {{
    color: {c["text_dim"]};
}}

/* Centred note shown while a tab's content surface has nothing in it yet. */
QLabel#InspectorEmptyState {{
    color: {c["text_dim"]};
    padding: 24px 16px;
}}

QLabel#InspectorSummary {{
    color: {c["text_secondary"]};
    line-height: 1.4;
}}

QLabel#AIRationaleLabel {{
    color: {c["text_secondary"]};
    line-height: 1.4;
}}

/* Inspector - Tabs & Pages */
QTabWidget#InspectorTabs::pane {{
    border: 1px solid {c["border"]};
    border-radius: 6px;
    background-color: {c["bg_card"]};
}}

QTabBar::tab {{
    background-color: {c["bg_input"]};
    color: {c["text_secondary"]};
    padding: 7px 14px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
    font-weight: 600;
    text-transform: none;
    letter-spacing: 0px;
}}

/* The inspector is a narrow, resizable dock. Its five tabs must keep their
   complete labels at the dock's 360px minimum width instead of collapsing
   into unreadable stubs behind scroll arrows. */
/* No font-size here on purpose. Pinning a point size in the stylesheet makes
   Qt size the tab strips from the stylesheet font while painting them with the
   application font, so the two disagree and the labels elide. The tab bar is
   given one concrete QFont in code instead, and the dock minimum width is
   derived from that same font's metrics. */
QTabWidget#InspectorTabs > QTabBar::tab {{
    padding: 6px 8px;
    margin-right: 2px;
    font-weight: 600;
    text-transform: none;
    letter-spacing: 0px;
}}

QTabBar::tab:selected {{
    background-color: {c["bg_card"]};
    color: {c["accent"]};
    border-top: 2px solid {c["accent"]};
}}

QTextEdit#InspectorDescText {{
    background-color: {c["bg_card"]};
    border: none;
    color: {c["text_primary"]};
    line-height: 1.5;
}}

/* Every content surface on a tab is a rounded card, so switching tabs does not
   change the visual language of the dock. */
/* The manifest is one card split into a scrolling path column and a pinned size
   column. The two views tile edge to edge: the left one keeps the card's left
   rounding, the right one its right rounding, and only the outer edges carry a
   border so the seam between them does not draw a second frame. */
QTableWidget#InspectorFilesTable {{
    background-color: {c["bg_card"]};
    alternate-background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    border-right: none;
    border-radius: 8px;
    border-top-right-radius: 0px;
    border-bottom-right-radius: 0px;
    color: {c["text_primary"]};
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
    gridline-color: transparent;
}}

/* The manifest's own scrollbars. The generic 6px bar is too small and too
   close to the card colour to read against this card, and the file list is the
   one place where the reader is genuinely scrolling a long list, so both axes
   get a wider track and a brighter handle.

   Both orientations are spelled out here, and with identical geometry and
   colours, because a rule that names only one axis leaves the other on the
   generic 6px/`border` styling beside it. That is what the caller saw after
   2.19.0: a wide bright bar next to a thin dim one, on the same card. Each
   orientation needs its own declaration -- `width` sizes a vertical bar and
   `height` sizes a horizontal one -- so the pair is kept symmetrical by hand
   and pinned by a test. Scoped to the path table so scrollbars elsewhere are
   untouched.

   The track is the card's own surface colour, and the 10px strip that carries
   each bar is filled with the same colour (see `InspectorFilesCorner` and
   `InspectorFilesFooter` below). A bar whose strip is a different shade reads
   as a bar floating in a hole, which is the same complaint in a different
   guise: one strip of the card looking unlike the rest of it. */
QTableWidget#InspectorFilesTable QScrollBar:vertical {{
    background: {c["bg_card"]};
    width: 10px;
    margin: 0px;
    border-radius: 5px;
}}

QTableWidget#InspectorFilesTable QScrollBar:horizontal {{
    background: {c["bg_card"]};
    height: 10px;
    margin: 0px;
    border-radius: 5px;
}}

QTableWidget#InspectorFilesTable QScrollBar::handle:vertical,
QTableWidget#InspectorFilesTable QScrollBar::handle:horizontal {{
    background-color: {c["text_dim"]};
    min-height: 28px;
    min-width: 28px;
    border-radius: 4px;
    margin: 2px;
}}

QTableWidget#InspectorFilesTable QScrollBar::handle:vertical:hover,
QTableWidget#InspectorFilesTable QScrollBar::handle:horizontal:hover {{
    background-color: {c["text_secondary"]};
}}

QTableWidget#InspectorFilesTable QScrollBar::add-line:vertical,
QTableWidget#InspectorFilesTable QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QTableWidget#InspectorFilesTable QScrollBar::add-line:horizontal,
QTableWidget#InspectorFilesTable QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

QTableView#InspectorFilesSizeView {{
    background-color: {c["bg_card"]};
    alternate-background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    border-left: none;
    border-radius: 8px;
    border-top-left-radius: 0px;
    border-bottom-left-radius: 0px;
    color: {c["text_primary"]};
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
    gridline-color: transparent;
}}

/* The bottom band of the manifest card, under both halves.

   The path table carries its horizontal scrollbar in the bottom 10px of its
   own frame. The Size column has no bar of its own -- it scrolls with the path
   column -- but the card has to end on one straight line, so the same 10px
   strip is reserved under the Size column too. That reserve used to be a bare
   QWidget, and a widget with no background renders as an unpainted hole: a
   black band under SIZE next to a themed bar strip, on a card whose whole point
   is that the two halves are one table.

   Both strips are filled with the card surface and closed with the same bottom
   border, so the band reads as one continuous piece of the card at every
   scroll position, whether or not either bar is currently scrollable. The
   corner between them -- the square where the two bars would meet -- is filled
   with the same colour for the same reason. */
QWidget#InspectorFilesCorner {{
    background-color: {c["bg_card"]};
    border: none;
}}

QWidget#InspectorFilesFooter {{
    background-color: {c["bg_card"]};
    border: none;
    border-bottom: 1px solid {c["border"]};
    border-radius: 0px;
    border-bottom-right-radius: 8px;
}}

QTableWidget#InspectorFilesTable QTableCornerButton::section,
QTableView#InspectorFilesSizeView QTableCornerButton::section {{
    background-color: {c["bg_card"]};
    border: none;
}}

QListWidget#InspectorReverseList {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    color: {c["text_primary"]};
    padding: 4px;
}}

/* The dependent list is the whole body of its tab, so its rows need to read as
   rows: separated, padded, and highlighted on hover rather than running together
   as one block of text. */
QListWidget#InspectorReverseList::item {{
    border-radius: 5px;
    padding: 6px 8px;
    margin: 1px 2px;
}}

QListWidget#InspectorReverseList::item:hover {{
    background-color: {c["bg_hover"]};
}}

QListWidget#InspectorReverseList::item:selected {{
    background-color: {c["bg_input"]};
    color: {c["accent"]};
}}

QTextBrowser#InspectorChangelogBrowser, QTextBrowser#InspectorScriptletsBrowser {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    color: {c["text_primary"]};
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
    padding: 8px;
}}

/* The advisory detail pane renders a description and a list of clickable
   references. It is its own rule rather than joining the console group below
   for two reasons: a console is monospaced and this is prose, and the anchors
   need an explicit colour. Qt's default link colour is pure #0000ff, which is
   1.71:1 against the dark card -- the same failure as text that is barely
   there. A Qt stylesheet cannot colour document anchors, so the document
   format itself sets the link colour from the theme accent (6.99:1 dark,
   5.45:1 light against the same surface) -- see
   ``SecurityAdvisoryDialog._detail_document``. */
QTextBrowser#AdvisoryDetail {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    color: {c["text_primary"]};
    padding: 8px;
    selection-background-color: {c["bg_selected"]};
    selection-color: {c["text_primary"]};
}}

/* Severity is carried by the row's text colour, which the table paints from
   the delegate palette; these rules only keep the table itself consistent with
   the other data tables in the window. */
QTableWidget#AdvisoryTable {{
    background-color: {c["bg_card"]};
    alternate-background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    gridline-color: transparent;
}}

/* Dynamic Theme-Adaptive Consoles & Terminal Outputs */
QTextEdit#ConsoleOutput,
QTextEdit#DryRunConsole,
QTextEdit#LocalRpmReqBox,
QTextEdit#TransactionPlanText {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    color: {c["text_primary"]};
}}

/* Theme-Adaptive Dialog, Drawer, Header & Inspector Semantic Labels */
QPushButton#HeaderDangerBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 10px;
    color: {c["badge_fg_missing"]};
    font-weight: 600;
}}

QPushButton#HeaderDangerBtn:hover {{
    background-color: {c["badge_bg_missing"]};
    border: 1px solid {c["badge_border_missing"]};
    color: {c["badge_fg_missing"]};
}}

QLabel#AboutFieldName {{
    font-weight: bold;
}}

QLabel#DialogTitleLabel {{
    color: {c["accent"]};
    font-weight: bold;
}}

QLabel#DrawerTitleLabel {{
    color: {c["accent"]};
    font-weight: bold;
}}

QLabel#DrawerSummaryLabel {{
    color: {c["text_secondary"]};
}}

QLabel#SubtleHelpLabel, QLabel#AIProvenanceLabel {{
    color: {c["text_secondary"]};
}}

QLabel#LocalRpmSummaryLabel {{
    color: {c["text_secondary"]};
}}

QLabel#LocalRpmStatusNotice[statusKind="installed"] {{
    background-color: {c["badge_bg_queued_in"]};
    border: 1px solid {c["badge_border_queued_in"]};
    color: {c["badge_fg_queued_in"]};
    border-radius: 6px;
    padding: 8px;
}}

QLabel#LocalRpmStatusNotice[statusKind="available"] {{
    background-color: {c["badge_bg_installed"]};
    border: 1px solid {c["badge_border_installed"]};
    color: {c["badge_fg_installed"]};
    border-radius: 6px;
    padding: 8px;
}}

/* Progress Bar */
QProgressBar {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 4px;
    text-align: center;
    color: {c["text_primary"]};
}}

QProgressBar::chunk {{
    background-color: {c["accent"]};
    border-radius: 3px;
}}

/* Cards & Frames */
QFrame#AICard, QFrame#StatsFrame {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

/* --------------------------------------------------------------------------
   Checkboxes and radio buttons

   The native Fusion indicator is drawn from palette roles Qt still resolves
   from a light scheme, so on a dark surface the unchecked box rendered as
   almost the same colour as its background (measured 1.05:1). The indicator is
   therefore drawn explicitly: an outlined well that reads at any font size,
   with a checkmark pushed in through an inline SVG image so the checked state
   cannot be confused with the unchecked one.
   -------------------------------------------------------------------------- */
QCheckBox, QRadioButton {{
    spacing: 8px;
    color: {c["text_primary"]};
    min-height: 22px;
}}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {c["border_strong"]};
    background-color: {c["bg_input"]};
}}

QCheckBox::indicator {{
    border-radius: 4px;
}}

QRadioButton::indicator {{
    border-radius: 8px;
}}

QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {c["accent"]};
    background-color: {c["bg_hover"]};
}}

QCheckBox::indicator:checked {{
    background-color: {c["accent"]};
    border-color: {c["accent"]};
{check_image}}}

QCheckBox::indicator:checked:hover {{
    background-color: {c["accent_hover"]};
    border-color: {c["accent_hover"]};
}}

/* A filled accent dot inside a ring, rather than a thick border which Qt
   renders as a square-cornered blob at this size. */
QRadioButton::indicator:checked {{
    border: 2px solid {c["accent"]};
    background-color: {c["bg_input"]};
{radio_image}}}

QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {c["border_subtle"]};
    background-color: {c["bg_base"]};
}}

QCheckBox::indicator:checked:disabled {{
    background-color: {c["bg_hover"]};
    border-color: {c["border_subtle"]};
{check_muted_image}}}

QRadioButton::indicator:checked:disabled {{
    border-color: {c["text_dim"]};
    background-color: {c["bg_input"]};
{radio_muted_image}}}

QCheckBox:disabled, QRadioButton:disabled {{
    color: {c["text_dim"]};
}}

/* --------------------------------------------------------------------------
   Drop-downs

   Without explicit sizing a QComboBox collapses to the font's own height
   (~22px) while every themed button is 34px, which read as a "too short"
   control. Fixed minimum height, explicit padding and a themed chevron keep it
   consistent with the buttons and inputs around it.
   -------------------------------------------------------------------------- */
QComboBox {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    color: {c["text_primary"]};
    padding: 0px 10px;
    min-height: 30px;
    min-width: 140px;
}}

QComboBox:hover {{
    border-color: {c["text_dim"]};
}}

QComboBox:focus {{
    border: 2px solid {c["accent"]};
    padding: 0px 9px;
}}

QComboBox:disabled {{
    background-color: {c["bg_base"]};
    border-color: {c["border_subtle"]};
    color: {c["text_dim"]};
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 26px;
    border-left: 1px solid {c["border"]};
}}

QComboBox::down-arrow {{
{chevron_image}    width: 12px;
    height: 12px;
}}

QComboBox QAbstractItemView {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    color: {c["text_primary"]};
    selection-background-color: {c["bg_selected"]};
    selection-color: {c["accent"]};
    padding: 4px;
    outline: none;
}}

/* Text inputs share the combo's height so a form never mixes tall and short
   controls on the same row. */
QLineEdit {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    color: {c["text_primary"]};
    padding: 0px 8px;
    min-height: 30px;
    selection-background-color: {c["accent"]};
    selection-color: {c["accent_text"]};
}}

QLineEdit:focus {{
    border: 2px solid {c["accent"]};
    padding: 0px 7px;
}}

QLineEdit:disabled {{
    background-color: {c["bg_base"]};
    border-color: {c["border_subtle"]};
    color: {c["text_dim"]};
}}

/* Repository Dialog capability and direct-configuration controls */
QFrame#CoprBox, QFrame#RepoAddBox {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QLineEdit#CoprInput, QLineEdit#RepoAddressInput, QLineEdit#RepoFilterInput {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    padding: 5px 12px;
    min-height: 26px;
    color: {c["text_primary"]};
    selection-background-color: {c["accent"]};
    selection-color: {c["accent_text"]};
}}

QLineEdit#CoprInput:hover, QLineEdit#RepoAddressInput:hover, QLineEdit#RepoFilterInput:hover {{
    border: 1px solid {c["text_dim"]};
}}

QLineEdit#CoprInput:focus, QLineEdit#RepoAddressInput:focus, QLineEdit#RepoFilterInput:focus {{
    border: 1px solid {c["accent"]};
    background-color: {c["bg_input"]};
}}

/* Compact Close Buttons */
QPushButton#InspectorCloseBtn, QPushButton#DrawerCloseBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    /* The generic QPushButton rule adds 14px horizontal padding and a 28px
       minimum height. Both would defeat the fixed 26-28px square these
       compact close buttons ask for, so the fallback glyph disappears. */
    padding: 0px;
    min-height: 0px;
    color: {c["text_dim"]};
    font-weight: 700;
}}

QPushButton#InspectorCloseBtn:hover, QPushButton#DrawerCloseBtn:hover {{
    background-color: {c["bg_hover"]};
    border: 1px solid {c["border"]};
    color: {c["text_primary"]};
}}

/* Dynamic Action Button in Inspector Panel */
QPushButton#InspectorQueueBtn[queueState="installed"] {{
    background-color: {c["badge_bg_queued_rm"]};
    color: {c["badge_fg_queued_rm"]};
    border: 1px solid {c["badge_border_queued_rm"]};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="queued_remove"] {{
    background-color: {c["bg_selected"]};
    color: {c["badge_fg_queued_in"]};
    border: 1px solid {c["border"]};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="queued_upgrade"] {{
    background-color: {c["bg_selected"]};
    color: {c["badge_fg_queued_in"]};
    border: 1px solid {c["border"]};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="available"] {{
    background-color: {c["badge_bg_installed"]};
    color: {c["badge_fg_installed"]};
    border: 1px solid {c["badge_border_installed"]};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="queued_install"] {{
    background-color: {c["bg_selected"]};
    color: {c["badge_fg_queued_in"]};
    border: 1px solid {c["border"]};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

/* --------------------------------------------------------------------------
   Tables (Repo Dialog, History Dialog, Files)
   -------------------------------------------------------------------------- */
QTableWidget {{
    background-color: {c["bg_card"]};
    /* Qt's default alternate-row colour is a fixed light grey. Without this,
       alternating rows of a Dark-mode table render as light grey strips with
       near-white text. */
    alternate-background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    color: {c["text_primary"]};
    gridline-color: {c["border_subtle"]};
}}

QTableWidget QTableCornerButton::section {{
    background-color: {c["bg_surface"]};
    border: none;
}}

/* Nine metadata columns must fit the Flatpak dialog's default width without
   cutting the last heading, so this table uses tighter cell padding. */
QTableWidget#FlatpakInventoryTable::item {{
    padding: 6px 7px;
}}

QTableWidget#FlatpakInventoryTable QHeaderView::section {{
    padding: 8px 7px;
}}

QTableWidget::item {{
    padding: 6px 10px;
}}

QTableWidget::item:selected {{
    background-color: {c["bg_selected"]};
    color: {c["accent"]};
}}

/* --------------------------------------------------------------------------
   Scrollbars (Minimal Floating Rounded Thumb)
   -------------------------------------------------------------------------- */
QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 2px 0px 2px 0px;
    border-radius: 3px;
}}

QScrollBar::handle:vertical {{
    background-color: {c["border"]};
    min-height: 28px;
    border-radius: 3px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {c["text_dim"]};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
    margin: 0px 2px 0px 2px;
    border-radius: 3px;
}}

QScrollBar::handle:horizontal {{
    background-color: {c["border"]};
    min-width: 28px;
    border-radius: 3px;
}}

QScrollBar::handle:horizontal:hover {{
    background-color: {c["text_dim"]};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

/* --------------------------------------------------------------------------
   Menus & Popup Overlays
   -------------------------------------------------------------------------- */
QMenu {{
    background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    padding: 6px;
}}

QMenu::item {{
    padding: 7px 24px 7px 12px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background-color: {c["bg_selected"]};
    color: {c["accent"]};
}}

/* --------------------------------------------------------------------------
   Status Bar & Splitters
   -------------------------------------------------------------------------- */
QStatusBar {{
    background-color: {c["bg_input"]};
    border-top: 1px solid {c["border"]};
    color: {c["text_dim"]};
    padding: 4px 8px;
}}

QSplitter::handle {{
    background-color: {c["border_subtle"]};
}}

QSplitter::handle:hover {{
    background-color: {c["accent"]};
}}
"""


# =============================================================================
# Public Theme API & Delegate Palette Generator
# =============================================================================


def get_resolved_theme_key(theme_choice: str) -> str:
    """Resolves theme choice to either 'dark' or 'light', aliasing legacy theme keys."""
    choice = (theme_choice or "auto").lower()
    if choice == "auto":
        return "dark" if is_system_dark_mode() else "light"
    # Backwards-compatible aliases for previously saved user settings
    if choice in ("dark", "mocha", "tokyo_night", "nord", "gruvbox"):
        return "dark"
    if choice in ("light", "latte", "solarized_light"):
        return "light"
    return "dark" if is_system_dark_mode() else "light"


def get_theme_stylesheet(theme_choice: str) -> str:
    """Retrieves the full stylesheet for a given theme choice."""
    key = get_resolved_theme_key(theme_choice)
    return build_stylesheet(THEMES_CONFIG[key])


def get_delegate_palette(theme_choice: str) -> dict[str, QColor]:
    """Generates QColor palette objects used by delegates and custom widgets."""
    key = get_resolved_theme_key(theme_choice)
    c = THEMES_CONFIG[key]
    return {
        "bg_base": QColor(c["bg_base"]),
        "bg_surface": QColor(c["bg_surface"]),
        "bg_card": QColor(c["bg_card"]),
        "bg_hover": QColor(c["bg_hover"]),
        "bg_selected": QColor(c["bg_selected"]),
        "text_main": QColor(c["text_primary"]),
        "text_secondary": QColor(c["text_secondary"]),
        "text_dim": QColor(c["text_dim"]),
        "accent": QColor(c["accent"]),
        "accent_text": QColor(c["accent_text"]),
        "border": QColor(c["border"]),
        "border_subtle": QColor(c["border_subtle"]),
        "badge_bg_installed": QColor(c["badge_bg_installed"]),
        "badge_fg_installed": QColor(c["badge_fg_installed"]),
        "badge_border_installed": QColor(c["badge_border_installed"]),
        "badge_bg_update": QColor(c["badge_bg_update"]),
        "badge_fg_update": QColor(c["badge_fg_update"]),
        "badge_border_update": QColor(c["badge_border_update"]),
        "badge_bg_missing": QColor(c["badge_bg_missing"]),
        "badge_fg_missing": QColor(c["badge_fg_missing"]),
        "badge_border_missing": QColor(c["badge_border_missing"]),
        "badge_bg_queued_in": QColor(c["badge_bg_queued_in"]),
        "badge_fg_queued_in": QColor(c["badge_fg_queued_in"]),
        "badge_border_queued_in": QColor(c["badge_border_queued_in"]),
        "badge_bg_queued_rm": QColor(c["badge_bg_queued_rm"]),
        "badge_fg_queued_rm": QColor(c["badge_fg_queued_rm"]),
        "badge_border_queued_rm": QColor(c["badge_border_queued_rm"]),
        "badge_bg_tag": QColor(c["badge_bg_tag"]),
        "badge_fg_tag": QColor(c["badge_fg_tag"]),
        "badge_border_tag": QColor(c["badge_border_tag"]),
    }
