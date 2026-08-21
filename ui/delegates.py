# dendro/ui/delegates.py
from __future__ import annotations

from typing import Dict, Optional, Tuple
from PyQt6.QtCore import QModelIndex, QObject, QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QProxyStyle, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from core.backend import PackageState
from core.models import CustomUserRoles, DependencyTreeModel


class ModernTreeStyle(QProxyStyle):
    """
    استایل بومی کیوت برای رندر فلش‌های وکتور شاخه‌های درخت (▶ / ▼)
    با مدیریت چرخه حیات والد برای جلوگیری از هرگونه خطای حافظه
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__()
        if parent:
            self.setParent(parent)

    def drawPrimitive(self, element: QStyle.PrimitiveElement, option, painter: QPainter, widget=None):
        if element == QStyle.PrimitiveElement.PE_IndicatorBranch:
            if option.state & QStyle.StateFlag.State_Children:
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)

                is_open = bool(option.state & QStyle.StateFlag.State_Open)
                is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

                arrow_color = QColor("#89b4fa") if (is_open or is_hover) else QColor("#a6adc8")
                
                pen = QPen(arrow_color)
                pen.setWidthF(2.2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)

                rect = option.rect
                cx = rect.center().x()
                cy = rect.center().y()

                path = QPainterPath()
                if is_open:
                    path.moveTo(QPointF(cx - 4.5, cy - 2.5))
                    path.lineTo(QPointF(cx, cy + 2.5))
                    path.lineTo(QPointF(cx + 4.5, cy - 2.5))
                else:
                    path.moveTo(QPointF(cx - 2.5, cy - 4.5))
                    path.lineTo(QPointF(cx + 2.5, cy))
                    path.lineTo(QPointF(cx - 2.5, cy + 4.5))

                painter.drawPath(path)
                painter.restore()
                return

            return

        super().drawPrimitive(element, option, painter, widget)


class PackageTreeItemDelegate(QStyledItemDelegate):
    COLOR_BG_HOVER = QColor("#1e1e2e")
    COLOR_BG_SELECTED = QColor("#313244")

    STATE_COLORS: Dict[PackageState, Tuple[QColor, QColor]] = {
        PackageState.INSTALLED: (QColor("#1e3a2f"), QColor("#a6e3a1")),
        PackageState.MISSING: (QColor("#45232e"), QColor("#f38ba8")),
        PackageState.QUEUED_INSTALL: (QColor("#453322"), QColor("#fab387")),
        PackageState.QUEUED_REMOVE: (QColor("#45252b"), QColor("#eba0ac")),
        PackageState.AVAILABLE: (QColor("#252737"), QColor("#89b4fa")),
    }

    TAG_ORPHAN_COLORS = (QColor("#3d2f47"), QColor("#cba6f7"))
    TAG_CYCLE_COLORS = (QColor("#45382e"), QColor("#f9e2af"))
    TAG_REVERSE_COLORS = (QColor("#2b334d"), QColor("#89b4fa"))

    def __init__(self, parent: Optional[QStyledItemDelegate] = None):
        super().__init__(parent)
        font_stack = ["Cantarell", "Inter", "Segoe UI", "Noto Color Emoji", "sans-serif"]

        self.badge_font = QFont()
        self.badge_font.setFamilies(font_stack)
        self.badge_font.setPointSize(9)
        self.badge_font.setBold(True)

        self.base_font = QFont()
        self.base_font.setFamilies(font_stack)
        self.base_font.setPointSize(10)

        self.bold_font = QFont()
        self.bold_font.setFamilies(font_stack)
        self.bold_font.setPointSize(10)
        self.bold_font.setBold(True)

        self.fm_badge = QFontMetrics(self.badge_font)
        self.fm_base = QFontMetrics(self.base_font)
        self.fm_bold = QFontMetrics(self.bold_font)

        self.color_text_dep = QColor("#a6adc8")
        self.color_text_main = QColor("#cdd6f4")
        self.color_text_dim = QColor("#6c7086")
        self.color_text_ver = QColor("#bac2de")

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        default_size = super().sizeHint(option, index)
        return QSize(default_size.width(), 36)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, self.COLOR_BG_SELECTED)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, self.COLOR_BG_HOVER)

        col = index.column()

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
        rect = option.rect
        name_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        is_dep = bool(index.data(CustomUserRoles.IsDependencyRole))
        is_rev_dep = bool(index.data(CustomUserRoles.IsReverseDepRole))
        is_orphan = bool(index.data(CustomUserRoles.IsOrphanRole))
        is_cycle = bool(index.data(CustomUserRoles.IsCycleRole))

        font = self.base_font if is_dep else self.bold_font
        fm = self.fm_base if is_dep else self.fm_bold

        painter.setFont(font)
        painter.setPen(self.color_text_dep if is_dep else self.color_text_main)

        text_y = rect.top() + (rect.height() + fm.ascent() - fm.descent()) // 2
        text_x = rect.left() + 6

        painter.drawText(text_x, text_y, name_text)
        current_x = text_x + fm.horizontalAdvance(name_text) + 8

        if is_orphan and not is_dep:
            current_x = self._draw_tag(painter, rect, current_x, "ORPHAN", self.TAG_ORPHAN_COLORS)

        if is_rev_dep:
            current_x = self._draw_tag(painter, rect, current_x, "REQUIRED BY", self.TAG_REVERSE_COLORS)

        if is_cycle:
            self._draw_tag(painter, rect, current_x, "CYCLE", self.TAG_CYCLE_COLORS)

    def _paint_status_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        is_rev_dep = bool(index.data(CustomUserRoles.IsReverseDepRole))
        if is_rev_dep:
            bg_color, text_color = self.TAG_REVERSE_COLORS
        else:
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

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg_color))
        painter.drawRoundedRect(QRectF(pill_x, pill_y, pill_width, pill_height), 5.0, 5.0)

        painter.setPen(text_color)
        text_rect = QRect(int(pill_x), int(pill_y), int(pill_width), int(pill_height))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, status_text)

    def _paint_size_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        size_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect

        painter.setFont(self.base_font)
        painter.setPen(self.color_text_dim)
        text_y = rect.top() + (rect.height() + self.fm_base.ascent() - self.fm_base.descent()) // 2
        painter.drawText(rect.left() + 6, text_y, size_text)

    def _paint_version_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
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
