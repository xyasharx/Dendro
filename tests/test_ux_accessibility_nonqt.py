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


@pytest.mark.parametrize("theme_key", ("dark", "light", "dark_hc", "light_hc"))
def test_danger_button_palette_meets_wcag_aa_in_all_interactive_states(
    theme_key: str,
) -> None:
    """Destructive CTA text and surface tokens meet WCAG 2.2 AA contrast."""
    c = THEMES_CONFIG[theme_key]
    min_text_ratio = 7.0 if theme_key.endswith("_hc") else 4.5
    for state_key in ("danger_bg", "danger_hover", "danger_pressed"):
        ratio = _contrast_ratio(c["danger_text"], c[state_key])
        assert ratio >= min_text_ratio, (
            f"{theme_key} {state_key} vs danger_text contrast is {ratio:.2f}:1 (< {min_text_ratio}:1)"
        )
    assert _contrast_ratio(c["danger_bg"], c["bg_base"]) >= 3.0
    assert _contrast_ratio(c["danger_bg"], c["bg_surface"]) >= 3.0
    assert _contrast_ratio(c["focus_solid_border"], c["bg_base"]) >= 3.0
    assert _contrast_ratio(c["focus_solid_border"], c["bg_surface"]) >= 3.0


@pytest.mark.parametrize("theme_key", ("dark", "light", "dark_hc", "light_hc"))
def test_focus_ring_colors_meet_non_text_contrast(theme_key: str) -> None:
    """Focus ring colors preserve >= 3:1 contrast against adjacent surfaces."""
    c = THEMES_CONFIG[theme_key]
    assert _contrast_ratio(c["accent"], c["bg_surface"]) >= 3.0
    assert _contrast_ratio(c["accent"], c["bg_selected"]) >= 3.0
    assert _contrast_ratio(c["accent"], c["bg_hover"]) >= 3.0


@pytest.mark.parametrize("theme_key", ("dark", "light", "dark_hc", "light_hc"))
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
        "QTreeView::branch:hover",
        "QTreeView::branch:selected",
        "QTableWidget:focus",
        "QTableView#InspectorFilesTable:focus",
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

    # Hosted inspector host paints bg_surface + left dock divider across scrollbar
    # margins while the hosted inner panel drops its duplicate left border.
    host_scroll_match = re.search(
        r"QScrollArea#InspectorHostScroll \{(?P<body>[^}]*)\}", qss, re.DOTALL
    )
    assert host_scroll_match is not None
    host_body = host_scroll_match.group("body")
    assert c["bg_surface"] in host_body
    assert f"border-left: 1px solid {c['border']};" in host_body

    hosted_panel_match = re.search(
        r'QWidget#InspectorPanel\[scrollHosted="true"\] \{(?P<body>[^}]*)\}',
        qss,
        re.DOTALL,
    )
    assert hosted_panel_match is not None
    assert "border-left: none;" in hosted_panel_match.group("body")

    # Checkbox indicator focused vs resting sizes stay identical (18x18 total box).
    cb_focus_ind = re.search(
        r"QCheckBox::indicator:focus, QRadioButton::indicator:focus \{(?P<body>[^}]*)\}",
        qss,
        re.DOTALL,
    )
    assert cb_focus_ind is not None
    assert "width: 14px;" in cb_focus_ind.group("body")
    assert "border: 2px solid" in cb_focus_ind.group("body")

    branch_hover = re.search(
        r"QTreeView::branch:hover \{(?P<body>[^}]*)\}", qss, re.DOTALL
    )
    assert branch_hover is not None
    assert c["bg_hover"] in branch_hover.group("body")

    branch_selected = re.search(
        r"QTreeView::branch:selected \{(?P<body>[^}]*)\}", qss, re.DOTALL
    )
    assert branch_selected is not None
    assert c["bg_selected"] in branch_selected.group("body")

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


@pytest.mark.parametrize("theme_key", ("dark", "light", "dark_hc", "light_hc"))
def test_theme_palettes_meet_aa_and_aaa_contrast_targets(theme_key: str) -> None:
    """Text tokens meet AA (>=4.5:1) in standard themes and AAA (>=7:1) in HC themes."""
    c = THEMES_CONFIG[theme_key]
    is_hc = theme_key.endswith("_hc")
    text_floor = 7.0 if is_hc else 4.5

    for surface in ("bg_base", "bg_surface", "bg_card"):
        assert _contrast_ratio(c["text_primary"], c[surface]) >= text_floor
        assert _contrast_ratio(c["text_secondary"], c[surface]) >= text_floor
        assert _contrast_ratio(c["text_dim"], c[surface]) >= 4.5

    assert _contrast_ratio(c["text_primary"], c["bg_selected"]) >= text_floor
    assert _contrast_ratio(c["accent_text"], c["accent"]) >= text_floor
    assert _contrast_ratio(c["accent"], c["bg_base"]) >= (4.5 if is_hc else 3.0)

    for badge in ("installed", "update", "missing", "queued_in", "queued_rm", "tag"):
        assert (
            _contrast_ratio(c[f"badge_fg_{badge}"], c[f"badge_bg_{badge}"])
            >= text_floor
        )

    qss = build_stylesheet(c)
    assert "QScrollArea#SettingsPageScroll" in qss


