# ui/header.py
"""
Top application header bar featuring debounced search and transaction queue trigger.
"""

from __future__ import annotations

from typing import Optional
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget


class HeaderBar(QWidget):
    """Top navigation bar containing the real-time search input and apply changes button."""
    
    search_changed = pyqtSignal(str)
    apply_clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("HeaderContainer")
        self._init_ui()
        self._setup_debounce()

    def _init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(16)

        # Search Input Field
        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchBar")
        self.search_input.setPlaceholderText("🔍 Search installed packages, libraries, or dependencies...")
        self.search_input.setClearButtonEnabled(True)

        # Apply Changes Action Button
        self.apply_btn = QPushButton("Apply Changes (0)")
        self.apply_btn.setObjectName("ApplyButton")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_btn.clicked.connect(self.apply_clicked.emit)

        layout.addWidget(self.search_input, stretch=1)
        layout.addWidget(self.apply_btn)

    def _setup_debounce(self):
        """
        Debounce timer to prevent RPM/Model query thrashing while typing fast.
        """
        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(250)  # 250ms debounce window
        self.debounce_timer.timeout.connect(self._emit_search)
        self.search_input.textChanged.connect(self.debounce_timer.start)

    def _emit_search(self):
        self.search_changed.emit(self.search_input.text())

    def update_queue_badge(self, count: int):
        self.apply_btn.setText(f"Apply Changes ({count})")
        self.apply_btn.setEnabled(count > 0)