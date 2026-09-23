# dendro/ui/styles.py
from __future__ import annotations

import subprocess
from typing import Dict, Final, List, Optional, Tuple
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QGuiApplication

# =============================================================================
# Theme Color Palettes (Curated High-Contrast Dark & Light Themes)
# =============================================================================

THEMES_CONFIG: Final[Dict[str, Dict[str, str]]] = {
    # 1. Catppuccin Mocha (Default Modern Dark)
    "mocha": {
        "name": "Catppuccin Mocha (Dark)",
        "is_dark": "true",
        "bg_base": "#1e1e2e",
        "bg_surface": "#181825",
        "bg_input": "#11111b",
        "bg_hover": "#313244",
        "bg_selected": "#45475a",
        "text_primary": "#cdd6f4",
        "text_secondary": "#a6adc8",
        "text_dim": "#6c7086",
        "border": "#313244",
        "border_subtle": "#24273a",
        "accent": "#89b4fa",
        "accent_hover": "#b4befe",
        "accent_text": "#11111b",
        "badge_bg_installed": "#1e3a2f",
        "badge_fg_installed": "#a6e3a1",
        "badge_bg_missing": "#45232e",
        "badge_fg_missing": "#f38ba8",
        "badge_bg_queued_in": "#453322",
        "badge_fg_queued_in": "#fab387",
        "badge_bg_queued_rm": "#45252b",
        "badge_fg_queued_rm": "#eba0ac",
        "badge_bg_tag": "#3d2f47",
        "badge_fg_tag": "#cba6f7",
    },
    # 2. Catppuccin Latte (Clean Modern Light)
    "latte": {
        "name": "Catppuccin Latte (Light)",
        "is_dark": "false",
        "bg_base": "#eff1f5",
        "bg_surface": "#e6e9ef",
        "bg_input": "#dce0e8",
        "bg_hover": "#ccd0da",
        "bg_selected": "#bcc0cc",
        "text_primary": "#4c4f69",
        "text_secondary": "#5c5f77",
        "text_dim": "#8c8fa1",
        "border": "#ccd0da",
        "border_subtle": "#dce0e8",
        "accent": "#1e66f5",
        "accent_hover": "#04a5e5",
        "accent_text": "#ffffff",
        "badge_bg_installed": "#dcefe3",
        "badge_fg_installed": "#40a02b",
        "badge_bg_missing": "#fedee2",
        "badge_fg_missing": "#d20f39",
        "badge_bg_queued_in": "#feebd6",
        "badge_fg_queued_in": "#df8e1d",
        "badge_bg_queued_rm": "#fedee2",
        "badge_fg_queued_rm": "#e64553",
        "badge_bg_tag": "#ece6fa",
        "badge_fg_tag": "#8839ef",
    },
    # 3. Tokyo Night (Popular Modern Dark)
    "tokyo_night": {
        "name": "Tokyo Night (Dark)",
        "is_dark": "true",
        "bg_base": "#1a1b26",
        "bg_surface": "#16161e",
        "bg_input": "#13141c",
        "bg_hover": "#292e42",
        "bg_selected": "#3b4261",
        "text_primary": "#c0caf5",
        "text_secondary": "#9aa5ce",
        "text_dim": "#565f89",
        "border": "#292e42",
        "border_subtle": "#1f2335",
        "accent": "#7aa2f7",
        "accent_hover": "#89ddff",
        "accent_text": "#1a1b26",
        "badge_bg_installed": "#1a3632",
        "badge_fg_installed": "#9ece6a",
        "badge_bg_missing": "#3b232e",
        "badge_fg_missing": "#f7768e",
        "badge_bg_queued_in": "#362e24",
        "badge_fg_queued_in": "#ff9e64",
        "badge_bg_queued_rm": "#3b232e",
        "badge_fg_queued_rm": "#f7768e",
        "badge_bg_tag": "#2c2440",
        "badge_fg_tag": "#bb9af7",
    },
    # 4. Nord (Arctic Clean Dark)
    "nord": {
        "name": "Nord (Dark)",
        "is_dark": "true",
        "bg_base": "#2e3440",
        "bg_surface": "#242933",
        "bg_input": "#1e222a",
        "bg_hover": "#3b4252",
        "bg_selected": "#434c5e",
        "text_primary": "#eceff4",
        "text_secondary": "#d8dee9",
        "text_dim": "#7b88a1",
        "border": "#434c5e",
        "border_subtle": "#2e3440",
        "accent": "#88c0d0",
        "accent_hover": "#81a1c1",
        "accent_text": "#2e3440",
        "badge_bg_installed": "#293d39",
        "badge_fg_installed": "#a3be8c",
        "badge_bg_missing": "#3d2a31",
        "badge_fg_missing": "#bf616a",
        "badge_bg_queued_in": "#3d362a",
        "badge_fg_queued_in": "#ebcb8b",
        "badge_bg_queued_rm": "#3d2a31",
        "badge_fg_queued_rm": "#d08770",
        "badge_bg_tag": "#382e3f",
        "badge_fg_tag": "#b48ead",
    },
    # 5. Solarized Light (Classic Document Light)
    "solarized_light": {
        "name": "Solarized Light (Light)",
        "is_dark": "false",
        "bg_base": "#fdf6e3",
        "bg_surface": "#eee8d5",
        "bg_input": "#e4dec7",
        "bg_hover": "#ddd6be",
        "bg_selected": "#d3ccb3",
        "text_primary": "#657b83",
        "text_secondary": "#586e75",
        "text_dim": "#93a1a1",
        "border": "#d3ccb3",
        "border_subtle": "#e4dec7",
        "accent": "#268bd2",
        "accent_hover": "#2aa198",
        "accent_text": "#ffffff",
        "badge_bg_installed": "#daf1dc",
        "badge_fg_installed": "#859900",
        "badge_bg_missing": "#fae0de",
        "badge_fg_missing": "#dc322f",
        "badge_bg_queued_in": "#faeed8",
        "badge_fg_queued_in": "#b58900",
        "badge_bg_queued_rm": "#fae0de",
        "badge_fg_queued_rm": "#cb4b16",
        "badge_bg_tag": "#eee3f0",
        "badge_fg_tag": "#6c71c4",
    },
    # 6. Gruvbox Dark (Warm Retro Dark)
    "gruvbox": {
        "name": "Gruvbox Dark (Dark)",
        "is_dark": "true",
        "bg_base": "#282828",
        "bg_surface": "#1d2021",
        "bg_input": "#18191a",
        "bg_hover": "#3c3836",
        "bg_selected": "#504945",
        "text_primary": "#ebdbb2",
        "text_secondary": "#d5c4a1",
        "text_dim": "#928374",
        "border": "#3c3836",
        "border_subtle": "#282828",
        "accent": "#fe8019",
        "accent_hover": "#fabd2f",
        "accent_text": "#282828",
        "badge_bg_installed": "#2d3824",
        "badge_fg_installed": "#b8bb26",
        "badge_bg_missing": "#3c2424",
        "badge_fg_missing": "#fb4934",
        "badge_bg_queued_in": "#3c3422",
        "badge_fg_queued_in": "#fabd2f",
        "badge_bg_queued_rm": "#3c2424",
        "badge_fg_queued_rm": "#fe8019",
        "badge_bg_tag": "#342838",
        "badge_fg_tag": "#d3869b",
    }
}

