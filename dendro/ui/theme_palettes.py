# dendro/ui/theme_palettes.py
"""Theme color palette dictionaries and delegate QColor conversion for Dendro."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from PyQt6.QtGui import QColor

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
        "danger_bg": "#f38ba8",
        "danger_hover": "#f7aec2",
        "danger_pressed": "#e06c8e",
        "danger_text": "#11111b",
        "focus_solid_border": "#ffffff",
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
        "danger_bg": "#c41235",
        "danger_hover": "#a40e2b",
        "danger_pressed": "#880a22",
        "danger_text": "#ffffff",
        "focus_solid_border": "#11111b",
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


def build_delegate_palette(resolved_key: str) -> dict[str, QColor]:
    """Build ``QColor`` palette entries for a resolved theme key (``dark`` or ``light``)."""
    from PyQt6.QtGui import QColor

    c = THEMES_CONFIG[resolved_key]
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
        "border_strong": QColor(c["border_strong"]),
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


__all__ = (
    "THEMES_CONFIG",
    "THEME_DISPLAY_OPTIONS",
    "build_delegate_palette",
)
