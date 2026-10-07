# dendro/ui/repo_dialog.py
"""
Software Repository & COPR Manager Dialog for Fedora Linux.
Provides clean toggling for Fedora Core, RPM Fusion, and COPR repositories,
plus one-click community COPR enablement.
Uses native FreeDesktop theme icons.
"""
from __future__ import annotations

from typing import List, Optional
from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon
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
from ui.styles import get_delegate_palette


class RepoManagerDialog(QDialog):
    """
    Dialog for managing Fedora software repositories, enabling/disabling channels,
    cleaning package cache, removing third-party/COPR repos, and onboarding new COPRs.
    """

    repo_toggle_requested = pyqtSignal(str, bool)     # (repo_id, enable_boolean)
    repo_remove_requested = pyqtSignal(str, str)      # (repo_id, repo_file_path)
    enable_copr_requested = pyqtSignal(str)          # "user/project"
    clean_cache_requested = pyqtSignal()             # Trigger dnf clean all

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
        header_icon = QLabel()
        title_icon = QIcon.fromTheme("system-software-install") or QIcon.fromTheme("software-properties")
        if not title_icon.isNull():
            header_icon.setPixmap(title_icon.pixmap(20, 20))
            header_bar.addWidget(header_icon)

        title = QLabel("System Software Repositories")
        title.setStyleSheet("font-weight: bold;")

        self.btn_refresh = QPushButton(" Refresh")
        self.btn_refresh.setIcon(QIcon.fromTheme("view-refresh"))
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.load_repositories)

        header_bar.addWidget(title, stretch=1)
        header_bar.addWidget(self.btn_refresh)
        layout.addLayout(header_bar)

        # ---------------------------------------------------------------------
        # 2. Add New COPR Repo Box (Theme-aware #CoprBox)
        # ---------------------------------------------------------------------
        copr_box = QFrame()
        copr_box.setObjectName("CoprBox")
        copr_layout = QHBoxLayout(copr_box)
        copr_layout.setContentsMargins(8, 4, 8, 4)
        copr_layout.setSpacing(8)

        copr_icon = QLabel()
        copr_pix = QIcon.fromTheme("package-x-generic") or QIcon.fromTheme("applications-development")
        if not copr_pix.isNull():
            copr_icon.setPixmap(copr_pix.pixmap(18, 18))
            copr_layout.addWidget(copr_icon)

        self.copr_input = QLineEdit()
        self.copr_input.setObjectName("CoprInput")
        self.copr_input.setPlaceholderText("Enable Community COPR repository (e.g. user/project)...")
        self.copr_input.returnPressed.connect(self._on_enable_copr_clicked)

        self.copr_btn = QPushButton("Enable COPR")
        self.copr_btn.setIcon(QIcon.fromTheme("list-add") or QIcon.fromTheme("add"))
        self.copr_btn.setObjectName("ApplyButton")
        self.copr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copr_btn.clicked.connect(self._on_enable_copr_clicked)

        copr_layout.addWidget(self.copr_input, stretch=1)
        copr_layout.addWidget(self.copr_btn)
        layout.addWidget(copr_box)

        # ---------------------------------------------------------------------
        # 3. Live Filter Search Bar (Theme-aware #RepoFilterInput)
        # ---------------------------------------------------------------------
        self.filter_input = QLineEdit()
        self.filter_input.setObjectName("RepoFilterInput")
        self.filter_input.setPlaceholderText("Filter repositories by ID, name, or channel (e.g. fusion, testing)...")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._filter_table)
        layout.addWidget(self.filter_input)

        # ---------------------------------------------------------------------
        # 4. Repository Management Table (Theme-aware #RepoTable)
        # ---------------------------------------------------------------------
        self.table = QTableWidget(0, 4)
        self.table.setObjectName("RepoTable")
        self.table.setHorizontalHeaderLabels(["Status", "Repository ID", "Display Name", "Channel Origin"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(1, 230)
        self.table.setShowGrid(False)
        layout.addWidget(self.table, stretch=1)

        # ---------------------------------------------------------------------
        # 5. Bottom Action Bar (Cache Maintenance & Close)
        # ---------------------------------------------------------------------
        bottom_bar = QHBoxLayout()
        info_icon = QLabel()
        lock_pix = QIcon.fromTheme("dialog-information") or QIcon.fromTheme("security-medium")
        if not lock_pix.isNull():
            info_icon.setPixmap(lock_pix.pixmap(16, 16))
            bottom_bar.addWidget(info_icon)

        info_lbl = QLabel("Changes require administrative elevation.")
        info_lbl.setObjectName("InspectorPackagerLabel")

        # Cache cleanup maintenance button directly in Repositories dialog
        self.btn_clean_cache = QPushButton(" Clean Cache")
        self.btn_clean_cache.setIcon(QIcon.fromTheme("edit-clear") or QIcon.fromTheme("drive-harddisk"))
        self.btn_clean_cache.setToolTip("Free disk space by deleting expired metadata and downloaded RPMs (dnf clean all)")
        self.btn_clean_cache.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clean_cache.clicked.connect(self._on_clean_cache_clicked)

        # Remove third-party / COPR repository action
        self.btn_remove_repo = QPushButton(" Remove Repo")
        self.btn_remove_repo.setIcon(QIcon.fromTheme("list-remove") or QIcon.fromTheme("user-trash"))
        self.btn_remove_repo.setToolTip("Delete third-party or COPR repository configuration file")
        self.btn_remove_repo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_remove_repo.clicked.connect(self._on_remove_repo_clicked)

        self.close_btn = QPushButton("Close")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.accept)

        bottom_bar.addWidget(info_lbl)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.btn_clean_cache)
        bottom_bar.addWidget(self.btn_remove_repo)
        bottom_bar.addWidget(self.close_btn)
        layout.addLayout(bottom_bar)

    def load_repositories(self):
        """Loads and parses all /etc/yum.repos.d/*.repo files."""
        self._repos = RepoManagerHelper.get_system_repositories()
        self._populate_table(self._repos)

    def _populate_table(self, repos: List[RepoInfo]):
        settings = QSettings("FedoraCommunity", "Dendro")
        theme_choice = settings.value("theme", "auto", type=str)
        pal = get_delegate_palette(theme_choice)

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

            tooltip_text = f"Repo ID: {r.id}\nFile: {r.repo_file}\nBase URL: {r.baseurl or 'Mirrorlist / Metalink'}"

            mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            id_item = QTableWidgetItem(r.id)
            id_item.setFont(mono_font)
            id_item.setToolTip(tooltip_text)
            if not r.enabled:
                id_item.setForeground(pal["text_dim"])

            name_item = QTableWidgetItem(r.name)
            name_item.setToolTip(tooltip_text)
            if not r.enabled:
                name_item.setForeground(pal["text_dim"])

            # Channel / Origin Badging using theme palette
            if r.is_core:
                type_str = "Fedora Project"
                type_color = pal["accent"]
            elif r.is_rpmfusion:
                type_str = "RPM Fusion"
                type_color = pal["badge_fg_tag"]
            elif r.is_copr:
                type_str = "COPR Community"
                type_color = pal["badge_fg_installed"]
            else:
                type_str = "Third-Party"
                type_color = pal["badge_fg_queued_in"]

            type_item = QTableWidgetItem(type_str)
            type_item.setForeground(type_color)
            type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.table.setCellWidget(row, 0, chk_widget)
            self.table.setItem(row, 1, id_item)
            self.table.setItem(row, 2, name_item)
            self.table.setItem(row, 3, type_item)

    def _filter_table(self, query: str):
        query = query.strip().lower()
        for row in range(self.table.rowCount()):
            id_item = self.table.item(row, 1)
            name_item = self.table.item(row, 2)
            type_item = self.table.item(row, 3)

            matches = True
            if query:
                id_match = id_item and query in id_item.text().lower()
                name_match = name_item and query in name_item.text().lower()
                type_match = type_item and query in type_item.text().lower()
                matches = id_match or name_match or type_match

            self.table.setRowHidden(row, not matches)

    def _on_enable_copr_clicked(self):
        text = self.copr_input.text().strip()
        if not text:
            return
        if "/" not in text or len(text.split("/")) != 2:
            QMessageBox.warning(
                self,
                "Invalid COPR Format",
                "Please enter the COPR repository in the format:\n<b>username/projectname</b>\n\nExample: <i>atim/lazygit</i>"
            )
            return

        self.enable_copr_requested.emit(text)
        self.copr_input.clear()
        self.accept()

    def _on_clean_cache_clicked(self):
        reply = QMessageBox.question(
            self,
            "Clean DNF Package Cache",
            "Are you sure you want to clean the package cache?\n\n"
            "This will remove downloaded RPM archives and expired repository metadata from /var/cache/libdnf5.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.clean_cache_requested.emit()
            self.accept()

    def _on_remove_repo_clicked(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.information(self, "No Selection", "Please select a repository row to remove.")
            return

        row = selected_rows[0].row()
        id_item = self.table.item(row, 1)
        if not id_item:
            return

        repo_id = id_item.text().strip()
        repo = next((r for r in self._repos if r.id == repo_id), None)
        if not repo:
            return

        if repo.is_core:
            QMessageBox.warning(
                self,
                "Protected Core Repository",
                f"Cannot remove official Fedora repository '{repo.id}'.\n\nYou can disable it instead using the checkbox."
            )
            return

        reply = QMessageBox.question(
            self,
            "Remove Repository",
            f"Are you sure you want to permanently remove repository '{repo.name}' ({repo.id})?\n\n"
            f"This will delete file: {repo.repo_file}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.repo_remove_requested.emit(repo.id, repo.repo_file)
            self.accept()
