"""Regression coverage for Dark/Light layout defects found in visual review.

Each test pins one defect that only appeared when the widgets were rendered:
clipped inspector card text, hidden close-button glyphs, mismatched table
alternate rows, truncated status pills, unreadable tab labels, a stray
mnemonic in a tab name, and Flatpak inventory columns pushed out of view.
"""

from __future__ import annotations

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPalette
from PyQt6.QtWidgets import QAbstractItemView, QFrame, QLabel, QPushButton

from core.backend import (
    DependencyNode,
    FileVerificationResult,
    PackageFileInfo,
    PackageInfo,
    PackageState,
    format_byte_size,
)
from core.flatpak_inventory import FlatpakEntry, FlatpakInventory
from core.host_environment import HostEnvironment, HostEnvironmentKind
from core.models import STATUS_EXTRA_LABELS, STATUS_LABELS, DependencyTreeModel
from ui.delegates import (
    NO_VALUE_PLACEHOLDER,
    DendroTreeView,
    PackageTreeItemDelegate,
)
from ui.flatpak_dialog import (
    _HEADER_LETTER_SPACING,
    _SIZE_COLUMN,
    _SORT_INDICATOR_WIDTH,
    FlatpakInventoryDialog,
)
from ui.flatpak_dialog import (
    COLUMN_HEADERS as _FLATPAK_COLUMN_HEADERS,
)
from ui.inspector_panel import PackageInspectorPanel
from ui.main_window import MainWindow
from ui.settings_dialog import DendroSettingsDialog
from ui.styles import THEMES_CONFIG, get_theme_stylesheet
from ui.transaction_drawer import TransactionDrawer


def _package(name: str = "gnome-shell", **overrides) -> PackageInfo:
    values = {
        "version": "48.1",
        "release": "1.fc43",
        "arch": "x86_64",
        "summary": "A representative summary.",
        "description": "A description paragraph.\n\n- one bullet\n- two bullets",
        "state": PackageState.INSTALLED,
        "parent_pillar": "pillar_apps",
        "sub_category": "desktop_addons",
        "primary_category": "desktop_addons",
        "classification_evidence": "rpm_manifest",
        "classification_input_scope": "installed_manifest",
        "classification_rationale": [
            "Ships a desktop entry in /usr/share/applications.",
            "Links against GTK4 and libadwaita.",
        ],
        "secondary_tags": ["gtk", "gui"],
        "is_protected": True,
    }
    values.update(overrides)
    return PackageInfo(name=name, **values)


# ---------------------------------------------------------------------------
# Inspector overview card: wrapped labels must not be clipped
# ---------------------------------------------------------------------------


def test_inspector_safety_card_reserves_height_for_wrapped_labels(qapp) -> None:
    panel = PackageInspectorPanel()
    panel.resize(392, 780)
    panel.show()
    qapp.processEvents()

    panel.set_package_info(_package())
    for _ in range(4):
        qapp.processEvents()
        panel.tab_overview.layout().activate()
        panel.safety_card.layout().activate()

    # The risk badge is a full sentence ("Removal Risk: CRITICAL (Protected
    # System Component)") and must wrap onto a second line instead of being cut.
    #
    # Compare against heightForWidth, not sizeHint: a word-wrapped label's
    # sizeHint is computed at its own preferred width, which on a narrower font
    # can need *more* lines than the width the layout actually gives it, so
    # sizeHint would fail a label that renders perfectly.
    #
    # How many lines that sentence takes is a property of the installed font,
    # not of the layout: a narrower font fits it on one line, and demanding two
    # lines made CI depend on Fedora's default font metrics.
    badge = panel.safety_status_badge
    assert badge.wordWrap()
    assert badge.height() >= badge.heightForWidth(badge.width())

    # The whole card must be tall enough for all three wrapped labels at the
    # width it is actually laid out at.
    card = panel.safety_card
    assert card.sizePolicy().hasHeightForWidth()
    required = card.layout().heightForWidth(card.width())
    assert card.height() >= required, (
        f"card is {card.height()}px but its wrapped content needs {required}px"
    )
    for label in (panel.taxonomy_badge, panel.provenance_label, panel.rationale_label):
        assert label.wordWrap()
        assert label.height() >= label.heightForWidth(label.width())

    panel.close()


# ---------------------------------------------------------------------------
# Compact close buttons: fallback glyph must fit inside the fixed size
# ---------------------------------------------------------------------------


def test_compact_close_buttons_show_their_fallback_glyph(qapp) -> None:
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))

    drawer = TransactionDrawer()
    drawer.show()
    qapp.processEvents()

    # Without icon themes (headless CI, minimal desktops) both close buttons
    # fall back to a text glyph. The generic button padding used to push the
    # glyph out of the 28px square, leaving an apparently empty button.
    assert drawer.close_btn.text() == "x"
    assert drawer.close_btn.objectName() == "DrawerCloseBtn"
    assert drawer.close_btn.height() <= 30
    assert drawer.close_btn.width() == 28

    panel = PackageInspectorPanel()
    panel.show()
    qapp.processEvents()
    assert panel.close_btn.objectName() == "InspectorCloseBtn"
    assert panel.close_btn.height() <= 30
    assert panel.close_btn.width() == 26
    drawer.close()
    panel.close()


# ---------------------------------------------------------------------------
# Alternate table rows must follow the active theme
# ---------------------------------------------------------------------------


def test_table_alternate_row_color_is_themed(qapp) -> None:
    dark = get_theme_stylesheet("dark")
    light = get_theme_stylesheet("light")

    assert "alternate-background-color" in dark
    assert "alternate-background-color" in light
    assert "alternate-background-color: #ffffff" not in dark
    assert "alternate-background-color: #eff1f5" not in dark


def test_alternate_base_role_is_set_by_the_main_window(qapp) -> None:
    window = MainWindow(host_environment=HostEnvironment.traditional())
    try:
        for theme in ("dark", "light"):
            window._apply_theme(theme)
            qapp.processEvents()

            # The application palette drives dialogs and item views that do not
            # override it.
            app_alternate = qapp.palette().color(QPalette.ColorRole.AlternateBase)
            assert (
                app_alternate.name() == QColor(THEMES_CONFIG[theme]["bg_input"]).name()
            ), theme

            # The tree view owns an explicit palette, so it needs its own value.
            tree_alternate = window.tree_view.palette().color(
                QPalette.ColorRole.AlternateBase
            )
            assert (
                tree_alternate.name() == QColor(THEMES_CONFIG[theme]["bg_card"]).name()
            ), theme
            assert tree_alternate.name() != "#f7f7f7"
    finally:
        window.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Status pills: every state label must fit the default column
# ---------------------------------------------------------------------------


def test_status_labels_fit_the_default_status_column(qapp) -> None:
    window = MainWindow(host_environment=HostEnvironment.traditional())
    try:
        delegate: PackageTreeItemDelegate = window.tree_delegate
        width = window.tree_view.columnWidth(DependencyTreeModel.COL_STATUS)
        assert width >= window._status_column_minimum_width()
        for label in (*STATUS_LABELS.values(), *STATUS_EXTRA_LABELS):
            pill = delegate.fm_badge.horizontalAdvance(label.upper()) + 14
            assert pill <= width - 8, f"{label!r} needs {pill}px in a {width}px column"
    finally:
        window.close()
        qapp.processEvents()


def test_status_labels_are_not_elided_in_the_default_column(qapp) -> None:
    window = MainWindow(host_environment=HostEnvironment.traditional())
    try:
        window.proxy_model.set_category_filter("all")
        window.tree_model.set_packages(
            [
                _package("missing-case", state=PackageState.MISSING),
                _package("queued-case", state=PackageState.QUEUED_UPGRADE),
                _package("available-case", state=PackageState.AVAILABLE),
            ]
        )
        qapp.processEvents()

        column = DependencyTreeModel.COL_STATUS
        width = window.tree_view.columnWidth(column)
        metrics = QFontMetrics(window.tree_delegate.badge_font)
        for row in range(window.proxy_model.rowCount()):
            index = window.proxy_model.index(row, column)
            text = str(index.data(Qt.ItemDataRole.DisplayRole) or "").upper()
            assert text
            assert metrics.horizontalAdvance(text) + 14 <= width - 8
    finally:
        window.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Inspector tabs: no scroll arrows, no accidental mnemonic
# ---------------------------------------------------------------------------


def test_inspector_tabs_all_remain_readable_at_minimum_width(qapp) -> None:
    panel = PackageInspectorPanel()
    panel.resize(panel.minimumWidth(), 780)
    panel.show()
    for _ in range(3):
        qapp.processEvents()

    assert panel.tabs.usesScrollButtons() is False
    assert panel.tabs.tabBar().elideMode() == Qt.TextElideMode.ElideRight

    bar = panel.tabs.tabBar()
    assert bar.sizeHint().width() <= bar.width(), (
        "tab labels must fit the minimum panel width without scrolling"
    )
    for index in range(panel.tabs.count()):
        rect = bar.tabRect(index)
        assert rect.width() > 0
        assert rect.right() <= bar.width()
    panel.close()


def test_settings_about_tab_does_not_render_a_stray_underscore(qapp) -> None:
    dialog = DendroSettingsDialog(host_environment=HostEnvironment.traditional())
    try:
        labels = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
        # A single "&" is a mnemonic marker, so "About & Legal" rendered as
        # "ABOUT _LEGAL". The doubled ampersand renders as a literal "&".
        assert any("&&" in label for label in labels)
        assert "About & Legal" not in labels
    finally:
        dialog.close()
        qapp.processEvents()


def test_about_tab_shows_the_real_version_and_never_invents_one(qapp) -> None:
    """A launcher with no application version must not show "Dendro unknown"."""
    from PyQt6.QtWidgets import QLabel

    def _title() -> str:
        dialog = DendroSettingsDialog(host_environment=HostEnvironment.traditional())
        try:
            titles = [
                label.text()
                for label in dialog.findChildren(QLabel)
                if label.text().startswith("<b>Dendro")
            ]
            assert len(titles) == 1
            return titles[0]
        finally:
            dialog.close()
            qapp.processEvents()

    previous = QCoreApplication.applicationVersion()
    try:
        QCoreApplication.setApplicationVersion("2.17.0")
        title = _title()
        assert title == "<b>Dendro</b> 2.17.0"
        assert "unknown" not in title.lower()

        QCoreApplication.setApplicationVersion("")
        title = _title()
        assert title == "<b>Dendro</b>"
        assert "unknown" not in title.lower()
        assert not title.endswith(" ")
    finally:
        QCoreApplication.setApplicationVersion(previous)


# ---------------------------------------------------------------------------
# Flatpak inventory: nine columns must fit without cutting the last heading
# ---------------------------------------------------------------------------


def _flatpak_entry(**overrides) -> FlatpakEntry:
    values = {
        "kind": "app",
        "name": "Freedesktop Platform",
        "application_id": "org.freedesktop.Platform",
        "version": "24.08.14",
        "branch": "24.08",
        "arch": "x86_64",
        "origin": "flathub",
        "installation": "system",
        "ref": "runtime/org.freedesktop.Platform/x86_64/24.08",
        "installed_size_text": "1.2 GB",
        "installed_size_bytes": 1_288_490_188,
        "runtime": "",
    }
    values.update(overrides)
    return FlatpakEntry(**values)


def test_flatpak_inventory_columns_fit_the_default_dialog_width(qapp) -> None:
    dialog = FlatpakInventoryDialog()
    dialog.show()
    try:
        serial = dialog.begin_loading()
        dialog.show_inventory_if_current(
            serial, FlatpakInventory(entries=(_flatpak_entry(),))
        )
        for _ in range(4):
            qapp.processEvents()

        header = dialog.table.horizontalHeader()
        assert dialog.table.columnCount() == len(_FLATPAK_COLUMN_HEADERS)
        header_width = sum(
            header.sectionSize(i) for i in range(len(_FLATPAK_COLUMN_HEADERS))
        )
        viewport_width = dialog.table.viewport().width()
        assert header_width <= viewport_width, (
            f"columns need {header_width}px but only {viewport_width}px is visible"
        )
        # The size column is the last one and is the one that used to be cut off.
        assert len(_FLATPAK_COLUMN_HEADERS) - 1 == _SIZE_COLUMN
        assert header.sectionSize(_SIZE_COLUMN) > 0
    finally:
        dialog.close()
        qapp.processEvents()


def test_flatpak_sorted_heading_has_room_for_its_sort_arrow(qapp) -> None:
    """``KIND\u25be`` used to be painted over the heading text itself."""
    dialog = FlatpakInventoryDialog()
    dialog.show()
    try:
        serial = dialog.begin_loading()
        dialog.show_inventory_if_current(
            serial, FlatpakInventory(entries=(_flatpak_entry(),))
        )
        for _ in range(4):
            qapp.processEvents()

        header = dialog.table.horizontalHeader()
        sorted_column = header.sortIndicatorSection()
        assert header.isSortIndicatorShown()
        metrics = QFontMetrics(header.font())
        label = _FLATPAK_COLUMN_HEADERS[sorted_column].upper()
        needed = metrics.horizontalAdvance(label) + int(
            _HEADER_LETTER_SPACING * len(label)
        )
        usable = header.sectionSize(sorted_column) - _SORT_INDICATOR_WIDTH
        assert usable >= needed, (
            f"heading {label!r} needs {needed}px but only {usable}px is left "
            "beside the sort arrow"
        )
    finally:
        dialog.close()
        qapp.processEvents()


