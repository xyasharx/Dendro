# dendro/ui/delegates.py
"""
Custom delegates and tree view classes for Dendro's main package view.
Features self-contained row background painting, micro-bordered status badges,
split-color version upgrade paths (current -> target), and native vector chevrons.
Zero font emoji glyphs to prevent Fontconfig shaping failures.
"""
from __future__ import annotations

from typing import Dict, Final, Optional, Tuple
from PyQt6.QtCore import QModelIndex, QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import (
    QHeaderView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QWidget,
)

from core.backend import PackageState
from core.models import CustomUserRoles, DependencyTreeModel
from ui.styles import get_delegate_palette


# =============================================================================
# Custom TreeView with Native Vector Chevron Branches
# =============================================================================

class DendroTreeView(QTreeView):
    """
    Subclassed QTreeView that renders custom anti-aliased vector chevrons
    directly in drawBranches() without breaking QSS stylesheet cascading.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.chevron_color = QColor("#89b4fa")

    def set_chevron_color(self, color: QColor):
        self.chevron_color = color
        self.viewport().update()

    def drawBranches(self, painter: QPainter, rect: QRect, index: QModelIndex):
        model = self.model()
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
            # Chevron pointing right (›)
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

    def __init__(self, parent: Optional[QStyledItemDelegate] = None):
        super().__init__(parent)
        font_stack = ["Cantarell", "Inter", "Segoe UI", "system-ui", "sans-serif"]

        self.badge_font = QFont()
        self.badge_font.setFamilies(font_stack)
        self.badge_font.setPointSize(8)
        self.badge_font.setBold(True)
        self.badge_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4)

        self.base_font = QFont()
        self.base_font.setFamilies(font_stack)
        self.base_font.setPointSize(10)

        self.bold_font = QFont()
        self.bold_font.setFamilies(font_stack)
        self.bold_font.setPointSize(10)
        self.bold_font.setBold(True)

        self.version_bold_font = QFont()
        self.version_bold_font.setFamilies(font_stack)
        self.version_bold_font.setPointSize(10)
        self.version_bold_font.setBold(True)

        self.fm_badge = QFontMetrics(self.badge_font)
        self.fm_base = QFontMetrics(self.base_font)
        self.fm_bold = QFontMetrics(self.bold_font)
        self.fm_ver_bold = QFontMetrics(self.version_bold_font)

        # Dynamic palette variables
        self.color_bg_base = QColor("#181825")
        self.color_bg_hover = QColor("#313244")
        self.color_bg_selected = QColor("#45475a")
        self.color_text_main = QColor("#cdd6f4")
        self.color_text_dep = QColor("#a6adc8")
        self.color_text_dim = QColor("#6c7086")
        self.color_text_ver = QColor("#89b4fa")
        self.color_accent = QColor("#89b4fa")

        # Status: (Background, Foreground, Border)
        self.state_colors: Dict[PackageState, Tuple[QColor, QColor, QColor]] = {}
        self.tag_orphan_colors = (QColor("#3d2f47"), QColor("#cba6f7"), QColor("#573d69"))
        self.tag_cycle_colors = (QColor("#453322"), QColor("#fab387"), QColor("#6e4b2d"))
        self.tag_reverse_colors = (QColor("#1e3a2f"), QColor("#89b4fa"), QColor("#2d5a47"))

        self.set_theme("auto")

    def set_theme(self, theme_choice: str):
        """Applies dynamic color palette according to the active theme."""
        pal = get_delegate_palette(theme_choice)

        self.color_bg_base = pal["bg_base"]
        self.color_bg_hover = pal["bg_hover"]
        self.color_bg_selected = pal["bg_selected"]
        self.color_text_main = pal["text_main"]
        self.color_text_dep = pal["text_secondary"]
        self.color_text_dim = pal["text_dim"]
        self.color_text_ver = pal["accent"]
        self.color_accent = pal["accent"]

        self.state_colors = {
            PackageState.INSTALLED: (
                pal["badge_bg_installed"],
                pal["badge_fg_installed"],
                pal["badge_border_installed"]
            ),
            PackageState.MISSING: (
                pal["badge_bg_missing"],
                pal["badge_fg_missing"],
                pal["badge_border_missing"]
            ),
            PackageState.QUEUED_INSTALL: (
                pal["badge_bg_queued_in"],
                pal["badge_fg_queued_in"],
                pal["badge_border_queued_in"]
            ),
            PackageState.QUEUED_REMOVE: (
                pal["badge_bg_queued_rm"],
                pal["badge_fg_queued_rm"],
                pal["badge_border_queued_rm"]
            ),
            PackageState.AVAILABLE: (
                pal["badge_bg_tag"],
                pal["accent"],
                pal["badge_border_tag"]
            ),
        }

        self.tag_orphan_colors = (pal["badge_bg_tag"], pal["badge_fg_tag"], pal["badge_border_tag"])
        self.tag_cycle_colors = (pal["badge_bg_queued_in"], pal["badge_fg_queued_in"], pal["badge_border_queued_in"])
        self.tag_reverse_colors = (pal["badge_bg_installed"], pal["accent"], pal["badge_border_installed"])

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        default_size = super().sizeHint(option, index)
        return QSize(default_size.width(), 34)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect

        # ---------------------------------------------------------------------
        # 1. Self-Contained Row Background Painting (Zero Host Theme Bleed)
        # ---------------------------------------------------------------------
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(self.color_bg_selected))
            painter.drawRoundedRect(QRectF(rect.left() + 2, rect.top() + 1, rect.width() - 4, rect.height() - 2), 4.0, 4.0)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(self.color_bg_hover))
            painter.drawRoundedRect(QRectF(rect.left() + 2, rect.top() + 1, rect.width() - 4, rect.height() - 2), 4.0, 4.0)
        else:
            # Force background to match the active theme palette
            painter.fillRect(rect, self.color_bg_base)

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
            current_x = self._draw_tag(painter, rect, current_x, "ORPHAN", self.tag_orphan_colors)

        if is_rev_dep:
            current_x = self._draw_tag(painter, rect, current_x, "REQUIRED BY", self.tag_reverse_colors)

        if is_cycle:
            self._draw_tag(painter, rect, current_x, "CYCLE", self.tag_cycle_colors)

    def _paint_status_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        is_rev_dep = bool(index.data(CustomUserRoles.IsReverseDepRole))
        if is_rev_dep:
            bg_color, text_color, border_color = self.tag_reverse_colors
        else:
            state: PackageState = index.data(CustomUserRoles.PackageStateRole) or PackageState.AVAILABLE
            bg_color, text_color, border_color = self.state_colors.get(
                state, self.state_colors[PackageState.AVAILABLE]
            )

        status_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "").upper()
        rect = option.rect

        painter.setFont(self.badge_font)
        text_width = self.fm_badge.horizontalAdvance(status_text)
        pill_width = text_width + 16
        pill_height = 20
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
            status_text
        )

    def _paint_version_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        ver_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect
        text_y = rect.top() + (rect.height() + self.fm_base.ascent() - self.fm_base.descent()) // 2
        text_x = rect.left() + 6

        # Check for upgrade path formatting: "current -> target"
        if " -> " in ver_text:
            parts = ver_text.split(" -> ", 1)
            current_ver = parts[0].strip()
            target_ver = parts[1].strip()

            # 1. Current version (muted text)
            painter.setFont(self.base_font)
            painter.setPen(self.color_text_dim)
            painter.drawText(text_x, text_y, current_ver)
            text_x += self.fm_base.horizontalAdvance(current_ver) + 5

            # 2. Upgrade arrow glyph (accent color)
            arrow_str = "→"
            painter.setPen(self.color_accent)
            painter.drawText(text_x, text_y, arrow_str)
            text_x += self.fm_base.horizontalAdvance(arrow_str) + 5

            # 3. New target version (bold accent color)
            painter.setFont(self.version_bold_font)
            painter.setPen(self.color_accent)
            painter.drawText(text_x, text_y, target_ver)
        else:
            painter.setFont(self.base_font)
            painter.setPen(self.color_text_ver)
            painter.drawText(text_x, text_y, ver_text)

    def _paint_size_column(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        size_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = option.rect

        painter.setFont(self.base_font)
        painter.setPen(self.color_text_dim)
        text_y = rect.top() + (rect.height() + self.fm_base.ascent() - self.fm_base.descent()) // 2
        painter.drawText(rect.left() + 6, text_y, size_text)

    def _draw_tag(
        self,
        painter: QPainter,
        row_rect: QRect,
        start_x: int,
        text: str,
        colors: Tuple[QColor, QColor, QColor],
    ) -> int:
        bg_col, txt_col, border_col = colors
        painter.setFont(self.badge_font)

        tag_w = self.fm_badge.horizontalAdvance(text) + 12
        tag_h = 16
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
