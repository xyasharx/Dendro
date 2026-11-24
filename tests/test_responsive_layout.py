"""Regression tests for half-screen tiling, responsive header wrapping, and Wayland/debug logging."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QLoggingCategory, QPoint, QPointF, QRect, QSize, Qt
from PyQt6.QtGui import QFont, QMouseEvent
from PyQt6.QtWidgets import QApplication, QFrame

import main as dendro_main
from core.host_environment import HostEnvironment
from ui.header import HeaderBar
from ui.main_window import MainWindow
from ui.styles import get_theme_stylesheet


def _flush_show(qtbot, window: MainWindow) -> None:
    fitted = {"done": False}
    orig_fit = window._fit_to_screen

    def _tracked_fit() -> None:
        orig_fit()
        fitted["done"] = True

    window._fit_to_screen = _tracked_fit
    window.show()
    qtbot.waitUntil(lambda: fitted["done"])
    QApplication.processEvents()


def test_mainwindow_minimum_width_fits_half_screen_tiles_across_states(
    qtbot, monkeypatch, tmp_path
) -> None:
    """MainWindow minimumWidth must allow GNOME Wayland side-by-side tiling at 1280px+."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)
    app = QApplication.instance()
    original_font = QFont(app.font())
    original_stylesheet = app.styleSheet()
    try:
        for point_size in (10, 11, 12):
            app.setFont(QFont("DejaVu Sans", point_size))
            window = MainWindow(host_environment=HostEnvironment.traditional())
            qtbot.addWidget(window)
            monkeypatch.setattr(
                window.thread_pool, "start", lambda *_args, **_kwargs: None
            )

            # Default startup state
            assert window.minimumSizeHint().width() <= 640
            assert window.minimumSizeHint().height() <= 320

            # Background worker discovers updates, user enters Updates view,
            # stages queue items, and exposes orphan cleanup.
            window.header.update_available_updates_badge(14)
            window.header.set_updates_view_active(True, 14)
            window.header.update_queue_badge(5)
            window.header.set_orphan_clean_visible(True)
            QApplication.processEvents()

            assert window.minimumWidth() <= 640
            assert window.minimumSizeHint().width() <= 640
            assert window.minimumSizeHint().height() <= 320
            window.close()
    finally:
        app.setFont(original_font)
        app.setStyleSheet(original_stylesheet)


def test_header_bar_single_row_at_1280_and_wraps_cleanly_when_narrow(
    qtbot,
) -> None:
    """HeaderBar stays one row on standard desktop and wraps without clipping on tiles."""
    app = QApplication.instance()
    original_font = QFont(app.font())
    original_stylesheet = app.styleSheet()
    try:
        app.setStyleSheet(get_theme_stylesheet("dark"))
        app.setFont(QFont("DejaVu Sans", 10))
        header = HeaderBar()
        qtbot.addWidget(header)
        header.show()
        QApplication.processEvents()

        # Default state at 1280px stays on a single clean row (one row <= 58px;
        # wrapped two-row heights are >= 74px).
        assert header.heightForWidth(1280) <= 58

        app.setFont(QFont("DejaVu Sans", 11))
        QApplication.processEvents()
        assert header.heightForWidth(1440) <= 58

        # With all optional contextual controls visible, test every width down to 500px.
        header.update_available_updates_badge(9)
        header.set_updates_view_active(True, 9)
        header.update_queue_badge(2)
        header.set_orphan_clean_visible(True)
        QApplication.processEvents()

        previous_height = 0
        for width in (1440, 1280, 960, 720, 640, 500):
            header.resize(width, header.heightForWidth(width))
            QApplication.processEvents()
            assert header.height() >= previous_height
            previous_height = header.height()

            # Every visible control stays inside header bounds and preserves readable width.
            assert header.search_input.width() >= 120
            for child in (
                header.sidebar_toggle_btn,
                header.search_help_btn,
                header.updates_btn,
                header.upgrade_context_btn,
                header.refresh_live_btn,
                header.clean_orphans_btn,
                header.reload_btn,
                header.history_btn,
                header.advisories_btn,
                header.repos_btn,
                header.flatpak_btn,
                header.settings_btn,
                header.inspector_btn,
                header.discard_btn,
                header.apply_btn,
            ):
                if child.isVisible():
                    rect = child.geometry()
                    assert rect.height() > 0
                    if child.parentWidget() is header:
                        assert rect.left() >= 0
                        assert rect.right() <= width
                        assert rect.bottom() <= header.height()

            # Hairline separator must never sit orphaned at the start or end of a wrapped row.
            separators = [
                frame
                for frame in header.findChildren(QFrame)
                if frame.frameShape() == QFrame.Shape.VLine
            ]
            assert len(separators) == 1
            sep_geom = separators[0].geometry()
            if sep_geom.width() > 0:
                left, _, right, _ = header.layout().getContentsMargins()
                assert sep_geom.left() > left
                assert sep_geom.right() < width - right
            else:
                assert sep_geom == QRect(0, 0, 0, 0)

        # Sweeping widths exercises exact row-break boundaries for the separator.
        sep = next(
            frame
            for frame in header.findChildren(QFrame)
            if frame.frameShape() == QFrame.Shape.VLine
        )
        left, _, right, _ = header.layout().getContentsMargins()
        for sweep_w in range(480, 1440, 20):
            header.resize(sweep_w, header.heightForWidth(sweep_w))
            QApplication.processEvents()
            g = sep.geometry()
            if g.width() == 0:
                assert g == QRect(0, 0, 0, 0)
            else:
                assert g.left() > left
                assert g.right() < sweep_w - right
    finally:
        app.setFont(original_font)
        app.setStyleSheet(original_stylesheet)


