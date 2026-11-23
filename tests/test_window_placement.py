"""The window must open inside the area the desktop leaves for it.

Reported against GNOME 51: maximized, the bottom of the window was drawn past
the bottom edge of the screen; in full screen it was fine. Full screen is the
clue -- the whole difference between the two is ``QScreen::availableGeometry()``,
which is the screen minus the strips the compositor reserves for its panels.

Both defects behind it are silent, which is why the arithmetic is pinned here
rather than left to a visual review:

* ``restoreGeometry`` only rescues a window that is *completely* off-screen or
  whose title bar has been lost, so a geometry saved on a larger display is
  restored and then overhangs;
* a window whose layout needs more height than the available area is drawn at
  that minimum and the overflow falls off the bottom edge.

:mod:`ui.window_placement` only ever asks a widget for a size, a screen and its
window state, so most of these tests drive it with plain objects and no Qt at
all: the arithmetic is what can be wrong, and this keeps it checkable without a
``QApplication``, a compositor or a window manager. One test then proves that a
real ``QWidget`` really does satisfy that interface, so the doubles cannot drift
away from Qt silently.
"""

from __future__ import annotations

from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QWidget

from ui.window_placement import (
    fit_size_to_available_geometry,
    fit_to_available_geometry,
)

# A 1080-row display with GNOME Shell's top bar, which is the shape of the
# report: the panel takes rows the window is not allowed to use.
FULL_SCREEN_HEIGHT = 1080
PANEL_HEIGHT = 34
GNOME_AVAILABLE_HEIGHT = FULL_SCREEN_HEIGHT - PANEL_HEIGHT


class _FakeRect:
    def __init__(self, width: int, height: int) -> None:
        self._width = width
        self._height = height

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height


class _FakeScreen:
    """The parts of ``QScreen`` the module under test reads.

    ``geometry()`` is the whole screen and ``availableGeometry()`` the part left
    after the panels, so an implementation that fits to the screen -- the
    original bug -- fails here instead of passing by accident.
    """

    def __init__(self, available_width: int, available_height: int) -> None:
        self._available = _FakeRect(available_width, available_height)

    def availableGeometry(self) -> _FakeRect:
        return self._available

    def geometry(self) -> _FakeRect:
        return _FakeRect(1920, FULL_SCREEN_HEIGHT)


class _FakeWidget:
    """A window in the sense :mod:`ui.window_placement` cares about."""

    def __init__(
        self,
        width: int,
        height: int,
        screen: _FakeScreen | None = None,
        *,
        frame_extra: int = 0,
        maximized: bool = False,
        full_screen: bool = False,
    ) -> None:
        self._width = width
        self._height = height
        self._screen = screen
        self._frame_extra = frame_extra
        self._maximized = maximized
        self._full_screen = full_screen
        self.resizes = 0

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height

    def screen(self) -> _FakeScreen | None:
        return self._screen

    def isMaximized(self) -> bool:
        return self._maximized

    def isFullScreen(self) -> bool:
        return self._full_screen

    def frameGeometry(self) -> _FakeRect:
        return _FakeRect(self._width, self._height + self._frame_extra)

    def resize(self, width: int, height: int) -> None:
        assert width >= 0 and height >= 0, (
            f"resize to a negative size: {width}x{height}"
        )
        self._width = width
        self._height = height
        self.resizes += 1

    def size(self) -> tuple[int, int]:
        return (self._width, self._height)


def _gnome_window(width: int, height: int, **kwargs) -> _FakeWidget:
    return _FakeWidget(
        width, height, _FakeScreen(1920, GNOME_AVAILABLE_HEIGHT), **kwargs
    )


def test_a_window_taller_than_the_available_area_is_shrunk_to_it() -> None:
    """The reported defect: the size fits the screen but not the panel-free area.

    The free screen is 1080 rows, so a window asking for 1080 is accepted and
    then loses its last rows behind the 34px panel.
    """
    window = _gnome_window(1600, FULL_SCREEN_HEIGHT)

    assert fit_to_available_geometry(window) is True
    assert window.height() == GNOME_AVAILABLE_HEIGHT
    # The width was already inside the screen, so it is left alone.
    assert window.width() == 1600


def test_a_window_that_already_fits_is_left_exactly_as_it_is() -> None:
    """The guard that makes this safe to run on every show.

    Without it each call would re-derive a size and the window could creep, and
    the test asserts "no resize happened at all" rather than trusting that the
    result merely equals the input.
    """
    window = _gnome_window(1200, 800)

    assert fit_to_available_geometry(window) is False
    assert window.resizes == 0
    assert window.size() == (1200, 800)