THEME_DISPLAY_OPTIONS: Final[List[Tuple[str, str]]] = [
    ("auto", "💻 System Default (Auto)"),
    ("mocha", "🌙 Catppuccin Mocha (Dark)"),
    ("latte", "☀️ Catppuccin Latte (Light)"),
    ("tokyo_night", "🌃 Tokyo Night (Dark)"),
    ("nord", "❄️ Nord (Dark)"),
    ("solarized_light", "📜 Solarized Light (Light)"),
    ("gruvbox", "🪵 Gruvbox Dark (Dark)"),
]


# =============================================================================
# System Color Scheme Detection (Fedora FreeDesktop Portal & QStyleHints)
# =============================================================================

def is_system_dark_mode() -> bool:
    """
    Detects if the Fedora desktop environment is currently in dark mode.
    Uses Qt 6 QStyleHints portal integration with gsettings fallback.
    """
    app = QGuiApplication.instance()
    if app and hasattr(app, "styleHints"):
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        elif scheme == Qt.ColorScheme.Light:
            return False

    # Fallback check for GNOME / Desktop interface schema
    try:
        res = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=1
        )
        if "dark" in res.stdout.lower():
            return True
        elif "light" in res.stdout.lower() or "default" in res.stdout.lower():
            return False
    except Exception:
        pass

    return True  # Safe default to Dark if detection is unavailable


# =============================================================================
# Dynamic QSS Stylesheet Builder
# =============================================================================

