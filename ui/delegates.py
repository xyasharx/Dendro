# dendro/ui/delegates.py
"""
Custom delegates and tree view classes for Dendro's main package view.
Features self-contained row background painting, micro-bordered status badges,
split-color version upgrade paths (current -> target), and native vector chevrons.
Zero font emoji glyphs to prevent Fontconfig shaping failures.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QModelIndex, QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QWidget,
)

from core.backend import PackageState
from core.models import CustomUserRoles, DependencyTreeModel
from ui.styles import get_delegate_palette

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
        self.chevron_color = QColor("#89b4fa")
        self._empty_title_color = QColor("#a6adc8")
        self._empty_detail_color = QColor("#888ea8")
        self._empty_title = ""
        self._empty_detail = ""

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
            # be told the cached sizes are stale.
            self.scheduleDelayedItemsLayout()
            self.viewport().update()

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

        base_font = self.font()
        base_pt = base_font.pointSize() if base_font.pointSize() > 0 else 11

        title_font = QFont(base_font)
        title_font.setPointSize(base_pt + 2)
        title_font.setBold(True)

        detail_font = QFont(base_font)
        detail_font.setPointSize(base_pt)

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
        font_stack = ["Cantarell", "Inter", "Segoe UI", "system-ui", "sans-serif"]

        app = QApplication.instance()
        sys_font = app.font() if app else QFont()
        base_pt = sys_font.pointSize() if sys_font.pointSize() > 0 else 11

        self.base_font = QFont()
        self.base_font.setFamilies(font_stack)
        self.base_font.setPointSize(base_pt)

        self.bold_font = QFont()
        self.bold_font.setFamilies(font_stack)
        self.bold_font.setPointSize(base_pt)
        self.bold_font.setBold(True)

        # Badges scale with system font (never smaller than 9pt)
        self.badge_font = QFont()
        self.badge_font.setFamilies(font_stack)
        self.badge_font.setPointSize(max(9, base_pt - 1))
        self.badge_font.setBold(True)
        self.badge_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4)

        # Monospace system font for version strings and upgrade paths
        self.version_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        if self.version_font.pointSize() <= 0:
            self.version_font.setPointSize(base_pt)

        self.version_bold_font = QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont
        )
        if self.version_bold_font.pointSize() <= 0:
            self.version_bold_font.setPointSize(base_pt)
        self.version_bold_font.setBold(True)

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
        )

    def __init__(self, parent: QStyledItemDelegate | None = None):
        super().__init__(parent)

        self.refresh_fonts()

        # Dynamic palette variables

        # Dynamic palette variables
        self.color_bg_base = QColor("#181825")
        self.color_bg_hover = QColor("#313244")
        self.color_bg_selected = QColor("#45475a")
        self.color_text_main = QColor("#cdd6f4")
        self.color_text_dep = QColor("#a6adc8")
        self.color_text_dim = QColor("#888ea8")
        self.color_text_ver = QColor("#89b4fa")
        self.color_accent = QColor("#89b4fa")

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
        self.color_text_dep = pal["text_secondary"]
        self.color_text_dim = pal["text_dim"]
        self.color_text_ver = pal["accent"]
        self.color_accent = pal["accent"]

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

        # ---------------------------------------------------------------------
        # 1. Self-Contained Row Background Painting (Zero Host Theme Bleed)
        # ---------------------------------------------------------------------
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(self.color_bg_selected))
            painter.drawRoundedRect(
                QRectF(
                    rect.left() + 2, rect.top() + 1, rect.width() - 4, rect.height() - 2
                ),
                4.0,
                4.0,
            )
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(self.color_bg_hover))
            painter.drawRoundedRect(
                QRectF(
                    rect.left() + 2, rect.top() + 1, rect.width() - 4, rect.height() - 2
                ),
                4.0,
                4.0,
            )
        else:
            # Force background to match the active theme palette
            painter.fillRect(rect, self.color_bg_base)

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

        painter.restore()

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
            tags.append(("ORPHAN", self.tag_orphan_colors))
        if is_rev_dep:
            tags.append(("REQUIRED BY", self.tag_reverse_colors))
        if is_cycle:
            tags.append(("CYCLE", self.tag_cycle_colors))

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
                painter.setPen(self.color_text_dim)
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
            painter.setPen(self.color_text_ver)
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
        if not size_text:
            placeholder = self.size_placeholder_text(index)
            if not placeholder:
                return
            painter.setFont(self.base_font)
            painter.setPen(self.color_text_dim)
            text_y = (
                rect.top()
                + (rect.height() + self.fm_base.ascent() - self.fm_base.descent()) // 2
            )
            painter.drawText(text_x, text_y, placeholder)
            return

        painter.setFont(self.base_font)
        painter.setPen(self.color_text_dim)
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
        painter.setFont(self.base_font)
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