def test_fitting_never_makes_a_window_larger() -> None:
    """A small window the user chose stays small: this is a ceiling, not a size."""
    window = _gnome_window(620, 400)

    fit_to_available_geometry(window)

    assert window.size() == (620, 400)
    assert window.resizes == 0


def test_a_window_saved_on_a_larger_display_is_brought_back_into_the_area() -> None:
    """The restore path, which ``restoreGeometry`` does not correct.

    A 2560x1400 geometry saved on a bigger monitor is not "off-screen" when it
    is restored here, so Qt keeps it and the bottom 320 rows become unreachable.
    """
    window = _gnome_window(2560, 1400)

    assert fit_to_available_geometry(window) is True
    assert window.width() == 1920
    assert window.height() == GNOME_AVAILABLE_HEIGHT


def test_the_initial_size_is_kept_when_it_fits() -> None:
    """Startup: the preferred size is kept when the screen can hold it."""
    window = _gnome_window(0, 0)

    fit_size_to_available_geometry(window, 1280, 800)

    assert window.size() == (1280, 800)


def test_the_initial_size_cannot_exceed_a_short_screen() -> None:
    """A 1366x734 laptop gets 1280x734, not 1280x800 with the bottom cut off."""
    window = _FakeWidget(0, 0, _FakeScreen(1366, 734))

    fit_size_to_available_geometry(window, 1280, 800)

    assert window.size() == (1280, 734)


def test_a_window_on_a_secondary_screen_uses_its_own_screen() -> None:
    """The panel reservation is per-screen, so the window's own screen decides.

    Fitting against the primary screen would clip a window opened on a smaller
    neighbour and waste space on a larger one.
    """
    window = _FakeWidget(1600, 1000, _FakeScreen(1280, 720))

    fit_to_available_geometry(window)

    assert window.size() == (1280, 720)


def test_a_degenerate_screen_cannot_collapse_the_window() -> None:
    """Reconfiguring screens can report a zero height for a moment.

    Subtracting it unchecked would leave a window one pixel tall that the user
    cannot recover by dragging the frame.
    """
    window = _FakeWidget(1200, 800, _FakeScreen(1920, 0))

    fit_to_available_geometry(window)

    assert window.width() >= 1
    assert window.height() >= 1


def test_the_frame_is_subtracted_from_the_height_budget() -> None:
    """A window's height excludes its title bar; the screen's budget does not.

    Fitting the client area to the available height and then drawing a frame
    around it still overhangs by exactly the height of that frame.
    """
    window = _gnome_window(1200, 1400, frame_extra=40)

    fit_to_available_geometry(window)

    assert window.height() == GNOME_AVAILABLE_HEIGHT - 40


def test_a_maximized_window_is_left_to_the_window_manager() -> None:
    """A maximized window does not own its size.

    The window manager keeps it equal to the available area, so a resize here
    would either be discarded or un-maximize the window -- turning a size fix
    into a window-state bug.
    """
    window = _gnome_window(1920, FULL_SCREEN_HEIGHT, maximized=True)

    assert fit_to_available_geometry(window) is False
    assert window.resizes == 0


def test_a_full_screen_window_is_left_alone() -> None:
    """Full screen legitimately covers the panel, so it must not be clamped."""
    window = _gnome_window(1920, FULL_SCREEN_HEIGHT, full_screen=True)

    assert fit_to_available_geometry(window) is False
    assert window.resizes == 0


def test_a_window_with_no_screen_is_left_alone() -> None:
    """A widget with no screen yet must not raise during early construction."""
    window = _FakeWidget(1200, 800, None)

    assert fit_to_available_geometry(window) is False
    assert window.size() == (1200, 800)


def test_the_double_matches_a_real_widget(qapp) -> None:
    """The doubles above must not drift away from the real widget.

    Every test in this file stands in for a ``QWidget``, which is only sound
    while ``QWidget`` still answers the same calls. This is the one test that
    uses the real class: it asserts the interface the module depends on exists
    and that the function returns a bool rather than raising. Without it, a Qt
    rename would leave twelve passing tests exercising a fiction.
    """
    window = QWidget()
    try:
        for name in (
            "width",
            "height",
            "resize",
            "screen",
            "frameGeometry",
            "isMaximized",
            "isFullScreen",
        ):
            assert callable(getattr(window, name, None)), f"QWidget has no {name}()"

        # A real widget clamps without raising, whatever the screen reports.
        assert isinstance(fit_to_available_geometry(window), bool)
        assert fit_size_to_available_geometry(window, 1280, 800) is None
    finally:
        window.deleteLater()

    # ``QRect`` is what the module returns from the real screen, so it must
    # keep the two accessors the arithmetic reads.
    assert QRect(0, 0, 10, 20).width() == 10
    assert QRect(0, 0, 10, 20).height() == 20
