"""Regression coverage for restoring the package-detail inspector."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSplitter, QWidget

from ui.main_window import MainWindow


class _InspectorWindow:
    """Small harness for MainWindow's splitter-only inspector behavior."""

    _inspector_panel_is_open = MainWindow._inspector_panel_is_open
    _show_inspector_panel = MainWindow._show_inspector_panel
    _toggle_inspector_panel = MainWindow._toggle_inspector_panel


def test_details_toggle_restores_a_collapsed_saved_inspector(qtbot) -> None:
    """A visible-but-offscreen panel must be restored by the Details control."""
    splitter = QSplitter(Qt.Orientation.Horizontal)
    sidebar = QWidget()
    workspace = QWidget()
    inspector = QWidget()
    inspector.setMinimumWidth(360)

    splitter.addWidget(sidebar)
    splitter.addWidget(workspace)
    splitter.addWidget(inspector)
    splitter.resize(1280, 700)
    qtbot.addWidget(splitter)
    splitter.show()
    qtbot.waitUntil(lambda: splitter.width() > 0)

    # This reproduces a persisted QSplitter state that has collapsed the
    # inspector. QWidget.isVisible() remains true even though it is offscreen.
    splitter.setSizes([260, 1020, 0])
    qtbot.waitUntil(lambda: splitter.sizes()[2] == 0)
    assert inspector.isVisible()
    assert inspector.geometry().x() < 0

    window = _InspectorWindow()
    window.main_splitter = splitter
    window.inspector_panel = inspector
    window._toggle_inspector_panel()

    qtbot.waitUntil(lambda: splitter.sizes()[2] >= inspector.minimumWidth())
    assert inspector.geometry().x() >= 0


def test_stat_name_gutter_fits_the_widest_name_including_tracking(qtbot) -> None:
    """The fixed name gutter must not clip the widest field name.

    ``QFontMetrics`` knows nothing about stylesheet styling, so measuring the
    bare font understates each name by roughly one tracking step per character.
    ``Architecture`` is the long pole: it advanced 77 px against an 81 px
    gutter, but needs 81 px once the ``letter-spacing: 0.3px`` from the
    ``StatFieldName`` rule is applied, so it sat flush against its value with
    no gap at all.

    The panel therefore has to apply that tracking to the font before
    measuring. This asserts the measured gutter actually accommodates the
    painted advance of every name plus the intended padding.
    """
    from PyQt6.QtGui import QFont, QFontMetrics

    from ui.fonts import OVERLINE_PT_DELTA, scaled_font
    from ui.inspector_panel import PackageInspectorPanel

    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)

    font = scaled_font(OVERLINE_PT_DELTA, weight=QFont.Weight.DemiBold)
    font.setLetterSpacing(
        QFont.SpacingType.AbsoluteSpacing,
        PackageInspectorPanel.STAT_NAME_TRACKING_PX,
    )
    metrics = QFontMetrics(font)
    widest = max(metrics.horizontalAdvance(name) for name in panel._stat_name_labels)

    assert panel._stat_name_width >= widest, (
        "the name gutter clips the widest label: "
        f"{panel._stat_name_width}px available, {widest}px needed"
    )
    # And it must leave real breathing room rather than sitting flush.
    assert panel._stat_name_width > widest


def test_stat_name_gutter_is_remeasured_on_a_font_change_event(qtbot) -> None:
    """The gutter is a fixed width derived from metrics, so it must refresh.

    ``scaled_font`` resolves against the application font rather than the
    widget's, so the event arrives without the metrics having been recomputed
    by Qt. Without the ``changeEvent`` handler the gutter keeps its old width
    and the names clip once the type scale grows.
    """
    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication

    from ui.inspector_panel import PackageInspectorPanel

    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    original = panel._stat_name_width

    # Drive the real path: raising the application font makes Qt post a
    # FontChange to every widget, which the panel answers by re-measuring.
    app = QApplication.instance()
    previous = QFont(app.font())
    wider = QFont(app.font())
    wider.setPointSizeF(app.font().pointSizeF() + 8)
    app.setFont(wider)
    try:
        QCoreApplication.sendPostedEvents(panel, QEvent.Type.FontChange.value)
        QCoreApplication.processEvents()
        assert panel._stat_name_width > original
        # Every name label follows the new gutter rather than keeping the old.
        # setFixedWidth pins both bounds, so max == the freshly measured width.
        for label in panel._stat_names.values():
            assert label.maximumWidth() == panel._stat_name_width
    finally:
        app.setFont(previous)
        QCoreApplication.sendPostedEvents(panel, QEvent.Type.FontChange.value)
        QCoreApplication.processEvents()

    # ...and settled back down once the previous font was restored.
    assert panel._stat_name_width == original


