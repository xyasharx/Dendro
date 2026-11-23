"""Keep the window inside the area the desktop actually leaves for it.

A maximized window is not the whole screen: the compositor reserves the panel
strips and hands the client ``QScreen::availableGeometry()``. GNOME Shell's top
bar is the common case -- with a 34px bar, a 1080-row display leaves a maximized
window 1046 rows -- and on a shorter panel, a larger text scale, or a second
monitor whose strips differ from the primary one's, that reservation is where
the bottom of a window goes missing.

Two things can put a window outside that area, and both are silent:

* a saved geometry from a larger monitor, a higher text scale, or an earlier
  session is restored verbatim -- ``restoreGeometry`` only rescues a window that
  is *completely* off-screen or whose title bar is lost, so a window that is
  merely 60px too tall stays 60px too tall;
* a window whose layout needs more height than the available area cannot be
  laid out smaller, so it is drawn at its minimum and the overflow falls off
  the bottom edge -- exactly what happens when the screen is smaller than the
  session that sized the layout's contents.

The remedy is the same for both: before the first paint, ask the screen the
window is on what it has left, and fit the window to that.
"""

from __future__ import annotations

from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication, QWidget


def _available_geometry(widget: QWidget) -> QRect | None:
    """The usable area of the screen ``widget`` is on, or ``None``.

    ``screen()`` is the authoritative answer once a window has been shown; a
    window that has not been shown yet reports the primary screen, which is also
    what ``restoreGeometry`` resolves against. Both paths are therefore covered
    without having to guess from a coordinate.
    """
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is None:
        return None
    return screen.availableGeometry()


def fit_to_available_geometry(widget: QWidget) -> bool:
    """Shrink ``widget`` to the available area of its screen, never growing it.

    Returns ``True`` when the size was changed. A window that already fits is
    left exactly as it is, so this is safe to run on every show: it cannot fight
    the user's own resizing, and it cannot move a window the user positioned.

    The *position* is deliberately not touched. Only the size is the problem
    here, and moving a window without being asked to is a worse bug than the one
    this fixes.
    """
    # A maximized or full-screen window does not own its size: the window
    # manager keeps it equal to the available area (or to the whole screen), and
    # a resize here would be discarded or would un-maximize it. Those states are
    # already correct by construction, so they are left alone.
    if widget.isMaximized() or widget.isFullScreen():
        return False

    available = _available_geometry(widget)
    if available is None:
        return False

    # What the compositor draws *around* the client area: the title bar and
    # border. It is not part of the widget's height, so a window fitted to the
    # available height without it still overhangs by exactly this much. Before
    # the first paint the frame does not exist yet, and 0 is then correct.
    chrome = widget.frameGeometry().height() - widget.height()

    limit_width = available.width()
    limit_height = max(1, available.height() - chrome)

    new_width = min(widget.width(), limit_width)
    new_height = min(widget.height(), limit_height)

    if (new_width, new_height) == (widget.width(), widget.height()):
        return False

    widget.resize(new_width, new_height)
    return True


def fit_size_to_available_geometry(widget: QWidget, width: int, height: int) -> None:
    """Choose an initial size that cannot exceed what the screen offers.

    The same ceiling as :func:`fit_to_available_geometry`, applied to a preferred
    size before the window is shown. Used at startup when there is no saved
    geometry to honour.
    """
    available = _available_geometry(widget)
    if available is None:
        widget.resize(width, height)
        return
    widget.resize(
        max(1, min(width, available.width())),
        max(1, min(height, available.height())),
    )
