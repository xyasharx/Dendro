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
