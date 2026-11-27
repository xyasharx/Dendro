# dendro/ui/history_dialog.py
"""
DNF Transaction History Viewer for Fedora Linux.
Provides search, filtering, full command inspection, and safe clipboard
terminal-command helpers backed by Fedora's read-only DNF5 daemon history API.
Uses native FreeDesktop theme icons.
"""

from __future__ import annotations

from typing import Final

from PyQt6.QtCore import QEvent, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from dendro.core.backend import HistoryEntry
from dendro.core.i18n import format_number, tr, tr_plural
from dendro.core.settings import get_app_settings
from dendro.ui.fonts import (
    DETAIL_PT_DELTA,
    apply_labelled_fonts,
    scaled_fixed_font,
    style_text,
)
from dendro.ui.icons import theme_icon
from dendro.ui.styles import get_delegate_palette
from dendro.ui.table_rows import WholeRowFitter

# The transaction table's columns are named here so the extraction tool can see
# the message ids: a literal list handed to ``setHorizontalHeaderLabels`` is
# invisible to it, and these five shipped in English in all seven languages.
COLUMN_HEADERS: Final[tuple[str, ...]] = (
    "ID",
    "Action / Status",
    "Date & Time",
    "Altered",
    "Command Line",
)


class DnfHistoryDialog(QDialog):
    """
    Read-only dialog for exploring Fedora DNF transaction history with search,
    full detail inspection, and clipboard-assisted terminal undo commands.
    """

    refresh_requested = pyqtSignal()  # Request to reload history

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("DNF Transaction History"))
        self.resize(860, 560)
        self._entries: list[HistoryEntry] = []
        self._history_request_serial = 0
        self._undo_supported = True
        self._undo_disabled_reason = ""
        self._copied_undo_command: str | None = None

        self._init_ui()
        apply_labelled_fonts(self)
        self._update_action_buttons()

    def set_undo_supported(self, supported: bool, *, tooltip: str = "") -> None:
        """Enable or disable copying undo commands according to host capabilities."""
        self._undo_supported = bool(supported)
        self._undo_disabled_reason = tooltip
        if not self._undo_supported and tooltip:
            self.capability_notice_label.setText(tooltip)
        elif self._undo_supported:
            self.capability_notice_label.setText(
                tr(
                    "Read-only history viewer: Fedora's DNF5 system daemon exposes transaction logs without a "
                    "rollback RPC. Select a transaction to inspect details or copy its terminal undo command."
                )
            )
        self._update_action_buttons()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # ---------------------------------------------------------------------
        # 1. Header & Controls
        # ---------------------------------------------------------------------
        header_layout = QHBoxLayout()
        header_icon = QLabel()
        hist_icon = theme_icon("document-open-recent", "view-history")
        if not hist_icon.isNull():
            header_icon.setPixmap(hist_icon.pixmap(20, 20))
            header_layout.addWidget(header_icon)

        self.title_label = QLabel(tr("System Package Transaction History"))
        self.title_label.setObjectName("DialogTitleLabel")

        self.refresh_btn = QPushButton(tr("Refresh"))
        self.refresh_btn.setIcon(QIcon.fromTheme("view-refresh"))
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)

        header_layout.addWidget(self.title_label, stretch=1)
        header_layout.addWidget(self.refresh_btn)
        layout.addLayout(header_layout)

        # Upfront daemon capability notice (never mislead with fake rollback CTA)
        capability_box = QFrame()
        capability_box.setObjectName("CoprBox")
        capability_layout = QHBoxLayout(capability_box)
        capability_layout.setContentsMargins(10, 7, 10, 7)
        info_icon = QIcon.fromTheme("dialog-information")
        if not info_icon.isNull():
            capability_icon = QLabel()
            capability_icon.setPixmap(info_icon.pixmap(18, 18))
            capability_layout.addWidget(capability_icon)
        self.capability_notice_label = QLabel(
            tr(
                "Read-only history viewer: Fedora's DNF5 system daemon exposes transaction logs without a "
                "rollback RPC. Select a transaction to inspect details or copy its terminal undo command."
            )
        )
        self.capability_notice_label.setObjectName("HistoryCapabilityLabel")
        self.capability_notice_label.setTextFormat(Qt.TextFormat.PlainText)
        self.capability_notice_label.setWordWrap(True)
        capability_layout.addWidget(self.capability_notice_label, stretch=1)
        layout.addWidget(capability_box)

        self.status_label = QLabel(tr("Loading DNF transaction history…"))
        self.status_label.setObjectName("HistoryStatusLabel")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # ---------------------------------------------------------------------
        # 2. Filter Search Bar
        # ---------------------------------------------------------------------
        self.search_input = QLineEdit()
        self.search_input.setAccessibleName(tr("Filter transaction history"))
        self.search_input.setPlaceholderText(
            tr("Filter by command, action (Install/Erase), or date...")
        )
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter_table)
        layout.addWidget(self.search_input)

        # ---------------------------------------------------------------------
        # 3. Transaction Table
        # ---------------------------------------------------------------------
        self.table = QTableWidget(0, len(COLUMN_HEADERS))
        self.table.setObjectName("HistoryTable")
        self.table.setAccessibleName(tr("DNF transaction history table"))
        self.table.setHorizontalHeaderLabels([tr(text) for text in COLUMN_HEADERS])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._row_fitter = WholeRowFitter(self.table)
        self.table.installEventFilter(self._row_fitter)
        self.table.selectionModel().selectionChanged.connect(
            self._update_action_buttons
        )
        self.table.itemDoubleClicked.connect(
            lambda _item: self._open_detail_for_selected()
        )
        layout.addWidget(self.table, stretch=1)

        # ---------------------------------------------------------------------
        # 4. Inline Transaction Details Panel (Shown on Demand)
        # ---------------------------------------------------------------------
        self.detail_panel = QFrame()
        self.detail_panel.setObjectName("CoprBox")
        detail_layout = QVBoxLayout(self.detail_panel)
        detail_layout.setContentsMargins(10, 8, 10, 8)
        detail_layout.setSpacing(6)

        detail_header = QHBoxLayout()
        self.detail_title_label = QLabel("")
        self.detail_title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_title_label.setStyleSheet("font-weight: bold;")
        self.btn_hide_detail = QPushButton(tr("Hide Details"))
        self.btn_hide_detail.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hide_detail.clicked.connect(self._hide_detail_panel)
        detail_header.addWidget(self.detail_title_label, stretch=1)
        detail_header.addWidget(self.btn_hide_detail)
        detail_layout.addLayout(detail_header)

        self.detail_meta_label = QLabel("")
        self.detail_meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_meta_label.setWordWrap(True)
        detail_layout.addWidget(self.detail_meta_label)

        self.detail_command_view = QTextEdit()
        self.detail_command_view.setObjectName("ConsoleOutput")
        self.detail_command_view.setAccessibleName(tr("Transaction command details"))
        self.detail_command_view.setReadOnly(True)
        self.detail_command_view.setFont(scaled_fixed_font())
        self.detail_command_view.setMinimumHeight(
            max(68, self.fontMetrics().lineSpacing() * 3 + 18)
        )
        detail_layout.addWidget(self.detail_command_view)

        self.detail_panel.hide()
        layout.addWidget(self.detail_panel)

        # ---------------------------------------------------------------------
        # 5. Bottom Action Bar
        # ---------------------------------------------------------------------
        bottom_layout = QHBoxLayout()

        self.inspect_btn = QPushButton(tr("Inspect Details"))
        self.inspect_btn.setIcon(
            theme_icon("dialog-information", "document-properties")
        )
        self.inspect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inspect_btn.clicked.connect(self._on_inspect_clicked)

        self.copy_undo_btn = QPushButton(tr("Copy Undo Command"))
        self.copy_undo_btn.setIcon(theme_icon("edit-copy", "edit-undo"))
        self.copy_undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_undo_btn.clicked.connect(self._on_copy_undo_clicked)

        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.accept)

        bottom_layout.addWidget(self.inspect_btn)
        bottom_layout.addWidget(self.copy_undo_btn)
        bottom_layout.addStretch(1)
        bottom_layout.addWidget(self.close_btn)
        layout.addLayout(bottom_layout)

        self.setTabOrder(self.search_input, self.refresh_btn)
        self.setTabOrder(self.refresh_btn, self.table)
        self.setTabOrder(self.table, self.inspect_btn)
        self.setTabOrder(self.inspect_btn, self.copy_undo_btn)
        self.setTabOrder(self.copy_undo_btn, self.close_btn)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(
            self, "detail_command_view"
        ):
            self.detail_command_view.setFont(scaled_fixed_font())
            self.detail_command_view.setMinimumHeight(
                max(68, self.fontMetrics().lineSpacing() * 3 + 18)
            )
            if self._entries:
                self._populate_table(self._entries)

    def retranslate(self) -> None:
        self.setWindowTitle(tr("DNF Transaction History"))
        self.title_label.setText(tr("System Package Transaction History"))
        self.refresh_btn.setText(tr("Refresh"))
        self.search_input.setAccessibleName(tr("Filter transaction history"))
        self.search_input.setPlaceholderText(
            tr("Filter by command, action (Install/Erase), or date...")
        )
        self.table.setAccessibleName(tr("DNF transaction history table"))
        self.table.setHorizontalHeaderLabels([tr(text) for text in COLUMN_HEADERS])
        self.btn_hide_detail.setText(tr("Hide Details"))
        self.detail_command_view.setAccessibleName(tr("Transaction command details"))
        self.copy_undo_btn.setText(tr("Copy Undo Command"))
        self.close_btn.setText(tr("Close"))
        self.set_undo_supported(
            self._undo_supported, tooltip=self._undo_disabled_reason
        )
        if self._entries:
            self._populate_table(self._entries)
            count = len(self._entries)
            self.status_label.setText(
                tr_plural(
                    "Loaded {count} DNF transaction.",
                    "Loaded {count} DNF transactions.",
                    count,
                ).format(count=format_number(count))
            )
        else:
            self.status_label.setText(tr("Loading DNF transaction history…"))
        self._update_action_buttons()

    def set_history_loading(self) -> int:
        """Start one identified load without erasing previously loaded rows."""
        self._history_request_serial += 1
        self.status_label.setText(tr("Loading DNF transaction history…"))
        self.status_label.setStyleSheet("")
        self.status_label.setToolTip("")
        self.refresh_btn.setEnabled(False)
        self.inspect_btn.setEnabled(False)
        self.copy_undo_btn.setEnabled(False)
        return self._history_request_serial

    def set_history_entries_if_current(
        self, request_serial: int, entries: list[HistoryEntry]
    ) -> bool:
        """Accept only the terminal result belonging to the current refresh."""
        if request_serial != self._history_request_serial:
            return False
        self.set_history_entries(entries)
        return True

    def set_history_error_if_current(
        self, request_serial: int, package_name: str, detail: str
    ) -> bool:
        """Ignore a stale failure after the user has requested a newer refresh."""
        if request_serial != self._history_request_serial:
            return False
        self.set_history_error(package_name, detail)
        return True

    @pyqtSlot(list)
    def set_history_entries(self, entries: list[HistoryEntry]) -> None:
        self._entries = entries
        self._populate_table(entries)
        self.refresh_btn.setEnabled(True)
        if entries:
            count = len(entries)
            self.status_label.setText(
                tr_plural(
                    "Loaded {count} DNF transaction.",
                    "Loaded {count} DNF transactions.",
                    count,
                ).format(count=format_number(count))
            )
        else:
            self.status_label.setText(tr("No DNF transactions were found."))
        self.status_label.setStyleSheet("")
        self.status_label.setToolTip("")
        self._update_action_buttons()

    @pyqtSlot(str, str)
    def set_history_error(self, _package_name: str, detail: str) -> None:
        """Keep existing rows, but visibly distinguish a failed refresh from no history."""
        self.refresh_btn.setEnabled(True)
        suffix = (
            " " + tr("Showing the last successfully loaded entries.")
            if self._entries
            else ""
        )
        summary = " ".join(detail.split())
        if len(summary) > 360:
            summary = f"{summary[:357].rstrip()}…"
        message = (
            tr("Could not load DNF history: {detail}").format(detail=summary)
            if summary
            else tr("Could not load DNF history.")
        )
        settings = get_app_settings()
        pal = get_delegate_palette(settings.value("theme", "auto", type=str))
        self.status_label.setText(f"{message}{suffix}")
        style_text(
            self.status_label,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=pal["badge_fg_missing"].name(),
        )
        self.status_label.setToolTip(detail)
        self._update_action_buttons()

    def _populate_table(self, entries: list[HistoryEntry]):
        settings = get_app_settings()
        theme_choice = settings.value("theme", "auto", type=str)
        pal = get_delegate_palette(theme_choice)

        mono_font = scaled_fixed_font()
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            id_item = QTableWidgetItem(f"#{entry.id}")
            id_item.setData(Qt.ItemDataRole.UserRole, entry.id)
            id_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            id_item.setFont(mono_font)

            action_item = QTableWidgetItem(entry.action)
            action_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            act_lower = entry.action.lower()
            if "install" in act_lower:
                action_item.setForeground(pal["badge_fg_installed"])
            elif "erase" in act_lower or "remove" in act_lower:
                action_item.setForeground(pal["badge_fg_missing"])
            elif "upgrade" in act_lower or "update" in act_lower:
                action_item.setForeground(pal["accent"])

            dt_item = QTableWidgetItem(entry.date_time)
            dt_item.setFont(mono_font)

            alt_item = QTableWidgetItem(
                tr("{count} pkgs").format(count=format_number(entry.altered_count))
            )
            alt_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            alt_item.setFont(mono_font)

            cmd_item = QTableWidgetItem(entry.command_line)
            cmd_item.setFont(mono_font)
            cmd_item.setToolTip(entry.command_line)

            self.table.setItem(row, 0, id_item)
            self.table.setItem(row, 1, action_item)
            self.table.setItem(row, 2, dt_item)
            self.table.setItem(row, 3, alt_item)
            self.table.setItem(row, 4, cmd_item)

        # Rows are a fixed height and the table's height comes from the dialog,
        # so the bottom edge used to cut through the last visible row.
        self._row_fitter.fit()
        if entries:
            self.table.selectRow(0)
        self._update_action_buttons()

    def _filter_table(self, query: str):
        query = query.strip().lower()
        if not query:
            self._populate_table(self._entries)
            return

        filtered = [
            e
            for e in self._entries
            if query in e.command_line.lower()
            or query in e.action.lower()
            or query in e.date_time.lower()
            or query in str(e.id)
        ]
        self._populate_table(filtered)

    def _selected_entry(self) -> HistoryEntry | None:
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return None
        selected_rows = selection_model.selectedRows()
        if not selected_rows:
            return None
        row_idx = selected_rows[0].row()
        id_item = self.table.item(row_idx, 0)
        if id_item is None:
            return None
        raw_id = id_item.data(Qt.ItemDataRole.UserRole)
        if isinstance(raw_id, int):
            trans_id = raw_id
        else:
            try:
                trans_id = int(id_item.text().replace("#", "").strip())
            except ValueError:
                return None
        return next((entry for entry in self._entries if entry.id == trans_id), None)

    def _update_action_buttons(self) -> None:
        entry = self._selected_entry()
        has_selection = entry is not None
        self.inspect_btn.setEnabled(has_selection)
        self.copy_undo_btn.setEnabled(self._undo_supported and has_selection)
        if not self._undo_supported:
            self.copy_undo_btn.setToolTip(self._undo_disabled_reason)
        elif entry is not None:
            self.copy_undo_btn.setToolTip(
                tr("Copy terminal command: {command}").format(
                    command=f"sudo dnf history undo {entry.id}"
                )
            )
        else:
            self.copy_undo_btn.setToolTip("")

        if self.detail_panel.isVisible():
            if entry is not None:
                self._populate_detail_panel(entry)
            else:
                self._hide_detail_panel()
        else:
            self.inspect_btn.setText(tr("Inspect Details"))

    def _populate_detail_panel(self, entry: HistoryEntry) -> None:
        self.detail_panel.setProperty("activeTransactionId", entry.id)
        self.detail_title_label.setText(
            tr("Transaction #{id} — {action}").format(
                id=format_number(entry.id), action=entry.action
            )
        )
        self.detail_meta_label.setText(
            tr("Date: {date}  |  Altered: {altered}  |  Exit code: {code}").format(
                date=entry.date_time,
                altered=format_number(entry.altered_count),
                code=format_number(entry.return_code),
            )
        )
        command_line = entry.command_line or tr("(No command line recorded)")
        info_cmd = f"sudo dnf history info {entry.id}"
        undo_cmd = f"sudo dnf history undo {entry.id}"
        self.detail_command_view.setPlainText(
            "\n".join(
                (
                    tr("Command: {command}").format(command=command_line),
                    tr("Inspect in terminal: {info_cmd}").format(info_cmd=info_cmd),
                    tr("Undo in terminal: {undo_cmd}").format(undo_cmd=undo_cmd),
                )
            )
        )
        self.inspect_btn.setText(tr("Hide Details"))

    def _open_detail_for_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self._populate_detail_panel(entry)
        self.detail_panel.show()
        self._row_fitter.fit()

    def _hide_detail_panel(self) -> None:
        self.detail_panel.hide()
        self.inspect_btn.setText(tr("Inspect Details"))
        self._row_fitter.release_cap()
        self._row_fitter.fit()

    def _on_inspect_clicked(self) -> None:
        if self.detail_panel.isVisible():
            self._hide_detail_panel()
            return
        self._open_detail_for_selected()

    def _on_copy_undo_clicked(self) -> None:
        entry = self._selected_entry()
        if entry is None or not self._undo_supported:
            return
        command = f"sudo dnf history undo {entry.id}"
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(command)
        self._copied_undo_command = command
        self.status_label.setText(
            tr("Copied terminal command: {command}").format(command=command)
        )
        self.status_label.setStyleSheet("")
