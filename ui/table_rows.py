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
        self._default_min_height = view.minimumHeight()
        self._default_max_height = view.maximumHeight()
        self._capped = False
        self._floor_applied = False
        self._parent = view.parentWidget()
        if self._parent is not None:
            self._parent.installEventFilter(self)

    def _release_height_cap(self) -> None:
        if self._capped:
            self._capped = False
            self._view.setMaximumHeight(self._default_max_height)

    def _release_height_floor(self) -> None:
        if self._floor_applied:
            self._floor_applied = False
            self._floor = self._default_min_height
            self._view.setMinimumHeight(self._default_min_height)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        event_type = event.type()
        if watched is self._parent and event_type == QEvent.Type.Resize:
            self._release_height_cap()
            return False
        if watched is self._view:
            if event_type == QEvent.Type.ParentAboutToChange:
                if self._parent is not None:
                    self._parent.removeEventFilter(self)
                    self._parent = None
            elif event_type == QEvent.Type.ParentChange:
                new_parent = self._view.parentWidget()
                if new_parent is not self._parent:
                    if self._parent is not None:
                        self._parent.removeEventFilter(self)
                    self._parent = new_parent
                    if self._parent is not None:
                        self._parent.installEventFilter(self)
            elif event_type in (QEvent.Type.Resize, QEvent.Type.Show):
                self.fit()
        return False

    def fit(self) -> None:
        """Shrink the view to a whole number of rows without locking min height."""
        view = self._view
        if self._fitting:
            return
        # An empty table has no partial row to fix, and shrinking it would only
        # open a gap below its frame.
        if view.model() is None or view.model().rowCount() == 0:
            self._release_height_cap()
            self._release_height_floor()
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

        # Shrinking the view can hide the horizontal scrollbar, which is part of
        # the chrome, so the height that removes the remainder can itself change
        # the arithmetic. Iterate to a fixed point instead of assuming that the
        # chrome stays put: each pass measures it live and the loop stops as soon
        # as the view already sits on a row boundary.
        self._fitting = True
        try:
            for _ in range(4):
                chrome = view.height() - view.viewport().height()
                viewport_height = view.height() - chrome
                in_layout = (
                    view.parentWidget() is not None
                    and view.isVisible()
                    and view.height() > 0
                )
                if in_layout:
                    floor = max(self._default_min_height, chrome + row_height)
                    if view.maximumHeight() < floor:
                        view.setMaximumHeight(self._default_max_height)
                        self._capped = False
                    if view.minimumHeight() != floor:
                        view.setMinimumHeight(floor)
                        self._floor = floor
                        self._floor_applied = True
                if viewport_height % row_height == 0:
                    return
                target = viewport_height // row_height * row_height + chrome
                if target <= chrome:
                    if not in_layout:
                        # Not even one row fits; leave a standalone view alone
                        # rather than collapsing it to nothing.
                        return
                    target = chrome + row_height
                if target == view.height():
                    return
                if view.parentWidget() is not None:
                    view.setMaximumHeight(target)
                    self._capped = True
                else:
                    view.resize(view.width(), target)
        finally:
            self._fitting = False
