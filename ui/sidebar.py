# ui/sidebar.py
from __future__ import annotations

from typing import Dict, Optional
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QWidget


class CategorySidebar(QListWidget):
    category_selected = pyqtSignal(str)

    CATEGORIES = [
        ("Main Packages", "installed"),
        ("All Packages (with Libs)", "all"),
        ("Development", "development"),
        ("System", "system"),
        ("Orphans", "orphans"),
        ("Pending Changes", "queued"),
    ]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SidebarList")
        self.setFixedWidth(230)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._category_items: Dict[str, QListWidgetItem] = {}
        
        self._init_items()
        self.itemClicked.connect(self._on_item_clicked)

    def _init_items(self):
        for label, tag in self.CATEGORIES:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, tag)
            self.addItem(item)
            self._category_items[tag] = item

        # انتخاب پیش‌فرض پکیج‌های اصلی
        self.setCurrentRow(0)

    def update_category_counts(self, counts: Dict[str, int]):
        for label, tag in self.CATEGORIES:
            item = self._category_items.get(tag)
            if not item:
                continue

            count = counts.get(tag, 0)
            if count > 0 and tag in ("installed", "orphans", "queued"):
                item.setText(f"{label} ({count})")
            else:
                item.setText(label)

    def _on_item_clicked(self, item: QListWidgetItem):
        tag = item.data(Qt.ItemDataRole.UserRole)
        self.category_selected.emit(tag)
