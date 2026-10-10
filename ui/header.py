# dendro/ui/header.py
"""
Top toolbar header container:
Features an integrated search bar with a shortcut hint chip, dedicated update review pill,
direct 1-click system upgrade action, orphan cleanup action, grouped secondary tool cluster,
theme picker, inspector toggle, and primary CTA controls.
Exclusively utilizes native FreeDesktop vector icons with zero font emoji glyphs.
"""
from __future__ import annotations

import math
from typing import Optional
from PyQt6.QtCore import QPointF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
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


def _find_theme_icon(*names: str) -> QIcon:
    """Finds the first non-null QIcon from the candidate theme icon names."""
    for name in names:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    return QIcon()


def _create_theme_toggle_icon(is_dark: bool) -> QIcon:
    """
    Renders resolution-independent vector Sun/Moon icons directly via QPainterPath.
    Completely avoids dark-on-dark Adwaita symbolic SVG bugs (#2e3436) and external
    icon theme dependencies while guaranteeing high contrast in both themes.
    """
    size = 40
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(2.0)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    center = QPointF(10.0, 10.0)

    if is_dark:
        # Sun icon (amber/gold) displayed in dark mode for switching to light mode
        color = QColor("#fab387")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        painter.drawEllipse(center, 3.6, 3.6)

        pen = QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        for i in range(8):
            angle = i * (math.pi / 4.0)
            x1 = center.x() + 5.6 * math.cos(angle)
            y1 = center.y() + 5.6 * math.sin(angle)
            x2 = center.x() + 8.2 * math.cos(angle)
            y2 = center.y() + 8.2 * math.sin(angle)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    else:
        # Moon icon (indigo/blue) displayed in light mode for switching to dark mode
        color = QColor("#1e66f5")
        outer = QPainterPath()
        outer.addEllipse(center, 6.2, 6.2)

        cutout = QPainterPath()
        cutout.addEllipse(QPointF(center.x() + 2.6, center.y() - 2.4), 5.4, 5.4)

        crescent = outer.subtracted(cutout)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        painter.drawPath(crescent)

    painter.end()
    return QIcon(pixmap)


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
    theme_toggle_clicked = pyqtSignal()
    repos_clicked = pyqtSignal()
    updates_clicked = pyqtSignal()
    upgrade_system_clicked = pyqtSignal()
    clean_orphans_clicked = pyqtSignal()
    refresh_updates_clicked = pyqtSignal()
    toggle_sidebar_clicked = pyqtSignal()
    clear_filter_clicked = pyqtSignal()
    remote_search_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("HeaderContainer")
        self._init_ui()
        self._setup_debounce()

    def _init_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(14, 8, 14, 8)
        main_layout.setSpacing(8)

        # Sidebar Toggle Button (Left of Search Bar)
        self.sidebar_toggle_btn = QPushButton()
        self.sidebar_toggle_btn.setObjectName("HeaderToolBtn")
        self.sidebar_toggle_btn.setFixedSize(34, 32)
        sidebar_icon = _find_theme_icon("view-left-pane", "sidebar-show", "format-indent-more")
        if not sidebar_icon.isNull():
            self.sidebar_toggle_btn.setIcon(sidebar_icon)
        else:
            self.sidebar_toggle_btn.setText("||")
        self.sidebar_toggle_btn.setToolTip("Toggle category sidebar (Ctrl+B)")
        self.sidebar_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sidebar_toggle_btn.clicked.connect(self.toggle_sidebar_clicked.emit)
        main_layout.addWidget(self.sidebar_toggle_btn)

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

        search_icon = _find_theme_icon("system-search", "edit-find")
        if not search_icon.isNull():
            self.search_input.addAction(search_icon, QLineEdit.ActionPosition.LeadingPosition)

        # Trailing action: Click or press Enter to search remote repositories on demand
        repo_search_icon = _find_theme_icon("system-software-install", "system-search")
        self.remote_search_action = QAction(repo_search_icon, "Search remote repositories (Enter)", self)
        self.remote_search_action.setToolTip("Search remote Fedora repositories to install new packages (Press Enter)")
        self.remote_search_action.triggered.connect(lambda: self.remote_search_requested.emit(self.search_input.text()))
        self.search_input.addAction(self.remote_search_action, QLineEdit.ActionPosition.TrailingPosition)
        self.search_input.returnPressed.connect(lambda: self.remote_search_requested.emit(self.search_input.text()))

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
        # Updates Indicator (Clicking switches to the Updates tab)
        self.updates_btn = QPushButton("Updates (0)")
        updates_icon = _find_theme_icon("software-update-available", "system-software-update")
        if not updates_icon.isNull():
            self.updates_btn.setIcon(updates_icon)
        self.updates_btn.setObjectName("UpdatesIndicatorBtn")
        self.updates_btn.setToolTip("View list of available package updates")
        self.updates_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.updates_btn.setVisible(False)
        self.updates_btn.clicked.connect(self.updates_clicked.emit)

        # Contextual Action: "Back to All Packages" (Visible when viewing filtered updates)
        self.back_to_all_btn = QPushButton("Show All Packages")
        back_icon = _find_theme_icon("go-home", "view-list-tree")
        if not back_icon.isNull():
            self.back_to_all_btn.setIcon(back_icon)
        self.back_to_all_btn.setObjectName("HeaderToolBtn")
        self.back_to_all_btn.setToolTip("Exit updates view and return to all packages")
        self.back_to_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_to_all_btn.setVisible(False)
        self.back_to_all_btn.clicked.connect(self.clear_filter_clicked.emit)

        # Contextual Action: "Upgrade System" (Only visible when viewing Available Updates)
        self.upgrade_context_btn = QPushButton("Upgrade All Packages")
        upgrade_icon = _find_theme_icon("system-software-update", "emblem-default")
        if not upgrade_icon.isNull():
            self.upgrade_context_btn.setIcon(upgrade_icon)
        self.upgrade_context_btn.setObjectName("ApplyButton")
        self.upgrade_context_btn.setToolTip("Perform a complete system upgrade (dnf upgrade)")
        self.upgrade_context_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upgrade_context_btn.setVisible(False)
        self.upgrade_context_btn.clicked.connect(self.upgrade_system_clicked.emit)

        # Contextual Action: "Clean Orphans" (Only visible when viewing Orphan Packages)
        self.clean_orphans_btn = QPushButton("Clean Leaf Orphans")
        orphans_icon = _find_theme_icon("edit-clear", "user-trash")
        if not orphans_icon.isNull():
            self.clean_orphans_btn.setIcon(orphans_icon)
        self.clean_orphans_btn.setObjectName("UpdatesIndicatorBtn")
        self.clean_orphans_btn.setToolTip("Remove all unneeded leaf dependencies (dnf autoremove)")
        self.clean_orphans_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clean_orphans_btn.setVisible(False)
        self.clean_orphans_btn.clicked.connect(self.clean_orphans_clicked.emit)

        # Contextual Action: "Live Refresh" (Bypasses cache using --refresh)
        self.refresh_live_btn = QPushButton("Check Mirrors (--refresh)")
        refresh_icon = _find_theme_icon("view-refresh")
        if not refresh_icon.isNull():
            self.refresh_live_btn.setIcon(refresh_icon)
        self.refresh_live_btn.setObjectName("HeaderToolBtn")
        self.refresh_live_btn.setToolTip("Bypass local cache and query remote mirrors for the freshest updates")
        self.refresh_live_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_live_btn.setVisible(False)
        self.refresh_live_btn.clicked.connect(self.refresh_updates_clicked.emit)

        # ---------------------------------------------------------------------
        # 3. Secondary Tool Cluster (Unified Compact Action Bar)
        # ---------------------------------------------------------------------
        tool_cluster = QWidget()
        cluster_layout = QHBoxLayout(tool_cluster)
        cluster_layout.setContentsMargins(0, 0, 0, 0)
        cluster_layout.setSpacing(4)

        self.reload_btn = QPushButton("Reload")
        reload_icon = _find_theme_icon("view-refresh")
        if not reload_icon.isNull():
            self.reload_btn.setIcon(reload_icon)
        self.reload_btn.setObjectName("HeaderToolBtn")
        self.reload_btn.setToolTip("Reload and re-index system RPM database (Ctrl+R)")
        self.reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reload_btn.clicked.connect(self.reload_clicked.emit)

        self.history_btn = QPushButton("History")
        history_icon = _find_theme_icon("document-open-recent", "view-history")
        if not history_icon.isNull():
            self.history_btn.setIcon(history_icon)
        self.history_btn.setObjectName("HeaderToolBtn")
        self.history_btn.setToolTip("View DNF transaction history and rollback operations (Ctrl+H)")
        self.history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_btn.clicked.connect(self.history_clicked.emit)

        self.repos_btn = QPushButton("Repos")
        repos_icon = _find_theme_icon("system-software-install", "software-properties")
        if not repos_icon.isNull():
            self.repos_btn.setIcon(repos_icon)
        self.repos_btn.setObjectName("HeaderToolBtn")
        self.repos_btn.setToolTip("Manage software repositories, RPM Fusion & COPR channels")
        self.repos_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.repos_btn.clicked.connect(self.repos_clicked.emit)

        # 1-Click Dark / Light Mode Toggle Button
        self.theme_toggle_btn = QPushButton()
        self.theme_toggle_btn.setObjectName("HeaderToolBtn")
        self.theme_toggle_btn.setFixedSize(34, 32)
        self.theme_toggle_btn.setIconSize(QSize(18, 18))
        self.theme_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_toggle_btn.clicked.connect(self.theme_toggle_clicked.emit)

        self.inspector_btn = QPushButton("Details")
        inspector_icon = _find_theme_icon("document-properties", "dialog-information")
        if not inspector_icon.isNull():
            self.inspector_btn.setIcon(inspector_icon)
        self.inspector_btn.setObjectName("HeaderToolBtn")
        self.inspector_btn.setToolTip("Toggle package detail inspector panel (Ctrl+I)")
        self.inspector_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inspector_btn.clicked.connect(self.toggle_inspector_clicked.emit)

        cluster_layout.addWidget(self.reload_btn)
        cluster_layout.addWidget(self.history_btn)
        cluster_layout.addWidget(self.repos_btn)
        cluster_layout.addWidget(self.theme_toggle_btn)
        cluster_layout.addWidget(self.inspector_btn)

        # Hairline vertical separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("background-color: transparent; border: none; border-left: 1px solid rgba(127,127,127,0.2);")
        sep.setFixedHeight(24)

        # ---------------------------------------------------------------------
        # 4. Transaction Actions (Discard & Apply)
        # ---------------------------------------------------------------------
        # 1-Click Discard Queue Button (Only appears when changes are staged)
        self.discard_btn = QPushButton("Discard")
        discard_icon = _find_theme_icon("edit-undo", "dialog-cancel")
        if not discard_icon.isNull():
            self.discard_btn.setIcon(discard_icon)
        self.discard_btn.setObjectName("HeaderToolBtn")
        self.discard_btn.setStyleSheet("color: #f38ba8;")
        self.discard_btn.setToolTip("Discard and cancel all staged pending changes")
        self.discard_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discard_btn.setVisible(False)

        # Primary Apply Transaction Button
        self.apply_btn = QPushButton("Apply Changes (0)")
        apply_icon = _find_theme_icon("emblem-default", "dialog-ok-apply")
        if not apply_icon.isNull():
            self.apply_btn.setIcon(apply_icon)
        self.apply_btn.setObjectName("ApplyButton")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_btn.clicked.connect(self.apply_clicked.emit)

        # Compose Layout
        main_layout.addWidget(search_container, stretch=1)
        main_layout.addWidget(self.updates_btn)
        main_layout.addWidget(self.back_to_all_btn)
        main_layout.addWidget(self.upgrade_context_btn)
        main_layout.addWidget(self.refresh_live_btn)
        main_layout.addWidget(self.clean_orphans_btn)
        main_layout.addWidget(tool_cluster)
        main_layout.addWidget(sep)
        main_layout.addWidget(self.discard_btn)
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
        self.discard_btn.setVisible(count > 0)

    def update_available_updates_badge(self, count: int):
        """Shows or updates the subtle header updates indicator pill."""
        if count > 0:
            self.updates_btn.setText(f"Updates ({count})")
            self.updates_btn.setVisible(True)
            self.upgrade_context_btn.setText(f"Upgrade All ({count})")
        else:
            self.updates_btn.setVisible(False)
            self.upgrade_context_btn.setVisible(False)

    def set_updates_view_active(self, active: bool, count: int = 0):
        """Displays 'Upgrade All Packages', 'Check Mirrors', and 'Show All Packages' when viewing updates."""
        self.back_to_all_btn.setVisible(active)
        self.upgrade_context_btn.setVisible(active and count > 0)
        self.refresh_live_btn.setVisible(active)

    def set_orphan_clean_visible(self, visible: bool, count: int = 0):
        """Displays the 'Clean Leaf Orphans' shortcut when viewing the orphans category."""
        if visible and count > 0:
            self.clean_orphans_btn.setText(f"Clean Orphans ({count})")
            self.clean_orphans_btn.setVisible(True)
        else:
            self.clean_orphans_btn.setVisible(False)

    def update_theme_toggle_icon(self, is_dark: bool):
        """Updates the theme toggle button icon and tooltip based on active theme state."""
        self.theme_toggle_btn.setIcon(_create_theme_toggle_icon(is_dark))
        if is_dark:
            self.theme_toggle_btn.setToolTip("Switch to light mode")
        else:
            self.theme_toggle_btn.setToolTip("Switch to dark mode")
