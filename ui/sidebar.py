# ui/sidebar.py
from __future__ import annotations

from typing import Dict, Optional
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QWidget


class CategorySidebar(QListWidget):
    category_selected = pyqtSignal(str)

    CATEGORIES_CONFIG = [
        # بخش ۱: نمای کلی
        ("📌 OVERVIEW", "", True),
        ("  🌳 Main Packages", "installed", False),
        ("  🖥️ Desktop Apps", "gui_apps", False),
        ("  ⌨️ CLI Tools", "cli_tools", False),
        ("  📦 All Packages", "all", False),

        # بخش ۲: موضوعی
        ("🏷️ CATEGORIES", "", True),
        ("  🛠️ Development & Code", "development", False),
        ("  ⚙️ System & Core", "system", False),
        ("  🎨 Multimedia & Graphics", "multimedia", False),
        ("  🌐 Internet & Network", "network", False),
        ("  🔤 Fonts & Locales", "fonts", False),
        ("  📚 Libraries & Base", "libraries", False),

        # بخش ۳: نگهداری و صف
        ("🧹 MAINTENANCE", "", True),
        ("  🍂 Orphan Packages", "orphans", False),
        ("  ⏳ Pending Changes", "queued", False),
    ]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SidebarList")
        self.setFixedWidth(250)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._category_items: Dict[str, QListWidgetItem] = {}
        self._counts: Dict[str, int] = {}  # ذخیره دائمی آمار دسته‌ها
        
        self._init_items()
        self.itemClicked.connect(self._on_item_clicked)

    def _init_items(self):
        for label, tag, is_header in self.CATEGORIES_CONFIG:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, tag)

            if is_header:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                self._category_items[tag] = item

            self.addItem(item)

        # انتخاب پیش‌فرض پکیج‌های اصلی (آیتم ایندکس 1)
        self.setCurrentRow(1)

    def update_category_counts(self, counts: Dict[str, int]):
        """آپدیت تجمیعی شمارنده‌ها بدون حذف مقادیر قبلی"""
        self._counts.update(counts)

        for label, tag, is_header in self.CATEGORIES_CONFIG:
            if is_header:
                continue

            item = self._category_items.get(tag)
            if not item:
                continue

            count = self._counts.get(tag, 0)
            if count > 0:
                item.setText(f"{label} ({count})")
            else:
                item.setText(label)

    def _on_item_clicked(self, item: QListWidgetItem):
        tag = item.data(Qt.ItemDataRole.UserRole)
        if tag:
            self.category_selected.emit(tag)
