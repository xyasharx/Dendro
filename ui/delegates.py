# ui/delegates.py
"""
Custom QStyledItemDelegate for rendering rich badges, status pills,
and tags directly within the QTreeView.
"""

from __future__ import annotations

from typing import Optional
from PyQt6.QtCore import QModelIndex, QPoint, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from core.backend import PackageState
from core.models import CustomUserRoles, DependencyTreeModel


class PackageTreeItemDelegate(QStyledItemDelegate):
    """
    Renders status pills, metadata tags (Orphan, Cycle),
    and typography for tree rows.
    """

    # Color Palette Tokens
    COLOR_BG_HOVER = QColor("#1e1e2e")
    COLOR_BG_SELECTED = QColor("#313244")
    
    # Status Pill Colors (Background, Text)
    STATE_COLORS = {
        PackageState.INSTALLED: (QColor("#1e3a2f"), QColor("#a6e3a1")),       # Dark emerald / Light green
        PackageState.MISSING: (QColor("#45232e"), QColor("#f38ba8")),         # Dark rose / Coral red
        PackageState.QUEUED_INSTALL: (QColor("#453322"), QColor("#fab387")),  # Dark amber / Peach
        PackageState.QUEUED_REMOVE: (QColor("#45252b"), QColor("#eba0ac")),   # Dark crimson / Maroon
        PackageState.AVAILABLE: (QColor("#252737"), QColor("#89b4fa")),       # Dark slate / Fedora blue
    }

    TAG_ORPHAN_COLORS = (QColor("#3d2f47"), QColor("#cba6f7"))  # Mauve
    TAG_CYCLE_COLORS = (QColor("#45382e"), QColor("#f9e2af"))   # Yellow

    def __init__(self, parent: Optional[QStyledItemDelegate] = None):
        super().__init__(parent)
        self.badge_font = QFont("Cantarell", 9)
        self.badge_font.setBold(True)
        self.base_font = QFont("Cantarell", 10)
        self.bold_font = QFont("Cantarell", 10)
        self.bold_font.setBold(True)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """Enforces comfortable row height with vertical padding."""
        default_size = super().sizeHint(option, index)
        return QSize(default_size.width(), 36)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. Render Row Background
        self._paint_background(painter, option, index)

        col = index.column()

        # 2. Render Column-Specific Content
        if col == DependencyTreeModel.COL_NAME:
            self._paint_name_column(painter, option, index)
        elif col == DependencyTreeModel.COL_STATUS:
            self._paint_status_column(painter, option, index)
        elif col == DependencyTreeModel.COL_SIZE:
            self._paint_size_column(painter, option, index)
        elif col == DependencyTreeModel.COL_VERSION:
            self._paint_version_column(painter, option, index)
        else:
            # Fallback to default rendering for summary / generic columns
            super().paint(painter, option, index)

        painter.restore()

    def _paint_background(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Draws clean selection and hover states."""
        rect = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, self.COLOR_BG_SELECTED)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(rect, self.COLOR_BG_HOVER)

    def _paint_name_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Draws package name, dependency indicators, and tags (Orphan, Cycle)."""
        rect = option.rect
        name_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        is_dep = bool(index.data(CustomUserRoles.IsDependencyRole))
        is_orphan = bool(index.data(CustomUserRoles.IsOrphanRole))
        is_cycle = bool(index.data(CustomUserRoles.IsCycleRole))

        painter.setFont(self.base_font if is_dep else self.bold_font)
        painter.setPen(QColor("#a6adc8") if is_dep else QColor("#cdd6f4"))

        # Compute text layout
        fm = QFontMetrics(painter.font())
        text_y = rect.top() + (rect.height() + fm.ascent() - fm.descent()) // 2
        text_x = rect.left() + 8

        painter.drawText(text_x, text_y, name_text)
        current_x = text_x + fm.horizontalAdvance(name_text) + 8

        # Draw [ORPHAN] tag if applicable
        if is_orphan and not is_dep:
            current_x = self._draw_tag(painter, rect, current_x, "ORPHAN", self.TAG_ORPHAN_COLORS)

        # Draw [CYCLE] tag if recursive circular dependency detected
        if is_cycle:
            self._draw_tag(painter, rect, current_x, "CYCLE", self.TAG_CYCLE_COLORS)

    def _paint_status_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Renders rounded pill badges for package states."""
        state: PackageState = index.data(CustomUserRoles.PackageStateRole) or PackageState.AVAILABLE
        bg_color, text_color = self.STATE_COLORS.get(state, self.STATE_COLORS[PackageState.AVAILABLE])

        status_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "").upper()
        rect = option.rect

        painter.setFont(self.badge_font)
        fm = QFontMetrics(self.badge_font)
        text_width = fm.horizontalAdvance(status_text)
        pill_width = text_width + 16
        pill_height = 20
        pill_x = rect.left() + 4
        pill_y = rect.top() + (rect.height() - pill_height) // 2

        pill_rect = QRectF(pill_x, pill_y, pill_width, pill_height)

        # Paint Pill Background
        path = QPainterPath()
        path.addRoundedRect(pill_rect, 5.0, 5.0)
        painter.fillPath(path, QBrush(bg_color))

        # Paint Pill Text
        painter.setPen(text_color)
        text_rect = QRect(int(pill_x), int(pill_y), int(pill_width), int(pill_height))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, status_text)

    def _paint_size_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Renders package sizes with subtle dimmed typography."""
        size_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect

        painter.setFont(self.base_font)
        painter.setPen(QColor("#6c7086"))  # Dimmed text
        fm = QFontMetrics(self.base_font)
        text_y = rect.top() + (rect.height() + fm.ascent() - fm.descent()) // 2
        painter.drawText(rect.left() + 6, text_y, size_text)

    def _paint_version_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Renders versions in a clean monospaced-style font."""
        ver_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect

        painter.setFont(self.base_font)
        painter.setPen(QColor("#bac2de"))
        fm = QFontMetrics(self.base_font)
        text_y = rect.top() + (rect.height() + fm.ascent() - fm.descent()) // 2
        painter.drawText(rect.left() + 6, text_y, ver_text)

    def _draw_tag(self, painter: QPainter, row_rect: QRect, start_x: int, text: str, colors: tuple[QColor, QColor]) -> int:
        """Helper to draw subtle informational tags."""
        bg_col, txt_col = colors
        painter.setFont(self.badge_font)
        fm = QFontMetrics(self.badge_font)

        tag_w = fm.horizontalAdvance(text) + 10
        tag_h = 16
        tag_y = row_rect.top() + (row_rect.height() - tag_h) // 2

        tag_rect = QRectF(start_x, tag_y, tag_w, tag_h)
        path = QPainterPath()
        path.addRoundedRect(tag_rect, 3.0, 3.0)
        painter.fillPath(path, QBrush(bg_col))

        painter.setPen(txt_col)
        painter.drawText(QRect(int(start_x), int(tag_y), int(tag_w), int(tag_h)), Qt.AlignmentFlag.AlignCenter, text)

        return int(start_x + tag_w + 6)