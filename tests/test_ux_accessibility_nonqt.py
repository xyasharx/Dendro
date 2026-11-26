"""Non-Qt regression checks for WCAG contrast, QSS focus rules, and source contracts."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from dendro.ui.stylesheet_builder import build_stylesheet
from dendro.ui.theme_palettes import THEMES_CONFIG

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _srgb_channel(channel: int) -> float:
    s = channel / 255.0
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    cleaned = hex_color.lstrip("#")
    r, g, b = (int(cleaned[i : i + 2], 16) for i in (0, 2, 4))
    return (
        0.2126 * _srgb_channel(r)
        + 0.7152 * _srgb_channel(g)
        + 0.0722 * _srgb_channel(b)
    )


def _contrast_ratio(fg: str, bg: str) -> float:
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    lighter, darker = (l1, l2) if l1 >= l2 else (l2, l1)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("theme_key", ("dark", "light"))
def test_danger_button_palette_meets_wcag_aa_in_all_interactive_states(
    theme_key: str,
) -> None:
    """Destructive CTA text and surface tokens meet WCAG 2.2 AA contrast."""
    c = THEMES_CONFIG[theme_key]
    for state_key in ("danger_bg", "danger_hover", "danger_pressed"):
        ratio = _contrast_ratio(c["danger_text"], c[state_key])
        assert ratio >= 4.5, (
            f"{theme_key} {state_key} vs danger_text contrast is {ratio:.2f}:1 (< 4.5:1)"
        )
    assert _contrast_ratio(c["danger_bg"], c["bg_base"]) >= 3.0
    assert _contrast_ratio(c["danger_bg"], c["bg_surface"]) >= 3.0
    assert _contrast_ratio(c["focus_solid_border"], c["bg_base"]) >= 3.0
    assert _contrast_ratio(c["focus_solid_border"], c["bg_surface"]) >= 3.0


@pytest.mark.parametrize("theme_key", ("dark", "light"))
def test_focus_ring_colors_meet_non_text_contrast(theme_key: str) -> None:
    """Focus ring colors preserve >= 3:1 contrast against adjacent surfaces."""
    c = THEMES_CONFIG[theme_key]
    assert _contrast_ratio(c["accent"], c["bg_surface"]) >= 3.0
    assert _contrast_ratio(c["accent"], c["bg_selected"]) >= 3.0
    assert _contrast_ratio(c["accent"], c["bg_hover"]) >= 3.0


@pytest.mark.parametrize("theme_key", ("dark", "light"))
def test_stylesheet_covers_danger_button_and_every_focusable_control(
    theme_key: str,
) -> None:
    """QSS must style DangerButton states and visible focus rings on every control."""
    c = THEMES_CONFIG[theme_key]
    qss = build_stylesheet(c)

    # Issue #1: DangerButton uses dedicated accessible tokens, never low-contrast maroon/surface.
    danger_match = re.search(
        r"QPushButton#DangerButton \{(?P<body>[^}]*)\}", qss, re.DOTALL
    )
    assert danger_match is not None
    assert c["danger_bg"] in danger_match.group("body")
    assert c["danger_text"] in danger_match.group("body")

    danger_hover = re.search(
        r"QPushButton#DangerButton:hover \{(?P<body>[^}]*)\}", qss, re.DOTALL
    )
    assert danger_hover is not None
    assert c["danger_hover"] in danger_hover.group("body")
    assert c["danger_text"] in danger_hover.group("body")
    assert c["badge_border_missing"] not in danger_hover.group("body")

    # Issue #4: Explicit focus selectors across buttons, inputs, tabs, views, and indicators.
    required_focus_selectors = (
        "QPushButton:focus",
        "QPushButton#ApplyButton:focus",
        "QPushButton#ApplyButton:hover:focus",
        "QPushButton#DangerButton:focus",
        "QPushButton#DangerButton:hover:focus",
        "QWidget#HeaderContainer QPushButton:focus",
        "QWidget#HeaderContainer QPushButton#ApplyButton:focus",
        "QWidget#HeaderContainer QPushButton#HeaderIconBtn:focus",
        "QPushButton#HeaderToolBtn:focus",
        "QPushButton#HeaderIconBtn:focus",
        "QPushButton#UpdatesIndicatorBtn:focus",
        "QPushButton#HeaderDangerBtn:focus",
        "QPushButton#InspectorCloseBtn:focus, QPushButton#DrawerCloseBtn:focus",
        "QPushButton#InspectorQueueBtn:focus",
        'QPushButton#InspectorQueueBtn[queueState="installed"]:focus',
        'QPushButton#InspectorQueueBtn[queueState="available"]:focus',
        "QLineEdit:focus",
        "QLineEdit#SearchBar:focus",
        'QLineEdit#SearchBar[filterState="valid"]:focus',
        'QLineEdit#SearchBar[filterState="invalid"]:focus',
        "QLineEdit#RepoAddressInput:focus, QLineEdit#RepoFilterInput:focus",
        "QComboBox:focus",
        "QCheckBox:focus, QRadioButton:focus",
        "QCheckBox::indicator:focus, QRadioButton::indicator:focus",
        "QCheckBox::indicator:checked:focus",
        "QRadioButton::indicator:checked:focus",
        "QTabBar::tab:focus",
        "QTabBar::tab:selected:focus",
        "QListWidget#SidebarList:focus",
        "QTreeView#PackageTreeView:focus",
        "QTableWidget:focus",
    )
    for selector in required_focus_selectors:
        assert selector in qss, f"Missing {selector!r} focus rule in {theme_key} QSS"

    # Existing inspector tab constraint: font-weight: 600 and no font-size override.
    inspector_tab_match = re.search(
        r"QTabWidget#InspectorTabs > QTabBar::tab \{(?P<body>[^}]*)\}", qss
    )
    assert inspector_tab_match is not None
    tab_body = inspector_tab_match.group("body")
    assert "font-weight: 600;" in tab_body
    assert "font-size" not in tab_body

    # Checkbox indicator focused vs resting sizes stay identical (18x18 total box).
    cb_focus_ind = re.search(
        r"QCheckBox::indicator:focus, QRadioButton::indicator:focus \{(?P<body>[^}]*)\}",
        qss,
        re.DOTALL,
    )
    assert cb_focus_ind is not None
    assert "width: 14px;" in cb_focus_ind.group("body")
    assert "border: 2px solid" in cb_focus_ind.group("body")

    # No custom-ID QPushButton styled in stylesheet_builder may lack a :focus rule.
    styled_button_ids = set(re.findall(r"QPushButton#([A-Za-z0-9_]+)", qss))
    focused_button_ids = set(
        re.findall(r"QPushButton#([A-Za-z0-9_]+)(?:\[[^\]]+\])?(?::hover)?:focus", qss)
    )
    assert styled_button_ids == focused_button_ids, (
        f"Buttons missing :focus styling: {styled_button_ids - focused_button_ids}"
    )


def test_history_dialog_source_never_presents_fake_rollback_flow() -> None:
    """History dialog must remain honest about DNF5 daemon read-only capabilities."""
    history_src = (PROJECT_ROOT / "dendro" / "ui" / "history_dialog.py").read_text(
        encoding="utf-8"
    )
    tx_src = (PROJECT_ROOT / "dendro" / "ui" / "main_window_transactions.py").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "Confirm Rollback",
        "Undo Selected Transaction",
        "undo_requested",
        "ApplyButton",
    ):
        assert forbidden not in history_src
    assert "_on_undo_transaction_requested" not in tx_src
    assert "History Undo Is Not Available" not in tx_src

    tree = ast.parse(history_src)
    classes = {
        node.name: {
            item.name for item in node.body if isinstance(item, ast.FunctionDef)
        }
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    assert "DnfHistoryDialog" in classes
    methods = classes["DnfHistoryDialog"]
    assert {
        "set_undo_supported",
        "_on_inspect_clicked",
        "_on_copy_undo_clicked",
    } <= methods
