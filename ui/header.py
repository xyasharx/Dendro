# dendro/ui/header.py
from __future__ import annotations

from typing import Optional
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QMenu, QPushButton, QWidget

from ui.styles import THEME_DISPLAY_OPTIONS


class HeaderBar(QWidget):
    """
    Top toolbar container:
    Features debounced search bar, database reload trigger, DNF history viewer,
    dynamic theme selector dropdown, details panel toggle, and transaction apply button.
    """

    search_changed = pyqtSignal(str)
    apply_clicked = pyqtSignal()
    toggle_inspector_clicked = pyqtSignal()
    history_clicked = pyqtSignal()
    reload_clicked = pyqtSignal()
    theme_selected = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("HeaderContainer")
        self._init_ui()
        self._setup_debounce()

    def _init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)

        # 1. Advanced Search Input Bar
        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchBar")
        self.search_input.setPlaceholderText("🔍 Search packages (e.g. firefox, size:>100M, repo:copr, license:gpl)...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setToolTip(
            "<b>Advanced Search Syntax:</b><br>"
            "• <code>size:&gt;100M</code> or <code>size:&lt;50K</code><br>"
            "• <code>repo:copr</code> or <code>repo:fusion</code><br>"
            "• <code>license:gpl</code> or <code>license:mit</code><br>"
            "• <code>status:orphan</code> or <code>status:queued</code>"
        )

        # 2. Reload / Re-index Database Button
        self.reload_btn = QPushButton(" Reload")
        self.reload_btn.setIcon(QIcon.fromTheme("view-refresh"))
        self.reload_btn.setObjectName("HeaderSecondaryBtn")
        self.reload_btn.setToolTip("Reload and re-index system RPM database (Ctrl+R)")
        self.reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reload_btn.clicked.connect(self.reload_clicked.emit)

        # 3. DNF Transaction History Button
        self.history_btn = QPushButton(" History")
        self.history_btn.setIcon(QIcon.fromTheme("document-open-recent") or QIcon.fromTheme("view-history"))
        self.history_btn.setObjectName("HeaderSecondaryBtn")
        self.history_btn.setToolTip("View DNF transaction history and rollback operations (Ctrl+H)")
        self.history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_btn.clicked.connect(self.history_clicked.emit)

        # 4. Multi-Theme Dropdown Menu Button
        self.theme_btn = QPushButton(" Theme")
        self.theme_btn.setIcon(QIcon.fromTheme("preferences-desktop-theme") or QIcon.fromTheme("color-management"))
        self.theme_btn.setObjectName("HeaderSecondaryBtn")
        self.theme_btn.setToolTip("Select application theme (Auto System, Dark, Light)")
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.theme_menu = QMenu(self)
        self.theme_action_group = QActionGroup(self)
        self.theme_action_group.setExclusive(True)

        for theme_key, display_label in THEME_DISPLAY_OPTIONS:
            action = QAction(display_label, self)
            action.setCheckable(True)
            action.setData(theme_key)
            action.triggered.connect(lambda checked, k=theme_key: self.theme_selected.emit(k))
            self.theme_action_group.addAction(action)
            self.theme_menu.addAction(action)

        self.theme_btn.setMenu(self.theme_menu)

        # 5. Details Inspector Panel Toggle Button
        self.inspector_btn = QPushButton(" Details")
        self.inspector_btn.setIcon(QIcon.fromTheme("document-properties") or QIcon.fromTheme("dialog-information"))
        self.inspector_btn.setObjectName("HeaderSecondaryBtn")
        self.inspector_btn.setToolTip("Toggle package detail inspector panel (Ctrl+I)")
        self.inspector_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inspector_btn.clicked.connect(self.toggle_inspector_clicked.emit)

        # 6. Apply Pending Transactions Button
        self.apply_btn = QPushButton("Apply Changes (0)")
        self.apply_btn.setIcon(QIcon.fromTheme("emblem-default") or QIcon.fromTheme("dialog-ok-apply"))
        self.apply_btn.setObjectName("ApplyButton")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_btn.clicked.connect(self.apply_clicked.emit)

        layout.addWidget(self.search_input, stretch=1)
        layout.addWidget(self.reload_btn)
        layout.addWidget(self.history_btn)
        layout.addWidget(self.theme_btn)
        layout.addWidget(self.inspector_btn)
        layout.addWidget(self.apply_btn)

    def _setup_debounce(self):
        """250ms debounce timer preventing rapid database queries while typing."""
        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(250)
        self.debounce_timer.timeout.connect(self._emit_search)
        self.search_input.textChanged.connect(self.debounce_timer.start)

    def _emit_search(self):
        self.search_changed.emit(self.search_input.text())

    def update_queue_badge(self, count: int):
        self.apply_btn.setText(f"Apply Changes ({count})")
        self.apply_btn.setEnabled(count > 0)

    def set_active_theme(self, theme_key: str):
        """Updates the checkmark in the theme menu to reflect current theme."""
        for action in self.theme_action_group.actions():
            if action.data() == theme_key:
                action.setChecked(True)
                break