def build_stylesheet(c: Dict[str, str]) -> str:
    """Generates the application-wide Qt stylesheet for any theme palette."""
    return f"""
/* Global Reset & Base Typography */
QWidget {{
    background-color: {c['bg_base']};
    color: {c['text_primary']};
    font-family: "Cantarell", "Inter", "Segoe UI", "Noto Color Emoji", "Apple Color Emoji", sans-serif;
    font-size: 13px;
    selection-background-color: {c['bg_selected']};
    selection-color: {c['accent']};
}}

/* Top Header & Search Bar */
QWidget#HeaderContainer {{
    background-color: {c['bg_surface']};
    border-bottom: 1px solid {c['border']};
}}

QLineEdit#SearchBar {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 8px 14px;
    color: {c['text_primary']};
    font-size: 13px;
}}

QLineEdit#SearchBar:focus {{
    border: 1px solid {c['accent']};
    background-color: {c['bg_surface']};
}}

/* Category Navigation Sidebar */
QListWidget#SidebarList {{
    background-color: {c['bg_surface']};
    border: none;
    border-right: 1px solid {c['border']};
    padding: 10px 6px;
}}

QListWidget#SidebarList::item {{
    height: 34px;
    border-radius: 6px;
    padding-left: 8px;
    margin-bottom: 2px;
    color: {c['text_secondary']};
    font-weight: 500;
}}

QListWidget#SidebarList::item:hover {{
    background-color: {c['bg_hover']};
    color: {c['text_primary']};
}}

QListWidget#SidebarList::item:selected {{
    background-color: {c['bg_selected']};
    color: {c['accent']};
    font-weight: bold;
}}

QListWidget#SidebarList::item:disabled {{
    color: {c['text_dim']};
    font-weight: 800;
    font-size: 11px;
    letter-spacing: 0.5px;
    padding-top: 10px;
    padding-bottom: 4px;
    background-color: transparent;
}}

/* Package Inspector Side Panel */
QWidget#InspectorPanel {{
    background-color: {c['bg_surface']};
    border-left: 1px solid {c['border']};
}}

/* Package Inspector - Stats Frame */
QFrame#StatsFrame {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 6px;
}}

QLabel#StatSizeLabel {{ color: {c['badge_fg_queued_in']}; font-weight: bold; font-size: 11px; }}
QLabel#StatArchLabel {{ color: {c['badge_fg_tag']}; font-weight: bold; font-size: 11px; }}
QLabel#StatLicenseLabel {{ color: {c['badge_fg_installed']}; font-weight: bold; font-size: 11px; }}
QLabel#StatRepoLabel {{ color: {c['accent']}; font-weight: bold; font-size: 11px; }}

/* Package Inspector - AI Card */
QFrame#AICard {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 8px;
}}

QLabel#AICategoryBadge {{
    font-weight: bold;
    color: {c['accent']};
    font-size: 12px;
}}

QLabel#AIConfidenceBadge {{
    color: {c['badge_fg_installed']};
    font-weight: bold;
    font-size: 11px;
}}

QLabel#AIRationaleLabel {{
    color: {c['text_secondary']};
    font-size: 11px;
    line-height: 1.3;
}}

/* Package Inspector - Overview Description Text */
QTextEdit#InspectorDescText {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    color: {c['text_primary']};
    font-size: 12px;
    line-height: 1.4;
}}

/* Package Inspector - Files Table & Reverse List */
QTableWidget#InspectorFilesTable {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    color: {c['text_primary']};
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
    font-size: 11px;
}}

QListWidget#InspectorReverseList {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    color: {c['text_primary']};
    font-size: 12px;
}}

QLabel#InspectorPackagerLabel {{
    color: {c['text_dim']};
    font-size: 11px;
}}

/* Tab Bar */
QTabWidget#InspectorTabs::pane {{
    border: 1px solid {c['border']};
    border-radius: 6px;
    background-color: {c['bg_surface']};
}}

QTabBar::tab {{
    background-color: {c['bg_input']};
    color: {c['text_secondary']};
    padding: 6px 14px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
    font-size: 12px;
    font-weight: bold;
}}

QTabBar::tab:selected {{
    background-color: {c['bg_selected']};
    color: {c['accent']};
}}

/* Package Tree View */
QTreeView#PackageTreeView {{
    background-color: {c['bg_base']};
    border: none;
    outline: none;
    padding: 4px;
    show-decoration-selected: 1;
}}

QTreeView#PackageTreeView::item {{
    border: none;
    border-radius: 4px;
}}

QHeaderView::section {{
    background-color: {c['bg_surface']};
    color: {c['text_secondary']};
    padding: 8px 12px;
    border: none;
    border-bottom: 1px solid {c['border']};
    border-right: 1px solid {c['border_subtle']};
    font-weight: bold;
    font-size: 12px;
}}

QHeaderView::section:hover {{
    background-color: {c['bg_hover']};
    color: {c['accent']};
}}

/* Buttons */
QPushButton {{
    background-color: {c['bg_selected']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: bold;
    color: {c['text_primary']};
}}

QPushButton:hover {{
    background-color: {c['bg_hover']};
}}

QPushButton#ApplyButton {{
    background-color: {c['accent']};
    color: {c['accent_text']};
    border: none;
    font-weight: 700;
}}

QPushButton#ApplyButton:hover {{
    background-color: {c['accent_hover']};
}}

QPushButton#ApplyButton:disabled {{
    background-color: {c['bg_selected']};
    color: {c['text_dim']};
}}

QPushButton#HeaderSecondaryBtn {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    color: {c['text_primary']};
}}

QPushButton#HeaderSecondaryBtn:hover {{
    background-color: {c['bg_hover']};
    color: {c['accent']};
}}

/* Dynamic Action Button in Inspector Panel */
QPushButton#InspectorQueueBtn[queueState="installed"] {{
    background-color: {c['badge_bg_queued_rm']};
    color: {c['badge_fg_queued_rm']};
    border: 1px solid {c['border']};
    font-weight: bold;
}}

QPushButton#InspectorQueueBtn[queueState="queued_remove"] {{
    background-color: {c['bg_selected']};
    color: {c['badge_fg_queued_in']};
    border: 1px solid {c['border']};
    font-weight: bold;
}}

QPushButton#InspectorQueueBtn[queueState="available"] {{
    background-color: {c['badge_bg_installed']};
    color: {c['badge_fg_installed']};
    border: 1px solid {c['border']};
    font-weight: bold;
}}

QPushButton#InspectorQueueBtn[queueState="queued_install"] {{
    background-color: {c['bg_selected']};
    color: {c['badge_fg_queued_in']};
    border: 1px solid {c['border']};
    font-weight: bold;
}}

/* Terminal & Log Output */
QTextEdit#ConsoleOutput {{
    background-color: {c['bg_input']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
    font-size: 12px;
    color: {c['text_secondary']};
    padding: 8px;
}}

/* ScrollBars */
QScrollBar:vertical {{
    background-color: {c['bg_surface']};
    width: 8px;
    margin: 0px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical {{
    background-color: {c['border']};
    min-height: 24px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {c['bg_hover']};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    height: 0px;
}}

/* Menus */
QMenu {{
    background-color: {c['bg_surface']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 6px;
}}

QMenu::item {{
    padding: 8px 24px 8px 12px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background-color: {c['bg_selected']};
    color: {c['accent']};
}}

/* Status Bar & Splitter */
QStatusBar {{
    background-color: {c['bg_input']};
    border-top: 1px solid {c['border']};
    color: {c['text_secondary']};
}}

QSplitter::handle {{
    background-color: {c['bg_surface']};
}}
"""