def test_flatpak_inventory_columns_keep_headings_readable(qapp) -> None:
    dialog = FlatpakInventoryDialog()
    dialog.show()
    try:
        serial = dialog.begin_loading()
        dialog.show_inventory_if_current(
            serial, FlatpakInventory(entries=(_flatpak_entry(),))
        )
        for _ in range(4):
            qapp.processEvents()

        header = dialog.table.horizontalHeader()
        metrics = QFontMetrics(header.font())
        for column, label in enumerate(_FLATPAK_COLUMN_HEADERS):
            needed = metrics.horizontalAdvance(label.upper())
            assert header.sectionSize(column) >= needed, (
                f"heading {label!r} is truncated in a "
                f"{header.sectionSize(column)}px column"
            )
    finally:
        dialog.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Inspector data paths that the visual review exercised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    (
        (0, "0.0 B"),
        (512, "512.0 B"),
        (1024, "1.0 KB"),
        (12_582_912, "12.0 MB"),
        (1_073_741_824, "1.0 GB"),
        (2_199_023_255_552, "2.0 TB"),
    ),
)
def test_byte_sizes_use_one_shared_format(qapp, size_bytes: int, expected: str) -> None:
    """The tree and the file manifest must not disagree about the same size."""
    assert format_byte_size(size_bytes) == expected
    assert _package(size_bytes=size_bytes).human_size == expected


def test_empty_files_show_a_real_zero_size_instead_of_a_blank_cell(qapp) -> None:
    """An empty RPM-owned file is data, not a failed size query."""
    panel = PackageInspectorPanel()
    panel.show()
    try:
        panel.set_package_info(_package())
        panel.set_package_files(
            "gnome-shell",
            [
                PackageFileInfo(path="/var/lock/example.lock", size_bytes=0),
                PackageFileInfo(path="/usr/bin/gnome-shell", size_bytes=1024),
            ],
        )
        sizes = [
            panel.files_table.item(row, 1).text()
            for row in range(panel.files_table.rowCount())
        ]
        assert sizes == ["0.0 B", "1.0 KB"]
    finally:
        panel.close()
        qapp.processEvents()


def test_inspector_renders_files_verification_and_changelog(qapp) -> None:
    panel = PackageInspectorPanel()
    panel.show()
    try:
        panel.set_package_info(_package())
        panel.set_package_files(
            "gnome-shell",
            [PackageFileInfo(path="/usr/bin/gnome-shell", size_bytes=1024)],
        )
        panel.set_package_verification(
            "gnome-shell",
            [
                FileVerificationResult(
                    path="/etc/gnome-shell/environments",
                    status_flags="S.5....T.",
                    is_config=True,
                    is_missing=False,
                    size_differs=True,
                    mode_differs=False,
                    digest_differs=True,
                    mtime_differs=True,
                    raw_line="S.5....T.  c /etc/gnome-shell/environments",
                )
            ],
        )
        assert panel.files_table.rowCount() == 1
        assert "Modified: 1" in panel.lbl_verify_status.text()
        panel.set_reverse_dependencies(
            "gnome-shell",
            [
                DependencyNode(
                    raw_requirement="gnome-shell",
                    resolved_package_name="gnome-tweaks",
                    is_reverse=True,
                )
            ],
        )
        assert panel.reverse_list.count() == 1
    finally:
        panel.close()
        qapp.processEvents()


@pytest.mark.parametrize("count", (1, 2, 5))
def test_reverse_dependency_status_uses_correct_pluralization(qapp, count: int) -> None:
    """``Found 1 packages`` reads as a bug even when the data is right."""
    panel = PackageInspectorPanel()
    panel.show()
    try:
        panel.set_package_info(_package())
        panel.set_reverse_dependencies(
            "gnome-shell",
            [
                DependencyNode(
                    raw_requirement="gnome-shell",
                    resolved_package_name=f"dependent-{index}",
                    is_reverse=True,
                )
                for index in range(count)
            ],
        )
        label = panel.rev_status_label.text()
        expected_noun = "package" if count == 1 else "packages"
        assert f"Found {count} {expected_noun} depending on this:" == label
    finally:
        panel.close()
        qapp.processEvents()


def test_files_tab_keeps_the_reason_it_could_not_load_the_manifest(qapp) -> None:
    """The status-bar message expires; the empty tab must explain itself."""
    panel = PackageInspectorPanel()
    panel.show()
    try:
        panel.set_package_info(_package())
        panel.set_package_files_error(
            "gnome-shell",
            "Could not read the package file list: the command is not installed on this system",
        )
        text = panel.files_status_label.text()
        assert text.startswith("Could not load the installed file manifest.")
        assert "the command is not installed on this system" in text
        assert "the command is not installed on this system" in (
            panel.files_status_label.toolTip()
        )
    finally:
        panel.close()
        qapp.processEvents()


def test_button_labels_have_no_leading_space() -> None:
    """A leading space in a label leaves every text-only button misaligned.

    Icon buttons from a themed icon set look fine, but when the host has no
    icon theme (minimal desktops, containers) ``QIcon.fromTheme`` returns a
    null icon and the stray space shows up as an off-centre caption.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders: list[str] = []
    pattern = re.compile(r'QPushButton\(\s*"\s')
    for directory in ("ui",):
        for name in sorted(os.listdir(os.path.join(project_root, directory))):
            if not name.endswith(".py"):
                continue
            path = os.path.join(project_root, directory, name)
            with open(path, encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    if pattern.search(line):
                        offenders.append(f"{directory}/{name}:{number}")

    assert offenders == []


def test_dependency_rows_mark_an_absent_size_instead_of_leaving_it_blank(
    qapp, qtbot
) -> None:
    """A blank SIZE cell beside filled ones reads like a failed lookup."""
    from PyQt6.QtGui import QImage, QPainter

    model = DependencyTreeModel()
    model.set_packages([_package()])
    model.attach_dependencies(
        "gnome-shell",
        [
            DependencyNode(
                raw_requirement="libgtk-4.so.1()(64bit)", resolved_package_name="gtk4"
            )
        ],
    )
    root = model.index(0, 0)
    dependency = model.index(0, DependencyTreeModel.COL_SIZE, root)
    assert dependency.data(Qt.ItemDataRole.DisplayRole) == ""

    view = DendroTreeView()
    view.setModel(model)
    delegate = PackageTreeItemDelegate(view)
    view.setItemDelegate(delegate)
    view.resize(900, 300)
    view.expandAll()
    qtbot.addWidget(view)
    view.show()
    qapp.processEvents()

    # The placeholder is painted, not stored in the model, so assert on the
    # delegate's own decision for this cell.
    assert delegate.size_placeholder_text(dependency) == NO_VALUE_PLACEHOLDER
    image = QImage(900, 300, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        view.render(painter)
    finally:
        painter.end()


def test_protected_removal_cta_uses_the_destructive_button_style(qapp) -> None:
    """A plan that removes protected packages must not look like a routine apply."""
    from core.dnf5daemon import TransactionItem, TransactionPlan, TransactionRequest
    from ui.transaction_plan_dialog import TransactionPlanReviewDialog

    def _plan(critical: tuple[str, ...]) -> TransactionPlan:
        return TransactionPlan(
            token="token",
            session_path="/org/rpm/dnf/v0/session/1",
            request=TransactionRequest.from_values(removals=["glibc"]),
            items=(
                TransactionItem(
                    object_type="Package",
                    action="Remove",
                    reason="User",
                    attributes={},
                    package={
                        "name": "glibc",
                        "epoch": "0",
                        "version": "2.41",
                        "release": "5.fc43",
                        "arch": "x86_64",
                    },
                ),
            ),
            resolver_result=0,
            warnings=(),
            critical_removals=critical,
        )

    safe = TransactionPlanReviewDialog(_plan(()))
    unsafe = TransactionPlanReviewDialog(_plan(("glibc",)))
    try:
        assert safe.execute_button.objectName() == "ApplyButton"
        assert unsafe.execute_button.objectName() == "DangerButton"
        assert unsafe.execute_button.text() == "Execute Protected Removal"
        # The dangerous action must never be the default-activated button.
        assert unsafe.execute_button.autoDefault() is False
    finally:
        safe.close()
        unsafe.close()
        qapp.processEvents()


def test_dependency_version_column_never_shows_a_state_word(qapp) -> None:
    """``satisfied`` is a resolution state, not a version."""
    model = DependencyTreeModel()
    model.set_packages([_package()])
    model.attach_dependencies(
        "gnome-shell",
        [
            # No installed version is known, but the dependency is satisfied.
            DependencyNode(
                raw_requirement="libgtk-4.so.1()(64bit)",
                resolved_package_name="gtk4",
                is_satisfied=True,
            ),
            # A requested constraint is the only version-ish fact available.
            DependencyNode(
                raw_requirement="python3dist(x) >= 2.0",
                resolved_package_name="python3-requests",
                version_constraint=">= 2.0",
                is_satisfied=True,
            ),
            # A known installed version wins, and the constraint stays visible.
            DependencyNode(
                raw_requirement="libm.so.6()(64bit)",
                resolved_package_name="glibc",
                version_constraint=">= 2.38",
                installed_version="2.41-5.fc43",
            ),
        ],
    )
    root = model.index(0, 0)
    versions = [
        model.index(row, DependencyTreeModel.COL_VERSION, root).data(
            Qt.ItemDataRole.DisplayRole
        )
        for row in range(model.rowCount(root))
    ]

    assert versions == ["", ">= 2.0", "2.41-5.fc43 (>= 2.38)"]
    assert all("satisfied" not in str(value).lower() for value in versions)


def test_drawer_preview_escapes_markup_and_wraps(qapp) -> None:
    drawer = TransactionDrawer()
    drawer.show()
    try:
        drawer.set_transaction_preview(["<b>evil</b>"], ["a&b"], ["c"])
        text = drawer.summary_label.text()
        assert "&lt;b&gt;evil&lt;/b&gt;" in text
        assert "a&amp;b" in text
        assert drawer.commit_btn.isEnabled()
        drawer.set_transaction_preview([], [], [])
        assert not drawer.commit_btn.isEnabled()
    finally:
        drawer.close()
        qapp.processEvents()


def test_button_helper_is_a_real_push_button(qapp) -> None:
    """Guards the close-button tests against a widget-type regression."""
    drawer = TransactionDrawer()
    try:
        assert isinstance(drawer.close_btn, QPushButton)
    finally:
        drawer.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Report: "a dark navy colour appears underneath some text"
#
# The global QWidget rule used to set a background colour, so every label that
# a stylesheet rule matched painted its own opaque rectangle. Inside a card
# (surface #24273a) or the About header (a gradient card) each label therefore
# sat on a visibly darker base-coloured patch. The fix is that a QWidget has no
# background of its own; containers opt in through their own rules.
# ---------------------------------------------------------------------------


def test_labels_do_not_paint_their_own_background(qapp) -> None:
    stylesheet = get_theme_stylesheet("dark")
    # The universal rule must neutralize the background, not fill it.
    universal = re.search(r"\nQWidget \{(?P<body>[^}]*)\}", stylesheet)
    assert universal is not None, "the global QWidget rule must still exist"
    body = universal.group("body")
    assert "transparent" in body
    assert THEMES_CONFIG["dark"]["bg_base"] not in body
    # Top-level windows still paint a surface, otherwise the window is unpainted.
    assert "QMainWindow" in stylesheet and "QDialog" in stylesheet


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_card_labels_render_on_the_card_surface(qapp, theme) -> None:
    """Sample rendered pixels: a card label must not sit on a different colour."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet(theme))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        panel.resize(panel.minimumWidth(), 640)
        for _ in range(4):
            app.processEvents()
        panel.set_package_info(_package())
        for _ in range(6):
            app.processEvents()
            panel.tab_overview.layout().activate()
            panel.safety_card.layout().activate()

        card = panel.safety_card
        image = panel.grab().toImage()
        card_colour = QColor(THEMES_CONFIG[theme]["bg_card"])

        # A pixel just inside the card's right edge is the card's own surface.
        origin = card.mapTo(panel, card.rect().topRight())
        middle = card.mapTo(panel, card.rect().center())
        surface = image.pixelColor(origin.x() - 6, middle.y())
        assert surface.name() == card_colour.name()

        for label in (
            panel.taxonomy_badge,
            panel.provenance_label,
            panel.rationale_label,
        ):
            rect = label.rect().translated(label.mapTo(panel, label.rect().topLeft()))
            # The trailing edge of a wrapped label is never covered by glyphs.
            pixel = image.pixelColor(rect.right() - 1, rect.center().y())
            assert pixel.name() == card_colour.name(), label.objectName()
    finally:
        panel.close()
        qapp.processEvents()


def test_about_header_text_has_no_opaque_band_behind_it(qapp) -> None:
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    dialog = DendroSettingsDialog(host_environment=HostEnvironment.traditional())
    try:
        dialog.resize(760, 860)
        dialog.show()
        dialog.tabs.setCurrentIndex(1)
        for _ in range(6):
            app.processEvents()

        header_contents = [
            label
            for label in dialog.findChildren(object)
            if hasattr(label, "objectName")
            and label.objectName() in ("SubtleHelpLabel", "")
            and getattr(label, "text", lambda: "")()
            and ("Dendro" in label.text() or "Visual Package Manager" in label.text())
        ]
        assert header_contents, "the About header labels must be present"
        for label in header_contents:
            assert label.autoFillBackground() is False
    finally:
        dialog.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Report: "the theme-change dropdown has become too short in height"
#
# QComboBox had no rule at all, so it collapsed to the font height (~22px)
# while every button and input was 34px.
# ---------------------------------------------------------------------------


