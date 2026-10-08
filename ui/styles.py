# dendro/ui/styles.py
"""
Theme palettes, dynamic QSS builder, and desktop portal color scheme detection for Dendro.
Provides curated high-contrast dark and light color palettes with a modern, elevated UI design system.
Explicitly styles tree views, viewports, headers, sidebars, and cards to prevent host theme bleed.
Zero emoji glyphs and zero color emoji font chains to prevent Fontconfig crashes.
"""
from __future__ import annotations

import configparser
import os
import shutil
import subprocess
from typing import Dict, Final, List, Optional, Tuple
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QGuiApplication

# =============================================================================
# Theme Color Palettes (Curated High-Contrast Dark & Light Themes)
# =============================================================================

THEMES_CONFIG: Final[Dict[str, Dict[str, str]]] = {
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
        "text_dim": "#6c7086",
        "border": "#313244",
        "border_subtle": "#252739",
        "accent": "#89b4fa",
        "accent_hover": "#b4befe",
        "accent_text": "#11111b",
        "badge_bg_installed": "#1e3a2f",
        "badge_fg_installed": "#a6e3a1",
        "badge_border_installed": "#2d5a47",
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
    # 2. Light Theme (Clean High-Contrast Light)
    "light": {
        "name": "Light",
        "is_dark": "false",
        "bg_base": "#eff1f5",
        "bg_surface": "#ffffff",
        "bg_card": "#f8f9fc",
        "bg_input": "#e6e9ef",
        "bg_hover": "#e2e5ec",
        "bg_selected": "#dce0e8",
        "text_primary": "#4c4f69",
        "text_secondary": "#5c5f77",
        "text_dim": "#8c8fa1",
        "border": "#ccd0da",
        "border_subtle": "#e6e9ef",
        "accent": "#1e66f5",
        "accent_hover": "#04a5e5",
        "accent_text": "#ffffff",
        "badge_bg_installed": "#eaf5ed",
        "badge_fg_installed": "#2d7d1e",
        "badge_border_installed": "#bce3c5",
        "badge_bg_missing": "#fdecee",
        "badge_fg_missing": "#d20f39",
        "badge_border_missing": "#f8bac4",
        "badge_bg_queued_in": "#fff4e6",
        "badge_fg_queued_in": "#df8e1d",
        "badge_border_queued_in": "#fbd5a4",
        "badge_bg_queued_rm": "#fdecee",
        "badge_fg_queued_rm": "#e64553",
        "badge_border_queued_rm": "#f8bac4",
        "badge_bg_tag": "#f3effa",
        "badge_fg_tag": "#7c35dd",
        "badge_border_tag": "#dfd2f6",
    },
}

THEME_DISPLAY_OPTIONS: Final[List[Tuple[str, str]]] = [
    ("auto", "Follow System"),
    ("dark", "Dark Mode"),
    ("light", "Light Mode"),
]


# =============================================================================
# System Color Scheme Detection (Fedora FreeDesktop Portal, GTK 3 & QStyleHints)
# =============================================================================

