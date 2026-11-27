# dendro/ui/delegates.py
"""
Custom delegates and tree view classes for Dendro's main package view.
Features self-contained row background painting, micro-bordered status badges,
split-color version upgrade paths (current -> target), and native vector chevrons.
Zero font emoji glyphs to prevent Fontconfig shaping failures.
"""

from __future__ import annotations

from PyQt6.QtCore import (
    QEvent,
    QItemSelection,
    QModelIndex,
    QPersistentModelIndex,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import (
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QWidget,
)

from dendro.core.backend import PackageState
from dendro.core.i18n import tr
from dendro.core.models import CustomUserRoles, DependencyTreeModel
from dendro.ui.fonts import (
    DETAIL_PT_DELTA,
    TITLE_PT_DELTA,
    scaled_fixed_font,
    scaled_font,
)
from dendro.ui.styles import get_delegate_palette

# Dependency rows have no installed size. The tree shows this placeholder so
# the empty cell is unmistakably "no value" rather than a size that failed to
# load.
NO_VALUE_PLACEHOLDER = "\u2014"


# =============================================================================
# Custom TreeView with Native Vector Chevron Branches
# =============================================================================


class DendroTreeView(QTreeView):
    """
    Subclassed QTreeView that renders custom anti-aliased vector chevrons
    directly in drawBranches() and a calm, centered empty-state explanation
    when the active category or search filter has zero visible rows.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAllColumnsShowFocus(True)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAccessibleName(tr("Packages"))
        self.chevron_color = QColor("#89b4fa")
        self._empty_title_color = QColor("#a6adc8")
        self._empty_detail_color = QColor("#888ea8")
        self._empty_title = ""
        self._empty_detail = ""
        self._hovered_index = QPersistentModelIndex()

    def retranslate(self) -> None:
        """Update accessible labels and repaint view/headers in the active locale."""
        self.setAccessibleName(tr("Packages"))
        self.viewport().update()
        self.header().viewport().update()

    def changeEvent(self, event) -> None:
        """Rebuild the row delegate when the application font changes.

        ``PackageTreeItemDelegate`` caches fonts and metrics for the paint path
        and derives its row height from them, so a font change has to reach the
        delegate. Qt delivers ``FontChange`` here without the metrics being
        recomputed, which is what makes this hook necessary.
        """
        super().changeEvent(event)
        if event.type() != QEvent.Type.FontChange:
            return
        delegate = self.itemDelegate()
        refresh = getattr(delegate, "refresh_fonts", None)
        if callable(refresh):
            refresh()
            # Row height comes from the delegate's sizeHint, so the view has to
            # be told the cached sizes are stale (including uniformRowHeights).
            self.scheduleDelayedItemsLayout()
            self.updateGeometries()
            self.viewport().update()

    def _branch_gutter_rect(self, index: QModelIndex) -> QRect:
        if not index.isValid() or not self.rootIsDecorated():
            return QRect()
        tree_col = self.treePosition()
        tree_idx = index.siblingAtColumn(tree_col)
        rect = self.visualRect(tree_idx)
        if not rect.isValid():
            return QRect()
        col_left = self.header().sectionViewportPosition(tree_col)
        width = rect.left() - col_left
        if width <= 0:
            return QRect()
        return QRect(col_left, rect.top(), width, rect.height())

    def _invalidate_branch_gutter(self, index: QModelIndex) -> None:
        gutter = self._branch_gutter_rect(index)
        if not gutter.isEmpty():
            self.viewport().update(gutter)

    def _set_hovered_index(self, index: QModelIndex) -> None:
        old = (
            QModelIndex(self._hovered_index)
            if self._hovered_index.isValid()
            else QModelIndex()
        )
        if old == index:
            return
        self._invalidate_branch_gutter(old)
        self._hovered_index = (
            QPersistentModelIndex(index) if index.isValid() else QPersistentModelIndex()
        )
        self._invalidate_branch_gutter(index)

    def _invalidate_selected_and_current_branches(self) -> None:
        curr = self.currentIndex()
        if curr.isValid():
            self._invalidate_branch_gutter(curr)
        sel = self.selectionModel()
        if sel is not None:
            for idx in sel.selectedRows(self.treePosition()):
                self._invalidate_branch_gutter(idx)

    def viewportEvent(self, event: QEvent) -> bool:
        etype = event.type()
        if etype in (
            QEvent.Type.HoverEnter,
            QEvent.Type.HoverMove,
            QEvent.Type.MouseMove,
        ):
            pos = (
                event.position().toPoint()
                if hasattr(event, "position")
                else event.pos()
            )
            self._set_hovered_index(self.indexAt(pos).siblingAtColumn(0))
        elif etype in (QEvent.Type.HoverLeave, QEvent.Type.Leave):
            self._set_hovered_index(QModelIndex())
        return super().viewportEvent(event)

    def selectionChanged(
        self, selected: QItemSelection, deselected: QItemSelection
    ) -> None:
        super().selectionChanged(selected, deselected)
        for idx in selected.indexes():
            if idx.column() == 0:
                self._invalidate_branch_gutter(idx)
        for idx in deselected.indexes():
            if idx.column() == 0:
                self._invalidate_branch_gutter(idx)

    def currentChanged(self, current: QModelIndex, previous: QModelIndex) -> None:
        super().currentChanged(current, previous)
        self._invalidate_branch_gutter(previous)
        self._invalidate_branch_gutter(current)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self._invalidate_selected_and_current_branches()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self._invalidate_selected_and_current_branches()

    def set_chevron_color(self, color: QColor):
        self.chevron_color = color
        self.viewport().update()

    def set_empty_state_colors(self, title_color: QColor, detail_color: QColor) -> None:
        """Update empty-state typography colors to match the active theme."""
        self._empty_title_color = title_color
        self._empty_detail_color = detail_color
        self.viewport().update()

    def set_empty_state(self, title: str, detail: str = "") -> None:
        """Set the placeholder message painted when the model exposes zero rows."""
        normalized_title = title.strip()
        normalized_detail = detail.strip()
        if (
            normalized_title == self._empty_title
            and normalized_detail == self._empty_detail
        ):
            return
        self._empty_title = normalized_title
        self._empty_detail = normalized_detail
        self.viewport().update()

    def clear_empty_state(self) -> None:
        self.set_empty_state("", "")

    def empty_state(self) -> tuple[str, str]:
        """Return ``(title, detail)`` currently configured for empty views."""
        return self._empty_title, self._empty_detail

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._empty_title:
            return
        model = self.model()
        if model is None or model.rowCount() > 0:
            return

        vp_rect = self.viewport().rect()
        max_width = min(460, vp_rect.width() - 48)
        if max_width < 120 or vp_rect.height() < 72:
            return

        painter = QPainter(self.viewport())
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        title_font = scaled_font(TITLE_PT_DELTA - 1, bold=True)
        detail_font = scaled_font()

        flags = int(Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap)
        bounds = QRect(0, 0, max_width, vp_rect.height())

        title_rect = QFontMetrics(title_font).boundingRect(
            bounds, flags, self._empty_title
        )
        detail_rect = (
            QFontMetrics(detail_font).boundingRect(bounds, flags, self._empty_detail)
            if self._empty_detail
            else QRect()
        )
        spacing = 8 if self._empty_detail else 0
        total_height = title_rect.height() + spacing + detail_rect.height()

        left = vp_rect.left() + (vp_rect.width() - max_width) // 2
        top = max(
            vp_rect.top() + 16,
            vp_rect.top() + (vp_rect.height() - total_height) // 2,
        )

        painter.setFont(title_font)
        painter.setPen(self._empty_title_color)
        painter.drawText(
            QRect(left, top, max_width, title_rect.height() + 4),
            flags,
            self._empty_title,
        )

        if self._empty_detail:
            painter.setFont(detail_font)
            painter.setPen(self._empty_detail_color)
            painter.drawText(
                QRect(
                    left,
                    top + title_rect.height() + spacing,
                    max_width,
                    detail_rect.height() + 4,
                ),
                flags,
                self._empty_detail,
            )

        painter.restore()

    def drawBranches(self, painter: QPainter, rect: QRect, index: QModelIndex):
        if not rect.isValid() or rect.width() <= 0:
            return

        model = self.model()
        delegate = self.itemDelegate()
        if isinstance(delegate, PackageTreeItemDelegate):
            sel_model = self.selectionModel()
            is_selected = bool(
                sel_model is not None and sel_model.isSelected(index.siblingAtColumn(0))
            )
            is_focused = self.hasFocus() and self.currentIndex().siblingAtColumn(
                0
            ) == index.siblingAtColumn(0)
            is_hover = (
                self._hovered_index.isValid()
                and self._hovered_index == index.siblingAtColumn(0)
            )
            is_active_selection = (
                is_selected and self.isActiveWindow() and self.hasFocus()
            )
            seg_pos = (
                QStyleOptionViewItem.ViewItemPosition.Beginning
                if self.header().visualIndex(self.treePosition()) == 0
                else QStyleOptionViewItem.ViewItemPosition.Middle
            )

            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.fillRect(rect, delegate.color_bg_base)
            if is_active_selection:
                delegate._paint_row_segment(
                    painter,
                    rect,
                    seg_pos,
                    fill=delegate.color_bg_selected,
                )
            elif is_selected:
                delegate._paint_row_segment(
                    painter,
                    rect,
                    seg_pos,
                    fill=delegate.color_bg_hover,
                    stroke=delegate.color_border_strong,
                    stroke_width=1.0,
                )
            elif is_hover:
                delegate._paint_row_segment(
                    painter,
                    rect,
                    seg_pos,
                    fill=delegate.color_bg_hover,
                )
            if is_focused:
                delegate._paint_row_focus_ring(
                    painter,
                    rect,
                    seg_pos,
                )
            painter.restore()

        if not model or not model.hasChildren(index):
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pen = QPen(self.chevron_color)
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)

        cx = rect.center().x()
        cy = rect.center().y()

        path = QPainterPath()
        if self.isExpanded(index):
            # Chevron pointing down (⌄)
            path.moveTo(QPointF(cx - 4.5, cy - 2.0))
            path.lineTo(QPointF(cx, cy + 2.5))
            path.lineTo(QPointF(cx + 4.5, cy - 2.0))
        else:
            # Chevron pointing right (>)
            path.moveTo(QPointF(cx - 2.5, cy - 4.5))
            path.lineTo(QPointF(cx + 2.0, cy))
            path.lineTo(QPointF(cx - 2.5, cy + 4.5))

        painter.drawPath(path)
        painter.restore()


# =============================================================================
# Package Tree Item Delegate
# =============================================================================


class PackageTreeItemDelegate(QStyledItemDelegate):
    """
    Custom delegate rendering tree items, self-contained row backgrounds,
    micro-bordered status badges, visual upgrade paths, and multi-role tags.
    """

    # Smallest row that still looks like a row with breathing room around the
    # badge and text at the default font.
    MINIMUM_ROW_HEIGHT = 34

    def refresh_fonts(self) -> None:
        """Rebuild every font and metric from the current application font.

        The delegate renders rows itself, so it caches fonts and their
        ``QFontMetrics`` for the hot paint path. Those have to be rebuilt when
        the user's desktop font changes, otherwise rows keep painting at the
        old size and the two sizes drift apart. Deriving the row height from
        the metrics at the same time keeps text from being clipped by a row
        pinned to a fixed pixel height.
        """
        self.base_font = scaled_font()
        self.bold_font = scaled_font(bold=True)
        self.badge_font = scaled_font(DETAIL_PT_DELTA, bold=True, letter_spacing=0.4)

        # Monospace system font scaled with the user's application font size
        self.version_font = scaled_fixed_font()
        self.version_bold_font = scaled_fixed_font(bold=True)

        self.fm_badge = QFontMetrics(self.badge_font)
        self.fm_base = QFontMetrics(self.base_font)
        self.fm_bold = QFontMetrics(self.bold_font)
        self.fm_ver = QFontMetrics(self.version_font)
        self.fm_ver_bold = QFontMetrics(self.version_bold_font)

        # Grow the row with the text rather than pinning it. The old fixed
        # 34px clipped any font large enough to need more than that.
        self.row_height = max(
            self.MINIMUM_ROW_HEIGHT,
            self.fm_base.height() + self.fm_badge.height() // 2,
            self.fm_badge.height() + 10,
        )

    def __init__(self, parent: QStyledItemDelegate | None = None):
        super().__init__(parent)

        self.refresh_fonts()

        # Dynamic palette variables
        self.color_bg_base = QColor("#181825")
        self.color_bg_hover = QColor("#313244")
        self.color_bg_selected = QColor("#3b3e54")
        self.color_text_main = QColor("#cdd6f4")
        self.color_text_selected = QColor("#cdd6f4")
        self.color_text_dep = QColor("#bac2de")
        self.color_text_dim = QColor("#a3aac4")
        self.color_text_ver = QColor("#89b4fa")
        self.color_accent = QColor("#89b4fa")
        self.color_border_strong = QColor("#8289a6")

        # Status: (Background, Foreground, Border)
        self.state_colors: dict[PackageState, tuple[QColor, QColor, QColor]] = {}
        self.tag_orphan_colors = (
            QColor("#3d2f47"),
            QColor("#cba6f7"),
            QColor("#573d69"),
        )
        self.tag_cycle_colors = (
            QColor("#453322"),
            QColor("#fab387"),
            QColor("#6e4b2d"),
        )
        self.tag_reverse_colors = (
            QColor("#1e3a2f"),
            QColor("#89b4fa"),
            QColor("#2d5a47"),
        )

        self.set_theme("auto")

    def set_theme(self, theme_choice: str):
        """Applies dynamic color palette according to the active theme."""
        pal = get_delegate_palette(theme_choice)

        self.color_bg_base = pal["bg_base"]
        self.color_bg_hover = pal["bg_hover"]
        self.color_bg_selected = pal["bg_selected"]
        self.color_text_main = pal["text_main"]
        self.color_text_selected = pal["text_selected"]
        self.color_text_dep = pal["text_secondary"]
        self.color_text_dim = pal["text_dim"]
        self.color_text_ver = pal["accent"]
        self.color_accent = pal["accent"]
        self.color_border_strong = pal["border_strong"]

        self.state_colors = {
            PackageState.INSTALLED: (
                pal["badge_bg_installed"],
                pal["badge_fg_installed"],
                pal["badge_border_installed"],
            ),
            PackageState.MISSING: (
                pal["badge_bg_missing"],
                pal["badge_fg_missing"],
                pal["badge_border_missing"],
            ),
            PackageState.QUEUED_INSTALL: (
                pal["badge_bg_queued_in"],
                pal["badge_fg_queued_in"],
                pal["badge_border_queued_in"],
            ),
            PackageState.QUEUED_REMOVE: (
                pal["badge_bg_queued_rm"],
                pal["badge_fg_queued_rm"],
                pal["badge_border_queued_rm"],
            ),
            PackageState.QUEUED_UPGRADE: (
                pal["badge_bg_queued_in"],
                pal["badge_fg_queued_in"],
                pal["badge_border_queued_in"],
            ),
            PackageState.AVAILABLE: (
                pal["badge_bg_tag"],
                pal["accent"],
                pal["badge_border_tag"],
            ),
        }

        self.tag_orphan_colors = (
            pal["badge_bg_tag"],
            pal["badge_fg_tag"],
            pal["badge_border_tag"],
        )
        self.tag_cycle_colors = (
            pal["badge_bg_queued_in"],
            pal["badge_fg_queued_in"],
            pal["badge_border_queued_in"],
        )
        self.tag_reverse_colors = (
            pal["badge_bg_installed"],
            pal["accent"],
            pal["badge_border_installed"],
        )
        self.update_badge_colors = (
            pal["badge_bg_update"],
            pal["badge_fg_update"],
            pal["badge_border_update"],
        )

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        default_size = super().sizeHint(option, index)
        return QSize(default_size.width(), self.row_height)

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        is_active = bool(option.state & QStyle.StateFlag.State_Active)
        widget_has_focus = (
            option.widget.hasFocus() if option.widget is not None else is_active
        )
        is_active_selection = is_selected and is_active and widget_has_focus

        # ---------------------------------------------------------------------
        # 1. Self-Contained Row Background Painting (Zero Host Theme Bleed)
        # ---------------------------------------------------------------------
        seg_pos = option.viewItemPosition
        if (
            isinstance(option.widget, DendroTreeView)
            and option.widget.rootIsDecorated()
            and index.column() == option.widget.treePosition()
            and option.widget.header().visualIndex(index.column()) == 0
        ):
            if seg_pos == QStyleOptionViewItem.ViewItemPosition.Beginning:
                seg_pos = QStyleOptionViewItem.ViewItemPosition.Middle
            elif seg_pos == QStyleOptionViewItem.ViewItemPosition.OnlyOne:
                seg_pos = QStyleOptionViewItem.ViewItemPosition.End

        painter.fillRect(rect, self.color_bg_base)
        if is_active_selection:
            self._paint_row_segment(
                painter,
                rect,
                seg_pos,
                fill=self.color_bg_selected,
            )
        elif is_selected:
            self._paint_row_segment(
                painter,
                rect,
                seg_pos,
                fill=self.color_bg_hover,
                stroke=self.color_border_strong,
                stroke_width=1.0,
            )
        elif is_hover:
            self._paint_row_segment(
                painter,
                rect,
                seg_pos,
                fill=self.color_bg_hover,
            )

        # Clip column contents to the cell rect so long names, versions, or badges
        # never bleed into neighbouring columns when columns are resized narrow.
        painter.setClipRect(rect)
        col = index.column()

        if col == DependencyTreeModel.COL_NAME:
            self._paint_name_column(painter, option, index)
        elif col == DependencyTreeModel.COL_STATUS:
            self._paint_status_column(painter, option, index)
        elif col == DependencyTreeModel.COL_VERSION:
            self._paint_version_column(painter, option, index)
        elif col == DependencyTreeModel.COL_SIZE:
            self._paint_size_column(painter, option, index)
        else:
            self._paint_summary_column(painter, option, index)

        if is_focused:
            self._paint_row_focus_ring(painter, rect, seg_pos)

        painter.restore()

    @staticmethod
    def _paint_row_segment(
        painter: QPainter,
        rect: QRect,
        pos: QStyleOptionViewItem.ViewItemPosition,
        *,
        fill: QColor,
        stroke: QColor | None = None,
        stroke_width: float = 1.0,
    ) -> None:
        """Paint a continuous multi-column row pill across tree columns."""
        top = float(rect.top() + 1)
        bottom = float(rect.top() + rect.height() - 1)
        left = float(rect.left() + 2)
        right = float(rect.left() + rect.width() - 2)
        edge_left = float(rect.left())
        edge_right = float(rect.left() + rect.width())
        radius = 4.0

        fill_path = QPainterPath()
        if pos == QStyleOptionViewItem.ViewItemPosition.Beginning:
            fill_path.moveTo(edge_right, top)
            fill_path.lineTo(left + radius, top)
            fill_path.quadTo(left, top, left, top + radius)
            fill_path.lineTo(left, bottom - radius)
            fill_path.quadTo(left, bottom, left + radius, bottom)
            fill_path.lineTo(edge_right, bottom)
            fill_path.closeSubpath()
        elif pos == QStyleOptionViewItem.ViewItemPosition.Middle:
            fill_path.addRect(
                QRectF(edge_left, top, max(0.0, edge_right - edge_left), bottom - top)
            )
        elif pos == QStyleOptionViewItem.ViewItemPosition.End:
            fill_path.moveTo(edge_left, top)
            fill_path.lineTo(right - radius, top)
            fill_path.quadTo(right, top, right, top + radius)
            fill_path.lineTo(right, bottom - radius)
            fill_path.quadTo(right, bottom, right - radius, bottom)
            fill_path.lineTo(edge_left, bottom)
            fill_path.closeSubpath()
        else:
            fill_path.addRoundedRect(
                QRectF(left, top, max(0.0, right - left), max(0.0, bottom - top)),
                radius,
                radius,
            )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(fill))
        painter.drawPath(fill_path)

        if stroke is None:
            return
        inset = stroke_width / 2.0
        s_top = top + inset
        s_bottom = bottom - inset
        s_left = left + inset
        s_right = right - inset
        stroke_pen = QPen(stroke, stroke_width)
        stroke_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        stroke_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(stroke_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if pos == QStyleOptionViewItem.ViewItemPosition.Beginning:
            edge = QPainterPath()
            edge.moveTo(edge_right, s_top)
            edge.lineTo(s_left + radius, s_top)
            edge.quadTo(s_left, s_top, s_left, s_top + radius)
            edge.lineTo(s_left, s_bottom - radius)
            edge.quadTo(s_left, s_bottom, s_left + radius, s_bottom)
            edge.lineTo(edge_right, s_bottom)
            painter.drawPath(edge)
        elif pos == QStyleOptionViewItem.ViewItemPosition.Middle:
            painter.drawLine(QPointF(edge_left, s_top), QPointF(edge_right, s_top))
            painter.drawLine(
                QPointF(edge_left, s_bottom), QPointF(edge_right, s_bottom)
            )
        elif pos == QStyleOptionViewItem.ViewItemPosition.End:
            edge = QPainterPath()
            edge.moveTo(edge_left, s_top)
            edge.lineTo(s_right - radius, s_top)
            edge.quadTo(s_right, s_top, s_right, s_top + radius)
            edge.lineTo(s_right, s_bottom - radius)
            edge.quadTo(s_right, s_bottom, s_right - radius, s_bottom)
            edge.lineTo(edge_left, s_bottom)
            painter.drawPath(edge)
        else:
            painter.drawRoundedRect(
                QRectF(
                    s_left,
                    s_top,
                    max(0.0, s_right - s_left),
                    max(0.0, s_bottom - s_top),
                ),
                radius,
                radius,
            )

    def _paint_row_focus_ring(
        self,
        painter: QPainter,
        rect: QRect,
        pos: QStyleOptionViewItem.ViewItemPosition,
    ) -> None:
        """Draw a crisp 2px keyboard-focus ring spanning visible tree columns."""
        top = float(rect.top()) + 1.5
        bottom = float(rect.top() + rect.height()) - 1.5
        left = float(rect.left()) + 1.5
        right = float(rect.left() + rect.width()) - 1.5
        edge_left = float(rect.left())
        edge_right = float(rect.left() + rect.width())
        radius = 4.0

        focus_pen = QPen(self.color_accent, 2.0)
        focus_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        focus_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(focus_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if pos == QStyleOptionViewItem.ViewItemPosition.Beginning:
            ring = QPainterPath()
            ring.moveTo(edge_right, top)
            ring.lineTo(left + radius, top)
            ring.quadTo(left, top, left, top + radius)
            ring.lineTo(left, bottom - radius)
            ring.quadTo(left, bottom, left + radius, bottom)
            ring.lineTo(edge_right, bottom)
            painter.drawPath(ring)
        elif pos == QStyleOptionViewItem.ViewItemPosition.Middle:
            painter.drawLine(QPointF(edge_left, top), QPointF(edge_right, top))
            painter.drawLine(QPointF(edge_left, bottom), QPointF(edge_right, bottom))
        elif pos == QStyleOptionViewItem.ViewItemPosition.End:
            ring = QPainterPath()
            ring.moveTo(edge_left, top)
            ring.lineTo(right - radius, top)
            ring.quadTo(right, top, right, top + radius)
            ring.lineTo(right, bottom - radius)
            ring.quadTo(right, bottom, right - radius, bottom)
            ring.lineTo(edge_left, bottom)
            painter.drawPath(ring)
        else:
            painter.drawRoundedRect(
                QRectF(left, top, max(0.0, right - left), max(0.0, bottom - top)),
                radius,
                radius,
            )

    def _paint_name_column(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ):
        rect = option.rect
        name_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        is_dep = bool(index.data(CustomUserRoles.IsDependencyRole))
        is_rev_dep = bool(index.data(CustomUserRoles.IsReverseDepRole))
        is_orphan = bool(index.data(CustomUserRoles.IsOrphanRole))
        is_cycle = bool(index.data(CustomUserRoles.IsCycleRole))

        tags: list[tuple[str, tuple[QColor, QColor, QColor]]] = []
        if is_orphan and not is_dep:
            tags.append((tr("ORPHAN"), self.tag_orphan_colors))
        if is_rev_dep:
            tags.append((tr("REQUIRED BY"), self.tag_reverse_colors))
        if is_cycle:
            tags.append((tr("CYCLE"), self.tag_cycle_colors))

        reserved_tag_w = sum(
            self.fm_badge.horizontalAdvance(label) + 18 for label, _colors in tags
        )
        font = self.base_font if is_dep else self.bold_font
        fm = self.fm_base if is_dep else self.fm_bold

        text_x = rect.left() + 6
        total_avail = max(0, rect.right() - 6 - text_x)
        if tags and total_avail - reserved_tag_w >= 48:
            name_avail = total_avail - reserved_tag_w - 4
            draw_tags = True
        else:
            name_avail = total_avail
            draw_tags = False

        elided_name = fm.elidedText(name_text, Qt.TextElideMode.ElideRight, name_avail)
        painter.setFont(font)
        highlighted = bool(
            option.state
            & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
        )
        if highlighted:
            painter.setPen(self.color_text_selected)
        else:
            painter.setPen(self.color_text_dep if is_dep else self.color_text_main)

        text_y = rect.top() + (rect.height() + fm.ascent() - fm.descent()) // 2
        painter.drawText(text_x, text_y, elided_name)

        if draw_tags:
            current_x = text_x + fm.horizontalAdvance(elided_name) + 8
            for label, colors in tags:
                if current_x >= rect.right() - 12:
                    break
                current_x = self._draw_tag(painter, rect, current_x, label, colors)

    def _paint_status_column(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ):
        is_rev_dep = bool(index.data(CustomUserRoles.IsReverseDepRole))
        has_update = bool(index.data(CustomUserRoles.HasUpdateRole))
        state: PackageState = (
            index.data(CustomUserRoles.PackageStateRole) or PackageState.AVAILABLE
        )
        if is_rev_dep:
            bg_color, text_color, border_color = self.tag_reverse_colors
        elif state == PackageState.INSTALLED and has_update:
            bg_color, text_color, border_color = self.update_badge_colors
        else:
            bg_color, text_color, border_color = self.state_colors.get(
                state, self.state_colors[PackageState.AVAILABLE]
            )

        status_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "").upper()
        rect = option.rect
        max_pill_w = max(0, rect.width() - 8)
        if max_pill_w < 16 or not status_text:
            return

        painter.setFont(self.badge_font)
        elided_status = self.fm_badge.elidedText(
            status_text, Qt.TextElideMode.ElideRight, max(0, max_pill_w - 14)
        )
        text_width = self.fm_badge.horizontalAdvance(elided_status)
        pill_width = min(max_pill_w, text_width + 14)
        pill_height = min(
            max(1, rect.height() - 2), max(20, self.fm_badge.height() + 6)
        )
        pill_x = rect.left() + 4
        pill_y = rect.top() + (rect.height() - pill_height) // 2

        # Draw micro-bordered rounded pill badge
        pill_rect = QRectF(pill_x, pill_y, pill_width, pill_height)
        painter.setPen(QPen(border_color, 1.0))
        painter.setBrush(QBrush(bg_color))
        painter.drawRoundedRect(pill_rect, 5.0, 5.0)

        # Draw centered status badge text
        painter.setPen(text_color)
        painter.drawText(
            QRect(int(pill_x), int(pill_y), int(pill_width), int(pill_height)),
            Qt.AlignmentFlag.AlignCenter,
            elided_status,
        )

    def _paint_version_column(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ):
        ver_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect
        text_y = (
            rect.top()
            + (rect.height() + self.fm_ver.ascent() - self.fm_ver.descent()) // 2
        )
        text_x = rect.left() + 6
        avail_w = max(0, rect.right() - 6 - text_x)
        if avail_w <= 0 or not ver_text:
            return

        highlighted = bool(
            option.state
            & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
        )

        # Check for upgrade path formatting: "current -> target"
        if " -> " in ver_text:
            parts = ver_text.split(" -> ", 1)
            current_ver = parts[0].strip()
            target_ver = parts[1].strip()
            arrow_str = "->"
            arrow_w = self.fm_ver.horizontalAdvance(arrow_str) + 10
            cur_w = self.fm_ver.horizontalAdvance(current_ver)
            tgt_w = self.fm_ver_bold.horizontalAdvance(target_ver)

            if cur_w + arrow_w + tgt_w <= avail_w:
                painter.setFont(self.version_font)
                painter.setPen(
                    self.color_text_selected if highlighted else self.color_text_dim
                )
                painter.drawText(text_x, text_y, current_ver)
                text_x += cur_w + 5

                painter.setPen(self.color_accent)
                painter.drawText(text_x, text_y, arrow_str)
                text_x += self.fm_ver.horizontalAdvance(arrow_str) + 5

                painter.setFont(self.version_bold_font)
                painter.drawText(text_x, text_y, target_ver)
            else:
                # Prioritize keeping the upgrade target legible in narrow columns.
                painter.setFont(self.version_bold_font)
                painter.setPen(self.color_accent)
                compact = self.fm_ver_bold.elidedText(
                    f"-> {target_ver}", Qt.TextElideMode.ElideRight, avail_w
                )
                painter.drawText(text_x, text_y, compact)
        else:
            painter.setFont(self.version_font)
            painter.setPen(
                self.color_text_selected if highlighted else self.color_text_ver
            )
            elided_ver = self.fm_ver.elidedText(
                ver_text, Qt.TextElideMode.ElideRight, avail_w
            )
            painter.drawText(text_x, text_y, elided_ver)

    def size_placeholder_text(self, index: QModelIndex) -> str:
        """Return the SIZE text for a row whose own size is intentionally absent.

        A dependency node has no installed size, so a blank cell reads like a
        failed lookup next to the filled-in rows above it. Any other empty size
        stays blank.
        """
        if index.data(Qt.ItemDataRole.DisplayRole):
            return ""
        if index.data(CustomUserRoles.IsDependencyRole):
            return NO_VALUE_PLACEHOLDER
        return ""

    def _paint_size_column(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ):
        size_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect
        text_x = rect.left() + 6
        avail_w = max(0, rect.right() - 6 - text_x)
        if avail_w <= 0:
            return
        highlighted = bool(
            option.state
            & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
        )
        size_color = self.color_text_selected if highlighted else self.color_text_dim
        if not size_text:
            placeholder = self.size_placeholder_text(index)
            if not placeholder:
                return
            painter.setFont(self.base_font)
            painter.setPen(size_color)
            text_y = (
                rect.top()
                + (rect.height() + self.fm_base.ascent() - self.fm_base.descent()) // 2
            )
            painter.drawText(text_x, text_y, placeholder)
            return

        painter.setFont(self.base_font)
        painter.setPen(size_color)
        text_y = (
            rect.top()
            + (rect.height() + self.fm_base.ascent() - self.fm_base.descent()) // 2
        )
        elided_size = self.fm_base.elidedText(
            size_text, Qt.TextElideMode.ElideRight, avail_w
        )
        painter.drawText(text_x, text_y, elided_size)

    def _paint_summary_column(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ):
        summary_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect
        text_x = rect.left() + 6
        avail_w = max(0, rect.right() - 8 - text_x)
        if avail_w <= 0 or not summary_text:
            return

        is_dep = bool(index.data(CustomUserRoles.IsDependencyRole))
        highlighted = bool(
            option.state
            & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
        )
        painter.setFont(self.base_font)
        if highlighted:
            painter.setPen(self.color_text_selected)
        else:
            painter.setPen(self.color_text_dim if is_dep else self.color_text_dep)
        text_y = (
            rect.top()
            + (rect.height() + self.fm_base.ascent() - self.fm_base.descent()) // 2
        )
        elided_summary = self.fm_base.elidedText(
            summary_text, Qt.TextElideMode.ElideRight, avail_w
        )
        painter.drawText(text_x, text_y, elided_summary)

    def _draw_tag(
        self,
        painter: QPainter,
        row_rect: QRect,
        start_x: int,
        text: str,
        colors: tuple[QColor, QColor, QColor],
    ) -> int:
        bg_col, txt_col, border_col = colors
        painter.setFont(self.badge_font)

        tag_w = self.fm_badge.horizontalAdvance(text) + 12
        tag_h = min(max(1, row_rect.height() - 2), max(16, self.fm_badge.height() + 4))
        tag_y = row_rect.top() + (row_rect.height() - tag_h) // 2

        tag_rect = QRectF(start_x, tag_y, tag_w, tag_h)
        painter.setPen(QPen(border_col, 1.0))
        painter.setBrush(QBrush(bg_col))
        painter.drawRoundedRect(tag_rect, 4.0, 4.0)

        painter.setPen(txt_col)
        painter.drawText(
            QRect(int(start_x), int(tag_y), int(tag_w), int(tag_h)),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )

        return int(start_x + tag_w + 6)
