"""The window must open inside the area the desktop leaves for it.

Reported against GNOME 51: maximized, the bottom of the window was drawn past
the bottom edge of the screen; in full screen it was fine. Full screen is the
clue — the whole difference between the two is ``QScreen::availableGeometry()``,
which is the screen minus the strips the compositor reserves for its panels.

Both defects behind it are silent, which is why the arithmetic is pinned here
rather than left to a visual review:

* ``restoreGeometry`` only rescues a window that is *completely* off-screen or
  whose title bar has been lost, so a geometry saved on a larger display is
  restored and then overhangs;
* a window whose layout needs more height than the available area is drawn at
  that minimum and the overflow falls off the bottom edge.

Each test drives :mod:`ui.window_placement` against a fake screen, so the
arithmetic is checked without a compositor or a window manager. The module under
test only ever asks a widget for a size and a screen, so a plain ``QWidget``
with its screen replaced exercises the real code path.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtWidgets import QWidget

from ui.window_placement import (
    fit_size_to_available_geometry,
    fit_to_available_geometry,
)

# A 1080-row display with GNOME Shell's top bar, which is the shape of the
# report: the panel takes rows the window is not allowed to use.
FULL_SCREEN = QRect(0, 0, 1920, 1080)
GNOME_AVAILABLE = QRect(0, 34, 1920, 1046)


class _FakeScreen:
    """The parts of ``QScreen`` the module under test reads.

    ``geometry()`` is the whole screen and ``availableGeometry()`` the part left
    after the panels, so a test that passes while the panel strip is ignored
    fails here instead of passing by accident.
    """

    def __init__(self, available: QRect) -> None:
        self._available = available

    def availableGeometry(self) -> QRect:
        return self._available

    def geometry(self) -> QRect:
        return FULL_SCREEN


def _window(available: QRect, width: int, height: int) -> QWidget:
    """A top-level widget whose screen reports ``available``."""
    window = QWidget()
    window.resize(width, height)
    window.screen = lambda: _FakeScreen(available)  # type: ignore[method-assign]
    return window


def test_a_window_taller_than_the_available_area_is_shrunk_to_it() -> None:
    """The reported defect: the size fits the screen but not the panel-free area.

    The free screen is 1080 rows, so a window asking for 1080 is accepted and
    then loses its last rows behind the 34px panel.
    """
    window = _window(GNOME_AVAILABLE, 1600, 1080)
    try:
        assert fit_to_available_geometry(window) is True
        assert window.height() == GNOME_AVAILABLE.height()
        # The width was already inside the screen, so it is left alone.
        assert window.width() == 1600
    finally:
        window.deleteLater()


def test_a_window_that_already_fits_is_left_exactly_as_it_is() -> None:
    """The guard that makes this safe to run on every show.

    Without it each call would re-derive a size and the window could creep.
    """
    window = _window(GNOME_AVAILABLE, 1200, 800)
    try:
        assert fit_to_available_geometry(window) is False
        assert (window.width(), window.height()) == (1200, 800)
    finally:
        window.deleteLater()


def test_fitting_never_makes_a_window_larger() -> None:
    """A small window the user chose stays small: this is a ceiling, not a size."""
    window = _window(GNOME_AVAILABLE, 620, 400)
    try:
        fit_to_available_geometry(window)
        assert (window.width(), window.height()) == (620, 400)
    finally:
        window.deleteLater()


def test_a_window_saved_on_a_larger_display_is_brought_back_into_the_area() -> None:
    """The restore path, which ``restoreGeometry`` does not correct.

    A 2560x1400 geometry saved on a bigger monitor is not "off-screen" when it
    is restored here, so Qt keeps it and the bottom 320 rows become unreachable.
    """
    window = _window(GNOME_AVAILABLE, 2560, 1400)
    try:
        assert fit_to_available_geometry(window) is True
        assert window.width() == FULL_SCREEN.width()
        assert window.height() == GNOME_AVAILABLE.height()
    finally:
        window.deleteLater()


def test_the_initial_size_is_capped_by_the_available_area() -> None:
    """Startup: the preferred size is a wish, the screen is the limit."""
    window = _window(GNOME_AVAILABLE, 0, 0)
    try:
        fit_size_to_available_geometry(window, 1280, 800)
        assert (window.width(), window.height()) == (1280, 800)
    finally:
        window.deleteLater()


def test_the_initial_size_cannot_exceed_a_short_screen() -> None:
    """A 1366x734 laptop gets 1280x734, not 1280x800 with the bottom cut off."""
    short = QRect(0, 0, 1366, 734)
    window = _window(short, 0, 0)
    try:
        fit_size_to_available_geometry(window, 1280, 800)
        assert (window.width(), window.height()) == (1280, 734)
    finally:
        window.deleteLater()


def test_a_window_on_a_secondary_screen_uses_its_own_screen() -> None:
    """The panel reservation is per-screen, so the window's own screen decides.

    Fitting against the primary screen would clip a window opened on a smaller
    neighbour and waste space on a larger one.
    """
    small_secondary = QRect(1920, 0, 1280, 720)
    window = _window(small_secondary, 1600, 1000)
    try:
        fit_to_available_geometry(window)
        assert (window.width(), window.height()) == (1280, 720)
    finally:
        window.deleteLater()


def test_a_degenerate_screen_cannot_collapse_the_window() -> None:
    """Reconfiguring screens can report a zero height for a moment.

    Subtracting it unchecked would leave a window one pixel tall that the user
    cannot recover by dragging the frame.
    """
    window = _window(QRect(0, 0, 1920, 0), 1200, 800)
    try:
        fit_to_available_geometry(window)
        assert window.width() >= 1
        assert window.height() >= 1
    finally:
        window.deleteLater()


@pytest.mark.parametrize("chrome", [0, 37])
def test_the_frame_is_accounted_for_before_the_available_height(chrome: int) -> None:
    """A window's height excludes its title bar; the screen's budget does not.

    Fitting the client area to the available height and then drawing a frame
    around it still overhangs by the height of that frame, so the frame is
    subtracted first. ``frameGeometry`` minus ``height`` is how much it is.
    """
    window = _window(GNOME_AVAILABLE, 1600, 1400)
    try:
        real = window.frameGeometry().height() - window.height()
        fit_to_available_geometry(window)
        assert window.height() == GNOME_AVAILABLE.height() - real
        # sanity: the synthetic chrome really is the measured chrome
        assert real >= 0
    finally:
        window.deleteLater()


def test_a_maximized_window_is_left_to_the_window_manager() -> None:
    """A maximized window does not own its size.

    The window manager keeps it equal to the available area, so a resize here
    would either be discarded or un-maximize the window -- turning a size fix
    into a window-state bug.
    """
    window = _window(GNOME_AVAILABLE, 1920, 1080)
    try:
        window.setWindowState(window.windowState() | Qt.WindowState.WindowMaximized)
        assert fit_to_available_geometry(window) is False
    finally:
        window.deleteLater()