def is_system_dark_mode() -> bool:
    """
    Detects if the desktop environment is currently in dark mode.
    Queries host desktop settings directly and synchronously so theme changes
    apply immediately without stale cache delays.
    """
    # 1. Check explicit GTK_THEME environment variable override
    env_gtk_theme = os.environ.get("GTK_THEME", "").lower()
    if env_gtk_theme:
        if ":dark" in env_gtk_theme or "-dark" in env_gtk_theme or env_gtk_theme.endswith("dark"):
            return True
        elif ":light" in env_gtk_theme or "-light" in env_gtk_theme:
            return False

    # 2. Query GNOME 42+ Color Scheme Preference directly via gsettings
    try:
        res = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=1
        )
        if res.returncode == 0:
            val = res.stdout.strip().strip("'\"").lower()
            if "prefer-dark" in val or val == "dark":
                return True
            elif "prefer-light" in val or val == "light":
                return False
    except Exception:
        pass

    # 3. Query FreeDesktop XDG Desktop Portal via gdbus (1=Dark, 2=Light)
    if shutil.which("gdbus"):
        try:
            portal_res = subprocess.run(
                [
                    "gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Desktop",
                    "--object-path", "/org/freedesktop/portal/desktop",
                    "--method", "org.freedesktop.portal.Settings.Read",
                    "org.freedesktop.appearance", "color-scheme"
                ],
                capture_output=True, text=True, timeout=1
            )
            if portal_res.returncode == 0:
                out = portal_res.stdout.strip()
                if "uint32 1" in out:
                    return True
                elif "uint32 2" in out:
                    return False
        except Exception:
            pass

    # 4. Check GTK 3 Theme via gsettings (e.g. 'Adwaita-dark', 'Breeze-Dark', 'Yaru-dark')
    try:
        res_gtk = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
            capture_output=True, text=True, timeout=1
        )
        if res_gtk.returncode == 0:
            theme_str = res_gtk.stdout.strip().strip("'\"").lower()
            if "-dark" in theme_str or theme_str.endswith("dark") or "dark" in theme_str:
                return True
    except Exception:
        pass

    # 5. Check GTK 3 Configuration Files (~/.config/gtk-3.0/settings.ini)
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
                    prefer_dark = cp.get("Settings", "gtk-application-prefer-dark-theme", fallback="").lower()
                    if prefer_dark in ("1", "true", "yes"):
                        return True
                    gtk_theme = cp.get("Settings", "gtk-theme-name", fallback="").lower()
                    if "-dark" in gtk_theme or gtk_theme.endswith("dark") or "dark" in gtk_theme:
                        return True
            except Exception:
                pass

    # 6. Check XFCE Desktop Theme (xfconf-query)
    if shutil.which("xfconf-query"):
        try:
            xf_res = subprocess.run(
                ["xfconf-query", "-c", "xsettings", "-p", "/Net/ThemeName"],
                capture_output=True, text=True, timeout=1
            )
            if xf_res.returncode == 0:
                xf_theme = xf_res.stdout.strip().lower()
                if "-dark" in xf_theme or xf_theme.endswith("dark") or "dark" in xf_theme:
                    return True
        except Exception:
            pass

    # 7. Check KDE Plasma Configuration (~/.config/kdeglobals)
    kde_globals = os.path.expanduser("~/.config/kdeglobals")
    if os.path.isfile(kde_globals):
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(kde_globals, encoding="utf-8")
            if cp.has_section("General"):
                color_scheme = cp.get("General", "ColorScheme", fallback="").lower()
                if "dark" in color_scheme:
                    return True
        except Exception:
            pass

    # 8. Check Qt 6 QStyleHints
    app = QGuiApplication.instance()
    if app and hasattr(app, "styleHints"):
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        elif scheme == Qt.ColorScheme.Light:
            return False

    # Default fallback: If no dark setting is active across GNOME/GTK/KDE, it is Light mode
    return False
    env_gtk_theme = os.environ.get("GTK_THEME", "").lower()
    if env_gtk_theme:
        if ":dark" in env_gtk_theme or "-dark" in env_gtk_theme or env_gtk_theme.endswith("dark"):
            return True
        elif ":light" in env_gtk_theme or "-light" in env_gtk_theme:
            return False

    # 3. GNOME 42+ Color Scheme Preference (libadwaita portal)
    try:
        res = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=1
        )
        val = res.stdout.lower()
        if "prefer-dark" in val or "dark" in val:
            return True
        elif "prefer-light" in val:
            return False
        # If 'default', do not assume light mode; fall through to inspect GTK 3 theme settings!
    except Exception:
        pass

    # 4. GTK 3 System Theme via gsettings (e.g. 'Adwaita-dark', 'Arc-Dark', 'Breeze-Dark')
    try:
        res_gtk = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
            capture_output=True, text=True, timeout=1
        )
        theme_str = res_gtk.stdout.strip().strip("'\"").lower()
        if "-dark" in theme_str or theme_str.endswith("dark") or "dark" in theme_str:
            return True
    except Exception:
        pass

    # 5. GTK 3 Configuration Files (~/.config/gtk-3.0/settings.ini)
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
                    prefer_dark = cp.get("Settings", "gtk-application-prefer-dark-theme", fallback="").lower()
                    if prefer_dark in ("1", "true", "yes"):
                        return True
                    gtk_theme = cp.get("Settings", "gtk-theme-name", fallback="").lower()
                    if "-dark" in gtk_theme or gtk_theme.endswith("dark") or "dark" in gtk_theme:
                        return True
            except Exception:
                pass

    # 6. XFCE Desktop Theme (xfconf-query)
    if shutil.which("xfconf-query"):
        try:
            xf_res = subprocess.run(
                ["xfconf-query", "-c", "xsettings", "-p", "/Net/ThemeName"],
                capture_output=True, text=True, timeout=1
            )
            xf_theme = xf_res.stdout.strip().lower()
            if "-dark" in xf_theme or xf_theme.endswith("dark") or "dark" in xf_theme:
                return True
        except Exception:
            pass

    # 7. KDE Plasma Configuration (~/.config/kdeglobals)
    kde_globals = os.path.expanduser("~/.config/kdeglobals")
    if os.path.isfile(kde_globals):
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(kde_globals, encoding="utf-8")
            if cp.has_section("General"):
                color_scheme = cp.get("General", "ColorScheme", fallback="").lower()
                if "dark" in color_scheme:
                    return True
        except Exception:
            pass

    # Fallback default: Dark mode
    return True


