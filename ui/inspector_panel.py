# dendro/ui/inspector_panel.py
"""
Side inspector panel displaying package details:
Features dynamic theme-aware styling, AI classification insights,
confidence badges, metadata grids, user-installed provenance,
file manifests, and reverse dependencies.
"""
from __future__ import annotations

import os
from typing import Dict, Final, List, Optional
from PyQt6.QtCore import QSize, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QIcon
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.backend import DependencyNode, PackageFileInfo, PackageInfo, PackageState


CATEGORY_PRETTY_NAMES: Final[Dict[str, str]] = {
    "desktop_app": "Desktop Application",
    "cli_tool": "Command-Line Utility",
    "system_settings": "System Settings & Applet",
    "graphics_driver": "Graphics & 3D Acceleration",
    "audio_sound": "Audio Architecture & Sound",
    "kernel_module": "Kernel / DKMS Module",
    "firmware": "Hardware Microcode & Firmware",
    "fedora_core": "Fedora Base Infrastructure",
    "systemd_service": "Systemd Daemon & Service",
    "security_pkg": "Security, PAM & SELinux",
    "media_plugin": "Codec & Media Plugin",
    "desktop_addon": "Desktop Addon & Worker",
    "gui_toolkit": "GUI Framework & Toolkit",
    "c_lib": "C/C++ Shared Library",
    "devel": "Development Headers & SDK",
    "font": "Typography & Font Asset",
    "locale": "Localization & Translations",
    "theme": "Themes, Icons & Wallpapers",
}


