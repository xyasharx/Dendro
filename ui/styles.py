# ui/styles.py
"""
Modern High-Definition Dark Theme with dynamically rendered
pixel-perfect branch expander chevron arrows for Qt6.
"""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap


def generate_chevron_base64(direction: str = "right", color_hex: str = "#a6adc8", size: int = 16) -> str:
    """
    Renders a crisp, anti-aliased vector chevron arrow in memory
    and exports it as a standard Base64 PNG (100% native Qt support).
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    pen = QPen(QColor(color_hex))
    pen.setWidthF(2.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    path = QPainterPath()
    if direction == "right":
        # فلش رو به راست (حالت بسته)
        path.moveTo(QPointF(size * 0.36, size * 0.22))
        path.lineTo(QPointF(size * 0.68, size * 0.50))
        path.lineTo(QPointF(size * 0.36, size * 0.78))
    else:
        # فلش رو به پایین (حالت باز)
        path.moveTo(QPointF(size * 0.22, size * 0.36))
        path.lineTo(QPointF(size * 0.50, size * 0.68))
        path.lineTo(QPointF(size * 0.78, size * 0.36))

    painter.drawPath(path)
    painter.end()

    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    base64_data = byte_array.toBase64().data().decode("utf-8")
    return f"data:image/png;base64,{base64_data}"


def get_dark_theme() -> str:
    """Generates the full modern dark theme stylesheet with active chevron icons."""
    icon_closed = generate_chevron_base64("right", "#a6adc8", 16)
    icon_closed_hover = generate_chevron_base64("right", "#89b4fa", 16)
    icon_open = generate_chevron_base64("down", "#89b4fa", 16)

    return f"""
/* ========================================================================= */
/* Global Reset & Base Typography                                            */
/* ========================================================================= */
QWidget {{
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: "Cantarell", "Inter", "Segoe UI", sans-serif;
    font-size: 13px;
    selection-background-color: #313244;
    selection-color: #89b4fa;
}}

/* ========================================================================= */
/* Top Header & Search Bar                                                  */
/* ========================================================================= */
QWidget#HeaderContainer {{
    background-color: #181825;
    border-bottom: 1px solid #313244;
}}

QLineEdit#SearchBar {{
    background-color: #11111b;
    border: 1px solid #313244;
    border-radius: 8px;
    padding: 8px 14px 8px 14px;
    color: #cdd6f4;
    font-size: 13px;
}}

QLineEdit#SearchBar:focus {{
    border: 1px solid #89b4fa;
    background-color: #181825;
}}

/* ========================================================================= */
/* Category Navigation Sidebar                                               */
/* ========================================================================= */
QListWidget#SidebarList {{
    background-color: #11111b;
    border: none;
    border-right: 1px solid #313244;
    padding: 10px 6px;
}}

QListWidget#SidebarList::item {{
    height: 34px;
    border-radius: 6px;
    padding-left: 8px;
    margin-bottom: 2px;
    color: #a6adc8;
    font-weight: 500;
}}

QListWidget#SidebarList::item:hover {{
    background-color: #181825;
    color: #cdd6f4;
}}

QListWidget#SidebarList::item:selected {{
    background-color: #313244;
    color: #89b4fa;
    font-weight: bold;
}}

QListWidget#SidebarList::item:disabled {{
    color: #6c7086;
    font-weight: 800;
    font-size: 11px;
    letter-spacing: 0.5px;
    padding-top: 10px;
    padding-bottom: 4px;
    background-color: transparent;
}}

/* ========================================================================= */
/* Expandable Dependency Tree View & Custom Branch Arrows                   */
/* ========================================================================= */
QTreeView#PackageTreeView {{
    background-color: #1e1e2e;
    border: none;
    outline: none;
    padding: 4px;
    show-decoration-selected: 1;
}}

QTreeView#PackageTreeView::item {{
    border: none;
    border-radius: 4px;
}}

QHeaderView::section {{
    background-color: #181825;
    color: #a6adc8;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid #313244;
    font-weight: bold;
    font-size: 12px;
}}

QHeaderView::section:hover {{
    background-color: #313244;
    color: #cdd6f4;
}}

/* فلش درختی رو به راست (حالت بسته) */
QTreeView::branch:has-children:!has-siblings:closed,
QTreeView::branch:closed:has-children:has-siblings {{
    border-image: none;
    image: url("{icon_closed}");
}}

/* هاور روی فلش بسته */
QTreeView::branch:has-children:!has-siblings:closed:hover,
QTreeView::branch:closed:has-children:has-siblings:hover {{
    border-image: none;
    image: url("{icon_closed_hover}");
}}

/* فلش درختی رو به پایین (حالت باز) */
QTreeView::branch:open:has-children:!has-siblings,
QTreeView::branch:open:has-children:has-siblings {{
    border-image: none;
    image: url("{icon_open}");
}}

/* ========================================================================= */
/* Buttons & Badges                                                         */
/* ========================================================================= */
QPushButton {{
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: bold;
    color: #cdd6f4;
}}

QPushButton:hover {{
    background-color: #45475a;
}}

QPushButton:pressed {{
    background-color: #585b70;
}}

QPushButton#ApplyButton {{
    background-color: #89b4fa;
    color: #11111b;
    border: none;
    font-weight: 700;
}}

QPushButton#ApplyButton:hover {{
    background-color: #b4befe;
}}

QPushButton#ApplyButton:disabled {{
    background-color: #313244;
    color: #585b70;
}}

/* ========================================================================= */
/* Terminal & Log Drawer                                                    */
/* ========================================================================= */
QTextEdit#ConsoleOutput {{
    background-color: #11111b;
    border: 1px solid #313244;
    border-radius: 8px;
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
    font-size: 12px;
    color: #a6adc8;
    padding: 8px;
}}

/* ========================================================================= */
/* ScrollBars                                                               */
/* ========================================================================= */
QScrollBar:vertical {{
    background-color: #181825;
    width: 8px;
    margin: 0px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical {{
    background-color: #313244;
    min-height: 24px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: #45475a;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    height: 0px;
}}

/* ========================================================================= */
/* Context Menus                                                            */
/* ========================================================================= */
QMenu {{
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 8px;
    padding: 6px;
}}

QMenu::item {{
    padding: 8px 24px 8px 12px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background-color: #313244;
    color: #89b4fa;
}}

/* ========================================================================= */
/* Status Bar & Splitter                                                    */
/* ========================================================================= */
QStatusBar {{
    background-color: #11111b;
    border-top: 1px solid #313244;
    color: #a6adc8;
}}

QSplitter::handle {{
    background-color: #181825;
}}
"""

# برای سازگاری کامل
MODERN_DARK_THEME = ""
