"""Keep a table's visible area a whole number of rows.

A table's height comes from its layout -- a splitter, a dialog, a resize -- while
its rows are a fixed height. Those two rarely agree, so the view paints the next
row's top sliver at its bottom edge and clips whatever the row draws there. In
the repository list that clipped the bottom of a status checkbox; in a manifest
of text it clips the glyphs and reads as a strikethrough.

Pixel-scrolled views cannot avoid this by themselves: ``QAbstractItemView``
sizes its viewport to whatever rectangle it is given, and
``setViewportMargins`` is overwritten by ``QTableView::updateGeometries`` on the
next pass, so the partial row is not reachable that way.

Shrinking the view to the largest whole multiple of the row height leaves the
remainder to the layout that owns it. The guard makes the pass idempotent: the
height is only written when it differs from the snapped value, and a snapped
view reports a zero remainder, so the next pass does nothing.
"""

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QAbstractItemView


class WholeRowFitter(QObject):
    """Resize ``view`` so its viewport holds only complete rows.

    Install on the view with ``view.installEventFilter(fitter)`` after the rows
    exist. The fitter re-runs on every resize and exposes :meth:`fit` for the
    caller to run once after populating, since the row height is only known once
    rows exist.
    """

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self._view = view
        self._fitting = False
        self._floor = 0

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self._view and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
        ):
            self.fit()
        return False

    def fit(self) -> None:
        """Shrink the view to a whole number of rows, never growing it."""
        view = self._view
        if self._fitting:
            return
        # An empty table has no partial row to fix, and shrinking it would only
        # open a gap below its frame.
        if view.model() is None or view.model().rowCount() == 0:
            return
        # ``sizeHintForRow`` is the delegate's preferred content height, not the
        # height the row is actually laid out at -- for these tables it reports
        # 27 against a real 30. Dividing by it leaves a remainder that no amount
        # of resizing removes, so take the laid-out height first.
        row_height = view.rowHeight(0)
        if row_height <= 0:
            row_height = view.verticalHeader().defaultSectionSize()
        if row_height <= 0:
            return

        # The viewport is the view minus its frame and its column header; the
        # header must be measured live because it is sized by the stylesheet.
        chrome = view.height() - view.viewport().height()
        whole = (view.height() - chrome) // row_height * row_height
        target = whole + chrome
        if target <= chrome:
            # Not even one row fits; leave the view alone rather than collapsing
            # it to nothing.
            return
        if view.height() == target:
            return
        # A remainder of one or two pixels is not a clipped row worth trading a
        # gap for; it is invisible at the bottom edge. Only reclaim a remainder
        # large enough to have been drawing something.
        if view.height() - target < row_height // 3:
            return
        self._fitting = True
        try:
            view.setFixedHeight(target)
        finally:
            self._fitting = False
