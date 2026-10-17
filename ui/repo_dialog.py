"""Repository management UI backed by Fedora's typed DNF5 daemon API.

The dialog receives repository records asynchronously from ``Repo.list``. It
never reads a host ``.repo`` file itself and never builds a DNF command line.
Repository enable/disable is delegated to the DNF5 daemon. Adding a direct
HTTPS ``.repo`` address and deletion each use separately constrained helpers
rather than a generic privileged command runner.
"""
from __future__ import annotations

from typing import Sequence
from urllib.parse import urlsplit

from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QIcon
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

from core.backend import RepoInfo
from core.dnf5daemon import Dnf5DaemonError, DnfRepository
from core.repository_addition import validate_repository_configuration_url
from ui.styles import get_delegate_palette


class RepoManagerDialog(QDialog):
    """Repository list and safe configuration controls for an RPM-host DNF5 daemon."""

    repo_toggle_requested = pyqtSignal(str, bool)
    repo_add_requested = pyqtSignal(str)
    repo_remove_requested = pyqtSignal(str, str)
    clean_cache_requested = pyqtSignal()
    refresh_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("System Software Repositories")
        self.resize(860, 630)
        self._repos: list[RepoInfo] = []
        self._loading = False
        self._busy = False
        self._init_ui()
        self.set_loading(True)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header_bar = QHBoxLayout()
        header_icon = QLabel()
        title_icon = QIcon.fromTheme("system-software-install") or QIcon.fromTheme("software-properties")
        if not title_icon.isNull():
            header_icon.setPixmap(title_icon.pixmap(20, 20))
            header_bar.addWidget(header_icon)
        title = QLabel("System Software Repositories")
        title.setStyleSheet("font-weight: bold;")
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setIcon(QIcon.fromTheme("view-refresh"))
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.refresh_requested.emit)
        header_bar.addWidget(title, stretch=1)
        header_bar.addWidget(self.btn_refresh)
        layout.addLayout(header_bar)

        capability = QFrame()
        capability.setObjectName("CoprBox")
        capability_layout = QHBoxLayout(capability)
        capability_layout.setContentsMargins(10, 7, 10, 7)
        capability_icon = QLabel()
        info_icon = QIcon.fromTheme("dialog-information")
        if not info_icon.isNull():
            capability_icon.setPixmap(info_icon.pixmap(18, 18))
            capability_layout.addWidget(capability_icon)
        capability_label = QLabel(
            "Repository enable/disable uses Fedora's DNF5 daemon. To add a repository, provide a direct HTTPS "
            ".repo configuration address; Dendro downloads it as your user account, validates it, and asks its "
            "fixed Polkit helper to create one new configuration file. Direct COPR repository-file addresses "
            "work here."
        )
        capability_label.setWordWrap(True)
        capability_layout.addWidget(capability_label, stretch=1)
        layout.addWidget(capability)

        add_box = QFrame()
        add_box.setObjectName("RepoAddBox")
        add_layout = QVBoxLayout(add_box)
        add_layout.setContentsMargins(10, 8, 10, 8)
        add_layout.setSpacing(5)
        add_label = QLabel("Add direct HTTPS repository configuration (.repo) address")
        add_label.setWordWrap(True)
        add_label.setStyleSheet("font-weight: bold;")
        add_layout.addWidget(add_label)
        add_row = QHBoxLayout()
        self.repo_address_input = QLineEdit()
        self.repo_address_input.setObjectName("RepoAddressInput")
        # URLs are structurally left-to-right even when the desktop language is RTL.
        self.repo_address_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.repo_address_input.setMaxLength(4096)
        self.repo_address_input.setClearButtonEnabled(True)
        self.repo_address_input.setPlaceholderText("https://example.org/path/repository.repo")
        self.repo_address_input.setToolTip(
            "Use the direct HTTPS URL of a .repo configuration file. Dendro does not run dnf, dnf5, "
            "or a shell for this action."
        )
        self.repo_address_input.returnPressed.connect(self._on_add_repo_clicked)
        self.btn_add_repo = QPushButton("Add Repo")
        self.btn_add_repo.setIcon(QIcon.fromTheme("list-add") or QIcon.fromTheme("document-new"))
        self.btn_add_repo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_repo.clicked.connect(self._on_add_repo_clicked)
        add_row.addWidget(self.repo_address_input, stretch=1)
        add_row.addWidget(self.btn_add_repo)
        add_layout.addLayout(add_row)
        layout.addWidget(add_box)

        self.status_label = QLabel("Loading repositories from the DNF5 daemon…")
        self.status_label.setObjectName("InspectorPackagerLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.filter_input = QLineEdit()
        self.filter_input.setObjectName("RepoFilterInput")
        self.filter_input.setPlaceholderText("Filter repositories by ID, name, or channel (e.g. fusion, testing)…")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._filter_table)
        layout.addWidget(self.filter_input)

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

        bottom_bar = QHBoxLayout()
        self.btn_clean_cache = QPushButton("Clean Cache")
        self.btn_clean_cache.setIcon(QIcon.fromTheme("edit-clear") or QIcon.fromTheme("drive-harddisk"))
        self.btn_clean_cache.setToolTip("Ask the DNF5 daemon to remove cached RPMs and metadata.")
        self.btn_clean_cache.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clean_cache.clicked.connect(self._on_clean_cache_clicked)

        self.btn_remove_repo = QPushButton("Remove Repo")
        self.btn_remove_repo.setIcon(QIcon.fromTheme("list-remove") or QIcon.fromTheme("user-trash"))
        self.btn_remove_repo.setToolTip("Delete one third-party .repo file through Dendro's constrained helper.")
        self.btn_remove_repo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_remove_repo.clicked.connect(self._on_remove_repo_clicked)

        self.close_btn = QPushButton("Close")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.accept)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.btn_clean_cache)
        bottom_bar.addWidget(self.btn_remove_repo)
        bottom_bar.addWidget(self.close_btn)
        layout.addLayout(bottom_bar)

    def set_loading(self, loading: bool, detail: str = "") -> None:
        self._loading = loading
        if loading:
            self.status_label.setText(detail or "Loading repositories from the DNF5 daemon…")
        self._update_enabled_state()

    def set_repositories(self, repositories: Sequence[DnfRepository]) -> None:
        self._repos = [_repo_info_from_daemon(repository) for repository in repositories]
        self._repos.sort(key=lambda repo: (not repo.is_core, not repo.is_rpmfusion, not repo.is_copr, repo.id.casefold()))
        self._populate_table(self._repos)
        self._loading = False
        self.status_label.setText(f"Loaded {len(self._repos)} repositories from the DNF5 daemon.")
        self._filter_table(self.filter_input.text())
        self._update_enabled_state()

    def set_load_error(self, title: str, detail: str) -> None:
        self._loading = False
        self.table.setRowCount(0)
        self.status_label.setText(f"{title}: {detail}")
        self._update_enabled_state()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self.status_label.setText("A repository operation is active. Waiting for its terminal result…")
        self._update_enabled_state()

    def clear_repository_address(self) -> None:
        self.repo_address_input.clear()

    def _update_enabled_state(self) -> None:
        enabled = not self._loading and not self._busy
        self.btn_refresh.setEnabled(not self._busy)
        self.repo_address_input.setEnabled(not self._busy)
        self.btn_add_repo.setEnabled(not self._busy)
        self.filter_input.setEnabled(enabled)
        self.table.setEnabled(enabled)
        self.btn_clean_cache.setEnabled(enabled)
        self.btn_remove_repo.setEnabled(enabled and bool(self._repos))

    def _populate_table(self, repositories: Sequence[RepoInfo]) -> None:
        settings = QSettings("FedoraCommunity", "Dendro")
        palette = get_delegate_palette(settings.value("theme", "auto", type=str))
        self.table.clearContents()
        self.table.setRowCount(len(repositories))

        for row, repository in enumerate(repositories):
            check_widget = QWidget()
            check_layout = QHBoxLayout(check_widget)
            check_layout.setContentsMargins(12, 0, 12, 0)
            check_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            checkbox = QCheckBox()
            checkbox.setChecked(repository.enabled)
            checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
            checkbox.toggled.connect(
                lambda state, repository_id=repository.id, table_row=row: self._on_repo_toggled(
                    repository_id, state, table_row
                )
            )
            check_layout.addWidget(checkbox)

            tooltip = (
                f"Repository ID: {repository.id}\n"
                f"Configuration file: {repository.repo_file or 'Not provided by DNF5'}\n"
                f"Source: {repository.baseurl or 'Mirrorlist / Metalink'}"
            )
            mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            id_item = QTableWidgetItem(repository.id)
            id_item.setFont(mono_font)
            id_item.setToolTip(tooltip)
            name_item = QTableWidgetItem(repository.name)
            name_item.setToolTip(tooltip)
            if not repository.enabled:
                id_item.setForeground(palette["text_dim"])
                name_item.setForeground(palette["text_dim"])

            origin, color = _repository_origin(repository, palette)
            origin_item = QTableWidgetItem(origin)
            origin_item.setForeground(color)
            origin_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(row, 0, check_widget)
            self.table.setItem(row, 1, id_item)
            self.table.setItem(row, 2, name_item)
            self.table.setItem(row, 3, origin_item)

    def _on_repo_toggled(self, repository_id: str, enabled: bool, row: int) -> None:
        for repository in self._repos:
            if repository.id == repository_id:
                repository.enabled = enabled
                break
        settings = QSettings("FedoraCommunity", "Dendro")
        palette = get_delegate_palette(settings.value("theme", "auto", type=str))
        for column in (1, 2):
            item = self.table.item(row, column)
            if item is not None:
                item.setForeground(palette["text_main"] if enabled else palette["text_dim"])
        self.repo_toggle_requested.emit(repository_id, enabled)

    def _filter_table(self, query: str) -> None:
        query = query.strip().casefold()
        for row in range(self.table.rowCount()):
            id_item = self.table.item(row, 1)
            name_item = self.table.item(row, 2)
            origin_item = self.table.item(row, 3)
            values = (
                id_item.text().casefold() if id_item else "",
                name_item.text().casefold() if name_item else "",
                origin_item.text().casefold() if origin_item else "",
            )
            self.table.setRowHidden(row, bool(query) and not any(query in value for value in values))

    def _on_add_repo_clicked(self) -> None:
        address = self.repo_address_input.text()
        if not address.strip():
            QMessageBox.information(
                self,
                "Repository Address Required",
                "Enter the direct HTTPS URL of a .repo configuration file.",
            )
            return
        try:
            address = validate_repository_configuration_url(address)
        except Dnf5DaemonError as error:
            QMessageBox.warning(self, error.title, error.detail)
            return
        reply = QMessageBox.question(
            self,
            "Add Repository Configuration",
            "Dendro will download the entered configuration as your current user, validate its HTTPS sources and "
            "signature policy, then request authorization to create one new .repo file. It will not run dnf, dnf5, "
            "or a shell.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.repo_add_requested.emit(address)

    def _on_clean_cache_clicked(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clean DNF Package Cache",
            "Ask the DNF5 daemon to remove cached RPM archives and metadata?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.clean_cache_requested.emit()

    def _on_remove_repo_clicked(self) -> None:
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.information(self, "No Selection", "Select a repository row to remove.")
            return
        row = selected_rows[0].row()
        id_item = self.table.item(row, 1)
        if id_item is None:
            return
        repository = next((item for item in self._repos if item.id == id_item.text()), None)
        if repository is None:
            return
        if repository.is_core:
            QMessageBox.warning(
                self,
                "Protected Core Repository",
                f"Cannot remove official Fedora repository '{repository.id}'. You can disable it instead.",
            )
            return
        if not repository.repo_file:
            QMessageBox.information(
                self,
                "Repository File Not Available",
                "The DNF5 daemon did not provide a repository file for this entry, so Dendro cannot delete it.",
            )
            return
        reply = QMessageBox.question(
            self,
            "Remove Repository",
            f"Permanently delete repository '{repository.name}' ({repository.id})?\n\n"
            f"File: {repository.repo_file}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.repo_remove_requested.emit(repository.id, repository.repo_file)


def _repo_info_from_daemon(repository: DnfRepository) -> RepoInfo:
    identifier = repository.identifier.casefold()
    try:
        source_host = (urlsplit(repository.baseurl).hostname or "").rstrip(".").casefold()
    except ValueError:
        # Repository metadata is supplied by the daemon but remains external
        # input. An invalid URL cannot establish an official repository origin.
        source_host = ""

    is_copr = "copr" in identifier or (
        source_host == "copr.fedorainfracloud.org"
        or source_host.endswith(".copr.fedorainfracloud.org")
    )
    is_rpmfusion = "rpmfusion" in identifier or (
        source_host == "rpmfusion.org" or source_host.endswith(".rpmfusion.org")
    )
    is_fedora_project_source = (
        source_host == "fedoraproject.org" or source_host.endswith(".fedoraproject.org")
    )
    is_core = not is_copr and not is_rpmfusion and (
        identifier.startswith("fedora") or "rawhide" in identifier or is_fedora_project_source
    )
    return RepoInfo(
        id=repository.identifier,
        name=repository.name,
        enabled=repository.enabled,
        repo_file=repository.repo_file,
        is_copr=is_copr,
        is_rpmfusion=is_rpmfusion,
        is_core=is_core,
        baseurl=repository.baseurl,
    )


def _repository_origin(repository: RepoInfo, palette: dict[str, object]) -> tuple[str, object]:
    if repository.is_core:
        return "Fedora Project", palette["accent"]
    if repository.is_rpmfusion:
        return "RPM Fusion", palette["badge_fg_tag"]
    if repository.is_copr:
        return "COPR Community", palette["badge_fg_installed"]
    return "Third-Party", palette["badge_fg_queued_in"]