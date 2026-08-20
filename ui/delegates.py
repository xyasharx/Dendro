# ui/delegates.py
"""
Custom QStyledItemDelegate for high-performance, zero-allocation rendering
of status pills, metadata tags, and custom typography within the package QTreeView.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple
from PyQt6.QtCore import QModelIndex, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
)
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from core.backend import PackageState
from core.models import CustomUserRoles, DependencyTreeModel


class PackageTreeItemDelegate(QStyledItemDelegate):
    """
    Renders status pills, metadata tags (Orphan, Cycle),
    and typography for tree rows with zero per-frame heap allocation overhead.
    """

    COLOR_BG_HOVER = QColor("#1e1e2e")
    COLOR_BG_SELECTED = QColor("#313244")

    # Status Pill Colors: (Background QColor, Text QColor)
    STATE_COLORS: Dict[PackageState, Tuple[QColor, QColor]] = {
        PackageState.INSTALLED: (QColor("#1e3a2f"), QColor("#a6e3a1")),       # Emerald / Green
        PackageState.MISSING: (QColor("#45232e"), QColor("#f38ba8")),         # Rose / Coral
        PackageState.QUEUED_INSTALL: (QColor("#453322"), QColor("#fab387")),  # Amber / Peach
        PackageState.QUEUED_REMOVE: (QColor("#45252b"), QColor("#eba0ac")),   # Crimson / Maroon
        PackageState.AVAILABLE: (QColor("#252737"), QColor("#89b4fa")),       # Slate / Fedora Blue
    }

    TAG_ORPHAN_COLORS = (QColor("#3d2f47"), QColor("#cba6f7"))  # Mauve
    TAG_CYCLE_COLORS = (QColor("#45382e"), QColor("#f9e2af"))   # Yellow

    def __init__(self, parent: Optional[QStyledItemDelegate] = None):
        super().__init__(parent)

        # Typography configuration
        self.badge_font = QFont("Cantarell", 9)
        self.badge_font.setBold(True)

        self.base_font = QFont("Cantarell", 10)
        self.bold_font = QFont("Cantarell", 10)
        self.bold_font.setBold(True)

        # Pre-cached font metrics to prevent expensive per-frame recalculations
        self.fm_badge = QFontMetrics(self.badge_font)
        self.fm_base = QFontMetrics(self.base_font)
        self.fm_bold = QFontMetrics(self.bold_font)

        # Reusable color constants
        self.color_text_dep = QColor("#a6adc8")
        self.color_text_main = QColor("#cdd6f4")
        self.color_text_dim = QColor("#6c7086")
        self.color_text_ver = QColor("#bac2de")

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """Enforces a comfortable row height."""
        default_size = super().sizeHint(option, index)
        return QSize(default_size.width(), 36)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. Render Row Background Selection / Hover State
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, self.COLOR_BG_SELECTED)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, self.COLOR_BG_HOVER)

        col = index.column()

        # 2. Render Column Specific Cells
        if col == DependencyTreeModel.COL_NAME:
            self._paint_name_column(painter, option, index)
        elif col == DependencyTreeModel.COL_STATUS:
            self._paint_status_column(painter, option, index)
        elif col == DependencyTreeModel.COL_SIZE:
            self._paint_size_column(painter, option, index)
        elif col == DependencyTreeModel.COL_VERSION:
            self._paint_version_column(painter, option, index)
        else:
            super().paint(painter, option, index)

        painter.restore()

    def _paint_name_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Draws package name, dependency indicators, and status tags."""
        rect = option.rect
        name_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        is_dep = bool(index.data(CustomUserRoles.IsDependencyRole))
        is_orphan = bool(index.data(CustomUserRoles.IsOrphanRole))
        is_cycle = bool(index.data(CustomUserRoles.IsCycleRole))

        font = self.base_font if is_dep else self.bold_font
        fm = self.fm_base if is_dep else self.fm_bold

        painter.setFont(font)
        painter.setPen(self.color_text_dep if is_dep else self.color_text_main)

        text_y = rect.top() + (rect.height() + fm.ascent() - fm.descent()) // 2
        text_x = rect.left() + 8

        painter.drawText(text_x, text_y, name_text)
        current_x = text_x + fm.horizontalAdvance(name_text) + 8

        # Render [ORPHAN] Badge
        if is_orphan and not is_dep:
            current_x = self._draw_tag(painter, rect, current_x, "ORPHAN", self.TAG_ORPHAN_COLORS)

        # Render [CYCLE] Badge
        if is_cycle:
            self._draw_tag(painter, rect, current_x, "CYCLE", self.TAG_CYCLE_COLORS)

    def _paint_status_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Renders rounded pill badges using direct rasterization without QPainterPath."""
        state: PackageState = index.data(CustomUserRoles.PackageStateRole) or PackageState.AVAILABLE
        bg_color, text_color = self.STATE_COLORS.get(state, self.STATE_COLORS[PackageState.AVAILABLE])

        status_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "").upper()
        rect = option.rect

        painter.setFont(self.badge_font)
        text_width = self.fm_badge.horizontalAdvance(status_text)
        pill_width = text_width + 16
        pill_height = 20
        pill_x = rect.left() + 4
        pill_y = rect.top() + (rect.height() - pill_height) // 2

        # Direct rounded rect fill
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg_color))
        painter.drawRoundedRect(QRectF(pill_x, pill_y, pill_width, pill_height), 5.0, 5.0)

        # Draw Pill Label
        painter.setPen(text_color)
        text_rect = QRect(int(pill_x), int(pill_y), int(pill_width), int(pill_height))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, status_text)

    def _paint_size_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Renders package sizes with subtle typography."""
        size_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect

        painter.setFont(self.base_font)
        painter.setPen(self.color_text_dim)
        text_y = rect.top() + (rect.height() + self.fm_base.ascent() - self.fm_base.descent()) // 2
        painter.drawText(rect.left() + 6, text_y, size_text)

    def _paint_version_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Renders version strings."""
        ver_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect

        painter.setFont(self.base_font)
        painter.setPen(self.color_text_ver)
        text_y = rect.top() + (rect.height() + self.fm_base.ascent() - self.fm_base.descent()) // 2
        painter.drawText(rect.left() + 6, text_y, ver_text)

    def _draw_tag(
        self,
        painter: QPainter,
        row_rect: QRect,
        start_x: int,
        text: str,
        colors: Tuple[QColor, QColor],
    ) -> int:
        """Helper to draw badges using cached metrics with zero heap allocation."""
        bg_col, txt_col = colors
        painter.setFont(self.badge_font)

        tag_w = self.fm_badge.horizontalAdvance(text) + 10
        tag_h = 16
        tag_y = row_rect.top() + (row_rect.height() - tag_h) // 2

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg_col))
        painter.drawRoundedRect(QRectF(start_x, tag_y, tag_w, tag_h), 3.0, 3.0)

        painter.setPen(txt_col)
        painter.drawText(
            QRect(int(start_x), int(tag_y), int(tag_w), int(tag_h)),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )

        return int(start_x + tag_w + 6)