# =============================================================================
# Public Theme API & Delegate Palette Generator
# =============================================================================

def get_resolved_theme_key(theme_choice: str) -> str:
    """Resolves 'auto' into either 'latte' (Light) or 'mocha' (Dark)."""
    if theme_choice == "auto":
        return "mocha" if is_system_dark_mode() else "latte"
    return theme_choice if theme_choice in THEMES_CONFIG else "mocha"


def get_theme_stylesheet(theme_choice: str) -> str:
    """Retrieves the full stylesheet for a given theme choice."""
    key = get_resolved_theme_key(theme_choice)
    return build_stylesheet(THEMES_CONFIG[key])


def get_delegate_palette(theme_choice: str) -> Dict[str, QColor]:
    """Generates QColor palette objects used by PackageTreeItemDelegate."""
    key = get_resolved_theme_key(theme_choice)
    c = THEMES_CONFIG[key]
    return {
        "bg_hover": QColor(c["bg_hover"]),
        "bg_selected": QColor(c["bg_selected"]),
        "text_main": QColor(c["text_primary"]),
        "text_secondary": QColor(c["text_secondary"]),
        "text_dim": QColor(c["text_dim"]),
        "accent": QColor(c["accent"]),
        "badge_bg_installed": QColor(c["badge_bg_installed"]),
        "badge_fg_installed": QColor(c["badge_fg_installed"]),
        "badge_bg_missing": QColor(c["badge_bg_missing"]),
        "badge_fg_missing": QColor(c["badge_fg_missing"]),
        "badge_bg_queued_in": QColor(c["badge_bg_queued_in"]),
        "badge_fg_queued_in": QColor(c["badge_fg_queued_in"]),
        "badge_bg_queued_rm": QColor(c["badge_bg_queued_rm"]),
        "badge_fg_queued_rm": QColor(c["badge_fg_queued_rm"]),
        "badge_bg_tag": QColor(c["badge_bg_tag"]),
        "badge_fg_tag": QColor(c["badge_fg_tag"]),
    }