def test_ui_source_has_no_hardcoded_font_stacks() -> None:
    """UI widgets inherit system sans/monospace fonts instead of hardcoded stacks."""
    forbidden = (
        "Cantarell",
        "Segoe UI",
        "JetBrains Mono",
        "Fira Code",
        "Consolas",
    )
    ui_dir = PROJECT_ROOT / "dendro" / "ui"
    for path in sorted(ui_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for family in forbidden:
                    assert family not in node.value, (
                        f"Hardcoded font family {family!r} found in {path.name}:{node.lineno}"
                    )


def test_performance_architecture_invariants() -> None:
    """Verify #9 static invariants: no FontRole QFont alloc, uniformRowHeights, virtual manifest."""
    models_src = (PROJECT_ROOT / "dendro" / "core" / "models.py").read_text(
        encoding="utf-8"
    )
    assert "FontRole" not in models_src
    assert "QFont" not in models_src

    main_win_src = (PROJECT_ROOT / "dendro" / "ui" / "main_window.py").read_text(
        encoding="utf-8"
    )
    assert "setUniformRowHeights(True)" in main_win_src

    manifest_src = (PROJECT_ROOT / "dendro" / "ui" / "inspector_manifest.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(manifest_src)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    assert "PackageFilesTableModel" in classes
    assert "_icon_cache" in manifest_src
    populate_fn = next(
        node
        for node in classes["PackageManifestMixin"].body
        if isinstance(node, ast.FunctionDef) and node.name == "_populate_files_table"
    )
    populate_src = ast.get_source_segment(manifest_src, populate_fn) or ""
    assert "QIcon.fromTheme" not in populate_src
    assert "QTableWidgetItem" not in manifest_src


def _rule_body(qss: str, selector: str) -> str:
    pattern = re.compile(
        rf"^{re.escape(selector)} \{{(?P<body>[^}}]*)\}}",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(qss)
    assert match is not None, f"Missing rule for {selector!r}"
    return match.group("body")


@pytest.mark.parametrize("theme_key", ("dark", "light"))
def test_standard_themes_use_borderless_tonal_surfaces(theme_key: str) -> None:
    """Standard dark and light themes use tonal elevation instead of heavy borders."""
    c = THEMES_CONFIG[theme_key]
    qss = build_stylesheet(c)

    assert "border: 1px solid transparent;" in _rule_body(qss, "QPushButton")
    assert "background-color: transparent;" in _rule_body(
        qss, "QPushButton#HeaderToolBtn"
    )
    assert "border: 1px solid transparent;" in _rule_body(
        qss, "QPushButton#HeaderToolBtn"
    )
    assert "border: 1px solid transparent;" in _rule_body(
        qss, "QPushButton#HeaderIconBtn"
    )
    assert "border: 1px solid transparent;" in _rule_body(qss, "QLineEdit")
    assert "border: 1px solid transparent;" in _rule_body(qss, "QComboBox")
    assert "border-left: none;" in _rule_body(qss, "QComboBox::drop-down")

    for card_sel in (
        "QFrame#StatGridCard",
        "QFrame#InspectorSectionCard",
        "QWidget#InspectorFilesCard",
        "QFrame#AICard, QFrame#StatsFrame",
        "QFrame#CoprBox, QFrame#RepoAddBox, QFrame#RepoOperationPanel",
        "QTabWidget#InspectorTabs::pane,\nQTabWidget#SettingsTabs::pane",
    ):
        assert "border: none;" in _rule_body(qss, card_sel)

    table_body = _rule_body(qss, "QTableWidget")
    assert "border: 2px solid transparent;" in table_body
    assert "gridline-color: transparent;" in table_body
    assert c["bg_card"] != c["bg_surface"]


@pytest.mark.parametrize("theme_key", ("dark_hc", "light_hc"))
def test_high_contrast_themes_preserve_explicit_borders(theme_key: str) -> None:
    """High-contrast accessibility themes preserve explicit control and card outlines."""
    c = THEMES_CONFIG[theme_key]
    qss = build_stylesheet(c)

    assert f"border: 1px solid {c['border_strong']};" in _rule_body(qss, "QPushButton")
    assert f"border: 1px solid {c['border_strong']};" in _rule_body(qss, "QComboBox")
    assert f"border-left: 1px solid {c['border_strong']};" in _rule_body(
        qss, "QComboBox::drop-down"
    )
    assert f"border: 1px solid {c['border']};" in _rule_body(qss, "QFrame#StatGridCard")
    assert f"border: 1px solid {c['border']};" in _rule_body(
        qss, "QTabWidget#InspectorTabs::pane,\nQTabWidget#SettingsTabs::pane"
    )
