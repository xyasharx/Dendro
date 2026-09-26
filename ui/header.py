# dendro/ui/header.py
"""
Top toolbar header container:
Features debounced search bar with advanced syntax, DNF history viewer,
software repository manager trigger, dynamic updates badge, theme selector,
inspector panel toggle, and transaction apply controls.
"""
from __future__ import annotations

from typing import Optional
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QMenu, QPushButton, QWidget

from ui.styles import THEME_DISPLAY_OPTIONS


class HeaderBar(QWidget):
    """
    Top toolbar container for Dendro:
    Houses search, repository management, updates notification, theme selection,
    inspector toggling, and transaction execution controls.
    """

    search_changed = pyqtSignal(str)
    apply_clicked = pyqtSignal()
    toggle_inspector_clicked = pyqtSignal()
    history_clicked = pyqtSignal()
    reload_clicked = pyqtSignal()
    theme_selected = pyqtSignal(str)
    repos_clicked = pyqtSignal()
    updates_clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("HeaderContainer")
        self._init_ui()
        self._setup_debounce()

    def _init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)

        # ---------------------------------------------------------------------
        # 1. Advanced Search Input Bar
        # ---------------------------------------------------------------------
        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchBar")
        self.search_input.setPlaceholderText("🔍 Search packages (e.g. firefox, status:update, status:user, arch:x86_64, size:>100M)...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setToolTip(
            "<b>Advanced Search Syntax:</b><br>"
            "• <code>status:update</code> or <code>status:user</code><br>"
            "• <code>status:orphan</code> or <code>status:queued</code><br>"
            "• <code>arch:x86_64</code> or <code>arch:noarch</code><br>"
            "• <code>cat:desktop_app</code> or <code>cat:theme</code><br>"
            "• <code>tag:python</code> or <code>tag:rust</code><br>"
            "• <code>size:&gt;100M</code> or <code>size:&lt;50K</code><br>"
            "• <code>repo:copr</code> or <code>repo:fusion</code><br>"
            "• <code>license:gpl</code> or <code>license:mit</code>"
        )

        # ---------------------------------------------------------------------
        # 2. Dynamic Available Updates Notification Button
        # ---------------------------------------------------------------------
        self.updates_btn = QPushButton(" Updates (0)")
        self.updates_btn.setIcon(QIcon.fromTheme("software-update-available") or QIcon.fromTheme("system-software-update"))
        self.updates_btn.setObjectName("ApplyButton")
        self.updates_btn.setToolTip("View available package updates and security errata")
        self.updates_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.updates_btn.setVisible(False)
        self.updates_btn.clicked.connect(self.updates_clicked.emit)

        # ---------------------------------------------------------------------
        # 3. Reload / Re-index Database Button
        # ---------------------------------------------------------------------
        self.reload_btn = QPushButton(" Reload")
        self.reload_btn.setIcon(QIcon.fromTheme("view-refresh"))
        self.reload_btn.setObjectName("HeaderSecondaryBtn")
        self.reload_btn.setToolTip("Reload and re-index system RPM database (Ctrl+R)")
        self.reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reload_btn.clicked.connect(self.reload_clicked.emit)

        # ---------------------------------------------------------------------
        # 4. DNF Transaction History Button
        # ---------------------------------------------------------------------
        self.history_btn = QPushButton(" History")
        self.history_btn.setIcon(QIcon.fromTheme("document-open-recent") or QIcon.fromTheme("view-history"))
        self.history_btn.setObjectName("HeaderSecondaryBtn")
        self.history_btn.setToolTip("View DNF transaction history and rollback operations (Ctrl+H)")
        self.history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_btn.clicked.connect(self.history_clicked.emit)

        # ---------------------------------------------------------------------
        # 5. Software Repositories & COPR Button
        # ---------------------------------------------------------------------
        self.repos_btn = QPushButton(" Repos")
        self.repos_btn.setIcon(QIcon.fromTheme("system-software-install") or QIcon.fromTheme("software-properties"))
        self.repos_btn.setObjectName("HeaderSecondaryBtn")
        self.repos_btn.setToolTip("Manage software repositories, RPM Fusion & COPR channels")
        self.repos_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.repos_btn.clicked.connect(self.repos_clicked.emit)

        # ---------------------------------------------------------------------
        # 6. Multi-Theme Dropdown Menu Button
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 7. Details Inspector Panel Toggle Button
        # ---------------------------------------------------------------------
        self.inspector_btn = QPushButton(" Details")
        self.inspector_btn.setIcon(QIcon.fromTheme("document-properties") or QIcon.fromTheme("dialog-information"))
        self.inspector_btn.setObjectName("HeaderSecondaryBtn")
        self.inspector_btn.setToolTip("Toggle package detail inspector panel (Ctrl+I)")
        self.inspector_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inspector_btn.clicked.connect(self.toggle_inspector_clicked.emit)

        # ---------------------------------------------------------------------
        # 8. Apply Pending Transactions Button
        # ---------------------------------------------------------------------
        self.apply_btn = QPushButton("Apply Changes (0)")
        self.apply_btn.setIcon(QIcon.fromTheme("emblem-default") or QIcon.fromTheme("dialog-ok-apply"))
        self.apply_btn.setObjectName("ApplyButton")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_btn.clicked.connect(self.apply_clicked.emit)

        layout.addWidget(self.search_input, stretch=1)
        layout.addWidget(self.updates_btn)
        layout.addWidget(self.reload_btn)
        layout.addWidget(self.history_btn)
        layout.addWidget(self.repos_btn)
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

    def update_available_updates_badge(self, count: int):
        """Shows or updates the dedicated updates button when updates exist."""
        if count > 0:
            self.updates_btn.setText(f" Updates ({count})")
            self.updates_btn.setVisible(True)
        else:
            self.updates_btn.setVisible(False)

    def set_active_theme(self, theme_key: str):
        """Updates the checkmark in the theme menu to reflect current theme."""
        for action in self.theme_action_group.actions():
            if action.data() == theme_key:
                action.setChecked(True)
                break