def test_dropdown_and_inputs_share_one_control_height(qapp) -> None:
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    settings = DendroSettingsDialog(host_environment=HostEnvironment.traditional())
    try:
        settings.show()
        settings.resize(760, 640)
        for _ in range(4):
            app.processEvents()
        combo = settings.theme_selector
        # A bare QComboBox is ~22px; a themed control must clear that clearly.
        assert combo.height() >= 30, f"dropdown collapsed to {combo.height()}px"

        # The point of the fix is that a drop-down lines up with the other
        # input controls rather than sitting shorter than all of them.
        from PyQt6.QtWidgets import QLineEdit

        reference = QLineEdit()
        reference.setStyleSheet(get_theme_stylesheet("dark"))
        # Give it exactly the height the stylesheet asks for, so the comparison
        # is against the control's own metrics rather than the container size.
        reference.resize(240, reference.sizeHint().height())
        reference.show()
        for _ in range(3):
            app.processEvents()
        assert abs(combo.height() - reference.height()) <= 2, (
            f"dropdown is {combo.height()}px but inputs are {reference.height()}px"
        )
        reference.close()
    finally:
        settings.close()
        qapp.processEvents()


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_checkbox_indicator_is_visible_against_its_surface(qapp, theme) -> None:
    """A disabled-by-default native indicator was invisible on dark surfaces."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet(theme))
    from PyQt6.QtWidgets import QCheckBox, QVBoxLayout, QWidget

    host = QWidget()
    try:
        layout = QVBoxLayout(host)
        box = QCheckBox("Toggle")
        layout.addWidget(box)
        host.resize(240, 60)
        host.show()
        for _ in range(3):
            app.processEvents()

        image = host.grab().toImage()
        origin = box.mapTo(host, box.rect().topLeft())
        surface = QColor(THEMES_CONFIG[theme]["bg_base"])
        # Scan the indicator square for the strongest deviation from the
        # surface; a drawn outline must stand clearly apart from it.
        best = 0.0
        for y in range(origin.y(), origin.y() + 22):
            for x in range(origin.x(), origin.x() + 22):
                pixel = image.pixelColor(x, y)
                best = max(
                    best,
                    abs(pixel.lightness() - surface.lightness()),
                )
        assert best >= 40, f"checkbox outline is nearly invisible (delta {best})"
    finally:
        host.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Report: inspector tabs collapsed to "Overv...", "Fi...", "Require..."
#
# Two separate faults combined: the stylesheet pinned an 11px font while Qt
# measured the tab strip with the application font, and the semibold weight the
# stylesheet painted with was wider than the metrics Qt sized the tabs from.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("point_size", [9, 11, 13])
def test_inspector_tab_labels_are_never_elided(qapp, point_size) -> None:
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    from PyQt6.QtGui import QFont

    previous = app.font()
    app.setFont(QFont("DejaVu Sans", point_size))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        panel.resize(panel.minimumWidth(), 720)
        for _ in range(4):
            app.processEvents()

        bar = panel.tabs.tabBar()
        metrics = bar.fontMetrics()
        for index in range(panel.tabs.count()):
            text = panel.tabs.tabText(index)
            available = bar.tabRect(index).width()
            # Paint and measurement must agree, or Qt elides a label that fits.
            painted = metrics.horizontalAdvance(text)
            assert available >= painted + 8, (
                f"{text!r} needs {painted}px but has {available}px at {point_size}pt"
            )
        assert panel.minimumWidth() >= 360
    finally:
        panel.close()
        app.setFont(previous)
        qapp.processEvents()


def test_tab_font_matches_the_weight_the_stylesheet_paints(qapp) -> None:
    """Sizing and painting must use the same weight, or labels elide."""
    panel = PackageInspectorPanel()
    try:
        bar = panel.tabs.tabBar()
        assert bar.font().weight() >= 600
        stylesheet = get_theme_stylesheet("dark")
        rule = re.search(
            r"QTabWidget#InspectorTabs > QTabBar::tab \{(?P<body>[^}]*)\}",
            stylesheet,
        )
        assert rule is not None
        body = rule.group("body")
        assert "font-weight: 600" in body
        # A font-size inside the tab rule desynchronizes measurement from paint.
        assert "font-size" not in body
    finally:
        panel.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Report: Settings help text and the repository banner were cut mid-sentence
# ---------------------------------------------------------------------------


def test_settings_help_text_is_fully_rendered(qapp) -> None:
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    dialog = DendroSettingsDialog(host_environment=HostEnvironment.traditional())
    try:
        dialog.show()
        dialog.resize(700, 720)
        for _ in range(6):
            app.processEvents()

        wrapped = [
            label
            for label in dialog.findChildren(object)
            if hasattr(label, "wordWrap")
            and label.wordWrap()
            and getattr(label, "text", lambda: "")()
            and hasattr(label, "heightForWidth")
        ]
        assert wrapped, "the wrapped help labels must exist"
        for label in wrapped:
            assert label.height() >= label.heightForWidth(label.width()), label.text()[
                :48
            ]
    finally:
        dialog.close()
        qapp.processEvents()


def test_repository_capability_banner_is_fully_rendered(qapp) -> None:
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    from ui.repo_dialog import RepoManagerDialog

    dialog = RepoManagerDialog()
    try:
        dialog.show()
        dialog.resize(900, 720)
        for _ in range(6):
            app.processEvents()
        banner = dialog.capability_label
        assert banner.wordWrap()
        assert banner.height() >= banner.heightForWidth(banner.width())
        # The banner must not stop at its own edge.
        assert banner.contentsRect().right() <= banner.width()
    finally:
        dialog.close()
        qapp.processEvents()


def test_transaction_drawer_paints_a_surface(qapp) -> None:
    """The drawer is a plain QWidget in a splitter, so it needs its own surface.

    Once the global QWidget rule stopped filling backgrounds, a container with
    no rule of its own rendered as an unpainted (white) rectangle in dark mode.
    """
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    drawer = TransactionDrawer()
    try:
        drawer.resize(700, 420)
        drawer.show()
        for _ in range(4):
            app.processEvents()

        image = drawer.grab().toImage()
        surface = QColor(THEMES_CONFIG["dark"]["bg_surface"])
        # Sample well inside the widget away from any child.
        pixel = image.pixelColor(drawer.width() // 2, 8)
        assert pixel.name() == surface.name(), pixel.name()
        assert drawer.objectName() == "TransactionDrawer"
    finally:
        drawer.close()
        qapp.processEvents()


def test_flatpak_headings_fit_even_before_inventory_arrives(qapp) -> None:
    """Column sizing must run for the empty state, and measure the bold weight.

    With no entries the table kept Qt's 100px default per column, and headings
    are painted bold while they were measured regular, so the longest headings
    were cut. Both are checked against the rendered section widths.
    """
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    dialog = FlatpakInventoryDialog()
    try:
        dialog.show()
        for _ in range(6):
            app.processEvents()

        header = dialog.table.horizontalHeader()
        assert dialog.table.rowCount() == 0
        widths = [
            header.sectionSize(column) for column in range(len(_FLATPAK_COLUMN_HEADERS))
        ]
        # Every column must have escaped the do-nothing default.
        assert len(set(widths)) > 1, widths
        # The row must fit the viewport, so no column is pushed out of reach.
        assert sum(widths) <= dialog.table.viewport().width() + 1
        # The longest heading keeps room for its own text and the sort arrow.
        heading = _FLATPAK_COLUMN_HEADERS[_SIZE_COLUMN].upper()
        bold = dialog.font()
        bold.setWeight(bold.Weight.Bold)
        needed = QFontMetrics(bold).horizontalAdvance(heading)
        assert widths[_SIZE_COLUMN] > needed
    finally:
        dialog.close()
        qapp.processEvents()


def test_stylesheet_survives_an_unwritable_marker_cache(monkeypatch, tmp_path) -> None:
    """The control markers are written to a cache dir; failing must not crash.

    A read-only or absent cache location is realistic (a locked-down home
    directory), and an ``image: url()`` with no path is invalid QSS that would
    stop the indicator rules applying.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))

    for theme in ("dark", "light"):
        stylesheet = get_theme_stylesheet(theme)
        assert "QCheckBox::indicator" in stylesheet
        assert "url()" not in stylesheet


# ---------------------------------------------------------------------------
# Report: the appearance drop-down overlapped its help text
#
# The dialog was created at a fixed 560x400, smaller than the height its content
# needs once the appearance description wraps to two lines. Qt squeezed the
# layout, so the 32px combo and the label beneath it were laid out on top of
# each other. The dialog now sizes to its content and refuses to be squashed.
# ---------------------------------------------------------------------------


def test_settings_controls_never_overlap_each_other(qapp) -> None:
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    dialog = DendroSettingsDialog(host_environment=HostEnvironment.traditional())
    try:
        dialog.show()
        for _ in range(6):
            app.processEvents()

        combo = dialog.theme_selector
        descriptions = [
            label
            for label in dialog.findChildren(object)
            if label.__class__.__name__ == "QLabel"
            and "Follow System keeps Dendro"
            in (getattr(label, "text", lambda: "")() or "")
        ]
        assert descriptions, "the appearance help text must exist"
        description = descriptions[0]

        # The help text sits directly under the combo: no vertical overlap, and
        # it keeps the full height its wrapped text needs.
        assert description.geometry().top() > combo.geometry().bottom(), (
            f"description starts at {description.geometry().top()} but the combo "
            f"ends at {combo.geometry().bottom()}"
        )
        assert description.height() >= description.heightForWidth(description.width())

        # Squashing the window must not reintroduce the overlap.
        for width, height in ((560, 400), (500, 360), (460, 320)):
            dialog.resize(width, height)
            for _ in range(4):
                app.processEvents()
            assert description.geometry().top() > combo.geometry().bottom(), (
                f"overlap returned when resized to {width}x{height}"
            )
            assert description.height() >= description.heightForWidth(
                description.width()
            )
    finally:
        dialog.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Report: the right sidebar looked empty once the text boxes stopped stretching
#
# The overview page has to fill its space deliberately rather than leaving an
# unbounded void, the description must not sit in a fixed empty box, and every
# tab has to present an explicit empty state instead of a bare surface.
# ---------------------------------------------------------------------------


def test_overview_description_card_follows_its_text(qapp) -> None:
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        panel.resize(panel.minimumWidth(), 860)
        for _ in range(4):
            app.processEvents()

        # Nothing selected yet: no description box at all.
        assert not panel.description_card.isVisible()

        panel.set_package_info(_package())
        for _ in range(6):
            app.processEvents()
            panel.tab_overview.layout().activate()

        assert panel.description_card.isVisible()
        # The box tracks its text instead of claiming the whole page or
        # collapsing to nothing.
        body = panel.desc_text.document().size().height()
        assert panel.desc_text.height() >= int(body)
        assert panel.desc_text.height() <= panel._description_max_height()

        # A short description must not reserve a large empty area.
        assert panel.description_card.height() < panel.tab_overview.height() * 0.75

        panel.clear()
        for _ in range(4):
            app.processEvents()
        assert not panel.description_card.isVisible()
    finally:
        panel.close()
        qapp.processEvents()


def test_content_tabs_show_an_explicit_empty_state(qapp) -> None:
    """An empty tab must say so, not present a bare surface."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        panel.resize(panel.minimumWidth(), 800)
        for _ in range(4):
            app.processEvents()

        # The pages themselves are only shown when their tab is current, so the
        # widget's own visibility relative to its page is what matters here.
        assert not panel.files_table.isVisibleTo(panel.tab_files)
        assert panel.files_empty_label.isVisibleTo(panel.tab_files)

        # Required By behaves the same way.
        assert not panel.reverse_list.isVisibleTo(panel.tab_reverse)
        assert panel.reverse_empty_label.isVisibleTo(panel.tab_reverse)

        # Changelog and Scripts announce their state in the surface itself
        # rather than presenting an empty card on first launch.
        assert "changelog" in panel.changelog_browser.toPlainText().lower()
        assert "scriptlet" in panel.scriptlets_browser.toPlainText().lower()

        # A populated surface replaces the note rather than stacking on it.
        from core.backend import PackageFileInfo

        panel.set_package_info(_package())
        panel.set_package_files(
            "gnome-shell",
            [PackageFileInfo(path="/usr/bin/gnome-shell", size_bytes=4096)],
        )
        for _ in range(6):
            app.processEvents()
        assert panel.files_table.isVisibleTo(panel.tab_files)
        assert not panel.files_empty_label.isVisibleTo(panel.tab_files)
        assert panel.files_table.rowCount() == 1
    finally:
        panel.close()
        qapp.processEvents()


def test_inspector_sections_share_one_visual_language(qapp) -> None:
    """Overview cards and tab surfaces must not look like different widgets."""
    panel = PackageInspectorPanel()
    try:
        # The overview presents named sections, each with a heading.
        assert panel.safety_card.objectName() == "AICard"
        assert panel.description_card.objectName() == "InspectorSectionCard"
        titles = [
            child
            for child in panel.tab_overview.findChildren(object)
            if getattr(child, "objectName", lambda: "")() == "InspectorSectionTitle"
        ]
        assert len(titles) >= 2, "both overview sections need a heading"

        # The scrollable content surfaces are cards too, not frameless boxes.
        stylesheet = get_theme_stylesheet("dark")
        # The manifest is a card assembled from two views, so the rounded frame
        # belongs to the container that holds both of them. Drawing half-frames
        # on each view was what left the card with a seam down its middle.
        for name in (
            "InspectorFilesCard",
            "InspectorReverseList",
            "InspectorChangelogBrowser",
            "InspectorScriptletsBrowser",
        ):
            rule = re.search(rf"[^\n]*{name}[^{{]*\{{(?P<body>[^}}]*)\}}", stylesheet)
            assert rule is not None, name
            assert "border-radius" in rule.group("body"), name
    finally:
        panel.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Report: the metadata card was "lifeless and static" -- one flat run of
# "Label: value" strings where every fact had the same weight
#
# It is now a table: a fixed-width field-name gutter, values on one left edge,
# three labelled groups divided by rules, a muted weight for facts the host did
# not report, and a status chip for the update state.
# ---------------------------------------------------------------------------


def test_metadata_card_is_a_table_not_a_list_of_sentences(qapp) -> None:
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        panel.resize(panel.minimumWidth(), 900)
        for _ in range(4):
            app.processEvents()
        panel.set_package_info(_package("firefox", license="GPL-2.0-only"))
        for _ in range(6):
            app.processEvents()

        # Field names and values are separate widgets, so the name carries the
        # muted small style while the value carries full contrast.
        assert panel._stat_names, "the card must expose named field cells"
        assert len(panel._stat_names) == len(panel._stat_values)

        # Every value starts on the same vertical line: that is what makes the
        # card read as a table.
        left_edges = {
            value.mapTo(panel, value.rect().topLeft()).x()
            for attribute, value in panel._stat_values.items()
            if attribute != "lbl_upgrade_status"
        }
        assert len(left_edges) == 1, f"values start at {sorted(left_edges)}"

        # And every field name shares one right edge.
        right_edges = {
            name.mapTo(panel, name.rect().topRight()).x()
            for name in panel._stat_names.values()
        }
        assert len(right_edges) == 1, f"names end at {sorted(right_edges)}"

        # The groups are separated by rules rather than only by whitespace.
        separators = [
            child
            for child in panel.findChildren(object)
            if getattr(child, "objectName", lambda: "")() == "StatSeparator"
        ]
        assert len(separators) >= 2
    finally:
        panel.close()
        qapp.processEvents()


def test_metadata_card_marks_absent_facts_as_muted(qapp) -> None:
    """A fact the host did not report must not look like an emphatic value."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        for _ in range(4):
            app.processEvents()

        # Nothing is selected: every value is an unknown, shown muted.
        for attribute, value in panel._stat_values.items():
            assert value.property("muted") is True, attribute

        panel.set_package_info(_package("firefox"))
        for _ in range(6):
            app.processEvents()
        for attribute in ("lbl_size", "lbl_arch", "lbl_license", "lbl_repo"):
            assert panel._stat_values[attribute].property("muted") is False, attribute
    finally:
        panel.close()
        qapp.processEvents()