def test_header_empty_space_double_click_and_drag_behavior(qtbot, monkeypatch) -> None:
    """Double-clicking empty header background toggles maximize; dragging calls startSystemMove."""
    header = HeaderBar()
    qtbot.addWidget(header)
    header.resize(960, 120)
    header.show()
    QApplication.processEvents()

    fake_window = SimpleNamespace(
        maximized=False,
        drag_calls=0,
    )
    fake_window.isMaximized = lambda: fake_window.maximized
    fake_window.showMaximized = lambda: setattr(fake_window, "maximized", True)
    fake_window.showNormal = lambda: setattr(fake_window, "maximized", False)
    fake_handle = SimpleNamespace(
        startSystemMove=lambda: setattr(
            fake_window, "drag_calls", fake_window.drag_calls + 1
        )
    )
    fake_window.windowHandle = lambda: fake_handle
    monkeypatch.setattr(header, "window", lambda: fake_window)

    # Pick an empty point outside every visible child widget.
    empty_pt = QPoint(header.width() - 6, header.height() - 6)
    assert header.childAt(empty_pt) is None

    dbl_event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(empty_pt),
        QPointF(empty_pt),
        QPointF(empty_pt),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    header.mouseDoubleClickEvent(dbl_event)
    assert fake_window.maximized is True

    header.mouseDoubleClickEvent(dbl_event)
    assert fake_window.maximized is False

    # Double-clicking over an interactive button must NOT toggle maximize.
    btn_pt = header.settings_btn.geometry().center()
    btn_dbl = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(btn_pt),
        QPointF(btn_pt),
        QPointF(btn_pt),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    header.mouseDoubleClickEvent(btn_dbl)
    assert fake_window.maximized is False

    # Dragging past startDragDistance on empty header background initiates window move.
    press_ev = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(empty_pt),
        QPointF(empty_pt),
        QPointF(empty_pt),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    move_far = QPointF(empty_pt.x() - 40, empty_pt.y())
    move_ev = QMouseEvent(
        QEvent.Type.MouseMove,
        move_far,
        move_far,
        move_far,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    header.mousePressEvent(press_ev)
    header.mouseMoveEvent(move_ev)
    assert fake_window.drag_calls == 1


def test_inspector_host_preserves_tab_width_and_handles_close_and_narrow_sidebar(
    qtbot, monkeypatch, tmp_path
) -> None:
    """Inspector scroll host allows 640px tiling while auto-collapsing/restoring sidebar safely."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)
    app = QApplication.instance()
    original_stylesheet = app.styleSheet()
    window = MainWindow(host_environment=HostEnvironment.traditional())
    qtbot.addWidget(window)
    monkeypatch.setattr(window.thread_pool, "start", lambda *_args, **_kwargs: None)
    try:
        _flush_show(qtbot, window)

        assert window.inspector_panel.minimumWidth() >= 360
        assert window._inspector_host.minimumWidth() == 240

        # Expand past three-pane threshold so all three panes sit side by side.
        window.resize(1280, 800)
        qtbot.waitUntil(lambda: window.width() == 1280)
        qtbot.waitUntil(lambda: window.sidebar.isVisible())
        qtbot.waitUntil(lambda: window._inspector_panel_is_open())
        assert window._inspector_host.isVisible()

        # Shrinking to a 640px half-tile collapses sidebar so tree + inspector stay usable.
        window.resize(640, 800)
        qtbot.waitUntil(lambda: window.width() == 640)
        qtbot.waitUntil(lambda: not window.sidebar.isVisible())
        assert window._inspector_host.isVisible()

        # Closing the inspector via its panel signal hides _inspector_host and restores sidebar.
        window.inspector_panel.closed.emit()
        qtbot.waitUntil(lambda: not window._inspector_host.isVisible())
        qtbot.waitUntil(lambda: window.sidebar.isVisible())
        assert not window._inspector_panel_is_open()

        # Re-opening inspector at 640px auto-collapses sidebar again and shows _inspector_host.
        window.header.toggle_inspector_clicked.emit()
        qtbot.waitUntil(lambda: window._inspector_host.isVisible())
        qtbot.waitUntil(lambda: not window.sidebar.isVisible())

        # Closing while tiled must restore the auto-collapsed sidebar before saving splitter state.
        window.close()
        qtbot.waitUntil(lambda: not window.sidebar.isHidden())

        # Re-opening on a wider window restores the visible sidebar rather than keeping it hidden.
        reopened = MainWindow(host_environment=HostEnvironment.traditional())
        qtbot.addWidget(reopened)
        monkeypatch.setattr(
            reopened.thread_pool, "start", lambda *_args, **_kwargs: None
        )
        assert not reopened.sidebar.isHidden()
        reopened.close()
    finally:
        app.setStyleSheet(original_stylesheet)


def test_qt_logging_environment_and_startup_diagnostics(
    qtbot, monkeypatch, tmp_path, capsys
) -> None:
    """QT_DEBUG_PLUGINS and -d override Fedora qtlogging.ini and emit startup diagnostics."""
    env_plugins: dict[str, str] = {"QT_DEBUG_PLUGINS": "1"}
    rules_plugins = dendro_main._configure_qt_logging_environment(
        env_plugins, debug_flag=False
    )
    assert rules_plugins == "qt.core.plugin.*=true"
    assert "DENDRO_DEBUG" not in env_plugins

    env_debug: dict[str, str] = {"QT_LOGGING_RULES": "custom.rule=true"}
    rules_debug = dendro_main._configure_qt_logging_environment(
        env_debug, debug_flag=True
    )
    assert env_debug["DENDRO_DEBUG"] == "1"
    assert "custom.rule=true" in rules_debug
    assert "qt.core.plugin.*=true" in rules_debug
    assert "qt.qpa.wayland.debug=true" in rules_debug
    assert "qt.qpa.qwaylandadwaitadecoration.debug=true" in rules_debug

    # Respect an explicit user override for a category prefix rather than duplicating it.
    env_user = {"QT_LOGGING_RULES": "qt.qpa.wayland.*=false"}
    rules_user = dendro_main._configure_qt_logging_environment(
        env_user, debug_flag=True
    )
    assert "qt.qpa.wayland.*=false" in rules_user
    assert "qt.qpa.wayland.debug=true" not in rules_user

    # Verify Qt logging category parsing works cleanly even when qtlogging.ini disables *.debug.
    assert isinstance(
        QLoggingCategory("qt.core.plugin.factoryloader").isDebugEnabled(), bool
    )

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("DENDRO_DEBUG", "1")
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)
    app = QApplication.instance()
    original_stylesheet = app.styleSheet()
    window = MainWindow(host_environment=HostEnvironment.traditional())
    qtbot.addWidget(window)
    monkeypatch.setattr(window.thread_pool, "start", lambda *_args, **_kwargs: None)
    try:
        _flush_show(qtbot, window)
        capsys.readouterr()

        fake_screen = SimpleNamespace(
            name=lambda: "eDP-1",
            geometry=lambda: QRect(0, 0, 1920, 1080),
            availableGeometry=lambda: QRect(0, 32, 1920, 1048),
            devicePixelRatio=lambda: 1.25,
        )
        fake_app = SimpleNamespace(
            platformName=lambda: "wayland",
            screens=lambda: [fake_screen],
        )
        dendro_main._log_startup_diagnostics(fake_app, window)
        window.resize(700, 560)
        qtbot.waitUntil(lambda: window.size() == QSize(700, 560))
        QApplication.processEvents()

        captured = capsys.readouterr().err
        assert "Qt platform='wayland'" in captured
        assert "Screen[0] name='eDP-1'" in captured
        assert "Window resize: size=700x560" in captured
        window.close()
    finally:
        app.setStyleSheet(original_stylesheet)
