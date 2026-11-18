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
from PyQt6.QtGui import QColor, QFontMetrics, QPalette
from PyQt6.QtWidgets import QPushButton

from core.backend import (
    DependencyNode,
    FileVerificationResult,
    PackageFileInfo,
    PackageInfo,
    PackageState,
    format_byte_size,
)
from core.flatpak_inventory import FlatpakEntry, FlatpakInventory
from core.host_environment import HostEnvironment
from core.models import STATUS_EXTRA_LABELS, STATUS_LABELS, DependencyTreeModel
from ui.delegates import (
    NO_VALUE_PLACEHOLDER,
    DendroTreeView,
    PackageTreeItemDelegate,
)
from ui.flatpak_dialog import (
    _COLUMNS,
    _HEADER_LETTER_SPACING,
    _SIZE_COLUMN,
    _SORT_INDICATOR_WIDTH,
    FlatpakInventoryDialog,
)
from ui.inspector_panel import PackageInspectorPanel
from ui.main_window import MainWindow
from ui.settings_dialog import DendroSettingsDialog
from ui.styles import THEMES_CONFIG, get_theme_stylesheet
from ui.transaction_drawer import TransactionDrawer


def _package(name: str = "gnome-shell", **overrides) -> PackageInfo:
    values = dict(
        version="48.1",
        release="1.fc43",
        arch="x86_64",
        summary="A representative summary.",
        description="A description paragraph.\n\n- one bullet\n- two bullets",
        state=PackageState.INSTALLED,
        parent_pillar="pillar_apps",
        sub_category="desktop_addons",
        primary_category="desktop_addons",
        classification_evidence="rpm_manifest",
        classification_input_scope="installed_manifest",
        classification_rationale=[
            "Ships a desktop entry in /usr/share/applications.",
            "Links against GTK4 and libadwaita.",
        ],
        secondary_tags=["gtk", "gui"],
        is_protected=True,
    )
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
    values = dict(
        kind="app",
        name="Freedesktop Platform",
        application_id="org.freedesktop.Platform",
        version="24.08.14",
        branch="24.08",
        arch="x86_64",
        origin="flathub",
        installation="system",
        ref="runtime/org.freedesktop.Platform/x86_64/24.08",
        installed_size_text="1.2 GB",
        installed_size_bytes=1_288_490_188,
        runtime="",
    )
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
        assert dialog.table.columnCount() == len(_COLUMNS)
        header_width = sum(header.sectionSize(i) for i in range(len(_COLUMNS)))
        viewport_width = dialog.table.viewport().width()
        assert header_width <= viewport_width, (
            f"columns need {header_width}px but only {viewport_width}px is visible"
        )
        # The size column is the last one and is the one that used to be cut off.
        assert len(_COLUMNS) - 1 == _SIZE_COLUMN
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
        label = _COLUMNS[sorted_column].upper()
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
        for column, label in enumerate(_COLUMNS):
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