class PackageInspectorPanel(QWidget):
    """
    Side panel displaying package details:
    Features dynamic theme-aware styling, AI classification insights,
    confidence badges, metadata grids, file manifests, and reverse dependencies.
    """

    package_action_requested = pyqtSignal(str)
    reverse_deps_requested = pyqtSignal(str)
    file_inspection_requested = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("InspectorPanel")
        self.setMinimumWidth(360)
        self._current_package: Optional[PackageInfo] = None
        self._all_files: List[PackageFileInfo] = []

        self._init_ui()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 12, 14, 12)
        main_layout.setSpacing(12)

        # ---------------------------------------------------------------------
        # 1. Header (Title, Version, Close button)
        # ---------------------------------------------------------------------
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        self.pkg_name_label = QLabel("Package Details")
        self.pkg_name_label.setObjectName("InspectorPkgTitle")
        self.pkg_name_label.setStyleSheet("font-size: 16px; font-weight: 800;")
        self.pkg_name_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("InspectorCloseBtn")
        self.close_btn.setFixedSize(26, 26)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.closed.emit)

        header_layout.addWidget(self.pkg_name_label, stretch=1)
        header_layout.addWidget(self.close_btn)
        main_layout.addLayout(header_layout)

        self.summary_label = QLabel("Select a package to inspect full metadata.")
        self.summary_label.setObjectName("InspectorSummary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("font-size: 12px;")
        main_layout.addWidget(self.summary_label)

        # ---------------------------------------------------------------------
        # 2. Action Toolbar
        # ---------------------------------------------------------------------
        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)

        self.queue_btn = QPushButton("Queue Action")
        self.queue_btn.setObjectName("InspectorQueueBtn")
        self.queue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.queue_btn.clicked.connect(self._on_queue_btn_clicked)

        self.copy_btn = QPushButton(" Copy")
        self.copy_btn.setIcon(QIcon.fromTheme("edit-copy"))
        self.copy_btn.setToolTip("Copy package name to clipboard")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.clicked.connect(self._copy_package_name)

        self.url_btn = QPushButton(" Homepage")
        self.url_btn.setIcon(QIcon.fromTheme("applications-internet") or QIcon.fromTheme("browser"))
        self.url_btn.setToolTip("Open official project website")
        self.url_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.url_btn.clicked.connect(self._open_project_url)

        action_layout.addWidget(self.queue_btn, stretch=2)
        action_layout.addWidget(self.copy_btn, stretch=1)
        action_layout.addWidget(self.url_btn, stretch=1)
        main_layout.addLayout(action_layout)

        # ---------------------------------------------------------------------
        # 3. Quick Stats Grid (3x2 Expanded)
        # ---------------------------------------------------------------------
        stats_frame = QFrame()
        stats_frame.setObjectName("StatsFrame")
        stats_layout = QGridLayout(stats_frame)
        stats_layout.setContentsMargins(8, 8, 8, 8)
        stats_layout.setSpacing(6)

        self.lbl_size = QLabel("Size: -")
        self.lbl_size.setObjectName("StatSizeLabel")

        self.lbl_arch = QLabel("Arch: -")
        self.lbl_arch.setObjectName("StatArchLabel")

        self.lbl_license = QLabel("License: -")
        self.lbl_license.setObjectName("StatLicenseLabel")

        self.lbl_repo = QLabel("Repo: -")
        self.lbl_repo.setObjectName("StatRepoLabel")

        self.lbl_install_type = QLabel("Installed: -")
        self.lbl_install_type.setObjectName("StatSizeLabel")

        self.lbl_packager_brief = QLabel("State: -")
        self.lbl_packager_brief.setObjectName("StatRepoLabel")

        stats_layout.addWidget(self.lbl_size, 0, 0)
        stats_layout.addWidget(self.lbl_arch, 0, 1)
        stats_layout.addWidget(self.lbl_license, 1, 0)
        stats_layout.addWidget(self.lbl_repo, 1, 1)
        stats_layout.addWidget(self.lbl_install_type, 2, 0)
        stats_layout.addWidget(self.lbl_packager_brief, 2, 1)

        main_layout.addWidget(stats_frame)

        # ---------------------------------------------------------------------
        # 4. Detail Tabs
        # ---------------------------------------------------------------------
        self.tabs = QTabWidget()
        self.tabs.setObjectName("InspectorTabs")

        # Tab 1: Overview & AI Insights
        self.tab_overview = QWidget()
        self._init_overview_tab()
        self.tabs.addTab(self.tab_overview, "Overview")

        # Tab 2: Files Manifest
        self.tab_files = QWidget()
        self._init_files_tab()
        self.tabs.addTab(self.tab_files, "Files")

        # Tab 3: Reverse Dependents
        self.tab_reverse = QWidget()
        self._init_reverse_tab()
        self.tabs.addTab(self.tab_reverse, "Required By")

        main_layout.addWidget(self.tabs, stretch=1)

    def _init_overview_tab(self):
        layout = QVBoxLayout(self.tab_overview)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        # Intelligent Categorization Card
        self.ai_card = QFrame()
        self.ai_card.setObjectName("AICard")
        ai_layout = QVBoxLayout(self.ai_card)
        ai_layout.setContentsMargins(8, 6, 8, 6)
        ai_layout.setSpacing(4)

        header_row = QHBoxLayout()
        self.ai_category_badge = QLabel("Category: Unknown")
        self.ai_category_badge.setObjectName("AICategoryBadge")

        self.ai_confidence_badge = QLabel("Confidence: 0%")
        self.ai_confidence_badge.setObjectName("AIConfidenceBadge")

        header_row.addWidget(self.ai_category_badge, stretch=1)
        header_row.addWidget(self.ai_confidence_badge)
        ai_layout.addLayout(header_row)

        self.ai_rationale_label = QLabel("Classification rationale will appear here.")
        self.ai_rationale_label.setObjectName("AIRationaleLabel")
        self.ai_rationale_label.setWordWrap(True)
        ai_layout.addWidget(self.ai_rationale_label)

        layout.addWidget(self.ai_card)

        # Detailed description
        self.desc_text = QTextEdit()
        self.desc_text.setObjectName("InspectorDescText")
        self.desc_text.setReadOnly(True)
        layout.addWidget(self.desc_text, stretch=1)

        self.packager_label = QLabel("Packager: -")
        self.packager_label.setObjectName("InspectorPackagerLabel")
        self.packager_label.setWordWrap(True)
        layout.addWidget(self.packager_label)

    def _init_files_tab(self):
        layout = QVBoxLayout(self.tab_files)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        self.file_search_input = QLineEdit()
        self.file_search_input.setPlaceholderText("Filter installed files (/bin, /etc, ...)")
        self.file_search_input.setClearButtonEnabled(True)
        self.file_search_input.textChanged.connect(self._filter_files_view)
        layout.addWidget(self.file_search_input)

        self.files_table = QTableWidget(0, 2)
        self.files_table.setObjectName("InspectorFilesTable")
        self.files_table.setHorizontalHeaderLabels(["File Path", "Size"])
        self.files_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.files_table.verticalHeader().setVisible(False)
        self.files_table.setShowGrid(False)
        self.files_table.setStyleSheet("QTableWidget::item { padding: 4px 6px; }")
        layout.addWidget(self.files_table, stretch=1)

    def _init_reverse_tab(self):
        layout = QVBoxLayout(self.tab_reverse)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        btn_layout = QHBoxLayout()
        self.rev_status_label = QLabel("Packages requiring this package:")
        self.rev_status_label.setStyleSheet("font-size: 11px;")

        self.btn_refresh_rev = QPushButton(" Re-Scan")
        self.btn_refresh_rev.setIcon(QIcon.fromTheme("view-refresh"))
        self.btn_refresh_rev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_rev.clicked.connect(self._request_reverse_deps)

        btn_layout.addWidget(self.rev_status_label, stretch=1)
        btn_layout.addWidget(self.btn_refresh_rev)
        layout.addLayout(btn_layout)

        self.reverse_list = QListWidget()
        self.reverse_list.setObjectName("InspectorReverseList")
        self.reverse_list.setStyleSheet("QListWidget::item { padding: 6px 8px; }")
        layout.addWidget(self.reverse_list, stretch=1)

    # -------------------------------------------------------------------------
    # Population & State Handlers
    # -------------------------------------------------------------------------
    def set_package_info(self, pkg: PackageInfo):
        """Populates the panel with detailed metadata and AI classification insights."""
        self._current_package = pkg

        # Header titles
        self.pkg_name_label.setText(f"{pkg.name} {pkg.version}")
        self.summary_label.setText(pkg.summary or "No summary provided.")

        # Friendly Ontology Title
        cat_key = pkg.primary_category
        cat_title = CATEGORY_PRETTY_NAMES.get(cat_key, cat_key.replace("_", " ").title())
        if pkg.secondary_tags:
            cat_title += f" [{', '.join(pkg.secondary_tags)}]"

        self.ai_category_badge.setText(f"🎯 {cat_title}")
        self.ai_confidence_badge.setText(f"Confidence: {int(pkg.classification_confidence * 100)}%")

        if pkg.classification_rationale:
            bullets = "\n".join(f"• {reason}" for reason in pkg.classification_rationale[:3])
            self.ai_rationale_label.setText(bullets)
        else:
            self.ai_rationale_label.setText("Standard category assignment.")

        self.desc_text.setPlainText(pkg.description or pkg.summary or "No detailed description available.")

        # Stats Cards
        self.lbl_size.setText(f"Size: {pkg.human_size}")
        self.lbl_arch.setText(f"Arch: {pkg.arch}")
        self.lbl_license.setText(f"License: {pkg.license or 'Unknown'}")
        self.lbl_repo.setText(f"Repo: {pkg.repository}")

        # Provenance / User-Installed Status
        if pkg.state == PackageState.INSTALLED:
            if pkg.is_user_installed:
                self.lbl_install_type.setText("Type: Explicit (User)")
            elif pkg.is_orphan:
                self.lbl_install_type.setText("Type: Orphan (Leaf)")
            else:
                self.lbl_install_type.setText("Type: Dependency (Auto)")
            self.lbl_packager_brief.setText("State: Installed")
        elif pkg.state == PackageState.AVAILABLE:
            self.lbl_install_type.setText("Type: Remote Package")
            self.lbl_packager_brief.setText("State: Available")
        elif pkg.state in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE):
            self.lbl_install_type.setText("Type: Staged Change")
            self.lbl_packager_brief.setText("State: Queued")

        self.packager_label.setText(f"Packager: {pkg.packager or pkg.vendor or 'Unknown'}\nBuild Date: {pkg.build_time or 'Unknown'}")

        # Dynamic Action Button State
        if pkg.state == PackageState.INSTALLED:
            self.queue_btn.setText("Queue Removal")
            self.queue_btn.setProperty("queueState", "installed")
        elif pkg.state == PackageState.QUEUED_REMOVE:
            self.queue_btn.setText("Cancel Removal")
            self.queue_btn.setProperty("queueState", "queued_remove")
        elif pkg.state == PackageState.AVAILABLE:
            self.queue_btn.setText("Queue Install")
            self.queue_btn.setProperty("queueState", "available")
        elif pkg.state == PackageState.QUEUED_INSTALL:
            self.queue_btn.setText("Cancel Install")
            self.queue_btn.setProperty("queueState", "queued_install")

        self.queue_btn.style().unpolish(self.queue_btn)
        self.queue_btn.style().polish(self.queue_btn)

        self.url_btn.setEnabled(bool(pkg.url))

        # Reset lists
        self.files_table.setRowCount(0)
        self.reverse_list.clear()
        self.rev_status_label.setText("Click 'Re-Scan' to query dependents.")

        # Request file loading
        self.file_inspection_requested.emit(pkg.name)

    @pyqtSlot(str, list)
    def set_package_files(self, pkg_name: str, files: List[PackageFileInfo]):
        if not self._current_package or self._current_package.name != pkg_name:
            return

        self._all_files = files
        self._populate_files_table(files)

    def _populate_files_table(self, files: List[PackageFileInfo]):
        self.files_table.setRowCount(len(files))
        for row, f in enumerate(files):
            # Rich iconography based on file nature
            if f.is_dir:
                prefix = "📁 "
            elif f.is_executable:
                prefix = "⚙️ "
            elif f.is_config:
                prefix = "📄 "
            elif f.path.endswith(".so") or ".so." in f.path:
                prefix = "📚 "
            else:
                prefix = "   "

            path_item = QTableWidgetItem(f"{prefix}{f.path}")

            size_str = f"{f.size_bytes / 1024:.1f} KB" if f.size_bytes > 0 else ""
            size_item = QTableWidgetItem(size_str)
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            self.files_table.setItem(row, 0, path_item)
            self.files_table.setItem(row, 1, size_item)

    def _filter_files_view(self, query: str):
        query = query.strip().lower()
        if not query:
            self._populate_files_table(self._all_files)
            return

        filtered = [f for f in self._all_files if query in f.path.lower()]
        self._populate_files_table(filtered)

    @pyqtSlot(str, list)
    def set_reverse_dependencies(self, pkg_name: str, reverse_deps: List[DependencyNode]):
        if not self._current_package or self._current_package.name != pkg_name:
            return

        self.reverse_list.clear()
        if not reverse_deps:
            self.rev_status_label.setText("No other packages depend on this package (Safe to remove).")
            item = QListWidgetItem("No dependents found (Leaf / Standalone)")
            item.setIcon(QIcon.fromTheme("emblem-ok-symbolic") or QIcon.fromTheme("dialog-ok"))
            self.reverse_list.addItem(item)
            return

        self.rev_status_label.setText(f"Found {len(reverse_deps)} packages depending on this:")
        for dep in reverse_deps:
            item = QListWidgetItem(f" {dep.resolved_package_name}")
            item.setIcon(QIcon.fromTheme("package-x-generic") or QIcon.fromTheme("system-software-install"))
            self.reverse_list.addItem(item)

    # -------------------------------------------------------------------------
    # Interactions
    # -------------------------------------------------------------------------
    def _on_queue_btn_clicked(self):
        if self._current_package:
            self.package_action_requested.emit(self._current_package.name)

    def _copy_package_name(self):
        if self._current_package:
            clipboard = QGuiApplication.clipboard()
            if clipboard:
                clipboard.setText(self._current_package.name)

    def _open_project_url(self):
        if self._current_package and self._current_package.url:
            QDesktopServices.openUrl(QUrl(self._current_package.url))

    def _request_reverse_deps(self):
        if self._current_package:
            self.rev_status_label.setText("Querying reverse dependencies...")
            self.reverse_deps_requested.emit(self._current_package.name)