@pytest.mark.parametrize(
    ("has_update", "version", "expected_tone"),
    [(True, "133.0.1", "update"), (False, "", "current")],
)
def test_metadata_card_update_state_carries_a_tone(
    qapp, has_update, version, expected_tone
) -> None:
    """The update cell doubles as a status chip, keyed off the data."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        for _ in range(4):
            app.processEvents()
        panel.set_package_info(
            _package(
                "firefox",
                has_update=has_update,
                available_update_version=version,
            )
        )
        for _ in range(6):
            app.processEvents()

        # The tone lives on the chip frame; the label inside it stays plain text.
        chip = panel._stat_chips["lbl_upgrade_status"]
        chip_text = panel._stat_values["lbl_upgrade_status"]
        assert chip.property("tone") == expected_tone
        if has_update:
            # The chip shows the version on its own; the attribute keeps the
            # plain-text form callers already read.
            assert version in chip_text.text()
            assert panel.lbl_upgrade_status.text() == f"Update: -> {version}"
        else:
            assert panel.lbl_upgrade_status.text() == "Update: Up to Date"

        # The stylesheet must resolve each tone, or the chip is unstyled.
        stylesheet = get_theme_stylesheet("dark")
        for tone in ("current", "update", "unknown"):
            assert f'[tone="{tone}"]' in stylesheet
    finally:
        panel.close()
        qapp.processEvents()


def test_metadata_card_stays_compact(qapp) -> None:
    """A table layout must not cost the narrow dock a screenful of height."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        panel.resize(panel.minimumWidth(), 900)
        for _ in range(4):
            app.processEvents()
        panel.set_package_info(_package("firefox"))
        for _ in range(6):
            app.processEvents()

        card = panel._stats_card
        # The previous flat list was ~120px; the grouped table has to stay well
        # short of crowding the tabs off the page. This is the portable budget:
        # the card does not depend on the font, so a fixed pixel ceiling means
        # the same thing on every platform.
        assert card.height() <= 260, f"metadata card grew to {card.height()}px"
        # It must also leave the page usable. Do not express that as a share of
        # tab_overview: the tab strip grows with the font, so the page shrinks at
        # the same time the card grows, and a fixed 50% share moves under the
        # assertion. Measured: 0.42 at 9pt, 0.63 at 13pt on the same layout.
        assert panel.tab_overview.height() - card.height() >= 120, (
            f"the metadata card leaves only "
            f"{panel.tab_overview.height() - card.height()}px for the page"
        )
    finally:
        panel.close()
        qapp.processEvents()