def test_delegate_rows_grow_with_the_application_font(qtbot) -> None:
    """Row heights must follow the font, not sit at a fixed pixel value.

    Both delegates used to pin their row height (34px for the tree, 32/36px for
    the sidebar), which clipped the text once the user's desktop font grew past
    what fit. At 24pt the tree's text needs 38px in a 34px row.
    """
    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication

    from ui.delegates import DendroTreeView, PackageTreeItemDelegate
    from ui.sidebar import CategorySidebar

    app = QApplication.instance()
    previous = QFont(app.font())

    view = DendroTreeView()
    qtbot.addWidget(view)
    tree_delegate = PackageTreeItemDelegate(view)
    view.setItemDelegate(tree_delegate)

    sidebar = CategorySidebar()
    qtbot.addWidget(sidebar)
    sidebar_delegate = sidebar.sidebar_delegate

    tree_before = tree_delegate.row_height
    sidebar_before = sidebar_delegate.item_height

    larger = QFont(app.font())
    larger.setPointSizeF(app.font().pointSizeF() + 12)
    app.setFont(larger)
    try:
        QCoreApplication.sendPostedEvents(view, QEvent.Type.FontChange.value)
        QCoreApplication.sendPostedEvents(sidebar, QEvent.Type.FontChange.value)
        QCoreApplication.processEvents()

        assert tree_delegate.base_font.pointSize() > previous.pointSize()
        assert tree_delegate.row_height > tree_before
        assert sidebar_delegate.item_font.pointSize() > previous.pointSize()
        assert sidebar_delegate.item_height > sidebar_before
        assert sidebar_delegate.header_height > 36
    finally:
        app.setFont(previous)
        QCoreApplication.sendPostedEvents(view, QEvent.Type.FontChange.value)
        QCoreApplication.sendPostedEvents(sidebar, QEvent.Type.FontChange.value)
        QCoreApplication.processEvents()


def test_delegate_rows_keep_their_designed_height_at_the_default_font(qtbot) -> None:
    """Growing with the font must not change the default look."""
    from PyQt6.QtWidgets import QApplication

    from ui.delegates import PackageTreeItemDelegate
    from ui.sidebar import SidebarItemDelegate

    if QApplication.instance().font().pointSizeF() != 9.0:
        import pytest

        pytest.skip("row heights are calibrated for the 9pt default font")

    tree_delegate = PackageTreeItemDelegate()
    sidebar_delegate = SidebarItemDelegate()

    assert tree_delegate.row_height == 34
    assert sidebar_delegate.item_height == 32
    assert sidebar_delegate.header_height == 36


def _verbose_package(name: str = "gnome-shell") -> object:
    """Return a PackageInfo sized to stress wrapped metadata and overview cards."""
    from core.backend import PackageInfo, PackageState

    long_paragraph = (
        "This package integrates desktop shell components, dynamic layout "
        "management, Wayland compositor bindings, extension sandboxing, and "
        "multi-monitor scaling policies across GNOME sessions. "
    ) * 3
    return PackageInfo(
        name=name,
        version="48.1-2.fc43",
        arch="x86_64",
        summary=(
            "Window management, compositor integration, and desktop shell "
            "experience for Wayland and X11 workspaces with hardware acceleration"
        ),
        repository="@System",
        state=PackageState.INSTALLED,
        size_bytes=18_432_000,
        license="GPL-2.0-or-later AND LGPL-2.1-or-later AND MIT AND BSD-3-Clause",
        url="https://gitlab.gnome.org/GNOME/gnome-shell",
        packager=(
            "Fedora Project Release Engineering <releng@fedoraproject.org> "
            "(https://fedoraproject.org)"
        ),
        build_time="2026-10-01 14:22:00",
        install_time="2026-11-17 09:00:00",
        has_update=True,
        available_update_version="48.2-1.fc43",
        description="\n\n".join([long_paragraph] * 6),
        primary_category="desktop_environments",
        sub_category="window_managers",
        parent_pillar="desktop",
        classification_evidence="FHS + Desktop Entries",
        classification_input_scope="Installed RPM Header",
        classification_rationale=(
            "Provides core Wayland compositor binaries under /usr/bin, "
            "session desktop entries under /usr/share/wayland-sessions, "
            "and shared shell extensions metadata across system directories."
        ),
    )


