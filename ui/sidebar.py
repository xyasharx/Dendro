# ui/sidebar.py
"""
Sidebar navigation widget with category counters and active filtering.
"""

from __future__ import annotations

from typing import Dict, Optional
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QWidget


class CategorySidebar(QListWidget):
    """Pamac-style category panel with live count badges."""
    
    category_selected = pyqtSignal(str)

    CATEGORIES = [
        ("All Packages", "all"),
        ("Installed", "installed"),
        ("Development", "development"),
        ("System", "system"),
        ("Orphans", "orphans"),
        ("Pending Changes", "queued"),
    ]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SidebarList")
        self.setFixedWidth(220)
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

        # Default select "All Packages"
        self.setCurrentRow(0)

    def update_category_counts(self, counts: Dict[str, int]):
        """
        Updates the sidebar item labels with dynamic counts (e.g. 'Orphans (14)').
        """
        for label, tag in self.CATEGORIES:
            item = self._category_items.get(tag)
            if not item:
                continue

            count = counts.get(tag, 0)
            if count > 0 and tag in ("orphans", "queued"):
                item.setText(f"{label} ({count})")
            else:
                item.setText(label)

    def _on_item_clicked(self, item: QListWidgetItem):
        tag = item.data(Qt.ItemDataRole.UserRole)
        self.category_selected.emit(tag)