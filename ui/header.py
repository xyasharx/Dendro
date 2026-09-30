# dendro/ui/header.py
"""
Top toolbar header container:
Features an integrated search bar with a shortcut hint chip, dedicated update review pill,
direct 1-click system upgrade action, orphan cleanup action, grouped secondary tool cluster,
theme picker, inspector toggle, and primary CTA controls.
Exclusively utilizes native FreeDesktop vector icons with zero font emoji glyphs.
"""
from __future__ import annotations

from typing import Optional
from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QWidget,
)

from ui.styles import THEME_DISPLAY_OPTIONS


class HeaderBar(QWidget):
    """
    Top toolbar control hub for Dendro:
    Houses search, repository management, update review, direct upgrade action,
    theme selection, inspector toggling, and transaction execution controls.
    """

    search_changed = pyqtSignal(str)
    apply_clicked = pyqtSignal()
    toggle_inspector_clicked = pyqtSignal()
    history_clicked = pyqtSignal()
    reload_clicked = pyqtSignal()
    theme_selected = pyqtSignal(str)
    repos_clicked = pyqtSignal()
    updates_clicked = pyqtSignal()
    upgrade_system_clicked = pyqtSignal()
    clean_orphans_clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("HeaderContainer")
        self._init_ui()
        self._setup_debounce()

    def _init_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(14, 8, 14, 8)
        main_layout.setSpacing(8)

        # ---------------------------------------------------------------------
        # 1. Search Bar with Leading Icon
        # ---------------------------------------------------------------------
        search_container = QWidget()
        search_layout = QHBoxLayout(search_container)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(0)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchBar")
        self.search_input.setPlaceholderText("Search packages (firefox, status:update, type:gui, arch:x86_64, size:>100M)...")
        self.search_input.setClearButtonEnabled(True)

        search_icon = QIcon.fromTheme("system-search") or QIcon.fromTheme("edit-find")
        if not search_icon.isNull():
            self.search_input.addAction(search_icon, QLineEdit.ActionPosition.LeadingPosition)

        self.search_input.setToolTip(
            "<b>Search Filter Prefixes:</b><br>"
            "- <code>status:update</code> or <code>status:user</code><br>"
            "- <code>status:orphan</code> or <code>status:queued</code><br>"
            "- <code>type:gui</code> or <code>type:cli</code> or <code>type:service</code><br>"
            "- <code>arch:x86_64</code> or <code>arch:noarch</code><br>"
            "- <code>cat:desktop_app</code> or <code>cat:themes</code><br>"
            "- <code>tag:python</code> or <code>tag:rust</code><br>"
            "- <code>size:&gt;100M</code> or <code>size:&lt;50K</code><br>"
            "- <code>repo:copr</code> or <code>repo:fusion</code><br>"
            "- <code>license:gpl</code> or <code>license:mit</code>"
        )
        search_layout.addWidget(self.search_input)

        # ---------------------------------------------------------------------
        # 2. Update Controls (Separate Review vs. Action)
        # ---------------------------------------------------------------------
        # Navigation Button: Inspect the list of updates in the tree
        self.updates_btn = QPushButton("Updates (0)")
        self.updates_btn.setIcon(QIcon.fromTheme("software-update-available"))
        self.updates_btn.setObjectName("UpdatesIndicatorBtn")
        self.updates_btn.setToolTip("View list of available package updates and errata")
        self.updates_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.updates_btn.setVisible(False)
        self.updates_btn.clicked.connect(self.updates_clicked.emit)

        # Action Button: Directly execute system upgrade (dnf upgrade)
        self.upgrade_now_btn = QPushButton("Upgrade All (0)")
        self.upgrade_now_btn.setIcon(QIcon.fromTheme("system-software-update") or QIcon.fromTheme("emblem-default"))
        self.upgrade_now_btn.setObjectName("ApplyButton")
        self.upgrade_now_btn.setToolTip("Execute a full system upgrade (dnf upgrade) via Polkit elevation")
        self.upgrade_now_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upgrade_now_btn.setVisible(False)
        self.upgrade_now_btn.clicked.connect(self.upgrade_system_clicked.emit)

        # Batch clean action for unneeded leaf dependencies
        self.clean_orphans_btn = QPushButton("Clean Leaf Orphans")
        self.clean_orphans_btn.setIcon(QIcon.fromTheme("edit-clear") or QIcon.fromTheme("user-trash"))
        self.clean_orphans_btn.setObjectName("UpdatesIndicatorBtn")
        self.clean_orphans_btn.setToolTip("Queue all unneeded leaf dependencies for safe removal (dnf autoremove)")
        self.clean_orphans_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clean_orphans_btn.setVisible(False)
        self.clean_orphans_btn.clicked.connect(self.clean_orphans_clicked.emit)

        # ---------------------------------------------------------------------
        # 3. Secondary Tool Cluster (Unified Compact Action Bar)
        # ---------------------------------------------------------------------
        tool_cluster = QWidget()
        cluster_layout = QHBoxLayout(tool_cluster)
        cluster_layout.setContentsMargins(0, 0, 0, 0)
        cluster_layout.setSpacing(4)

        self.reload_btn = QPushButton("Reload")
        self.reload_btn.setIcon(QIcon.fromTheme("view-refresh"))
        self.reload_btn.setObjectName("HeaderToolBtn")
        self.reload_btn.setToolTip("Reload and re-index system RPM database (Ctrl+R)")
        self.reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reload_btn.clicked.connect(self.reload_clicked.emit)

        self.history_btn = QPushButton("History")
        self.history_btn.setIcon(QIcon.fromTheme("document-open-recent") or QIcon.fromTheme("view-history"))
        self.history_btn.setObjectName("HeaderToolBtn")
        self.history_btn.setToolTip("View DNF transaction history and rollback operations (Ctrl+H)")
        self.history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_btn.clicked.connect(self.history_clicked.emit)

        self.repos_btn = QPushButton("Repos")
        self.repos_btn.setIcon(QIcon.fromTheme("system-software-install") or QIcon.fromTheme("software-properties"))
        self.repos_btn.setObjectName("HeaderToolBtn")
        self.repos_btn.setToolTip("Manage software repositories, RPM Fusion & COPR channels")
        self.repos_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.repos_btn.clicked.connect(self.repos_clicked.emit)

        self.theme_btn = QPushButton("Theme")
        self.theme_btn.setIcon(QIcon.fromTheme("preferences-desktop-theme") or QIcon.fromTheme("color-management"))
        self.theme_btn.setObjectName("HeaderToolBtn")
        self.theme_btn.setToolTip("Select application theme")
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

        self.inspector_btn = QPushButton("Details")
        self.inspector_btn.setIcon(QIcon.fromTheme("document-properties") or QIcon.fromTheme("dialog-information"))
        self.inspector_btn.setObjectName("HeaderToolBtn")
        self.inspector_btn.setToolTip("Toggle package detail inspector panel (Ctrl+I)")
        self.inspector_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inspector_btn.clicked.connect(self.toggle_inspector_clicked.emit)

        cluster_layout.addWidget(self.reload_btn)
        cluster_layout.addWidget(self.history_btn)
        cluster_layout.addWidget(self.repos_btn)
        cluster_layout.addWidget(self.theme_btn)
        cluster_layout.addWidget(self.inspector_btn)

        # Hairline vertical separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("background-color: transparent; border: none; border-left: 1px solid rgba(127,127,127,0.2);")
        sep.setFixedHeight(24)

        # ---------------------------------------------------------------------
        # 4. Primary Transaction Button (High Visual Weight)
        # ---------------------------------------------------------------------
        self.apply_btn = QPushButton("Apply Changes (0)")
        self.apply_btn.setIcon(QIcon.fromTheme("emblem-default") or QIcon.fromTheme("dialog-ok-apply"))
        self.apply_btn.setObjectName("ApplyButton")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_btn.clicked.connect(self.apply_clicked.emit)

        # Compose Layout
        main_layout.addWidget(search_container, stretch=1)
        main_layout.addWidget(self.updates_btn)
        main_layout.addWidget(self.upgrade_now_btn)
        main_layout.addWidget(self.clean_orphans_btn)
        main_layout.addWidget(tool_cluster)
        main_layout.addWidget(sep)
        main_layout.addWidget(self.apply_btn)

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
        """Shows or updates the dedicated review pill and 1-click upgrade button."""
        if count > 0:
            self.updates_btn.setText(f"Updates ({count})")
            self.updates_btn.setVisible(True)
            self.upgrade_now_btn.setText(f"Upgrade All ({count})")
            self.upgrade_now_btn.setVisible(True)
        else:
            self.updates_btn.setVisible(False)
            self.upgrade_now_btn.setVisible(False)

    def set_orphan_clean_visible(self, visible: bool, count: int = 0):
        """Displays the 'Clean Leaf Orphans' shortcut when viewing the orphans category."""
        if visible and count > 0:
            self.clean_orphans_btn.setText(f"Clean Orphans ({count})")
            self.clean_orphans_btn.setVisible(True)
        else:
            self.clean_orphans_btn.setVisible(False)

    def set_active_theme(self, theme_key: str):
        """Updates the checkmark in the theme menu to reflect the active theme."""
        for action in self.theme_action_group.actions():
            if action.data() == theme_key:
                action.setChecked(True)
                break
