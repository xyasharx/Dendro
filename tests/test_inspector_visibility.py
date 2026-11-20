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
