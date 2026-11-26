"""Regression coverage for restoring the package-detail inspector."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSplitter, QWidget

from dendro.ui.main_window import MainWindow


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

    from dendro.ui.fonts import OVERLINE_PT_DELTA, scaled_font
    from dendro.ui.inspector_panel import PackageInspectorPanel

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

    from dendro.ui.inspector_panel import PackageInspectorPanel

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

    from dendro.ui.delegates import DendroTreeView, PackageTreeItemDelegate
    from dendro.ui.sidebar import CategorySidebar

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

    from dendro.ui.delegates import PackageTreeItemDelegate
    from dendro.ui.sidebar import SidebarItemDelegate

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
    from dendro.core.backend import PackageInfo, PackageState

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

    from dendro.core.backend import PackageFileInfo
    from dendro.core.host_environment import HostEnvironment
    from dendro.ui.styles import get_theme_stylesheet

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

    from dendro.core.backend import PackageFileInfo
    from dendro.core.host_environment import HostEnvironment
    from dendro.ui.inspector_panel import PackageInspectorPanel
    from dendro.ui.styles import get_theme_stylesheet

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

    from dendro.ui.inspector_panel import PackageInspectorPanel
    from dendro.ui.styles import get_theme_stylesheet

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

    from dendro.core.backend import PackageFileInfo
    from dendro.ui.inspector_panel import PackageInspectorPanel
    from dendro.ui.styles import get_theme_stylesheet

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


def test_badge_and_tag_pills_scale_with_large_fonts(qtbot) -> None:
    """Counter, status, and tag pills grow with ``fm_badge`` at large font sizes."""
    from PyQt6.QtCore import QCoreApplication, QEvent, QRect
    from PyQt6.QtGui import QFont, QImage, QPainter
    from PyQt6.QtWidgets import QApplication, QStyleOptionViewItem

    from dendro.core.backend import PackageInfo, PackageState
    from dendro.core.models import DependencyTreeModel
    from dendro.ui.delegates import PackageTreeItemDelegate
    from dendro.ui.sidebar import CategorySidebar

    app = QApplication.instance()
    previous = QFont(app.font())
    larger = QFont("DejaVu Sans", 24)
    app.setFont(larger)
    try:
        sidebar = CategorySidebar()
        qtbot.addWidget(sidebar)
        QCoreApplication.sendPostedEvents(sidebar, QEvent.Type.FontChange.value)
        QCoreApplication.processEvents()

        tree_delegate = PackageTreeItemDelegate()
        sidebar_delegate = sidebar.sidebar_delegate

        assert sidebar_delegate.item_height >= sidebar_delegate.fm_badge.height() + 8
        assert tree_delegate.row_height >= tree_delegate.fm_badge.height() + 10

        expected_sidebar_pill = max(18, sidebar_delegate.fm_badge.height() + 4)
        expected_status_pill = max(20, tree_delegate.fm_badge.height() + 6)
        expected_tag_pill = max(16, tree_delegate.fm_badge.height() + 4)

        assert expected_sidebar_pill <= sidebar_delegate.item_height - 4
        assert expected_status_pill <= tree_delegate.row_height - 2
        assert expected_tag_pill <= tree_delegate.row_height - 2

        model = DependencyTreeModel()
        pkg = PackageInfo(
            name="orphan-example",
            version="1.0",
            release="1.fc45",
            arch="x86_64",
            summary="Test package",
            state=PackageState.INSTALLED,
            is_orphan=True,
        )
        model.set_packages([pkg])

        image = QImage(640, tree_delegate.row_height, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            for col in (DependencyTreeModel.COL_NAME, DependencyTreeModel.COL_STATUS):
                opt = QStyleOptionViewItem()
                opt.rect = QRect(0, 0, 320, tree_delegate.row_height)
                tree_delegate.paint(painter, opt, model.index(0, col))
        finally:
            painter.end()

        sidebar.update_category_counts({"all": 42})
        sb_image = QImage(
            320, sidebar_delegate.item_height, QImage.Format.Format_ARGB32
        )
        sb_painter = QPainter(sb_image)
        try:
            sb_opt = QStyleOptionViewItem()
            sb_opt.rect = QRect(0, 0, 320, sidebar_delegate.item_height)
            all_item = sidebar._category_items["all"]
            sidebar_delegate.paint(sb_painter, sb_opt, sidebar.indexFromItem(all_item))
        finally:
            sb_painter.end()
    finally:
        app.setFont(previous)
        QCoreApplication.processEvents()


def test_dialog_title_label_is_scaled_with_application_font(qtbot) -> None:
    """Dialog headers using ``DialogTitleLabel`` follow ``dendro/ui/fonts.py`` scaling."""
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication, QLabel

    from dendro.ui.flatpak_dialog import FlatpakInventoryDialog
    from dendro.ui.fonts import _LABEL_STYLES, HEADING_PT_DELTA, scaled_font
    from dendro.ui.history_dialog import DnfHistoryDialog
    from dendro.ui.repo_dialog import RepoManagerDialog

    assert "DialogTitleLabel" in _LABEL_STYLES
    assert _LABEL_STYLES["DialogTitleLabel"][0] == HEADING_PT_DELTA

    app = QApplication.instance()
    previous = QFont(app.font())
    app.setFont(QFont("DejaVu Sans", 16))
    try:
        expected = scaled_font(HEADING_PT_DELTA, weight=QFont.Weight.Bold)
        for dialog in (
            RepoManagerDialog(),
            DnfHistoryDialog(),
            FlatpakInventoryDialog(),
        ):
            qtbot.addWidget(dialog)
            title = dialog.findChild(QLabel, "DialogTitleLabel")
            assert title is not None
            assert title.font().pointSizeF() == expected.pointSizeF()
            assert title.font().weight() == QFont.Weight.Bold
    finally:
        app.setFont(previous)
        QApplication.processEvents()


def test_space_shortcut_is_scoped_to_package_tree_only(qtbot, monkeypatch) -> None:
    """Pressing Space outside the tree (e.g. search bar) must not queue packages."""
    from PyQt6.QtGui import QShortcut

    from dendro.core.host_environment import HostEnvironment

    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)
    window = MainWindow(host_environment=HostEnvironment.traditional())
    qtbot.addWidget(window)

    space_shortcuts = [
        shortcut
        for shortcut in window.findChildren(QShortcut)
        if shortcut.key().toString() == "Space"
    ]
    assert len(space_shortcuts) == 1
    shortcut = space_shortcuts[0]
    assert shortcut.parent() is window.tree_view
    assert shortcut.context() == Qt.ShortcutContext.WidgetWithChildrenShortcut
    window.close()


def test_narrow_window_keeps_inspector_vertical_scrollbar_pinned_to_visible_edge(
    qtbot, monkeypatch, tmp_path
) -> None:
    """Narrow windows (e.g. 511x676) keep the outer vertical bar visible on-screen.

    Regression for issue #124: previously MainWindow's ``_inspector_host``
    disabled vertical scrolling (``ScrollBarAlwaysOff``) while delegating
    vertical overflow to ``inspector_panel._panel_scroll``. When the window
    narrowed past ``inspector_panel.minimumWidth()``, ``_panel_scroll``'s
    vertical scrollbar remained off-screen at the right edge of the wider
    inner panel, forcing the user to scroll horizontally just to reach it.
    """
    from PyQt6.QtCore import QItemSelectionModel, QPoint, QPointF
    from PyQt6.QtGui import QFont, QWheelEvent
    from PyQt6.QtWidgets import QApplication

    from dendro.core.host_environment import HostEnvironment
    from dendro.ui.styles import get_theme_stylesheet

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)

    app = QApplication.instance()
    previous_stylesheet = app.styleSheet()
    previous_font = QFont(app.font())
    app.setFont(QFont("DejaVu Sans", 11))
    app.setStyleSheet(get_theme_stylesheet("dark"))

    window = MainWindow(host_environment=HostEnvironment.traditional())
    qtbot.addWidget(window)
    try:
        monkeypatch.setattr(window.thread_pool, "start", lambda *_args, **_kwargs: None)

        fitted = {"done": False}
        orig_fit = window._fit_to_screen

        def _tracked_fit() -> None:
            orig_fit()
            fitted["done"] = True

        window._fit_to_screen = _tracked_fit
        window.show()
        qtbot.waitUntil(lambda: fitted["done"])
        window.resize(1280, 800)
        qtbot.waitUntil(lambda: window.width() == 1280 and window.height() == 800)
        pref = window.inspector_panel.preferred_dock_width()
        window.main_splitter.setSizes([260, max(480, 1260 - 260 - pref), pref])
        for _ in range(6):
            app.processEvents()

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

        host = window._inspector_host
        host_vbar = host.verticalScrollBar()
        host_hbar = host.horizontalScrollBar()
        panel_vbar = window.inspector_panel._panel_scroll.verticalScrollBar()

        assert window.inspector_panel.property("scrollHosted") is True
        assert window.inspector_panel.preferred_dock_width() >= (
            window.inspector_panel.minimumWidth()
        )

        # At default 1280x800 dock width, reserving preferred_dock_width()
        # keeps the horizontal scrollbar hidden even if vertical overflow appears.
        assert not host_hbar.isVisible()
        assert host_hbar.maximum() == 0

        # Narrow across the three-pane responsive threshold so the sidebar
        # auto-collapses (lowering minimumWidth below 511 px), then shrink to
        # the narrow client dimensions of a 511x676 decorated window.
        window.resize(640, 580)
        qtbot.waitUntil(lambda: not window.sidebar.isVisible())
        window.resize(511, 580)
        qtbot.waitUntil(lambda: window.width() == 511 and window.height() == 580)
        for _ in range(8):
            app.processEvents()

        assert host.width() < window.inspector_panel.minimumWidth()
        assert host_hbar.isVisible()
        assert host_hbar.maximum() > 0
        assert host_vbar.isVisible()
        assert host_vbar.maximum() > 0
        assert not panel_vbar.isVisible()

        vbar_origin = host_vbar.mapTo(host, QPoint(0, 0))
        assert 0 <= vbar_origin.x() < host.width()
        assert vbar_origin.x() + host_vbar.width() <= host.width()

        # Scrolling horizontally must leave the vertical bar pinned to the
        # visible right edge of _inspector_host rather than drifting off-screen.
        host_hbar.setValue(host_hbar.maximum())
        for _ in range(4):
            app.processEvents()
        vbar_after_hscroll = host_vbar.mapTo(host, QPoint(0, 0))
        assert vbar_after_hscroll.x() == vbar_origin.x()
        assert host_vbar.isVisible()

        # Wheel events over non-scrolling header elements must bubble past the
        # disabled _panel_scroll directly to _inspector_host. (Synthetic
        # QApplication.sendEvent skips parent propagation, so walk the exact
        # parent chain Qt uses for spontaneous wheel events.)
        host_vbar.setValue(0)
        target: QWidget | None = window.inspector_panel.pkg_name_label
        wheel_down = QWheelEvent(
            QPointF(target.rect().center()),
            QPointF(target.mapToGlobal(target.rect().center())),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        wheel_down.ignore()
        while target is not None and not wheel_down.isAccepted():
            QApplication.sendEvent(target, wheel_down)
            if target is window.inspector_panel._panel_scroll:
                assert not wheel_down.isAccepted()
            target = target.parentWidget()
        for _ in range(4):
            app.processEvents()
        assert wheel_down.isAccepted()
        assert host_vbar.value() > 0
        assert panel_vbar.value() == 0

        # Switching tabs keeps the hosted vertical scrollbar synced and visible.
        window.inspector_panel.tabs.setCurrentIndex(1)
        for _ in range(6):
            app.processEvents()
        assert host_vbar.isVisible()
        assert host_vbar.maximum() > 0
        assert not panel_vbar.isVisible()
    finally:
        window.close()
        app.setFont(previous_font)
        app.setStyleSheet(previous_stylesheet)
        app.processEvents()
