# dendro/ui/repo_dialog.py
"""
Software Repository & COPR Manager Dialog for Fedora Linux.
Provides clean toggling for Fedora Core, RPM Fusion, and COPR repositories,
plus one-click community COPR enablement.
"""
from __future__ import annotations

from typing import List, Optional
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.backend import RepoInfo, RepoManagerHelper


class RepoManagerDialog(QDialog):
    """
    Dialog for managing Fedora software repositories, enabling/disabling channels,
    and onboarding new COPR repositories via Polkit elevation.
    """

    repo_toggle_requested = pyqtSignal(str, bool)     # (repo_id, enable_boolean)
    enable_copr_requested = pyqtSignal(str)          # "user/project"

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Software Repositories & COPR Manager")
        self.resize(860, 540)
        self._repos: List[RepoInfo] = []
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # ---------------------------------------------------------------------
        # 1. Header & Title Bar
        # ---------------------------------------------------------------------
        header_bar = QHBoxLayout()
        title = QLabel("📦 System Software Repositories (/etc/yum.repos.d)")
        title.setStyleSheet("font-size: 15px; font-weight: bold; color: #89b4fa;")

        self.btn_refresh = QPushButton(" Refresh")
        self.btn_refresh.setIcon(QIcon.fromTheme("view-refresh"))
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.load_repositories)

        header_bar.addWidget(title, stretch=1)
        header_bar.addWidget(self.btn_refresh)
        layout.addLayout(header_bar)

        # ---------------------------------------------------------------------
        # 2. Add New COPR Repo Box
        # ---------------------------------------------------------------------
        copr_box = QFrame()
        copr_box.setStyleSheet("""
            QFrame {
                background-color: #181825;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 6px;
            }
        """)
        copr_layout = QHBoxLayout(copr_box)
        copr_layout.setContentsMargins(8, 4, 8, 4)
        copr_layout.setSpacing(8)

        copr_icon = QLabel("🏗️")
        copr_icon.setStyleSheet("font-size: 14px;")

        self.copr_input = QLineEdit()
        self.copr_input.setPlaceholderText("Enable Community COPR repository (e.g. user/project)...")
        self.copr_input.setStyleSheet("""
            QLineEdit {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 6px;
                padding: 6px 10px;
                color: #cdd6f4;
            }
            QLineEdit:focus {
                border: 1px solid #89b4fa;
            }
        """)
        self.copr_input.returnPressed.connect(self._on_enable_copr_clicked)

        self.copr_btn = QPushButton("Enable COPR")
        self.copr_btn.setObjectName("ApplyButton")
        self.copr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copr_btn.clicked.connect(self._on_enable_copr_clicked)

        copr_layout.addWidget(copr_icon)
        copr_layout.addWidget(self.copr_input, stretch=1)
        copr_layout.addWidget(self.copr_btn)
        layout.addWidget(copr_box)

        # ---------------------------------------------------------------------
        # 3. Live Filter Search Bar
        # ---------------------------------------------------------------------
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("🔍 Filter repositories by ID, name, or channel (e.g. fusion, testing)...")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.setStyleSheet("""
            QLineEdit {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 6px;
                padding: 6px 12px;
                color: #cdd6f4;
            }
        """)
        self.filter_input.textChanged.connect(self._filter_table)
        layout.addWidget(self.filter_input)

        # ---------------------------------------------------------------------
        # 4. Repository Management Table
        # ---------------------------------------------------------------------
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Status", "Repository ID", "Display Name", "Channel Origin"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(1, 230)
        self.table.setShowGrid(False)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 8px;
                color: #cdd6f4;
                font-size: 12px;
            }
            QTableWidget::item {
                padding: 6px 8px;
            }
        """)
        layout.addWidget(self.table, stretch=1)

        # ---------------------------------------------------------------------
        # 5. Bottom Action Bar
        # ---------------------------------------------------------------------
        bottom_bar = QHBoxLayout()
        info_lbl = QLabel("🔒 Enabling or disabling repositories requires administrative elevation.")
        info_lbl.setStyleSheet("color: #6c7086; font-size: 11px;")

        self.close_btn = QPushButton("Close")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.accept)

        bottom_bar.addWidget(info_lbl)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.close_btn)
        layout.addLayout(bottom_bar)

    def load_repositories(self):
        """Loads and parses all /etc/yum.repos.d/*.repo files."""
        self._repos = RepoManagerHelper.get_system_repositories()
        self._populate_table(self._repos)

    def _populate_table(self, repos: List[RepoInfo]):
        self.table.setRowCount(len(repos))

        for row, r in enumerate(repos):
            # Centered Enable/Disable Checkbox
            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.setContentsMargins(12, 0, 12, 0)
            chk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk = QCheckBox()
            chk.setChecked(r.enabled)
            chk.setCursor(Qt.CursorShape.PointingHandCursor)
            chk.toggled.connect(lambda state, rid=r.id: self.repo_toggle_requested.emit(rid, state))
            chk_layout.addWidget(chk)

            id_item = QTableWidgetItem(r.id)
            id_item.setFont(QFont("JetBrains Mono", 9, QFont.Weight.Bold))
            if not r.enabled:
                id_item.setForeground(QColor("#6c7086"))

            name_item = QTableWidgetItem(r.name)
            name_item.setToolTip(f"File: {r.repo_file}\nBaseURL: {r.baseurl or 'Mirrorlist'}")
            if not r.enabled:
                name_item.setForeground(QColor("#6c7086"))

            # Channel / Origin Badging
            if r.is_core:
                type_str = "Fedora Project"
                type_color = QColor("#89b4fa")
            elif r.is_rpmfusion:
                type_
