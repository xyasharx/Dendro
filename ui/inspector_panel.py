# dendro/ui/inspector_panel.py
from __future__ import annotations

import os
from typing import List, Optional
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


class PackageInspectorPanel(QWidget):
    """
    پنل جانبی بازرس جزئیات بسته
    شامل متادیتا، مدیریت صف، لیست فایل‌های نصب‌شده و وابستگی‌های معکوس
    """

    package_action_requested = pyqtSignal(str)       # درخواست تغییر وضعیت صف بسته
    reverse_deps_requested = pyqtSignal(str)         # درخواست محاسبه وابستگی‌های معکوس
    file_inspection_requested = pyqtSignal(str)      # درخواست استخراج فایل‌های بسته
    closed = pyqtSignal()                            # درخواست بستن پنل

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
        # ۱. هدر پنل (نام پکیج، وضعیت، دکمه بستن)
        # ---------------------------------------------------------------------
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        self.pkg_name_label = QLabel("Package Details")
        self.pkg_name_label.setObjectName("InspectorPkgTitle")
        self.pkg_name_label.setStyleSheet("font-size: 16px; font-weight: 800; color: #89b4fa;")
        self.pkg_name_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("InspectorCloseBtn")
        self.close_btn.setFixedSize(26, 26)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.closed.emit)

        header_layout.addWidget(self.pkg_name_label, stretch=1)
        header_layout.addWidget(self.close_btn)
        main_layout.addLayout(header_layout)

        # خلاصه کوتاه بسته
        self.summary_label = QLabel("Select a package to inspect full metadata.")
        self.summary_label.setObjectName("InspectorSummary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color: #a6adc8; font-size: 12px;")
        main_layout.addWidget(self.summary_label)

        # ---------------------------------------------------------------------
        # ۲. نوار ابزار اقدامات سریع (Action Bar)
        # ---------------------------------------------------------------------
        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)

        self.queue_btn = QPushButton("Queue Action")
        self.queue_btn.setObjectName("InspectorQueueBtn")
        self.queue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.queue_btn.clicked.connect(self._on_queue_btn_clicked)

        # دکمه کپی با آیکون سیستمی و فال‌بک متنی
        self.copy_btn = QPushButton(" Copy")
        self.copy_btn.setIcon(QIcon.fromTheme("edit-copy"))
        self.copy_btn.setToolTip("Copy package name to clipboard")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.clicked.connect(self._copy_package_name)

        # دکمه لینک سایت با آیکون وب
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
        # ۳. کارت‌های مشخصات کلیدی (Quick Stats Grid)
        # ---------------------------------------------------------------------
        stats_frame = QFrame()
        stats_frame.setObjectName("StatsFrame")
        stats_frame.setStyleSheet("""
            QFrame#StatsFrame {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 6px;
            }
            QLabel {
                font-size: 11px;
            }
        """)
        stats_layout = QGridLayout(stats_frame)
        stats_layout.setContentsMargins(8, 8, 8, 8)
        stats_layout.setSpacing(6)

        self.lbl_size = QLabel("Size: -")
        self.lbl_license = QLabel("License: -")
        self.lbl_repo = QLabel("Repo: -")
        self.lbl_arch = QLabel("Arch: -")

        self.lbl_size.setStyleSheet("color: #fab387; font-weight: bold;")
        self.lbl_license.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        self.lbl_repo.setStyleSheet("color: #89b4fa; font-weight: bold;")
        self.lbl_arch.setStyleSheet("color: #cba6f7; font-weight: bold;")

        stats_layout.addWidget(self.lbl_size, 0, 0)
        stats_layout.addWidget(self.lbl_arch, 0, 1)
        stats_layout.addWidget(self.lbl_license, 1, 0)
        stats_layout.addWidget(self.lbl_repo, 1, 1)

        main_layout.addWidget(stats_frame)

        # ---------------------------------------------------------------------
        # ۴. تب‌بندی اطلاعات عمیق (Tabs: Overview, Files, Reverse Deps)
        # ---------------------------------------------------------------------
        self.tabs = QTabWidget()
        self.tabs.setObjectName("InspectorTabs")

        # تب ۱: توضیحات و مشخصات سیستمی
        self.tab_overview = QWidget()
        self._init_overview_tab()
        self.tabs.addTab(self.tab_overview, "Overview")

        # تب ۲: لیست فایل‌های نصب‌شده
        self.tab_files = QWidget()
        self._init_files_tab()
        self.tabs.addTab(self.tab_files, "Files")

        # تب ۳: بسته‌های وابسته (Reverse Dependencies)
        self.tab_reverse = QWidget()
        self._init_reverse_tab()
        self.tabs.addTab(self.tab_reverse, "Required By")

        main_layout.addWidget(self.tabs, stretch=1)

    def _init_overview_tab(self):
        layout = QVBoxLayout(self.tab_overview)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        self.desc_text = QTextEdit()
        self.desc_text.setReadOnly(True)
        self.desc_text.setStyleSheet("""
            QTextEdit {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 6px;
                color: #cdd6f4;
                font-size: 12px;
                line-height: 1.4;
            }
        """)
        layout.addWidget(self.desc_text, stretch=1)

        self.packager_label = QLabel("Packager: -")
        self.packager_label.setStyleSheet("color: #6c7086; font-size: 11px;")
        self.packager_label.setWordWrap(True)
        layout.addWidget(self.packager_label)

    def _init_files_tab(self):
        layout = QVBoxLayout(self.tab_files)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        # جستجوی فایل در پکیج
        self.file_search_input = QLineEdit()
        self.file_search_input.setPlaceholderText("Filter installed files (/bin, /etc, ...)")
        self.file_search_input.setClearButtonEnabled(True)
        self.file_search_input.textChanged.connect(self._filter_files_view)
        layout.addWidget(self.file_search_input)

        # جدول فایل‌ها
        self.files_table = QTableWidget(0, 2)
        self.files_table.setHorizontalHeaderLabels(["File Path", "Size"])
        self.files_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.files_table.verticalHeader().setVisible(False)
        self.files_table.setShowGrid(False)
        self.files_table.setStyleSheet("""
            QTableWidget {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 6px;
                color: #cdd6f4;
                font-family: "JetBrains Mono", "Fira Code", "Noto Color Emoji", "Consolas", monospace;
                font-size: 11px;
            }
            QTableWidget::item {
                padding: 4px 6px;
            }
        """)
        layout.addWidget(self.files_table, stretch=1)

    def _init_reverse_tab(self):
        layout = QVBoxLayout(self.tab_reverse)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        btn_layout = QHBoxLayout()
        self.rev_status_label = QLabel("Packages requiring this package:")
        self.rev_status_label.setStyleSheet("color: #a6adc8; font-size: 11px;")

        self.btn_refresh_rev = QPushButton(" Re-Scan")
        self.btn_refresh_rev.setIcon(QIcon.fromTheme("view-refresh"))
        self.btn_refresh_rev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_rev.clicked.connect(self._request_reverse_deps)

        btn_layout.addWidget(self.rev_status_label, stretch=1)
        btn_layout.addWidget(self.btn_refresh_rev)
        layout.addLayout(btn_layout)

        self.reverse_list = QListWidget()
        self.reverse_list.setStyleSheet("""
            QListWidget {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 6px;
                color: #cdd6f4;
                font-size: 12px;
            }
            QListWidget::item {
                padding: 6px 8px;
                border-bottom: 1px solid #181825;
            }
        """)
        layout.addWidget(self.reverse_list, stretch=1)

    # -------------------------------------------------------------------------
    # متدهای بارگذاری اطلاعات
    # -------------------------------------------------------------------------
    def set_package_info(self, pkg: PackageInfo):
        """به‌روزرسانی پنل با مشخصات پکیج انتخاب‌شده"""
        self._current_package = pkg

        # هدر و متن‌ها
        self.pkg_name_label.setText(f"{pkg.name} {pkg.version}")
        self.summary_label.setText(pkg.summary or "No summary provided.")
        self.desc_text.setPlainText(pkg.description or pkg.summary or "No detailed description available.")

        # کارت‌های آمار
        self.lbl_size.setText(f"Size: {pkg.human_size}")
        self.lbl_arch.setText(f"Arch: {pkg.arch}")
        self.lbl_license.setText(f"License: {pkg.license or 'Unknown'}")
        self.lbl_repo.setText(f"Repo: {pkg.repository}")
        self.packager_label.setText(f"Packager: {pkg.packager or pkg.vendor or 'Unknown'}\nBuild Date: {pkg.build_time or 'Unknown'}")

        # تنظیم دکمه اکشن صف
        if pkg.state == PackageState.INSTALLED:
            self.queue_btn.setText("Queue Removal")
            self.queue_btn.setStyleSheet("background-color: #45252b; color: #eba0ac; font-weight: bold;")
        elif pkg.state == PackageState.QUEUED_REMOVE:
            self.queue_btn.setText("Cancel Removal")
            self.queue_btn.setStyleSheet("background-color: #313244; color: #fab387; font-weight: bold;")
        elif pkg.state == PackageState.AVAILABLE:
            self.queue_btn.setText("Queue Install")
            self.queue_btn.setStyleSheet("background-color: #1e3a2f; color: #a6e3a1; font-weight: bold;")
        elif pkg.state == PackageState.QUEUED_INSTALL:
            self.queue_btn.setText("Cancel Install")
            self.queue_btn.setStyleSheet("background-color: #313244; color: #fab387; font-weight: bold;")

        self.url_btn.setEnabled(bool(pkg.url))

        # ریست لیست فایل‌ها و وابستگی‌های معکوس
        self.files_table.setRowCount(0)
        self.reverse_list.clear()
        self.rev_status_label.setText("Click 'Re-Scan' to query dependents.")

        # درخواست بارگذاری فایل‌های بسته
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
            # پیشوند نوع فایل
            prefix = "📁 " if f.is_dir else ("⚙️ " if f.is_executable else ("📄 " if f.is_config else "  "))
            path_item = QTableWidgetItem(f"{prefix}{f.path}")
            
            size_str = f"{f.size_bytes / 1024:.1f} KB" if f.size_bytes > 0 else ""
            size_item = QTableWidgetItem(size_str)
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            if f.is_config:
                path_item.setForeground(QColor("#f9e2af"))
            elif f.is_executable:
                path_item.setForeground(QColor("#a6e3a1"))

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
            item.setForeground(QColor("#a6e3a1"))
            self.reverse_list.addItem(item)
            return

        self.rev_status_label.setText(f"Found {len(reverse_deps)} packages depending on this:")
        for dep in reverse_deps:
            item = QListWidgetItem(f" {dep.resolved_package_name}")
            item.setIcon(QIcon.fromTheme("package-x-generic") or QIcon.fromTheme("system-software-install"))
            item.setForeground(QColor("#cdd6f4"))
            self.reverse_list.addItem(item)

    # -------------------------------------------------------------------------
    # رویدادهای کلیک و تعامل
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
