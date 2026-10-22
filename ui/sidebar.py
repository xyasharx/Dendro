# dendro/ui/sidebar.py
"""
Navigation sidebar for two-tier hierarchical package categories.
Organizes all installed packages across 6 System Pillars with fine-grained subcategories.
Features custom delegate rendering with right-aligned counter pill badges,
typographic section headers with hairline dividers, and full-color FreeDesktop vector icons.
Zero emoji glyphs to prevent Fontconfig shaping and layout crashes.
"""

from __future__ import annotations

from typing import Dict, Final, Optional
from PyQt6.QtCore import QModelIndex, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPen,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from core.taxonomy import category_spec, sidebar_rows
from ui.styles import get_delegate_palette


# Custom Item Data Roles
class SidebarRoles:
    TagRole: Final[int] = Qt.ItemDataRole.UserRole
    CountRole: Final[int] = Qt.ItemDataRole.UserRole + 1
    IsHeaderRole: Final[int] = Qt.ItemDataRole.UserRole + 2
    IsSubcategoryRole: Final[int] = Qt.ItemDataRole.UserRole + 3


# =============================================================================
# Custom Item Delegate for Elevated Sidebar Aesthetics
# =============================================================================


class SidebarItemDelegate(QStyledItemDelegate):
    """
    Renders sidebar items with:
    - Typographic section headers with subtle hairline dividers
    - True hierarchical indentation for subcategories
    - Right-aligned rounded counter pill badges
    - Left-edge accent indicator for selected items
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QApplication

        font_stack = ["Cantarell", "Inter", "Segoe UI", "system-ui", "sans-serif"]

        app = QApplication.instance()
        sys_font = app.font() if app else QFont()
        base_pt = sys_font.pointSize() if sys_font.pointSize() > 0 else 11

        # Base item font inherits system point size
        self.item_font = QFont()
        self.item_font.setFamilies(font_stack)
        self.item_font.setPointSize(base_pt)
        self.item_font.setWeight(QFont.Weight.Medium)

        # Selected item font
        self.selected_font = QFont()
        self.selected_font.setFamilies(font_stack)
        self.selected_font.setPointSize(base_pt)
        self.selected_font.setBold(True)

        # Section header font scales with system font
        self.header_font = QFont()
        self.header_font.setFamilies(font_stack)
        self.header_font.setPointSize(max(9, base_pt - 1))
        self.header_font.setBold(True)
        self.header_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)

        # Counter pill badge font
        self.badge_font = QFont()
        self.badge_font.setFamilies(font_stack)
        self.badge_font.setPointSize(max(9, base_pt - 1))
        self.badge_font.setBold(True)

        self.fm_item = QFontMetrics(self.item_font)
        self.fm_badge = QFontMetrics(self.badge_font)
        self.current_theme = "auto"
        # Precompute palette once to avoid subprocess calls inside the paint loop
        self.pal = get_delegate_palette(self.current_theme)

    def set_theme(self, theme_choice: str):
        self.current_theme = theme_choice
        self.pal = get_delegate_palette(theme_choice)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        is_header = bool(index.data(SidebarRoles.IsHeaderRole))
        if is_header:
            return QSize(option.rect.width(), 36)
        return QSize(option.rect.width(), 32)

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Use precomputed palette (zero disk/subprocess overhead during scrolling)
        pal = self.pal
        rect = option.rect
        is_header = bool(index.data(SidebarRoles.IsHeaderRole))

        # ---------------------------------------------------------------------
        # 1. Render Typographic Section Header
        # ---------------------------------------------------------------------
        if is_header:
            # Hairline divider above header (skip for the very first item)
            if index.row() > 0:
                painter.setPen(QPen(pal["border_subtle"], 1))
                painter.drawLine(
                    rect.left() + 8, rect.top() + 4, rect.right() - 8, rect.top() + 4
                )

            header_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "").upper()
            painter.setFont(self.header_font)
            painter.setPen(pal["text_dim"])

            text_rect = QRect(
                rect.left() + 10, rect.top() + 8, rect.width() - 20, rect.height() - 8
            )
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                header_text,
            )

            painter.restore()
            return

        # ---------------------------------------------------------------------
        # 2. Render Interactive Category Item
        # ---------------------------------------------------------------------
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        is_sub = bool(index.data(SidebarRoles.IsSubcategoryRole))

        item_rect = QRectF(
            rect.left() + 4, rect.top() + 1, rect.width() - 8, rect.height() - 2
        )

        # Background Pill Highlight
        if is_selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(pal["bg_selected"]))
            painter.drawRoundedRect(item_rect, 6.0, 6.0)

            # Left Accent Bar
            accent_bar = QRectF(
                rect.left() + 4, rect.top() + 6, 3.5, rect.height() - 12
            )
            painter.setBrush(QBrush(pal["accent"]))
            painter.drawRoundedRect(accent_bar, 1.5, 1.5)

        elif is_hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(pal["bg_hover"]))
            painter.drawRoundedRect(item_rect, 6.0, 6.0)

        # Indentation for Subcategories
        left_offset = rect.left() + (26 if is_sub else 12)

        # Draw Category Vector Icon
        icon: QIcon = index.data(Qt.ItemDataRole.DecorationRole)
        icon_size = 16
        icon_y = int(rect.top() + (rect.height() - icon_size) / 2)
        if icon and not icon.isNull():
            icon_rect = QRect(left_offset, icon_y, icon_size, icon_size)
            icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter)
            text_x = left_offset + icon_size + 8
        else:
            text_x = left_offset

        # ---------------------------------------------------------------------
        # 3. Draw Right-Aligned Counter Pill Badge
        # ---------------------------------------------------------------------
        count = index.data(SidebarRoles.CountRole) or 0
        pill_width = 0

        if count > 0:
            count_str = f"{count:,}"
            badge_text_w = self.fm_badge.horizontalAdvance(count_str)
            pill_width = badge_text_w + 12
            pill_height = 18
            pill_x = rect.right() - pill_width - 10
            pill_y = rect.top() + (rect.height() - pill_height) // 2

            pill_rect = QRectF(pill_x, pill_y, pill_width, pill_height)

            # Subtle rounded badge background
            if is_selected:
                badge_bg = pal["accent"]
                badge_fg = pal["bg_base"]
            else:
                badge_bg = pal["bg_card"]
                badge_fg = pal["text_secondary"]

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(badge_bg))
            painter.drawRoundedRect(pill_rect, 9.0, 9.0)

            # Draw Counter Text
            painter.setFont(self.badge_font)
            painter.setPen(badge_fg)
            painter.drawText(
                QRect(int(pill_x), int(pill_y), int(pill_width), int(pill_height)),
                Qt.AlignmentFlag.AlignCenter,
                count_str,
            )

        # ---------------------------------------------------------------------
        # 4. Draw Category Title (Guaranteed Zero Overlap with Badge)
        # ---------------------------------------------------------------------
        label_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.setFont(self.selected_font if is_selected else self.item_font)

        if is_selected:
            painter.setPen(pal["accent"])
        elif is_hover:
            painter.setPen(pal["text_main"])
        else:
            painter.setPen(pal["text_secondary"])

        # Truncate text cleanly before the pill badge if label is long
        avail_width = (rect.right() - pill_width - 18) - text_x
        text_rect = QRect(text_x, rect.top(), max(0, avail_width), rect.height())
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.fm_item.elidedText(
                label_text, Qt.TextElideMode.ElideRight, avail_width
            ),
        )

        painter.restore()


# =============================================================================
# Category Sidebar Container
# =============================================================================


class CategorySidebar(QListWidget):
    """Registry-driven, two-tier package taxonomy navigation sidebar."""

    category_selected = pyqtSignal(str)

    # Compatibility views for existing callers.  They are calculated from the
    # central registry rather than independently maintained UI constants.
    CATEGORIES_CONFIG: Final[list[tuple[str, str, bool]]] = [
        (
            ("  " if row.is_subcategory else "") + row.label,
            row.tag,
            row.is_header,
        )
        for row in sidebar_rows()
    ]
    CATEGORY_ICONS: Final[Dict[str, list[str]]] = {
        row.tag: list(category_spec(row.tag).icon_names)
        for row in sidebar_rows()
        if not row.is_header and category_spec(row.tag) is not None
    }
    CATEGORY_TOOLTIPS: Final[Dict[str, str]] = {
        row.tag: category_spec(row.tag).tooltip
        for row in sidebar_rows()
        if not row.is_header and category_spec(row.tag) is not None
    }

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SidebarList")
        self.setMinimumWidth(180)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(20)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        self.sidebar_delegate = SidebarItemDelegate(self)
        self.setItemDelegate(self.sidebar_delegate)
        self._category_items: Dict[str, QListWidgetItem] = {}
        self._counts: Dict[str, int] = {}
        self._init_items()
        self.itemClicked.connect(self._on_item_clicked)

    def set_theme(self, theme_choice: str):
        """Notify the delegate when the active theme changes."""
        self.sidebar_delegate.set_theme(theme_choice)
        self.viewport().update()

    @staticmethod
    def _get_theme_icon(tag: str) -> QIcon:
        spec = category_spec(tag)
        for name in spec.icon_names if spec is not None else ("package-x-generic",):
            icon = QIcon.fromTheme(name)
            if not icon.isNull():
                return icon
        return QIcon.fromTheme("package-x-generic")

    def _init_items(self):
        first_selectable_item: QListWidgetItem | None = None
        for row in sidebar_rows():
            item = QListWidgetItem(row.label)
            item.setData(SidebarRoles.TagRole, row.tag)
            item.setData(SidebarRoles.IsHeaderRole, row.is_header)
            item.setData(SidebarRoles.IsSubcategoryRole, row.is_subcategory)
            item.setData(SidebarRoles.CountRole, 0)
            if row.is_header:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                spec = category_spec(row.tag)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setIcon(self._get_theme_icon(row.tag))
                if spec is not None:
                    item.setToolTip(spec.tooltip)
                self._category_items[row.tag] = item
                if first_selectable_item is None:
                    first_selectable_item = item
            self.addItem(item)

        if first_selectable_item is not None:
            self.setCurrentItem(first_selectable_item)

    def update_category_counts(self, counts: Dict[str, int]):
        """Update right-aligned badges using registry category IDs."""
        self._counts.update(counts)
        for tag, item in self._category_items.items():
            item.setData(SidebarRoles.CountRole, self._counts.get(tag, 0))
        self.viewport().update()

    def _on_item_clicked(self, item: QListWidgetItem):
        tag = item.data(SidebarRoles.TagRole)
        if tag:
            self.category_selected.emit(tag)