def _contrast_ratio(foreground: str, background: str) -> float:
    """WCAG relative-luminance contrast between two #rrggbb colours."""

    def channel(value: int) -> float:
        srgb = value / 255.0
        if srgb <= 0.03928:
            return srgb / 12.92
        return ((srgb + 0.055) / 1.055) ** 2.4

    def luminance(colour: str) -> float:
        digits = colour.lstrip("#")
        red, green, blue = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)

    lighter, darker = sorted(
        (luminance(foreground), luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_metadata_card_separators_are_actually_visible(qapp) -> None:
    """A rule that matches the card surface is not a separator.

    The subtle border colour sits within ~1.0 contrast of the card background on
    the dark theme, so a divider drawn in it is invisible -- which defeats the
    sectioning the layout is there to provide.
    """
    app = qapp
    for theme in ("dark", "light"):
        app.setStyleSheet(get_theme_stylesheet(theme))
        panel = PackageInspectorPanel()
        try:
            panel.show()
            for _ in range(4):
                app.processEvents()
            panel.set_package_info(_package("firefox"))
            for _ in range(6):
                app.processEvents()

            card = panel._stats_card
            rules = [
                child
                for child in card.findChildren(QFrame)
                if child.objectName() == "StatSeparator"
            ]
            assert len(rules) == 2, theme

            # Read the colour the stylesheet really resolved for the rule, not a
            # token we assume it used, then check it against the card surface.
            sheet = get_theme_stylesheet(theme)
            block = re.search(r"QFrame#StatSeparator\s*\{(.*?)\}", sheet, re.DOTALL)
            assert block is not None, theme
            declared = re.search(
                r"background-color:\s*(#[0-9a-fA-F]{6})", block.group(1)
            )
            assert declared is not None, theme
            rule_colour = declared.group(1)
            card_colour = THEMES_CONFIG[theme]["bg_card"]
            assert _contrast_ratio(rule_colour, card_colour) > 1.1, (
                f"{theme}: separator {rule_colour} on {card_colour}"
            )

            # And each rule is a real hairline that spans the card's content.
            for rule in rules:
                assert rule.isVisibleTo(panel), theme
                assert rule.width() > card.width() * 0.5, theme
        finally:
            panel.close()
            qapp.processEvents()


def test_metadata_status_chip_keeps_its_text_on_one_line(qapp) -> None:
    """The chip must reserve room for its own padding.

    A QLabel's stylesheet padding does not feed its size hint, so a chip styled
    only with padding measures one line, paints its text inset by padding it
    never reserved, and wraps the tail onto a second line the box then clips.
    The padding is layout margin instead, and this pins that.
    """
    app = qapp
    for theme in ("dark", "light"):
        app.setStyleSheet(get_theme_stylesheet(theme))
        for kind, expected in (
            (HostEnvironmentKind.ATOMIC_OSTREE, "Managed by OS Image"),
            (None, "Up to Date"),
        ):
            panel = PackageInspectorPanel()
            try:
                environment = (
                    HostEnvironment(kind=kind)
                    if kind is not None
                    else HostEnvironment.traditional()
                )
                panel.set_host_environment(environment)
                panel.show()
                panel.resize(panel.minimumWidth(), 900)
                for _ in range(4):
                    app.processEvents()
                panel.set_package_info(_package("firefox"))
                for _ in range(6):
                    app.processEvents()

                chip = panel._stat_chips["lbl_upgrade_status"]
                text = panel._stat_values["lbl_upgrade_status"]
                assert text.text() == expected, theme
                assert text.wordWrap() is False, theme

                # The text must fit inside the chip with the margins intact.
                horizontal = (
                    chip.layout().contentsMargins().left()
                    + chip.layout().contentsMargins().right()
                )
                needed = text.sizeHint().width() + horizontal
                assert chip.width() >= needed, (
                    f"{theme}/{kind}: chip {chip.width()} < {needed}"
                )
                # And one line of text must fit the chip's height.
                vertical = (
                    chip.layout().contentsMargins().top()
                    + chip.layout().contentsMargins().bottom()
                )
                assert chip.height() >= text.sizeHint().height() + vertical, theme
            finally:
                panel.close()
                qapp.processEvents()


# ---------------------------------------------------------------------------
# Report: a long path in the Files tab was elided with no way to reach the tail
#
# The path column used to stretch to the dock width, so every long path was cut
# off at "webcompat-..." and the only workaround was widening the whole panel,
# which took space from the tree and the sidebar. It now sizes to its content
# and scrolls horizontally, while the Size column stays pinned to the right edge
# so it never scrolls out of view.
# ---------------------------------------------------------------------------

_LONG_FILE_PATH = (
    "/usr/lib64/firefox/browser/features/webcompat-reporter@mozilla.org.xpi"
)


def _files_panel(qapp, paths):
    """A shown panel on the Files tab with the given paths loaded."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    panel.set_host_environment(HostEnvironment.traditional())
    panel.show()
    panel.resize(panel.minimumWidth(), 700)
    for _ in range(4):
        app.processEvents()
    panel.set_package_info(_package("firefox"))
    panel.set_package_files(
        "firefox",
        [
            PackageFileInfo(path=path, size_bytes=1_234_567 + index * 91_337)
            for index, path in enumerate(paths)
        ],
    )
    panel.tabs.setCurrentWidget(panel.tab_files)
    for _ in range(10):
        app.processEvents()
        panel.tab_files.layout().activate()
    return panel


def test_long_file_paths_are_reachable_without_widening_the_panel(qapp) -> None:
    """A path too long for the dock must scroll into view, not elide away."""
    panel = _files_panel(qapp, [_LONG_FILE_PATH, "/etc/firefox/pref/local.js"])
    try:
        table = panel.files_table
        bar = table.horizontalScrollBar()

        # The column is wider than the dock, so the tail needs scrolling.
        assert table.columnWidth(0) > table.viewport().width()
        assert bar.maximum() > 0, "no horizontal range to reach the path tail"

        # Per-item scrolling would move a whole row per step and hide the tail
        # anyway, so the range has to be measured in pixels.
        assert (
            table.horizontalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
        )

        # Nothing is elided, so what is on screen is real text, not "...".
        assert table.textElideMode() == Qt.TextElideMode.ElideNone

        # Scrolling to the end brings the whole path inside the viewport.
        bar.setValue(bar.maximum())
        for _ in range(6):
            qapp.processEvents()
        assert bar.value() == bar.maximum()
        visible = table.viewport().width() + bar.value()
        assert visible >= table.columnWidth(0)
    finally:
        panel.close()
        qapp.processEvents()


def test_size_column_stays_readable_while_the_path_scrolls(qapp) -> None:
    """The pinned Size column must not scroll away with the path column."""
    panel = _files_panel(qapp, [_LONG_FILE_PATH, "/etc/firefox/pref/local.js"])
    try:
        table = panel.files_table
        size_view = panel.files_size_view
        bar = table.horizontalScrollBar()

        # The model holds one Size column; the path view hides it so the reader
        # sees a single Size column, and the pinned view shows that same one.
        assert table.isColumnHidden(1)
        assert size_view.isColumnHidden(0)

        # The pinned view is wide enough for the column it shows plus its own
        # frame, so the values are never clipped inside a narrower viewport.
        assert size_view.columnWidth(1) <= size_view.viewport().width()
        assert size_view.width() >= size_view.columnWidth(1)

        # The header and the rows line up, or the two halves read as two tables.
        assert (
            size_view.horizontalHeader().height() == table.horizontalHeader().height()
        )
        assert [size_view.rowHeight(row) for row in range(table.rowCount())] == [
            table.rowHeight(row) for row in range(table.rowCount())
        ]

        # The size text is fully readable, not clipped to "1.2".
        metrics = size_view.fontMetrics()
        for row in range(table.rowCount()):
            text = table.item(row, 1).text()
            assert metrics.horizontalAdvance(text) < size_view.viewport().width()

        # Scrolling the path column never moves the size column.
        before = size_view.width()
        bar.setValue(bar.maximum())
        for _ in range(4):
            qapp.processEvents()
        assert size_view.width() == before
        assert size_view.columnViewportPosition(1) == 0
    finally:
        panel.close()
        qapp.processEvents()


def _first_visible_row(view) -> int:
    """Row index whose top edge is at or below the viewport's top edge."""
    model = view.model()
    for row in range(model.rowCount()):
        if view.rowViewportPosition(row) >= 0:
            return row
    return model.rowCount() - 1


def test_file_manifest_rows_stay_aligned_when_scrolled(qapp) -> None:
    """The pinned column must follow the path column's vertical scroll.

    Equal scrollbar values are not enough on their own: the two views are
    separate widget hierarchies, so their bars can hold the same number and
    still be scrolled to different rows. This checks the rows that are actually
    on screen, which is what the reader sees.
    """
    panel = _files_panel(
        qapp, [f"/usr/share/doc/example/file-{i}.txt" for i in range(40)]
    )
    try:
        table = panel.files_table
        size_view = panel.files_size_view
        bar = table.verticalScrollBar()

        assert bar.maximum() > 0, "the manifest needs to overflow to test scrolling"
        for fraction in (0.0, 0.25, 0.5, 1.0):
            bar.setValue(int(bar.maximum() * fraction))
            for _ in range(6):
                qapp.processEvents()
            assert size_view.verticalScrollBar().value() == bar.value()
            assert _first_visible_row(size_view) == _first_visible_row(table)

        bar.setValue(0)
        for _ in range(6):
            qapp.processEvents()
        assert size_view.verticalScrollBar().value() == 0
        assert _first_visible_row(size_view) == 0
    finally:
        panel.close()
        qapp.processEvents()


def test_file_manifest_scrolls_in_the_same_unit_in_both_views(qapp) -> None:
    """Row units on one side and pixels on the other made sync impossible.

    Dragging the path column's scrollbar moved the pinned Size column by a few
    pixels per row, so it never reached the bottom of a long manifest.
    """
    panel = _files_panel(
        qapp, [f"/usr/share/doc/example/file-{i}.txt" for i in range(120)]
    )
    try:
        table = panel.files_table
        size_view = panel.files_size_view

        assert table.verticalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
        assert (
            size_view.verticalScrollMode()
            == QAbstractItemView.ScrollMode.ScrollPerPixel
        )

        # Identical extents is the contract that makes copying values valid.
        assert (
            table.verticalScrollBar().maximum()
            == size_view.verticalScrollBar().maximum()
        )
        assert (
            table.verticalScrollBar().pageStep()
            == size_view.verticalScrollBar().pageStep()
        )
        assert table.viewport().height() == size_view.viewport().height()
    finally:
        panel.close()
        qapp.processEvents()


def test_file_manifest_reaches_its_last_row(qapp) -> None:
    """The end of the scroll range must show the end of the manifest."""
    count = 200
    panel = _files_panel(
        qapp, [f"/usr/share/doc/example/deep/file-{i}.txt" for i in range(count)]
    )
    try:
        table = panel.files_table
        size_view = panel.files_size_view
        bar = table.verticalScrollBar()
        model = table.model()

        bar.setValue(bar.maximum())
        for _ in range(8):
            qapp.processEvents()

        last = model.rowCount() - 1
        assert _first_visible_row(table) < last
        assert _first_visible_row(size_view) == _first_visible_row(table), (
            "the Size column must reach the same row as the path column"
        )
        # The final row is on screen, not clipped past the end of the range.
        assert table.rowViewportPosition(last) < table.viewport().height()
        assert size_view.rowViewportPosition(last) < size_view.viewport().height()
    finally:
        panel.close()
        qapp.processEvents()


def test_long_path_tails_are_reachable_by_scrolling(qapp) -> None:
    """A path longer than the dock must scroll into view, not sit behind "...".

    The caller reported the tails as elided with no horizontal scrollbar after
    the two views were given a shared footer: the bar existed but lived outside
    the column that needed it, so nothing on screen looked scrollable.
    """
    long_path = (
        "/usr/lib64/firefox/browser/omni.ja/chrome/browser/content/browser/"
        "preferences/preferences.xhtml"
    )
    panel = _files_panel(qapp, [long_path, "/etc/firefox/pref/0.js"])
    try:
        table = panel.files_table
        bar = table.horizontalScrollBar()

        # Nothing is elided and the column really is wider than the viewport.
        assert table.textElideMode() == Qt.TextElideMode.ElideNone
        assert table.columnWidth(0) > table.viewport().width()
        assert bar.maximum() > 0, "long paths need a horizontal range"

        # The bar is inside the path column, visible, and themed to a size a
        # reader can actually see and drag.
        assert bar.isVisibleTo(table)
        assert bar.height() >= 6

        bar.setValue(bar.maximum())
        for _ in range(6):
            qapp.processEvents()
        assert table.viewport().width() + bar.value() >= table.columnWidth(0)
    finally:
        panel.close()
        qapp.processEvents()


def test_the_two_views_keep_equal_viewports_with_or_without_a_scrollbar(
    qapp,
) -> None:
    """The path column must not resize itself when its footer appears.

    A horizontal scrollbar that came and went shortened only the path column's
    viewport, and the two views then computed different vertical ranges from the
    same model, which is what let the Size column fall out of step.
    """
    long_path = "/usr/share/doc/example/" + "very-long-directory-name/" * 6 + "x.txt"
    panel = _files_panel(qapp, [long_path, "/etc/os-release"] * 12)
    try:
        table = panel.files_table
        size_view = panel.files_size_view

        # The bar belongs to the card's grid, not to the table's frame, so it is
        # switched off inside the table and laid out as its own row. That is
        # what makes the two halves agree: the bar's height is taken out once,
        # above both views, instead of out of the path viewport alone.
        assert (
            table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        bar = panel._files_hbar
        assert bar.parent() is panel._files_card
        assert bar.isVisible()

        def snapshot():
            return (
                table.viewport().height(),
                size_view.viewport().height(),
                table.verticalScrollBar().maximum(),
                size_view.verticalScrollBar().maximum(),
            )

        wide = snapshot()
        assert wide[0] == wide[1], "viewports must agree while paths overflow"
        assert wide[2] == wide[3], "vertical ranges must agree while paths overflow"

        # Filter down to the short path so the horizontal range collapses, then
        # filter back. The geometry must not have moved in the meantime.
        panel.file_search_input.setText("/etc/os-release")
        for _ in range(8):
            qapp.processEvents()
            panel.tab_files.layout().activate()
        assert table.horizontalScrollBar().maximum() == 0
        assert snapshot()[0] == snapshot()[1]

        panel.file_search_input.setText("")
        for _ in range(8):
            qapp.processEvents()
            panel.tab_files.layout().activate()
        assert table.horizontalScrollBar().maximum() > 0
        assert snapshot() == wide
    finally:
        panel.close()
        qapp.processEvents()


def test_short_file_paths_do_not_show_a_scrollbar(qapp) -> None:
    """Short paths must not be padded out to force a scrollbar."""
    panel = _files_panel(qapp, ["/bin/sh", "/etc/os-release"])
    try:
        table = panel.files_table
        # The column fills the tab when the content is short, so the header band
        # runs the full width of the path half, and there is nothing to scroll.
        assert table.columnWidth(0) >= table.viewport().width()
        assert table.horizontalScrollBar().maximum() == 0
        # An idle bar is a groove of dead pixels with a full-width handle in it,
        # which reads as a broken control. It is collapsed when nothing overflows
        # and the space goes to the rows instead.
        assert not panel._files_hbar.isVisible()
    finally:
        panel.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Report: Re-Scan found several dependents but the Required By body still said
# "No reverse dependencies reported yet"
#
# The list and the empty-state note are mutually exclusive surfaces, and only
# some of the paths that changed the list re-evaluated which one to show.
# Adding rows without that call left a populated list hidden behind the note.
# ---------------------------------------------------------------------------


def _reverse_panel(qapp, name="gnome-shell"):
    """A shown panel sitting on the Required By tab with a package selected."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    panel.set_host_environment(HostEnvironment.traditional())
    panel.show()
    panel.resize(panel.minimumWidth(), 700)
    for _ in range(4):
        app.processEvents()
    panel.set_package_info(_package(name))
    panel.tabs.setCurrentWidget(panel.tab_reverse)
    for _ in range(6):
        app.processEvents()
    return panel


def _dependents(*names):
    return [
        DependencyNode(
            raw_requirement="gnome-shell",
            resolved_package_name=name,
            is_reverse=True,
        )
        for name in names
    ]


def test_reverse_dependencies_are_visible_after_a_successful_scan(qapp) -> None:
    """Found dependents must be shown, not hidden behind the empty-state note."""
    panel = _reverse_panel(qapp)
    try:
        names = ["gnome-tweaks", "gnome-classic-session", "gnome-shell-extensions"]
        panel.set_reverse_dependencies("gnome-shell", _dependents(*names))
        for _ in range(6):
            qapp.processEvents()

        assert panel.reverse_list.count() == len(names)
        # The surface that carries the answer has to be the one on screen.
        assert panel.reverse_list.isVisibleTo(panel.tab_reverse)
        assert not panel.reverse_empty_label.isVisibleTo(panel.tab_reverse)
        # And the note must not still be claiming there is nothing to report.
        assert "no reverse dependencies" not in (
            panel.reverse_empty_label.text().lower()
        )

        shown = [
            panel.reverse_list.item(row).text().strip()
            for row in range(panel.reverse_list.count())
        ]
        assert shown == names
        assert "3 packages" in panel.rev_status_label.text()
    finally:
        panel.close()
        qapp.processEvents()


def test_reverse_dependency_states_replace_each_other(qapp) -> None:
    """Every state must leave the surface it needs visible and the other hidden."""
    panel = _reverse_panel(qapp)
    try:
        # No dependents is a real answer: the note explains it, the list is gone.
        panel.set_reverse_dependencies("gnome-shell", [])
        for _ in range(4):
            qapp.processEvents()
        assert panel.reverse_list.count() == 0
        assert panel.reverse_empty_label.isVisibleTo(panel.tab_reverse)
        assert "safe to remove" in panel.rev_status_label.text().lower()
        assert "safe to remove" in panel.reverse_empty_label.text().lower()

        # A failure is a sentinel row, not a safe-removal claim.
        panel.set_reverse_dependencies_error("gnome-shell", "rpm database is locked")
        for _ in range(4):
            qapp.processEvents()
        assert panel.reverse_list.count() == 1
        assert panel.reverse_list.isVisibleTo(panel.tab_reverse)
        assert not panel.reverse_empty_label.isVisibleTo(panel.tab_reverse)

        # Recovering from a failure must show the rows again.
        panel.set_reverse_dependencies("gnome-shell", _dependents("gnome-tweaks"))
        for _ in range(4):
            qapp.processEvents()
        assert panel.reverse_list.count() == 1
        assert panel.reverse_list.isVisibleTo(panel.tab_reverse)
        assert not panel.reverse_empty_label.isVisibleTo(panel.tab_reverse)

        # Selecting another package resets the tab rather than showing the
        # previous package's dependents or its wording.
        panel.set_package_info(_package("htop"))
        for _ in range(6):
            qapp.processEvents()
        assert panel.reverse_list.count() == 0
        assert panel.reverse_empty_label.isVisibleTo(panel.tab_reverse)
        # The previous package's verdict must not linger on the new one.
        assert "safe to remove" not in panel.reverse_empty_label.text().lower()
        assert "re-scan" in panel.rev_status_label.text().lower()
    finally:
        panel.close()
        qapp.processEvents()


def test_reverse_dependency_leaf_has_no_sentinel_package(qapp) -> None:
    """A leaf package must not put a fake row in the dependent list."""
    panel = _reverse_panel(qapp)
    try:
        panel.set_reverse_dependencies("gnome-shell", [])
        for _ in range(4):
            qapp.processEvents()
        # An empty list is the honest answer; the note carries the message. A
        # sentinel row here would look like a package that needs this one.
        assert panel.reverse_list.count() == 0
        for row in range(panel.reverse_list.count()):
            text = panel.reverse_list.item(row).text().lower()
            assert "no dependents" not in text
    finally:
        panel.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Report: labels stayed tiny when the user raised their font size
# ---------------------------------------------------------------------------

# The stylesheet used to pin label sizes in pixels. A pixel size is absolute, so
# a user who raised their font through the desktop's accessibility settings got
# larger chrome wrapped around text that never grew: 24 of 26 visible labels in
# the main window stayed at 11-14px against a 24px application font. These tests
# pin the fix -- sizes are expressed as offsets from the application font and
# applied as a real QFont.


def _font_size_units(font) -> float:
    """A font's size in whichever unit it was defined.

    A platform may specify its application font in points or in pixels; when it
    is pixel-sized ``pointSizeF()`` is -1 and only ``pixelSize()`` is meaningful.
    """
    points = font.pointSizeF()
    if points > 0:
        return points
    pixels = font.pixelSize()
    return pixels * 0.75 if pixels > 0 else -1.0


def test_stylesheet_declares_no_absolute_label_font_size() -> None:
    """A px font-size in the sheet cannot follow the user's font setting."""
    stylesheet = get_theme_stylesheet("dark")
    offenders = []
    for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", stylesheet):
        selector, body = rule.group(1).strip(), rule.group(2)
        if re.search(r"font-size:\s*[0-9]+px", body):
            offenders.append(" ".join(selector.split())[:60])
    assert offenders == [], (
        "These rules pin an absolute pixel size, which ignores the user's font "
        f"setting: {offenders}"
    )


@pytest.mark.parametrize("point_size", [9, 13, 16])
def test_label_sizes_follow_the_application_font(qapp, point_size) -> None:
    """Raising the application font must grow the inspector's text with it."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    from PyQt6.QtGui import QFont

    previous = app.font()
    app.setFont(QFont("DejaVu Sans", point_size))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        panel.resize(panel.minimumWidth(), 720)
        for _ in range(4):
            app.processEvents()

        base = panel.fontMetrics().height()
        sizes = {
            "title": panel.pkg_name_label.fontMetrics().height(),
            "summary": panel.summary_label.fontMetrics().height(),
        }
        # A 9pt application font paints a smaller glyph than a 16pt one. Compare
        # resolved sizes in whichever unit the platform reports, because a
        # platform may specify its application font in pixels (pointSizeF() is
        # then -1) and the offsets are applied in that same unit.
        title_size = _font_size_units(panel.pkg_name_label.font())
        summary_size = _font_size_units(panel.summary_label.font())
        assert title_size >= summary_size + 3
        assert summary_size == _font_size_units(app.font())
        assert sizes["title"] > base, (
            f"title {sizes['title']}px did not exceed the {base}px body at "
            f"{point_size}pt"
        )
    finally:
        panel.close()
        app.setFont(previous)
        qapp.processEvents()


@pytest.mark.parametrize("point_size", [9, 13, 16])
def test_overline_labels_stay_one_step_below_body(qapp, point_size) -> None:
    """The type hierarchy must survive the font scaling, not collapse into one size."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    from PyQt6.QtGui import QFont

    previous = app.font()
    app.setFont(QFont("DejaVu Sans", point_size))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        panel.resize(panel.minimumWidth(), 720)
        for _ in range(4):
            app.processEvents()
        overline = next(iter(panel._stat_names.values()))
        body = panel.summary_label
        overline_size = _font_size_units(overline.font())
        body_size = _font_size_units(body.font())
        assert overline_size < body_size
        assert body_size - overline_size <= 2
    finally:
        panel.close()
        app.setFont(previous)
        qapp.processEvents()


def test_every_styled_label_receives_a_scaled_font(qapp) -> None:
    """No label named in the size table may be left on the raw application font."""
    from ui.fonts import _LABEL_STYLES, apply_labelled_fonts, scaled_font

    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    window = MainWindow()
    try:
        window.show()
        for _ in range(6):
            app.processEvents()
        apply_labelled_fonts(window)
        seen = set()
        for label in window.findChildren(QLabel):
            name = label.objectName()
            if name not in _LABEL_STYLES:
                continue
            seen.add(name)
            # Compare against the font the table itself resolves, rather than
            # converting units by hand: a platform may size the application font
            # in pixels, where a point offset lands on a rounded pixel boundary
            # and a lossy conversion would disagree with the real rendering.
            delta, weight, spacing = _LABEL_STYLES[name]
            expected = scaled_font(delta, weight=weight, letter_spacing=spacing)
            actual = label.font()
            assert actual.pixelSize() == expected.pixelSize(), (
                f"{name} is {actual.pixelSize()}px, expected {expected.pixelSize()}px"
            )
            assert actual.pointSizeF() == expected.pointSizeF(), (
                f"{name} is {actual.pointSizeF()}pt, expected {expected.pointSizeF()}pt"
            )
        # The window really does build most of the table, so the sweep is exercised.
        assert len(seen) >= 5, f"only matched {sorted(seen)}"
    finally:
        window.close()
        app.processEvents()


def test_scaled_font_never_returns_an_unreadable_size(qapp) -> None:
    """A tiny application font must not scale the overline text to zero."""
    from ui.fonts import OVERLINE_PT_DELTA, scaled_font

    app = qapp
    from PyQt6.QtGui import QFont

    previous = app.font()
    app.setFont(QFont("DejaVu Sans", 1))
    try:
        font = scaled_font(OVERLINE_PT_DELTA)
        assert _font_size_units(font) >= 6
    finally:
        app.setFont(previous)
        app.processEvents()


@pytest.mark.parametrize("point_size", [9, 11, 13, 16, 20])
def test_metadata_field_names_are_never_clipped(qapp, point_size) -> None:
    """The gutter was measured with the wrong font, clipping "Architecture"."""
    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    from PyQt6.QtGui import QFont

    previous = app.font()
    app.setFont(QFont("DejaVu Sans", point_size))
    panel = PackageInspectorPanel()
    try:
        panel.show()
        panel.resize(panel.minimumWidth(), 900)
        for _ in range(4):
            app.processEvents()
        for attribute, label in panel._stat_names.items():
            painted = label.fontMetrics().horizontalAdvance(label.text())
            assert label.width() >= painted, (
                f"{label.text()!r} paints {painted}px in a {label.width()}px "
                f"gutter at {point_size}pt ({attribute})"
            )
    finally:
        panel.close()
        app.setFont(previous)
        qapp.processEvents()


def test_pixel_sized_application_font_keeps_its_own_size(qapp) -> None:
    """Some platforms specify the application font in pixels, not points.

    ``pointSizeF()`` then reports -1. Treating that as "unknown" and substituting
    a fixed point size made every label larger than the user's own setting: the
    metadata card measured 268px against its 260px budget on Rawhide's Python
    3.15 / Qt build. The pixel size must be preserved instead.
    """
    from PyQt6.QtGui import QFont

    from ui.fonts import BODY_PT_DELTA, scaled_font

    app = qapp
    previous = app.font()
    try:
        for pixels in (12, 13, 14, 16):
            font = QFont()
            font.setPixelSize(pixels)
            app.setFont(font)
            assert app.font().pointSizeF() <= 0, "expected a pixel-sized app font"

            body = scaled_font(BODY_PT_DELTA)
            assert body.pixelSize() == pixels, (
                f"a {pixels}px application font resolved to "
                f"{body.pixelSize()}px / {body.pointSizeF()}pt"
            )
            assert body.pointSizeF() <= 0, "the result should stay pixel-sized"
    finally:
        app.setFont(previous)
        qapp.processEvents()


@pytest.mark.parametrize("pixels", [12, 13])
def test_metadata_card_stays_compact_with_a_pixel_sized_font(qapp, pixels) -> None:
    """The card must keep its height budget whatever unit the font uses."""
    from PyQt6.QtGui import QFont

    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    previous = app.font()
    font = QFont()
    font.setPixelSize(pixels)
    app.setFont(font)
    try:
        panel = PackageInspectorPanel()
        try:
            panel.show()
            panel.resize(panel.minimumWidth(), 900)
            for _ in range(4):
                app.processEvents()
            panel.set_package_info(_package("firefox"))
            for _ in range(6):
                app.processEvents()

            card = panel._stats_card
            assert card.height() <= 260, (
                f"metadata card grew to {card.height()}px at {pixels}px font"
            )
            headroom = panel.tab_overview.height() - card.height()
            assert headroom >= 120, (
                f"the metadata card leaves only {headroom}px for the page "
                f"at a {pixels}px font"
            )
        finally:
            panel.close()
            app.processEvents()
    finally:
        app.setFont(previous)
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Report: the Rawhide COPR build failed on a malformed %changelog date
# ---------------------------------------------------------------------------


def test_every_changelog_date_matches_its_weekday() -> None:
    """rpm rejects a changelog entry whose weekday does not match its date.

    A single wrong weekday aborts the whole build in %check, so the failure only
    appears once the package is actually assembled.
    """
    import datetime
    from pathlib import Path

    spec = Path(__file__).resolve().parent.parent / "dendro.spec"
    entries = 0
    for line in spec.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\* (\w{3}) (\w{3}) (\d{2}) (\d{4}) ", line)
        if not match:
            continue
        weekday, month, day, year = match.groups()
        parsed = datetime.datetime.strptime(f"{month} {day} {year}", "%b %d %Y")
        assert parsed.strftime("%a") == weekday, (
            f"changelog says {weekday} but {month} {day} {year} was a "
            f"{parsed.strftime('%a')}: {line.strip()}"
        )
        entries += 1
    assert entries > 0, "no changelog entries were found to check"


@pytest.mark.parametrize("pixels", [12, 13])
def test_suite_holds_when_the_platform_sizes_fonts_in_pixels(qapp, pixels) -> None:
    """Every sized label must resolve cleanly for a pixel-sized app font.

    Fedora's COPR build reports the application font in pixels, not points. That
    turned a point-offset table into rounded pixel sizes, and an earlier
    hand-written unit conversion disagreed with what was actually painted
    ("HostModeBannerDetail is 8.25, expected 8.0"). This pins the comparison to
    the font the table really resolves.
    """
    from PyQt6.QtGui import QFont

    from ui.fonts import _LABEL_STYLES, scaled_font

    app = qapp
    previous = app.font()
    font = QFont()
    font.setPixelSize(pixels)
    app.setFont(font)
    try:
        for name, (delta, weight, spacing) in _LABEL_STYLES.items():
            resolved = scaled_font(delta, weight=weight, letter_spacing=spacing)
            assert resolved.pixelSize() > 0, f"{name} kept no pixel size"
            assert resolved.pointSizeF() <= 0, f"{name} unexpectedly reports points"
            # The overline step must stay strictly below body text.
            if delta < 0:
                body = scaled_font(0)
                assert resolved.pixelSize() < body.pixelSize()
    finally:
        app.setFont(previous)
        qapp.processEvents()


def _title_ink_span(header) -> tuple[int, int] | None:
    """The horizontal span of the painted title inside a header, in its viewport.

    The header's own rendering is the only honest source here: the alignment
    flag alone cannot say where a title ended up, which is exactly how a centred
    title was able to sit hundreds of pixels off-screen while every property
    looked correct.
    """
    image = header.grab().toImage()
    # Skip the section's padding and its bottom border, where no glyph sits.
    top, bottom = 6, image.height() - 6
    # One point the title never reaches gives the section's own fill.
    background = image.pixelColor(image.width() - 1, 2).name()
    inked = [
        x
        for x in range(image.width() - 1)
        if sum(
            1 for y in range(top, bottom) if image.pixelColor(x, y).name() != background
        )
        > 2
    ]
    return (min(inked), max(inked)) if inked else None


def test_the_file_path_header_is_centred_and_stays_on_screen(qapp) -> None:
    """The "File Path" title must be centred *and* readable.

    The path column is deliberately sized to its widest path, so it is usually
    several times wider than the viewport. QHeaderView lays a title out against
    the whole column width, so a centred title lands hundreds of pixels right of
    the visible strip: the title existed, but no reader could see it. The caller
    reported that after 2.18.0.

    Anchoring the title to the leading edge brought it back but lost the
    centring, which the caller reported after 2.19.0. Centring has to be
    measured against the part of the column that is on screen, so this test
    reads the painted result rather than the alignment flag, and reads it again
    after scrolling: the title has to stay centred in the visible strip, not
    scroll away with the column.
    """
    long_path = "/usr/lib64/" + "/".join(["deeply-nested-segment"] * 14) + "/library.so"
    panel = _files_panel(qapp, [long_path, "/etc/firefox/pref/local.js"])
    try:
        table = panel.files_table
        header = table.horizontalHeader()
        alignment = header.defaultAlignment()

        # The premise: the column really is much wider than what is shown.
        assert table.columnWidth(0) > table.viewport().width() * 2

        # Centring is what is asked for, so it is what the header declares.
        assert alignment & Qt.AlignmentFlag.AlignHCenter

        title_width = table.fontMetrics().horizontalAdvance("File Path")
        assert title_width < header.viewport().width(), (
            "the title itself has to fit the viewport even when the column does not"
        )

        bar = table.horizontalScrollBar()
        assert bar.maximum() > 0, "a column this wide has to be scrollable"
        for label, offset in (
            ("start", 0),
            ("middle", bar.maximum() // 2),
            ("end", bar.maximum()),
        ):
            bar.setValue(offset)
            for _ in range(8):
                qapp.processEvents()

            span = _title_ink_span(header)
            assert span is not None, f"the title vanished at the {label} of the range"
            centre = (span[0] + span[1]) / 2
            visible_centre = header.viewport().width() / 2
            assert abs(centre - visible_centre) <= 2, (
                f"at the {label} of the range the title sits at {centre:.0f}px, "
                f"not the {visible_centre:.0f}px centre of what is on screen"
            )
    finally:
        panel.close()
        qapp.processEvents()


def test_the_file_path_header_centres_inside_a_column_that_fits(qapp) -> None:
    """A path column narrower than the dock must centre ordinarily.

    The over-wide case is the one that used to break; this pins the everyday
    case so a fix for the wide column cannot quietly shift the narrow one.
    """
    panel = _files_panel(qapp, ["/etc/hosts", "/etc/passwd"])
    try:
        table = panel.files_table
        header = table.horizontalHeader()
        # Short paths no longer leave the column narrower than the tab: the band
        # beside a narrow column is painted in the view's shade, not the
        # header's, which put a visible step in the middle of the FILE PATH
        # heading. The column fills the tab; the title still centres in it.
        assert table.columnWidth(0) >= header.viewport().width()

        span = _title_ink_span(header)
        assert span is not None
        centre = (span[0] + span[1]) / 2
        assert abs(centre - table.columnWidth(0) / 2) <= 2, (
            f"the title sits at {centre:.0f}px in a {table.columnWidth(0)}px column"
        )
    finally:
        panel.close()
        qapp.processEvents()


def test_the_manifest_vertical_scrollbar_is_wide_enough_to_find(qapp) -> None:
    """A vertical bar the reader cannot see counts as a missing bar.

    The generic 6px bar is both narrow and close to the card colour, so a long
    file list looked unscrollable even though the bar was present and functional.
    The path table carries a scoped rule that widens it and lifts the handle
    contrast; this pins that rule so it cannot be silently dropped.
    """
    long_path = "/usr/lib64/" + "/".join(["segment"] * 12) + "/thing.so"
    panel = _files_panel(qapp, [f"{long_path}{index}" for index in range(80)])
    try:
        table = panel.files_table
        bar = table.verticalScrollBar()

        assert bar.isVisible()
        assert bar.maximum() > 0, "the manifest has to be scrollable at all"
        assert bar.width() >= 10, (
            f"the vertical bar is {bar.width()}px wide; the scoped rule sets 10px"
        )
        # The bar is no longer wedged inside the path table, between the two
        # halves. It is a cell of the card beside the Size column, so it belongs
        # to the card rather than to the table and sits at the card's right edge.
        assert bar.parent() is panel._files_card
        card = panel._files_card
        right = bar.mapTo(card, bar.rect().topLeft()).x() + bar.width()
        assert right >= card.width() - 2, (
            f"the vertical bar ends at {right}px in a {card.width()}px card; it "
            "must sit at the card's right edge, past the Size column"
        )
    finally:
        panel.close()
        qapp.processEvents()


def _bar_ink(bar) -> tuple[int, str, str]:
    """Thickness, track colour and handle colour of a scrollbar, as painted.

    Reading the pixels is the point: the two axes are styled by separate rules
    that can each look correct on their own while producing two different bars
    on the same card, which is what the caller reported after 2.19.0.

    Colours are taken along the bar's long axis. The track fills most of it and
    the handle covers the rest, so the two most common colours are the two the
    reader actually sees; no assumption is made about where the handle sits,
    because that moves with the scroll position.
    """
    image = bar.grab().toImage()
    if bar.height() < bar.width():  # horizontal
        middle = image.height() // 2
        pixels = [image.pixelColor(x, middle) for x in range(image.width())]
        thickness = bar.height()
    else:
        middle = image.width() // 2
        pixels = [image.pixelColor(middle, y) for y in range(image.height())]
        thickness = bar.width()

    counts: dict[str, int] = {}
    for colour in pixels:
        counts[colour.name()] = counts.get(colour.name(), 0) + 1
    ranked = sorted(counts, key=lambda name: -counts[name])
    track = ranked[0]
    handle = ranked[1] if len(ranked) > 1 else track
    return thickness, track, handle


def test_the_manifest_scrollbars_match_on_both_axes(qapp) -> None:
    """Both scrollbars of the manifest must look the same.

    The scoped rule that widened the vertical bar and lifted its handle contrast
    named only the vertical axis, so the horizontal bar stayed on the generic
    6px, `border`-coloured styling. On one card that read as two different
    scrollbars, which is what the caller reported after 2.19.0. Both axes are
    now declared together; this pins the symmetry so one cannot drift again.
    """
    long_path = "/usr/lib64/" + "/".join(["segment"] * 12) + "/thing.so"
    panel = _files_panel(qapp, [f"{long_path}{index}" for index in range(80)])
    try:
        table = panel.files_table
        horizontal = table.horizontalScrollBar()
        vertical = table.verticalScrollBar()
        assert horizontal.maximum() > 0 and vertical.maximum() > 0

        h_thickness, h_track, h_handle = _bar_ink(horizontal)
        v_thickness, v_track, v_handle = _bar_ink(vertical)

        assert h_thickness == v_thickness == 10, (
            f"the bars are {h_thickness}px and {v_thickness}px thick"
        )
        assert h_track == v_track, (
            f"the tracks differ: horizontal {h_track}, vertical {v_track}"
        )
        assert h_handle == v_handle, (
            f"the handles differ: horizontal {h_handle}, vertical {v_handle}"
        )
        # And the handle has to stand out from the track it runs in, or the bar
        # is present, uniform, and still unreadable.
        assert h_handle != h_track, "the handle is the same colour as its track"
        assert v_handle != v_track, "the handle is the same colour as its track"
    finally:
        panel.close()
        qapp.processEvents()


def _widget_edge(widget, root_widget) -> tuple[int, int]:
    """Top and bottom edge of *widget* in *root_widget*'s coordinates."""
    top = widget.mapTo(root_widget, widget.rect().topLeft()).y()
    return top, top + widget.height()


def test_the_two_halves_of_the_manifest_sit_on_the_same_edges(qapp) -> None:
    """The path and Size halves must start and end on identical lines.

    The two halves are separate widgets sharing one model, so nothing in Qt
    keeps their frames aligned: they agree only as long as the strip reserved
    under the Size column is exactly as tall as the scrollbar strip under the
    path column. When it was not, the Size column's rows drifted a few pixels
    against the path column's and the card's bottom edge stepped where the two
    halves met.

    This pins the whole band, not just the viewports: same top, same bottom, and
    the same viewport height. It is checked with the vertical scrollbar both
    present and absent, because the strip's height must not depend on whether
    either bar is currently scrollable.
    """
    long_path = "/usr/lib64/" + "/".join(["segment"] * 12) + "/thing.so"
    for count in (80, 2):
        panel = _files_panel(qapp, [f"{long_path}{index}" for index in range(count)])
        try:
            table = panel.files_table
            size_view = panel.files_size_view

            table_top, table_bottom = _widget_edge(table, panel.tab_files)
            size_top, size_bottom = _widget_edge(size_view, panel.tab_files)

            assert table_top == size_top, (
                f"the halves start {table_top}px and {size_top}px down the tab"
            )
            # Both halves now occupy one grid row, so their bottom edges are the
            # same line by construction. A reserved strip used to be added under
            # the Size column to make them agree; there is no strip to check any
            # more, and the edges must match on their own.
            assert table_bottom == size_bottom, (
                "the two halves must end on the same line without a reserved "
                f"strip: {table_bottom} vs {size_bottom}"
            )
            assert table.viewport().height() == size_view.viewport().height(), (
                "different viewport heights put the same row at two different "
                f"heights: {table.viewport().height()} vs "
                f"{size_view.viewport().height()}"
            )
            assert table.rowViewportPosition(0) == size_view.rowViewportPosition(0)
        finally:
            panel.close()
            qapp.processEvents()


def test_the_manifest_bottom_band_is_one_piece(qapp) -> None:
    """The strip under the path column must read as part of the card.

    The band used to be assembled from three pieces -- the strip carrying the
    path bar, the square where the two bars met, and a reserve under the Size
    column -- and nothing tied their colours together. Each was wrong in turn and
    showed up as a band of a different shade at the foot of the card.

    The band is now a single grid cell of the card, so the property under test
    changed with it: rather than comparing shades across three widgets, this
    checks that no second shade exists beside the bar's own handle, and that the
    card's rounded frame closes below the band instead of being cut off.
    """
    long_path = "/usr/lib64/" + "/".join(["segment"] * 12) + "/thing.so"
    for count in (80, 2):
        panel = _files_panel(qapp, [f"{long_path}{index}" for index in range(count)])
        try:
            card_widget = panel._files_card
            image = card_widget.grab().toImage()
            bar = panel._files_hbar
            card = QColor(THEMES_CONFIG["dark"]["bg_card"]).name()

            if bar.isVisible():
                # Sample the middle of the bar's row, between its handle and the
                # Size half, where the old square used to be unpainted.
                y = bar.mapTo(card_widget, bar.rect().topLeft()).y() + bar.height() // 2
                xs = range(bar.width() + 1, image.width() - 2)
            else:
                # With the bar collapsed the same strip belongs to the views, so
                # sample the last painted row of the card instead.
                y = image.height() - 3
                xs = range(2, image.width() - 2)
            colours = [image.pixelColor(x, y).name() for x in xs]

            others = {colour for colour in colours if colour != card}
            assert len(others) <= 1, (
                f"the foot of the card is broken into more shades than a bar "
                f"and its track: {sorted(others)}"
            )

            # The card's own frame closes the shape: the outline is continuous
            # along the bottom edge, which is what the two half-frames could
            # never produce on their own.
            bottom = image.height() - 1
            border = QColor(THEMES_CONFIG["dark"]["border"]).name()
            edge_middle = image.pixelColor(image.width() // 4, bottom).name()
            assert edge_middle in (border, card), (
                f"the card's bottom edge is {edge_middle}, not its frame or fill"
            )
        finally:
            panel.close()
            qapp.processEvents()


def test_the_empty_manifest_does_not_leave_its_card_behind(qapp) -> None:
    """An empty manifest shows one centred note, not a note inside a frame.

    The card's fill and outline belong to a container that holds both views, so
    hiding the views for the empty state left an outlined empty box behind the
    note -- which reads as a widget that failed to load. The container has to be
    part of the empty-state switch.
    """
    panel = PackageInspectorPanel()
    panel.set_host_environment(HostEnvironment.traditional())
    panel.show()
    panel.resize(panel.minimumWidth(), 650)
    for _ in range(6):
        qapp.processEvents()
    panel.set_package_info(_package("firefox"))
    panel.tabs.setCurrentWidget(panel.tab_files)
    for _ in range(8):
        qapp.processEvents()
        panel.tab_files.layout().activate()
    try:
        assert panel.files_table.rowCount() == 0
        assert not panel._files_card.isVisible(), (
            "the card's frame is left behind under the empty-state note"
        )
        assert panel.files_empty_label.isVisible()

        panel.set_package_files(
            "firefox",
            [PackageFileInfo(path="/etc/os-release", size_bytes=1024)],
        )
        for _ in range(8):
            qapp.processEvents()
            panel.tab_files.layout().activate()
        assert panel._files_card.isVisible(), "the card did not come back"
        assert not panel.files_empty_label.isVisible()
    finally:
        panel.close()
        qapp.processEvents()


def test_no_manifest_row_is_ever_painted_cut_in_half(qapp) -> None:
    """A row straddling the viewport edge is drawn sliced through the glyphs.

    This is the defect the caller reported as a path "cut through the middle":
    the manifest's height is set by the splitter and the window, while rows are
    a fixed 30px, so almost every panel height left a partial row at the bottom
    that Qt painted as a sliver of clipped text.

    The views are now sized to a whole number of rows and the remainder is
    absorbed by a spacer row in the card, so a partial row has nowhere to be
    drawn -- at any panel height.
    """
    for height in (620, 640, 660, 680, 700, 720, 760):
        panel = PackageInspectorPanel()
        panel.set_host_environment(HostEnvironment.traditional())
        panel.show()
        panel.resize(panel.minimumWidth(), height)
        for _ in range(6):
            qapp.processEvents()
        panel.set_package_info(_package("firefox"))
        panel.tabs.setCurrentWidget(panel.tab_files)
        for _ in range(4):
            qapp.processEvents()
        try:
            panel.set_package_files(
                "firefox",
                [
                    PackageFileInfo(
                        path=f"/usr/share/distribution-gpg-keys/keys-{index}.gpg",
                        size_bytes=1700 * (index + 1),
                    )
                    for index in range(12)
                ],
            )
            for _ in range(10):
                qapp.processEvents()
                panel.tab_files.layout().activate()

            table = panel.files_table
            viewport_height = table.viewport().height()
            row_height = table.rowHeight(0)
            assert row_height > 0
            assert viewport_height % row_height == 0, (
                f"at panel height {height} the viewport is {viewport_height}px, "
                f"which is {viewport_height % row_height}px past a whole row"
            )
            for row in range(table.rowCount()):
                rect = table.visualRect(table.model().index(row, 0))
                assert not (rect.top() < viewport_height <= rect.bottom()), (
                    f"row {row} is cut by the viewport edge at panel height {height}"
                )

            # The other half must still agree, or the sizes would drift from the
            # paths they label.
            assert panel.files_size_view.viewport().height() == viewport_height
        finally:
            panel.close()
            qapp.processEvents()


def test_a_font_change_while_the_manifest_refills_does_not_reenter_the_views(
    qapp,
) -> None:
    """A font change during a repopulate must not mutate the view from a signal.

    This reproduces the crash Fedora 43/44 reported as an exit 139 during the
    suite. The stack was a font-change cascade reaching the shared header while
    the table was inside ``updateGeometries``:
    ``QWidget::setFont -> QWidgetPrivate::updateFont -> notifyInternal2 ->
    QHeaderView::viewportEvent -> QTableView::updateGeometries`` (SIGSEGV).

    ``_sync_files_rows`` is connected to ``rowsInserted`` and ``layoutChanged``,
    and the two halves of the manifest share one selection model, so setting the
    pinned view's font there started that cascade from inside the notification.
    The font now follows from the panel's own font-change hook, and the signal
    path only writes geometry, guarded against re-entry.
    """
    from PyQt6.QtGui import QFont

    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    panel.set_host_environment(HostEnvironment.traditional())
    panel.show()
    panel.resize(panel.minimumWidth(), 700)
    for _ in range(4):
        app.processEvents()
    panel.set_package_info(_package("firefox"))
    panel.tabs.setCurrentWidget(panel.tab_files)
    for _ in range(4):
        app.processEvents()

    previous = app.font()
    try:
        # Alternating the application font makes Qt deliver FontChange to every
        # live widget, including the two views; refilling the manifest in the
        # same turn puts the row notifications on the same stack.
        for point_size in (9, 16, 9, 16):
            app.setFont(QFont("DejaVu Sans", point_size))
            panel.set_package_files(
                "firefox",
                [
                    PackageFileInfo(
                        path=f"/usr/lib64/firefox/lib{index}.so",
                        size_bytes=1024 * (index + 1),
                    )
                    for index in range(40)
                ],
            )
            for _ in range(6):
                app.processEvents()
                panel.tab_files.layout().activate()

            # The two halves still agree, so the fix did not buy stability by
            # simply skipping the synchronisation.
            assert panel.files_size_view.model().rowCount() == (
                panel.files_table.rowCount()
            )
            assert panel.files_size_view.font() == panel.files_table.font()
            for row in (0, panel.files_table.rowCount() - 1):
                assert panel.files_size_view.rowHeight(row) == (
                    panel.files_table.rowHeight(row)
                )
    finally:
        app.setFont(previous)
        panel.close()
        app.processEvents()


def test_neither_manifest_half_copies_row_geometry_from_the_other(qapp) -> None:
    """Row geometry must be shared by construction, not copied between views.

    The two halves are configured identically (same font, same default section
    size, same header resize modes), so Qt already gives them the same row
    heights and header height. A copy step re-derived that agreement on every
    ``rowsInserted``/``layoutChanged`` notification, which is both redundant and
    dangerous: it ran inside a model notification, and reaching the other view's
    header from there segfaulted on Fedora's Qt build (exit 139). The copy, its
    re-entrancy guard and the three connections are gone; this test pins the
    outcome so the machinery cannot quietly return.
    """
    import inspect

    from PyQt6.QtGui import QFont

    panel_source = inspect.getsource(PackageInspectorPanel)
    for obsolete in ("_sync_files_rows", "_syncing_files_rows"):
        assert obsolete not in panel_source, (
            f"{obsolete} was removed; copying row geometry between the two "
            "manifest halves is redundant and re-enters Qt's geometry pass"
        )

    app = qapp
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    panel.set_host_environment(HostEnvironment.traditional())
    panel.show()
    panel.resize(panel.minimumWidth(), 700)
    for _ in range(4):
        app.processEvents()
    panel.set_package_info(_package("firefox"))
    panel.tabs.setCurrentWidget(panel.tab_files)
    for _ in range(4):
        app.processEvents()

    try:
        panel.set_package_files(
            "firefox",
            [
                PackageFileInfo(
                    path=f"/usr/lib64/firefox/lib{index}.so",
                    size_bytes=1024 * (index + 1),
                )
                for index in range(40)
            ],
        )
        for _ in range(6):
            app.processEvents()

        assert panel.files_table.rowCount() == 40
        # No copying happens anymore, yet the halves still line up exactly.
        assert panel.files_size_view.horizontalHeader().height() == (
            panel.files_table.horizontalHeader().height()
        )
        assert panel.files_size_view.verticalHeader().defaultSectionSize() == (
            panel.files_table.verticalHeader().defaultSectionSize()
        )
        for row in range(panel.files_table.rowCount()):
            assert panel.files_size_view.rowHeight(row) == (
                panel.files_table.rowHeight(row)
            )

        # Row height is geometry, not paint: a taller path row must not leave the
        # pinned column a row at a time behind. The font-change hook now carries
        # the height with the font, so forcing a font change repairs any drift
        # that a stylesheet or a larger font introduced on one half only.
        app_font = app.font()
        try:
            panel.files_table.verticalHeader().setDefaultSectionSize(64)
            for _ in range(4):
                app.processEvents()
            assert panel.files_table.rowHeight(0) != (
                panel.files_size_view.rowHeight(0)
            ), "the probe did not actually perturb one half"

            app.setFont(QFont("DejaVu Sans", 13))
            for _ in range(6):
                app.processEvents()
            for row in range(panel.files_table.rowCount()):
                assert panel.files_size_view.rowHeight(row) == (
                    panel.files_table.rowHeight(row)
                ), "the font-change hook no longer carries the row height"
        finally:
            app.setFont(app_font)
            for _ in range(4):
                app.processEvents()
    finally:
        panel.close()
        app.processEvents()


def test_the_two_halves_share_one_vertical_scroll_state(qapp) -> None:
    """Both halves must scroll as one surface, from either side.

    There used to be two vertical bars: the path table's, plus a second one
    inside the pinned Size view whose value was copied from the first after
    every scroll. The copy ran in one direction only, so a wheel event over the
    Size column scrolled that column alone and left the paths behind -- the two
    halves visibly tore apart.

    The old design also crashed: the copy ran from ``rangeChanged``, which Qt
    emits inside the view's own geometry pass, and mutating a sibling bar there
    faulted natively on Fedora's Qt build. Copying values between two views can
    only ever approximate one scroll surface. Both views are now given the same
    bar object, so there is a single value to hold, nothing to synchronise, and
    no slot on that path at all.
    """
    panel = _files_panel(
        qapp, [f"/usr/share/doc/example/file-{index}.txt" for index in range(60)]
    )
    try:
        table = panel.files_table
        size_view = panel.files_size_view
        bar = panel._files_vbar

        assert table.verticalScrollBar() is bar
        assert size_view.verticalScrollBar() is bar, (
            "the Size half has a scrollbar of its own; a wheel over it would "
            "move that half alone"
        )

        assert bar.maximum() > 0, "the manifest needs to overflow to test this"
        for fraction in (0.0, 0.25, 0.5, 1.0):
            bar.setValue(int(bar.maximum() * fraction))
            for _ in range(6):
                qapp.processEvents()
            assert table.verticalOffset() == size_view.verticalOffset(), (
                f"the halves are at {table.verticalOffset()} and "
                f"{size_view.verticalOffset()} at {fraction:.0%} down"
            )
            assert _first_visible_row(table) == _first_visible_row(size_view)

        bar.setValue(0)
        for _ in range(6):
            qapp.processEvents()
        assert _first_visible_row(table) == 0
        assert _first_visible_row(size_view) == 0
    finally:
        panel.close()
        qapp.processEvents()


def test_wheeling_over_either_half_scrolls_both(qapp) -> None:
    """A wheel event over one half must move the other half with it.

    This is the defect as reported: the Size column scrolled up and down on its
    own. Copying one bar's value to the other half's bar only ever fixed the
    path-to-size direction, so the wheel over the Size column bypassed the copy
    entirely. A single shared bar has no second direction to miss.
    """
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent

    panel = _files_panel(
        qapp, [f"/usr/share/doc/example/file-{index}.txt" for index in range(60)]
    )

    def wheel(widget, delta):
        viewport = widget.viewport()
        centre = viewport.rect().center()
        event = QWheelEvent(
            QPointF(centre),
            QPointF(widget.mapToGlobal(centre)),
            QPoint(0, 0),
            QPoint(0, delta),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        qapp.sendEvent(viewport, event)
        for _ in range(6):
            qapp.processEvents()

    try:
        table = panel.files_table
        size_view = panel.files_size_view

        # Wheel over the Size half: both must move, by the same amount.
        wheel(size_view, -120)
        assert table.verticalOffset() == size_view.verticalOffset()
        assert table.verticalOffset() > 0, "wheeling over Size did not scroll"

        # And over the path half: still both, still in step.
        before = table.verticalOffset()
        wheel(table, -120)
        assert table.verticalOffset() > before
        assert table.verticalOffset() == size_view.verticalOffset()
    finally:
        panel.close()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Controls and rows whose geometry only shows up when the widget is rendered:
# a button taller than the field beside it, a checkbox taller than the cell that
# holds it, and a row cut in half by the bottom of its table.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_default_font(qapp):
    """Undo a font change so it cannot leak into the next test in the file."""
    original = QFont(qapp.font())
    yield
    qapp.setFont(original)
    qapp.processEvents()


def _set_default_font(qapp, point_size: int) -> None:
    """Set the application font, as a desktop with a larger default would.

    Row fitting and control heights are both derived from widget metrics, and
    the only thing that differs on another machine is the font. These tests run
    at several point sizes so that a fix which only holds at the size it was
    developed on fails here rather than in CI.
    """
    font = QFont(qapp.font())
    font.setPointSize(point_size)
    qapp.setFont(font)


def _repo_dialog(qapp, rows=12):
    from core.backend import RepoInfo
    from ui.repo_dialog import RepoManagerDialog

    qapp.setStyleSheet(get_theme_stylesheet("dark"))
    dialog = RepoManagerDialog()
    dialog.show()
    dialog.resize(900, 560)
    for _ in range(8):
        qapp.processEvents()
    dialog._populate_table(
        [
            RepoInfo(
                id=f"repo-{index}",
                name=f"Repository {index}",
                enabled=bool(index % 2),
                repo_file=f"/etc/yum.repos.d/repo-{index}.repo",
                is_copr=False,
                is_rpmfusion=False,
                is_core=False,
            )
            for index in range(rows)
        ]
    )
    for _ in range(8):
        qapp.processEvents()
    return dialog


@pytest.mark.parametrize("point_size", [7, 9, 11, 14])
def test_a_button_beside_a_field_is_the_same_height(qapp, point_size) -> None:
    """A button and the text field on its row must share one band.

    The repository form puts "+ Add Repo" directly beside the address field. Its
    button was sized from 28px of content plus 6px of padding while the field was
    30px plus zero, so the button stood 4px proud of the field on both edges --
    it did not read as a taller control, it read as misaligned, because the field
    spans the row and the eye takes its edges as the row's.
    """
    _set_default_font(qapp, point_size)
    dialog = _repo_dialog(qapp)
    try:
        dialog.resize(900, 560)
        for _ in range(8):
            qapp.processEvents()
        field = dialog.repo_address_input
        button = dialog.btn_add_repo
        assert field.height() == button.height(), (
            f"the field is {field.height()}px and the button {button.height()}px"
        )
        field_top = field.mapTo(dialog, field.rect().topLeft()).y()
        button_top = button.mapTo(dialog, button.rect().topLeft()).y()
        assert field_top == button_top, (
            f"the field starts at y={field_top} and the button at y={button_top}"
        )
        # The row must be the same band whether or not the field is the taller
        # of the two, so also check that neither overflows the other's height.
        assert abs(field.height() - button.height()) == 0
    finally:
        dialog.close()
        qapp.processEvents()


@pytest.mark.parametrize("point_size", [7, 9, 11, 14])
def test_the_status_checkbox_is_never_clipped_by_its_cell(qapp, point_size) -> None:
    """The repository checkbox must fit inside the cell widget that holds it.

    A table cell widget is sized to its own size hint, not to the row, and the
    hint was the checkbox's 22px alone. The 30px row therefore left the box in an
    18px container, and the bottom of the indicator was cut off in every row --
    which is what was reported as "the bottom of the checkboxes is not visible".
    """
    from PyQt6.QtWidgets import QCheckBox

    _set_default_font(qapp, point_size)
    dialog = _repo_dialog(qapp)
    try:
        table = dialog.table
        assert table.rowCount() >= 3
        for row in range(table.rowCount()):
            widget = table.cellWidget(row, 0)
            assert widget is not None, f"row {row} has no checkbox cell"
            checkbox = widget.findChild(QCheckBox)
            assert checkbox is not None
            geometry = checkbox.geometry()
            assert geometry.bottom() < widget.height(), (
                f"row {row}: the {geometry.height()}px checkbox is drawn at "
                f"y={geometry.top()} in a {widget.height()}px cell, so its "
                "bottom edge is clipped"
            )
            assert geometry.top() >= 0
    finally:
        dialog.close()
        qapp.processEvents()


@pytest.mark.parametrize("point_size", [7, 9, 11, 14])
def test_no_table_row_is_cut_by_the_bottom_of_the_repository_list(
    qapp, point_size
) -> None:
    """Rows end on the viewport edge, so none is painted in part.

    The table's height comes from the dialog while its rows are a fixed height,
    so almost every dialog size cut through a row. The visible area is now a
    whole number of rows and the remainder is left to the layout.
    """
    _set_default_font(qapp, point_size)
    for height in (500, 560, 620, 700):
        dialog = _repo_dialog(qapp)
        try:
            dialog.resize(900, height)
            for _ in range(10):
                qapp.processEvents()
            dialog._row_fitter.fit()
            for _ in range(6):
                qapp.processEvents()

            table = dialog.table
            viewport_height = table.viewport().height()
            row_height = table.rowHeight(0)
            assert row_height > 0
            assert viewport_height % row_height == 0, (
                f"at dialog height {height} the viewport is {viewport_height}px, "
                f"{viewport_height % row_height}px past a whole row"
            )
            for row in range(table.rowCount()):
                rect = table.visualRect(table.model().index(row, 0))
                assert not (rect.top() < viewport_height <= rect.bottom()), (
                    f"row {row} is cut by the viewport edge at height {height}"
                )
        finally:
            dialog.close()
            qapp.processEvents()


def test_the_row_fitter_leaves_an_empty_table_alone(qapp) -> None:
    """An empty table has no partial row, and shrinking it only opens a gap."""
    from PyQt6.QtWidgets import QTableWidget

    from ui.table_rows import WholeRowFitter

    qapp.setStyleSheet(get_theme_stylesheet("dark"))
    table = QTableWidget(0, 2)
    table.show()
    for _ in range(4):
        qapp.processEvents()
    before = table.height()
    fitter = WholeRowFitter(table)
    table.installEventFilter(fitter)
    fitter.fit()
    for _ in range(4):
        qapp.processEvents()
    try:
        assert table.height() == before, "an empty table was resized"
    finally:
        table.close()
        qapp.processEvents()


def test_the_tabs_and_their_pane_share_one_frame(qapp) -> None:
    """The settings dialog's tab widget must use the app's tab styling.

    It carried no object name, so it matched no stylesheet rule and fell back to
    Qt's native frame: the pane kept a 1px frame of its own under the tab strip,
    and a seam of the dialog background showed between the two.
    """
    from PyQt6.QtWidgets import QTabWidget

    from ui.settings_dialog import DendroSettingsDialog

    qapp.setStyleSheet(get_theme_stylesheet("dark"))
    dialog = DendroSettingsDialog()
    dialog.show()
    dialog.resize(760, 636)
    for _ in range(10):
        qapp.processEvents()
    try:
        tabs = dialog.findChild(QTabWidget)
        assert tabs is not None
        assert tabs.objectName() == "SettingsTabs", (
            "the settings tab widget has no name, so no rule can style it"
        )
        stylesheet = get_theme_stylesheet("dark")
        assert "QTabWidget#SettingsTabs::pane" in stylesheet, (
            "the pane rule does not cover the settings dialog"
        )
    finally:
        dialog.close()
        qapp.processEvents()


def test_a_flat_topped_tab_does_not_paint_its_accent_above_the_tab(qapp) -> None:
    """The selected tab's accent must sit on the tab, not float above it.

    Tabs used to round their top corners while the selected tab drew a straight
    2px accent border-top. Qt cannot follow a rounded corner with a straight top
    border, so the accent was painted as an arc above a separately drawn rounded
    body, and the tab read as two shapes.
    """
    from PyQt6.QtWidgets import QTabWidget

    from ui.settings_dialog import DendroSettingsDialog

    qapp.setStyleSheet(get_theme_stylesheet("dark"))
    dialog = DendroSettingsDialog()
    dialog.show()
    dialog.resize(760, 636)
    for _ in range(10):
        qapp.processEvents()
    try:
        tabs = dialog.findChild(QTabWidget)
        bar = tabs.tabBar()
        rect = bar.tabRect(0)
        assert rect.isValid()
        image = bar.grab().toImage()
        accent = QColor(THEMES_CONFIG["dark"]["accent"]).name()

        # Down the middle of the selected tab the accent must appear exactly
        # once, at the very top: a second band further down would be the border
        # drawn inside a body that had been rounded away from it.
        x = rect.center().x()
        accents = [
            y
            for y in range(min(rect.height(), image.height()))
            if image.pixelColor(x, y).name() == accent
        ]
        assert accents, "the selected tab has no accent band at all"
        assert accents == list(range(accents[0], accents[0] + len(accents))), (
            f"the accent is broken into separate bands at {accents}"
        )
        assert accents[0] <= 2, f"the accent starts {accents[0]}px down the tab"
    finally:
        dialog.close()
        qapp.processEvents()