def test_selecting_a_verbose_package_never_expands_main_window(
    qtbot, monkeypatch
) -> None:
    """Selecting a package must never push MainWindow past the screen budget.

    Reproduces Issue #100: before wrapping the inspector and overview inside
    ``QScrollArea`` containers and relaxing manifest ``minimumHeight`` locks,
    selecting a package inflated ``MainWindow.minimumSizeHint().height()`` past
    the window's current height, forcing Qt's ``SetDefaultConstraint`` to resize
    the top-level window downward off the bottom edge of the display.
    """
    from PyQt6.QtCore import QItemSelectionModel
    from PyQt6.QtWidgets import QApplication

    from core.backend import PackageFileInfo
    from core.host_environment import HostEnvironment
    from ui.styles import get_theme_stylesheet

    app = QApplication.instance()
    previous_stylesheet = app.styleSheet()
    app.setStyleSheet(get_theme_stylesheet("dark"))
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)

    window = MainWindow(host_environment=HostEnvironment.traditional())
    qtbot.addWidget(window)
    try:
        monkeypatch.setattr(window.thread_pool, "start", lambda *_args, **_kwargs: None)
        window.resize(1280, 620)
        window.show()
        for _ in range(6):
            app.processEvents()

        initial_size = window.size()
        assert window.minimumSizeHint().height() <= 320

        pkg = _verbose_package()
        window.proxy_model.set_category_filter("all")
        window.tree_model.set_packages([pkg])
        app.processEvents()

        index = window.proxy_model.index(0, 0)
        assert index.isValid()
        window.tree_view.selectionModel().setCurrentIndex(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        for _ in range(6):
            app.processEvents()

        files = [
            PackageFileInfo(
                path=f"/usr/lib64/gnome-shell/plugins/component-{i:02d}.so",
                size_bytes=4096 * (i + 1),
            )
            for i in range(30)
        ]
        window.inspector_panel.set_package_files(pkg.name, files)
        for tab_idx in range(window.inspector_panel.tabs.count()):
            window.inspector_panel.tabs.setCurrentIndex(tab_idx)
            for _ in range(4):
                app.processEvents()

        window.inspector_panel.tabs.setCurrentIndex(0)
        for _ in range(4):
            app.processEvents()

        assert window.size() == initial_size
        assert window.minimumHeight() <= initial_size.height()
        assert window.minimumSizeHint().height() <= 320
        assert window.inspector_panel.minimumSizeHint().height() <= 160
        assert window.inspector_panel.files_table.minimumHeight() == 0
        assert window.inspector_panel.files_size_view.minimumHeight() == 0
        assert window.inspector_panel._overview_scroll.verticalScrollBar().maximum() > 0
    finally:
        window.close()
        app.setStyleSheet(previous_stylesheet)
        app.processEvents()


def test_inspector_shrinks_after_files_load_and_keeps_tabs_readable(qtbot) -> None:
    """Loaded manifests stay shrinkable and outer scrollbars never elide tabs."""
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication

    from core.backend import PackageFileInfo
    from core.host_environment import HostEnvironment
    from ui.inspector_panel import PackageInspectorPanel
    from ui.styles import get_theme_stylesheet

    app = QApplication.instance()
    previous_font = QFont(app.font())
    previous_stylesheet = app.styleSheet()
    larger = QFont(previous_font)
    larger.setPointSizeF(13.0)
    app.setFont(larger)
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    panel.set_host_environment(HostEnvironment.traditional())
    try:
        panel.resize(panel.minimumWidth(), 800)
        panel.show()
        for _ in range(4):
            app.processEvents()

        pkg = _verbose_package()
        panel.set_package_info(pkg)
        panel.tabs.setCurrentWidget(panel.tab_files)
        panel.set_package_files(
            pkg.name,
            [
                PackageFileInfo(
                    path=f"/usr/share/gnome-shell/theme/asset-{i:02d}.css",
                    size_bytes=2048 * (i + 1),
                )
                for i in range(25)
            ],
        )
        for _ in range(6):
            app.processEvents()

        panel.resize(panel.minimumWidth(), 480)
        for _ in range(6):
            app.processEvents()

        assert panel.height() == 480
        assert panel.minimumSizeHint().height() <= 160
        assert panel._panel_scroll.verticalScrollBar().isVisible()
        tab_bar = panel.tabs.tabBar()
        assert tab_bar.sizeHint().width() <= tab_bar.width()
        assert panel.lbl_verify_status.wordWrap() is True
        assert panel.rev_status_label.wordWrap() is True
    finally:
        panel.close()
        app.setFont(previous_font)
        app.setStyleSheet(previous_stylesheet)
        app.processEvents()


def test_inspector_scroll_resets_when_switching_packages_or_clearing(qtbot) -> None:
    """Switching packages or clearing returns both inspector scrollbars to top."""
    from PyQt6.QtWidgets import QApplication

    from ui.inspector_panel import PackageInspectorPanel
    from ui.styles import get_theme_stylesheet

    app = QApplication.instance()
    previous_stylesheet = app.styleSheet()
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    try:
        panel.resize(panel.minimumWidth(), 480)
        panel.show()
        for _ in range(4):
            app.processEvents()

        panel.set_package_info(_verbose_package("alpha"))
        for _ in range(4):
            app.processEvents()

        panel_vbar = panel._panel_scroll.verticalScrollBar()
        overview_vbar = panel._overview_scroll.verticalScrollBar()
        assert panel_vbar.maximum() > 0
        assert overview_vbar.maximum() > 0

        panel_vbar.setValue(panel_vbar.maximum())
        overview_vbar.setValue(overview_vbar.maximum())
        assert panel_vbar.value() > 0
        assert overview_vbar.value() > 0

        panel.set_package_info(_verbose_package("beta"))
        for _ in range(4):
            app.processEvents()
        assert panel_vbar.value() == 0
        assert overview_vbar.value() == 0

        panel_vbar.setValue(panel_vbar.maximum())
        overview_vbar.setValue(overview_vbar.maximum())
        panel.clear()
        for _ in range(4):
            app.processEvents()
        assert panel_vbar.value() == 0
        assert overview_vbar.value() == 0
        assert panel.files_table.maximumHeight() == 16777215
        assert panel._files_card.minimumHeight() == 0
    finally:
        panel.close()
        app.setStyleSheet(previous_stylesheet)
        app.processEvents()


def test_inspector_tabs_keep_compact_scroll_threshold_and_whole_manifest_rows(
    qtbot,
) -> None:
    """Tabs do not trip the outer scrollbar at 700px and keep whole rows at 480px."""
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication

    from core.backend import PackageFileInfo
    from ui.inspector_panel import PackageInspectorPanel
    from ui.styles import get_theme_stylesheet

    app = QApplication.instance()
    previous_font = QFont(app.font())
    previous_stylesheet = app.styleSheet()
    base_font = QFont("DejaVu Sans", 10)
    app.setFont(base_font)
    app.setStyleSheet(get_theme_stylesheet("dark"))
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    try:
        panel.resize(panel.minimumWidth(), 700)
        panel.show()
        for _ in range(4):
            app.processEvents()

        pkg = _verbose_package("firefox")
        panel.set_package_info(pkg)
        panel.tabs.setCurrentWidget(panel.tab_files)
        long_path = (
            "/usr/share/doc/example/" + "very-long-directory-name/" * 6 + "x.txt"
        )
        panel.set_package_files(
            pkg.name,
            [
                PackageFileInfo(path=p, size_bytes=1024 * (i + 1))
                for i, p in enumerate([long_path, "/etc/os-release"] * 12)
            ],
        )
        for _ in range(6):
            app.processEvents()

        assert panel._panel_scroll.verticalScrollBar().maximum() == 0
        wide_vp = panel.files_table.viewport().height()
        panel.file_search_input.setText("/etc/os-release")
        for _ in range(6):
            app.processEvents()
        panel.file_search_input.setText("")
        for _ in range(6):
            app.processEvents()
        assert panel.files_table.viewport().height() == wide_vp

        # Even at 13pt in a 480px window with a multi-line verification summary,
        # the card keeps at least one whole uncut manifest row.
        larger = QFont("DejaVu Sans", 13)
        app.setFont(larger)
        panel.lbl_verify_status.setText(
            "Audit: 12 modified configuration files detected across /etc and /usr/lib64"
        )
        panel.resize(panel.minimumWidth(), 480)
        for _ in range(8):
            app.processEvents()

        row_height = panel.files_table.rowHeight(0)
        viewport_height = panel.files_table.viewport().height()
        assert panel._panel_scroll.verticalScrollBar().isVisible()
        assert row_height > 0
        assert viewport_height >= row_height
        assert viewport_height % row_height == 0
    finally:
        panel.close()
        app.setFont(previous_font)
        app.setStyleSheet(previous_stylesheet)
        app.processEvents()
