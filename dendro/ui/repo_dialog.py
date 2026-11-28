"""Repository management UI backed by Fedora's typed DNF5 daemon API.

The dialog receives repository records asynchronously from ``Repo.list``. It
never reads a host ``.repo`` file itself and never builds a DNF command line.
Repository enable/disable is delegated to the DNF5 daemon. Adding a direct
HTTPS ``.repo`` address and deletion each use separately constrained helpers
rather than a generic privileged command runner.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from urllib.parse import urlsplit

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from dendro.core.backend import RepoInfo
from dendro.core.dnf5daemon import Dnf5DaemonError, DnfRepository
from dendro.core.i18n import format_number, tr, tr_plural
from dendro.core.repository_addition import validate_repository_configuration_url
from dendro.core.repository_deletion import (
    can_delete_repository_file,
    is_package_owned_repository_file,
)
from dendro.core.settings import get_app_settings
from dendro.ui.fonts import apply_labelled_fonts, scaled_fixed_font
from dendro.ui.icons import theme_icon
from dendro.ui.styles import get_delegate_palette
from dendro.ui.table_rows import WholeRowFitter

# The repository table's columns are named here so the extraction tool can see
# the message ids: a literal list handed to ``setHorizontalHeaderLabels`` is
# invisible to it, and these four shipped in English in all seven languages.
COLUMN_HEADERS: Final[tuple[str, ...]] = (
    "Status",
    "Repository ID",
    "Display Name",
    "Channel Origin",
)


class RepoManagerDialog(QDialog):
    """Repository list and safe configuration controls for an RPM-host DNF5 daemon."""

    repo_toggle_requested = pyqtSignal(str, bool)
    repo_add_requested = pyqtSignal(str)
    repo_remove_requested = pyqtSignal(str, str)
    clean_cache_requested = pyqtSignal()
    refresh_requested = pyqtSignal()
    cancel_operation_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("System Software Repositories"))
        self.resize(860, 630)
        self._repos: list[RepoInfo] = []
        self._loading = False
        self._busy = False
        self._read_only = False
        self._populating_table = False
        self._init_ui()
        apply_labelled_fonts(self)
        self.set_loading(True)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header_bar = QHBoxLayout()
        header_icon = QLabel()
        title_icon = theme_icon("system-software-install", "software-properties")
        if not title_icon.isNull():
            header_icon.setPixmap(title_icon.pixmap(20, 20))
            header_bar.addWidget(header_icon)
        self.title_label = QLabel(tr("System Software Repositories"))
        self.title_label.setObjectName("DialogTitleLabel")
        self.btn_refresh = QPushButton(tr("Refresh"))
        self.btn_refresh.setIcon(QIcon.fromTheme("view-refresh"))
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.refresh_requested.emit)
        header_bar.addWidget(self.title_label, stretch=1)
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
        self.capability_label = QLabel(
            tr(
                "Repository enable/disable uses Fedora's DNF5 daemon. To add a repository, provide a direct HTTPS "
                ".repo configuration address; Dendro downloads it as your user account, validates it, and asks its "
                "fixed Polkit helper to create one new configuration file. Direct COPR repository-file addresses "
                "work here."
            )
        )
        self.capability_label.setTextFormat(Qt.TextFormat.PlainText)
        self.capability_label.setWordWrap(True)
        capability_layout.addWidget(self.capability_label, stretch=1)
        layout.addWidget(capability)

        add_box = QFrame()
        add_box.setObjectName("RepoAddBox")
        add_layout = QVBoxLayout(add_box)
        add_layout.setContentsMargins(10, 8, 10, 8)
        add_layout.setSpacing(5)
        self.add_label = QLabel(
            tr("Add direct HTTPS repository configuration (.repo) address")
        )
        self.add_label.setObjectName("SectionHeadingLabel")
        self.add_label.setWordWrap(True)
        add_layout.addWidget(self.add_label)
        add_row = QHBoxLayout()
        self.repo_address_input = QLineEdit()
        self.repo_address_input.setObjectName("RepoAddressInput")
        self.repo_address_input.setAccessibleName(tr("Repository configuration URL"))
        # URLs are structurally left-to-right even when the desktop language is RTL.
        self.repo_address_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.repo_address_input.setMaxLength(4096)
        self.repo_address_input.setClearButtonEnabled(True)
        self.repo_address_input.setPlaceholderText(
            tr("https://example.org/path/repository.repo")
        )
        self.repo_address_input.setToolTip(
            tr(
                "Use the direct HTTPS URL of a .repo configuration file. Dendro does not run dnf, dnf5, "
                "or a shell for this action."
            )
        )
        self.add_label.setBuddy(self.repo_address_input)
        self.repo_address_input.returnPressed.connect(self._on_add_repo_clicked)
        self.btn_add_repo = QPushButton(tr("Add Repo"))
        self.btn_add_repo.setIcon(theme_icon("list-add", "document-new"))
        self.btn_add_repo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_repo.clicked.connect(self._on_add_repo_clicked)
        add_row.addWidget(self.repo_address_input, stretch=1)
        add_row.addWidget(self.btn_add_repo)
        add_layout.addLayout(add_row)
        layout.addWidget(add_box)

        self.status_label = QLabel(tr("Loading repositories from the DNF5 daemon…"))
        self.status_label.setObjectName("InspectorPackagerLabel")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.filter_input = QLineEdit()
        self.filter_input.setObjectName("RepoFilterInput")
        self.filter_input.setAccessibleName(tr("Filter repositories"))
        self.filter_input.setPlaceholderText(
            tr("Filter repositories by ID, name, or channel (e.g. fusion, testing)…")
        )
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._filter_table)
        layout.addWidget(self.filter_input)

        self.table = QTableWidget(0, len(COLUMN_HEADERS))
        self.table.setObjectName("RepoTable")
        self.table.setAccessibleName(tr("Software repositories table"))
        self.table.setHorizontalHeaderLabels([tr(text) for text in COLUMN_HEADERS])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(1, 230)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemChanged.connect(self._on_table_item_changed)
        # Rows are fitted to whole lines so the bottom edge never cuts a row
        # in half; this filter also lets Space toggle a selected row regardless
        # of which column currently holds the table focus cursor.
        self._row_fitter = WholeRowFitter(self.table)
        self.table.installEventFilter(self._row_fitter)
        self.table.installEventFilter(self)
        layout.addWidget(self.table, stretch=1)

        self.operation_panel = QFrame()
        self.operation_panel.setObjectName("RepoOperationPanel")
        operation_layout = QVBoxLayout(self.operation_panel)
        operation_layout.setContentsMargins(10, 8, 10, 8)
        operation_layout.setSpacing(6)

        op_header = QHBoxLayout()
        self.operation_status_label = QLabel("")
        self.operation_status_label.setObjectName("RepoOperationStatus")
        self.operation_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.operation_status_label.setWordWrap(True)

        self.btn_cancel_operation = QPushButton(tr("Cancel"))
        self.btn_cancel_operation.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel_operation.clicked.connect(self.cancel_operation_requested.emit)

        self.btn_dismiss_operation = QPushButton(tr("Dismiss"))
        self.btn_dismiss_operation.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dismiss_operation.clicked.connect(self._dismiss_operation_panel)
        self.btn_dismiss_operation.hide()

        op_header.addWidget(self.operation_status_label, stretch=1)
        op_header.addWidget(self.btn_cancel_operation)
        op_header.addWidget(self.btn_dismiss_operation)
        operation_layout.addLayout(op_header)

        self.operation_progress = QProgressBar()
        self.operation_progress.setAccessibleName(tr("Repository operation progress"))
        self.operation_progress.setRange(0, 0)
        self.operation_progress.setTextVisible(False)
        operation_layout.addWidget(self.operation_progress)

        self.operation_log = QTextEdit()
        self.operation_log.setObjectName("ConsoleOutput")
        self.operation_log.setAccessibleName(tr("Repository operation log"))
        self.operation_log.setReadOnly(True)
        self.operation_log.setFont(scaled_fixed_font())
        self.operation_log.document().setMaximumBlockCount(1000)
        self.operation_log.setPlaceholderText(
            tr("Live repository operation log will appear here…")
        )
        operation_layout.addWidget(self.operation_log)

        self.operation_panel.hide()
        layout.addWidget(self.operation_panel)

        bottom_bar = QHBoxLayout()
        self.btn_clean_cache = QPushButton(tr("Clean Cache"))
        self.btn_clean_cache.setIcon(theme_icon("edit-clear", "drive-harddisk"))
        self.btn_clean_cache.setToolTip(
            tr("Ask the DNF5 daemon to remove cached RPMs and metadata.")
        )
        self.btn_clean_cache.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clean_cache.clicked.connect(self._on_clean_cache_clicked)

        self.btn_remove_repo = QPushButton(tr("Remove Repo"))
        self.btn_remove_repo.setIcon(theme_icon("list-remove", "user-trash"))
        self.btn_remove_repo.setToolTip(
            tr("Delete one third-party .repo file through Dendro's constrained helper.")
        )
        self.btn_remove_repo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_remove_repo.clicked.connect(self._on_remove_repo_clicked)

        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.accept)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.btn_clean_cache)
        bottom_bar.addWidget(self.btn_remove_repo)
        bottom_bar.addWidget(self.close_btn)
        layout.addLayout(bottom_bar)

        self._sync_scaled_chrome()
        self.setTabOrder(self.btn_refresh, self.repo_address_input)
        self.setTabOrder(self.repo_address_input, self.btn_add_repo)
        self.setTabOrder(self.btn_add_repo, self.filter_input)
        self.setTabOrder(self.filter_input, self.table)
        self.setTabOrder(self.table, self.btn_clean_cache)
        self.setTabOrder(self.btn_clean_cache, self.btn_remove_repo)
        self.setTabOrder(self.btn_remove_repo, self.close_btn)

    def _sync_scaled_chrome(self) -> None:
        self.operation_progress.setMinimumHeight(
            max(10, self.fontMetrics().height() // 2 + 4)
        )
        self.operation_log.setFont(scaled_fixed_font())
        self.operation_log.setMinimumHeight(
            max(92, self.fontMetrics().lineSpacing() * 4 + 18)
        )

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "operation_log"):
            self._sync_scaled_chrome()
            if self._repos:
                self._populate_table(self._repos)
                self._filter_table(self.filter_input.text())

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is getattr(self, "table", None)
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select)
            and not self._read_only
            and not self._busy
            and not self._loading
        ):
            index = self.table.currentIndex()
            if index.isValid() and index.column() != 0:
                check_item = self.table.item(index.row(), 0)
                if check_item is not None and (
                    check_item.flags() & Qt.ItemFlag.ItemIsUserCheckable
                ):
                    next_state = (
                        Qt.CheckState.Unchecked
                        if check_item.checkState() == Qt.CheckState.Checked
                        else Qt.CheckState.Checked
                    )
                    check_item.setCheckState(next_state)
                    return True
        return super().eventFilter(watched, event)

    def retranslate(self) -> None:
        self.setWindowTitle(tr("System Software Repositories"))
        self.title_label.setText(tr("System Software Repositories"))
        self.btn_refresh.setText(tr("Refresh"))
        if not self._read_only:
            self.capability_label.setText(
                tr(
                    "Repository enable/disable uses Fedora's DNF5 daemon. To add a repository, provide a direct HTTPS "
                    ".repo configuration address; Dendro downloads it as your user account, validates it, and asks its "
                    "fixed Polkit helper to create one new configuration file. Direct COPR repository-file addresses "
                    "work here."
                )
            )
        self.add_label.setText(
            tr("Add direct HTTPS repository configuration (.repo) address")
        )
        self.repo_address_input.setAccessibleName(tr("Repository configuration URL"))
        self.repo_address_input.setPlaceholderText(
            tr("https://example.org/path/repository.repo")
        )
        self.repo_address_input.setToolTip(
            tr(
                "Use the direct HTTPS URL of a .repo configuration file. Dendro does not run dnf, dnf5, "
                "or a shell for this action."
            )
        )
        self.btn_add_repo.setText(tr("Add Repo"))
        self.filter_input.setAccessibleName(tr("Filter repositories"))
        self.filter_input.setPlaceholderText(
            tr("Filter repositories by ID, name, or channel (e.g. fusion, testing)…")
        )
        self.table.setAccessibleName(tr("Software repositories table"))
        self.table.setHorizontalHeaderLabels([tr(text) for text in COLUMN_HEADERS])
        self.btn_cancel_operation.setText(tr("Cancel"))
        self.btn_dismiss_operation.setText(tr("Dismiss"))
        self.operation_progress.setAccessibleName(tr("Repository operation progress"))
        self.operation_log.setAccessibleName(tr("Repository operation log"))
        self.operation_log.setPlaceholderText(
            tr("Live repository operation log will appear here…")
        )
        self.btn_clean_cache.setText(tr("Clean Cache"))
        self.btn_clean_cache.setToolTip(
            tr("Ask the DNF5 daemon to remove cached RPMs and metadata.")
        )
        self.btn_remove_repo.setText(tr("Remove Repo"))
        self.btn_remove_repo.setToolTip(
            tr("Delete one third-party .repo file through Dendro's constrained helper.")
        )
        self.close_btn.setText(tr("Close"))
        if self._repos:
            self._populate_table(self._repos)
            self._filter_table(self.filter_input.text())
            count = len(self._repos)
            self.status_label.setText(
                tr_plural(
                    "Loaded {count} repository from the DNF5 daemon.",
                    "Loaded {count} repositories from the DNF5 daemon.",
                    count,
                ).format(count=format_number(count))
            )

    def set_loading(self, loading: bool, detail: str = "") -> None:
        self._loading = loading
        if loading:
            self.status_label.setText(
                detail or tr("Loading repositories from the DNF5 daemon…")
            )
        self._update_enabled_state()

    def set_repositories(self, repositories: Sequence[DnfRepository]) -> None:
        self._repos = [
            _repo_info_from_daemon(repository) for repository in repositories
        ]
        self._repos.sort(
            key=lambda repo: (
                not repo.is_core,
                not repo.is_rpmfusion,
                not repo.is_copr,
                repo.id.casefold(),
            )
        )
        self._populate_table(self._repos)
        self._loading = False
        count = len(self._repos)
        self.status_label.setText(
            tr_plural(
                "Loaded {count} repository from the DNF5 daemon.",
                "Loaded {count} repositories from the DNF5 daemon.",
                count,
            ).format(count=format_number(count))
        )
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
            self.status_label.setText(
                tr("A repository operation is active. Waiting for its terminal result…")
            )
        self._update_enabled_state()

    def start_operation(
        self,
        description: str,
        *,
        cancellable: bool = True,
        cancel_tooltip: str = "",
    ) -> None:
        """Show inline progress, log output, and truthful cancellation controls."""
        self._busy = True
        self.status_label.setText(description)
        self.operation_status_label.setText(description)
        self._set_operation_status_tone("neutral")
        self.operation_progress.setRange(0, 0)
        self.operation_progress.show()
        self.operation_log.clear()
        self.operation_log.show()
        self.btn_dismiss_operation.hide()
        self.btn_cancel_operation.setEnabled(cancellable)
        self.btn_cancel_operation.setToolTip(
            tr("Cancel the active repository operation.")
            if cancellable
            else (
                cancel_tooltip
                or tr("DNF5 daemon maintenance calls cannot be cancelled mid-flight.")
            )
        )
        self.btn_cancel_operation.show()
        self.operation_panel.show()
        self._update_enabled_state()
        self._row_fitter.fit()

    def append_operation_log(self, text: str) -> None:
        """Append live progress lines to the inline operation console."""
        if not text:
            return
        if self.operation_panel.isHidden():
            self.operation_panel.show()
            self.operation_log.show()
        cursor = self.operation_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.operation_log.setTextCursor(cursor)
        if "\r" in text:
            for index, segment in enumerate(text.split("\r")):
                if index == 0 and segment:
                    self.operation_log.insertPlainText(segment)
                elif segment:
                    cur = self.operation_log.textCursor()
                    cur.movePosition(QTextCursor.MoveOperation.End)
                    cur.select(QTextCursor.SelectionType.BlockUnderCursor)
                    cur.removeSelectedText()
                    cur.insertText(segment)
        else:
            self.operation_log.insertPlainText(text)
        self.operation_log.ensureCursorVisible()

    def _set_operation_status_tone(self, tone: str) -> None:
        if self.operation_status_label.property("statusTone") != tone:
            self.operation_status_label.setProperty("statusTone", tone)
            self.operation_status_label.style().unpolish(self.operation_status_label)
            self.operation_status_label.style().polish(self.operation_status_label)

    def finish_operation(
        self,
        success: bool,
        summary: str,
        *,
        detail: str = "",
    ) -> None:
        """Present inline success or error feedback without modal popups."""
        self._busy = False
        self._update_enabled_state()
        self.btn_cancel_operation.hide()
        self.btn_dismiss_operation.show()
        self.operation_progress.hide()

        if success:
            self.operation_status_label.setText(summary)
            self._set_operation_status_tone("success")
            self.operation_log.hide()
            self.status_label.setText(summary)
            self._row_fitter.release_cap()
        else:
            headline = summary or detail or tr("Repository operation failed.")
            self.operation_status_label.setText(headline)
            self._set_operation_status_tone("error")
            status_summary = (
                f"{summary}: {detail}"
                if summary and detail and detail not in summary
                else headline
            )
            self.status_label.setText(status_summary)
            trimmed_detail = detail.strip()
            if (
                trimmed_detail
                and trimmed_detail not in self.operation_log.toPlainText()
            ):
                self.append_operation_log(f"{trimmed_detail}\n")
            self.operation_log.show()

        self.operation_panel.show()
        self._row_fitter.fit()

    def _dismiss_operation_panel(self) -> None:
        self.operation_panel.hide()
        self._row_fitter.release_cap()
        self._row_fitter.fit()

    def set_read_only_mode(self, read_only: bool, *, note: str = "") -> None:
        """Disable repository mutations in environments where DNF5 changes are unsafe."""
        self._read_only = bool(read_only)
        if note:
            self.capability_label.setText(note)
        self._sync_checkbox_editability()
        self._update_enabled_state()

    def _sync_checkbox_editability(self) -> None:
        self._populating_table = True
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is None:
                    continue
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if not self._read_only:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                item.setFlags(flags)
        finally:
            self._populating_table = False

    def clear_repository_address(self) -> None:
        self.repo_address_input.clear()

    def _update_enabled_state(self) -> None:
        enabled = not self._loading and not self._busy
        allow_mutations = not self._busy and not self._read_only
        self.btn_refresh.setEnabled(not self._busy and not self._read_only)
        self.repo_address_input.setEnabled(allow_mutations)
        self.btn_add_repo.setEnabled(allow_mutations)
        self.filter_input.setEnabled(enabled and not self._read_only)
        self.table.setEnabled(enabled)
        self.btn_clean_cache.setEnabled(enabled and not self._read_only)
        self.btn_remove_repo.setEnabled(
            enabled and not self._read_only and bool(self._repos)
        )

    def _populate_table(self, repositories: Sequence[RepoInfo]) -> None:
        settings = get_app_settings()
        palette = get_delegate_palette(settings.value("theme", "auto", type=str))
        self._populating_table = True
        try:
            self.table.clearContents()
            self.table.setRowCount(len(repositories))

            mono_font = scaled_fixed_font()
            for row, repository in enumerate(repositories):
                check_item = QTableWidgetItem("")
                check_item.setData(Qt.ItemDataRole.UserRole, repository.id)
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if not self._read_only:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                check_item.setFlags(flags)
                check_item.setCheckState(
                    Qt.CheckState.Checked
                    if repository.enabled
                    else Qt.CheckState.Unchecked
                )
                check_item.setToolTip(
                    tr("Enabled — toggle with Space or click")
                    if repository.enabled
                    else tr("Disabled — toggle with Space or click")
                )

                tooltip = "\n".join(
                    (
                        tr("Repository ID: {repo_id}").format(repo_id=repository.id),
                        tr("Configuration file: {repo_file}").format(
                            repo_file=repository.repo_file or tr("Not provided by DNF5")
                        ),
                        tr("Source: {source}").format(
                            source=repository.baseurl or tr("Mirrorlist / Metalink")
                        ),
                    )
                )
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
                self.table.setItem(row, 0, check_item)
                self.table.setItem(row, 1, id_item)
                self.table.setItem(row, 2, name_item)
                self.table.setItem(row, 3, origin_item)
        finally:
            self._populating_table = False

        # Row heights are only known once the rows exist.
        self._row_fitter.fit()

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._populating_table or item.column() != 0:
            return
        repository_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(repository_id, str) or not repository_id:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        tooltip = (
            tr("Enabled — toggle with Space or click")
            if enabled
            else tr("Disabled — toggle with Space or click")
        )
        self._populating_table = True
        try:
            # Updating item roles inside itemChanged re-emits itemChanged.
            if item.toolTip() != tooltip:
                item.setToolTip(tooltip)
            self._apply_row_enabled_style(item.row(), enabled)
        finally:
            self._populating_table = False
        self._on_repo_toggled(repository_id, enabled, item.row())

    def _apply_row_enabled_style(self, row: int, enabled: bool) -> None:
        settings = get_app_settings()
        palette = get_delegate_palette(settings.value("theme", "auto", type=str))
        for column in (1, 2):
            item = self.table.item(row, column)
            if item is not None:
                item.setForeground(
                    palette["text_main"] if enabled else palette["text_dim"]
                )

    def _on_repo_toggled(self, repository_id: str, enabled: bool, row: int) -> None:
        for repository in self._repos:
            if repository.id == repository_id:
                repository.enabled = enabled
                break
        self._populating_table = True
        try:
            self._apply_row_enabled_style(row, enabled)
        finally:
            self._populating_table = False
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
            self.table.setRowHidden(
                row, bool(query) and not any(query in value for value in values)
            )

    def _on_add_repo_clicked(self) -> None:
        address = self.repo_address_input.text()
        if not address.strip():
            QMessageBox.information(
                self,
                tr("Repository Address Required"),
                tr("Enter the direct HTTPS URL of a .repo configuration file."),
            )
            return
        try:
            address = validate_repository_configuration_url(address)
        except Dnf5DaemonError as error:
            QMessageBox.warning(self, error.title, error.detail)
            return
        reply = QMessageBox.question(
            self,
            tr("Add Repository Configuration"),
            tr(
                "Dendro will download the entered configuration as your current user, validate its HTTPS sources and "
                "signature policy, then request authorization to create one new .repo file. It will not run dnf, dnf5, "
                "or a shell."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.repo_add_requested.emit(address)

    def _on_clean_cache_clicked(self) -> None:
        reply = QMessageBox.question(
            self,
            tr("Clean DNF Package Cache"),
            tr("Ask the DNF5 daemon to remove cached RPM archives and metadata?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.clean_cache_requested.emit()

    def _on_remove_repo_clicked(self) -> None:
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.information(
                self, tr("No Selection"), tr("Select a repository row to remove.")
            )
            return
        row = selected_rows[0].row()
        id_item = self.table.item(row, 1)
        if id_item is None:
            return
        repository = next(
            (item for item in self._repos if item.id == id_item.text()), None
        )
        if repository is None:
            return
        if repository.is_core:
            QMessageBox.warning(
                self,
                tr("Protected Core Repository"),
                tr(
                    "Cannot remove official Fedora repository '{repo_id}'. You can disable it instead."
                ).format(repo_id=repository.id),
            )
            return
        if not repository.repo_file:
            QMessageBox.information(
                self,
                tr("Repository File Not Available"),
                tr(
                    "The DNF5 daemon did not provide a repository file for this entry, so Dendro cannot delete it."
                ),
            )
            return
        if is_package_owned_repository_file(repository.repo_file):
            QMessageBox.information(
                self,
                tr("Package-Owned Repository File"),
                tr(
                    "Repository '{name}' ({repo_id}) is provided by a package under /usr ({repo_file}) and cannot be deleted directly. Disable it with its checkbox instead, or uninstall the package that provides it."
                ).format(
                    name=repository.name,
                    repo_id=repository.id,
                    repo_file=repository.repo_file,
                ),
            )
            return
        if not can_delete_repository_file(repository.repo_file):
            QMessageBox.information(
                self,
                tr("Repository File Cannot Be Deleted"),
                tr(
                    "Repository '{name}' ({repo_id}) uses '{repo_file}', which is outside /etc/yum.repos.d and /etc/dnf/repos.d and cannot be deleted by Dendro. Disable it with its checkbox instead."
                ).format(
                    name=repository.name,
                    repo_id=repository.id,
                    repo_file=repository.repo_file,
                ),
            )
            return
        reply = QMessageBox.question(
            self,
            tr("Remove Repository"),
            tr("Permanently delete repository '{name}' ({repo_id})?").format(
                name=repository.name, repo_id=repository.id
            )
            + "\n\n"
            + tr("File: {repo_file}").format(repo_file=repository.repo_file),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.repo_remove_requested.emit(repository.id, repository.repo_file)


_FEDORA_UPDATES_REPO_IDS = frozenset(
    {
        "updates",
        "updates-source",
        "updates-debuginfo",
        "updates-testing",
        "updates-testing-source",
        "updates-testing-debuginfo",
        "updates-modular",
        "updates-testing-modular",
        "updates-archive",
    }
)


def _repo_info_from_daemon(repository: DnfRepository) -> RepoInfo:
    identifier = repository.identifier.casefold()
    try:
        source_host = (
            (urlsplit(repository.baseurl).hostname or "").rstrip(".").casefold()
        )
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
    is_core = (
        not is_copr
        and not is_rpmfusion
        and (
            identifier.startswith("fedora")
            or identifier in _FEDORA_UPDATES_REPO_IDS
            or "rawhide" in identifier
            or is_fedora_project_source
        )
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


def _repository_origin(
    repository: RepoInfo, palette: dict[str, object]
) -> tuple[str, object]:
    if repository.is_core:
        return tr("Fedora Project"), palette["accent"]
    if repository.is_rpmfusion:
        return tr("RPM Fusion"), palette["badge_fg_tag"]
    if repository.is_copr:
        return tr("COPR Community"), palette["badge_fg_installed"]
    return tr("Third-Party"), palette["badge_fg_queued_in"]