# =============================================================================
# Dynamic QSS Stylesheet Builder (Elevated Modern Desktop Theme)
# =============================================================================

def build_stylesheet(c: Dict[str, str]) -> str:
    """Generates the polished application-wide Qt stylesheet."""
    return f"""
/* Global Reset & Base Typography */
QWidget {{
    background-color: {c['bg_base']};
    color: {c['text_primary']};
    font-family: "Cantarell", "Inter", "Segoe UI", "system-ui", sans-serif;
    selection-background-color: {c['bg_selected']};
    selection-color: {c['accent']};
}}

/* Dialog Windows */
QDialog {{
    background-color: {c['bg_base']};
    color: {c['text_primary']};
}}

/* --------------------------------------------------------------------------
   Top Header & Control Toolbar
   -------------------------------------------------------------------------- */
QWidget#HeaderContainer {{
    background-color: {c['bg_surface']};
    border-bottom: 1px solid {c['border']};
    min-height: 48px;
}}

QLineEdit#SearchBar {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    border-radius: 18px;
    padding: 7px 16px;
    color: {c['text_primary']};
}}

QLineEdit#SearchBar:focus {{
    border: 1px solid {c['accent']};
    background-color: {c['bg_surface']};
}}

/* Header Secondary Tool Buttons (Grouped Cluster) */
QPushButton#HeaderToolBtn {{
    background-color: {c['bg_surface']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    color: {c['text_secondary']};
    padding: 6px 12px;
    font-weight: 600;
}}

QPushButton#HeaderToolBtn:hover {{
    background-color: {c['bg_hover']};
    border: 1px solid {c['border']};
    color: {c['text_primary']};
}}

QPushButton#HeaderToolBtn:pressed {{
    background-color: {c['bg_selected']};
}}

/* Dynamic Update Indicator Button */
QPushButton#UpdatesIndicatorBtn {{
    background-color: {c['badge_bg_queued_in']};
    border: 1px solid {c['badge_border_queued_in']};
    border-radius: 6px;
    color: {c['badge_fg_queued_in']};
    font-weight: 700;
    padding: 6px 14px;
}}

QPushButton#UpdatesIndicatorBtn:hover {{
    background-color: {c['badge_fg_queued_in']};
    color: {c['bg_base']};
}}

/* Primary Apply Transaction Button */
QPushButton#ApplyButton {{
    background-color: {c['accent']};
    color: {c['accent_text']};
    border: 1px solid {c['accent']};
    border-radius: 6px;
    padding: 7px 16px;
    font-weight: 700;
}}

QPushButton#ApplyButton:hover {{
    background-color: {c['accent_hover']};
    border-color: {c['accent_hover']};
}}

QPushButton#ApplyButton:disabled {{
    background-color: {c['bg_hover']};
    border-color: {c['border_subtle']};
    color: {c['text_dim']};
}}

/* --------------------------------------------------------------------------
   Sidebar Navigation
   -------------------------------------------------------------------------- */
QListWidget#SidebarList {{
    background-color: {c['bg_surface']};
    border: none;
    border-right: 1px solid {c['border']};
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
    background-color: {c['bg_base']};
    alternate-background-color: {c['bg_base']};
    color: {c['text_primary']};
    border: none;
    outline: none;
    padding: 0px;
    show-decoration-selected: 1;
}}

QTreeView::item {{
    background-color: transparent;
    color: {c['text_primary']};
    border: none;
    border-radius: 4px;
}}

QTreeView::item:hover {{
    background-color: {c['bg_hover']};
    color: {c['text_primary']};
}}

QTreeView::item:selected {{
    background-color: {c['bg_selected']};
    color: {c['accent']};
}}

QHeaderView::section {{
    background-color: {c['bg_surface']};
    color: {c['text_secondary']};
    padding: 8px 12px;
    border: none;
    border-bottom: 1px solid {c['border']};
    border-right: 1px solid {c['border_subtle']};
    font-weight: 700;
    letter-spacing: 0.3px;
    text-transform: uppercase;
}}

QHeaderView::section:hover {{
    background-color: {c['bg_hover']};
    color: {c['accent']};
}}

/* --------------------------------------------------------------------------
   Inspector Side Panel & Detail Dashboard
   -------------------------------------------------------------------------- */
QWidget#InspectorPanel {{
    background-color: {c['bg_surface']};
    border-left: 1px solid {c['border']};
}}

QLabel#InspectorPkgTitle {{
    font-size: 16px;
    font-weight: 800;
    color: {c['text_primary']};
}}

QLabel#InspectorSummary {{
    color: {c['text_secondary']};
    font-size: 12px;
    line-height: 1.4;
}}

/* Inspector - Metadata Chip Frame */
QFrame#MetaChipFrame {{
    background-color: {c['bg_card']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 8px;
}}

QLabel#ChipLabelTitle {{
    font-size: 10px;
    font-weight: 700;
    color: {c['text_dim']};
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}

QLabel#ChipLabelValue {{
    font-size: 12px;
    font-weight: 600;
    color: {c['text_primary']};
}}

/* Inspector - Safety Banner Card */
QFrame#SafetyBannerCard {{
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 10px;
    background-color: {c['bg_card']};
}}

QLabel#SafetyBadgeText {{
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.4px;
}}

QLabel#AIRationaleLabel {{
    color: {c['text_secondary']};
    font-size: 11px;
    line-height: 1.4;
}}

/* Inspector - Tabs & Pages */
QTabWidget#InspectorTabs::pane {{
    border: 1px solid {c['border']};
    border-radius: 6px;
    background-color: {c['bg_card']};
}}

QTabBar::tab {{
    background-color: {c['bg_input']};
    color: {c['text_secondary']};
    padding: 7px 14px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.3px;
}}

QTabBar::tab:selected {{
    background-color: {c['bg_card']};
    color: {c['accent']};
    border-top: 2px solid {c['accent']};
}}

QTextEdit#InspectorDescText {{
    background-color: {c['bg_card']};
    border: none;
    color: {c['text_primary']};
    line-height: 1.5;
}}

QTableWidget#InspectorFilesTable {{
    background-color: {c['bg_card']};
    border: none;
    color: {c['text_primary']};
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
}}

QListWidget#InspectorReverseList {{
    background-color: {c['bg_card']};
    border: none;
    color: {c['text_primary']};
}}

QTextBrowser#InspectorChangelogBrowser, QTextBrowser#InspectorScriptletsBrowser {{
    background-color: {c['bg_card']};
    border: none;
    color: {c['text_primary']};
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
    padding: 6px;
}}

/* Dynamic Theme-Adaptive Consoles & Terminal Outputs */
QTextEdit#ConsoleOutput, QTextEdit#DryRunConsole, QTextEdit#LocalRpmReqBox {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    color: {c['text_primary']};
}}

/* Progress Bar */
QProgressBar {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    border-radius: 4px;
    text-align: center;
    color: {c['text_primary']};
}}

QProgressBar::chunk {{
    background-color: {c['accent']};
    border-radius: 3px;
}}

/* Cards & Frames */
QFrame#AICard, QFrame#StatsFrame {{
    background-color: {c['bg_card']};
    border: 1px solid {c['border']};
    border-radius: 8px;
}}

/* Compact Close Buttons */
QPushButton#InspectorCloseBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 2px;
    color: {c['text_dim']};
}}

QPushButton#InspectorCloseBtn:hover {{
    background-color: {c['bg_hover']};
    border: 1px solid {c['border']};
    color: {c['text_primary']};
}}

/* Dynamic Action Button in Inspector Panel */
QPushButton#InspectorQueueBtn[queueState="installed"] {{
    background-color: {c['badge_bg_queued_rm']};
    color: {c['badge_fg_queued_rm']};
    border: 1px solid {c['badge_border_queued_rm']};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="queued_remove"] {{
    background-color: {c['bg_selected']};
    color: {c['badge_fg_queued_in']};
    border: 1px solid {c['border']};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="queued_upgrade"] {{
    background-color: {c['bg_selected']};
    color: {c['badge_fg_queued_in']};
    border: 1px solid {c['border']};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="available"] {{
    background-color: {c['badge_bg_installed']};
    color: {c['badge_fg_installed']};
    border: 1px solid {c['badge_border_installed']};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="queued_install"] {{
    background-color: {c['bg_selected']};
    color: {c['badge_fg_queued_in']};
    border: 1px solid {c['border']};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

/* --------------------------------------------------------------------------
   Tables (Repo Dialog, History Dialog, Files)
   -------------------------------------------------------------------------- */
QTableWidget {{
    background-color: {c['bg_card']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    color: {c['text_primary']};
    gridline-color: {c['border_subtle']};
}}

QTableWidget::item {{
    padding: 6px 10px;
}}

QTableWidget::item:selected {{
    background-color: {c['bg_selected']};
    color: {c['accent']};
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
    background-color: {c['border']};
    min-height: 28px;
    border-radius: 3px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {c['text_dim']};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    height: 0px;
}}

/* --------------------------------------------------------------------------
   Menus & Popup Overlays
   -------------------------------------------------------------------------- */
QMenu {{
    background-color: {c['bg_surface']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 6px;
}}

QMenu::item {{
    padding: 7px 24px 7px 12px;
    border-radius: 4px;
    font-size: 12px;
}}

QMenu::item:selected {{
    background-color: {c['bg_selected']};
    color: {c['accent']};
}}

/* --------------------------------------------------------------------------
   Status Bar & Splitters
   -------------------------------------------------------------------------- */
QStatusBar {{
    background-color: {c['bg_input']};
    border-top: 1px solid {c['border']};
    color: {c['text_dim']};
    padding: 4px 8px;
}}

QSplitter::handle {{
    background-color: {c['border_subtle']};
}}

QSplitter::handle:hover {{
    background-color: {c['accent']};
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


def get_delegate_palette(theme_choice: str) -> Dict[str, QColor]:
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
        "border": QColor(c["border"]),
        "border_subtle": QColor(c["border_subtle"]),
        "badge_bg_installed": QColor(c["badge_bg_installed"]),
        "badge_fg_installed": QColor(c["badge_fg_installed"]),
        "badge_border_installed": QColor(c["badge_border_installed"]),
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
