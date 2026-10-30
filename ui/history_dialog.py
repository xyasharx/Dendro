# dendro/ui/history_dialog.py
"""
DNF Transaction History & Rollback Viewer for Fedora Linux.
Provides search, filtering, and transaction rollback capabilities.
Uses native FreeDesktop theme icons.
"""

from __future__ import annotations

from typing import List, Optional
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFontDatabase, QIcon
from PyQt6.QtWidgets import (
    QDialog,
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

from core.backend import HistoryEntry
from ui.icons import theme_icon


class DnfHistoryDialog(QDialog):
    """
    Dialog for managing and exploring Fedora DNF transaction history
    with search and rollback (undo) capabilities.
    """

    undo_requested = pyqtSignal(int)  # Transaction ID to undo
    refresh_requested = pyqtSignal()  # Request to reload history

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("DNF Transaction History & Rollback")
        self.resize(850, 520)
        self._entries: List[HistoryEntry] = []
        self._history_request_serial = 0

        self._init_ui()

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

        title_label = QLabel("System Package Transaction History")
        title_label.setStyleSheet("font-weight: bold; color: #89b4fa;")

        self.refresh_btn = QPushButton(" Refresh")
        self.refresh_btn.setIcon(QIcon.fromTheme("view-refresh"))
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)

        header_layout.addWidget(title_label, stretch=1)
        header_layout.addWidget(self.refresh_btn)
        layout.addLayout(header_layout)

        self.status_label = QLabel("Loading DNF transaction history…")
        self.status_label.setObjectName("HistoryStatusLabel")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.status_label)

        # ---------------------------------------------------------------------
        # 2. Filter Search Bar
        # ---------------------------------------------------------------------
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Filter by command, action (Install/Erase), or date..."
        )
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter_table)
        layout.addWidget(self.search_input)

        # ---------------------------------------------------------------------
        # 3. Transaction Table
        # ---------------------------------------------------------------------
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("HistoryTable")
        self.table.setHorizontalHeaderLabels(
            ["ID", "Action / Status", "Date & Time", "Altered", "Command Line"]
        )
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
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, stretch=1)

        # ---------------------------------------------------------------------
        # 4. Bottom Action Bar
        # ---------------------------------------------------------------------
        bottom_layout = QHBoxLayout()

        self.undo_btn = QPushButton(" Undo Selected Transaction")
        self.undo_btn.setIcon(QIcon.fromTheme("edit-undo"))
        self.undo_btn.setObjectName("ApplyButton")
        self.undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_btn.clicked.connect(self._on_undo_clicked)

        self.close_btn = QPushButton("Close")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.accept)

        bottom_layout.addWidget(self.undo_btn)
        bottom_layout.addStretch(1)
        bottom_layout.addWidget(self.close_btn)
        layout.addLayout(bottom_layout)

    def set_history_loading(self) -> int:
        """Start one identified load without erasing previously loaded rows."""
        self._history_request_serial += 1
        self.status_label.setText("Loading DNF transaction history…")
        self.status_label.setStyleSheet("font-size: 11px;")
        self.status_label.setToolTip("")
        self.refresh_btn.setEnabled(False)
        self.undo_btn.setEnabled(False)
        return self._history_request_serial

    def set_history_entries_if_current(
        self, request_serial: int, entries: List[HistoryEntry]
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
    def set_history_entries(self, entries: List[HistoryEntry]) -> None:
        self._entries = entries
        self._populate_table(entries)
        self.refresh_btn.setEnabled(True)
        self.undo_btn.setEnabled(bool(entries))
        self.status_label.setText(
            f"Loaded {len(entries):,} DNF transaction{'s' if len(entries) != 1 else ''}."
            if entries
            else "No DNF transactions were found."
        )
        self.status_label.setStyleSheet("font-size: 11px;")
        self.status_label.setToolTip("")

    @pyqtSlot(str, str)
    def set_history_error(self, _package_name: str, detail: str) -> None:
        """Keep existing rows, but visibly distinguish a failed refresh from no history."""
        self.refresh_btn.setEnabled(True)
        self.undo_btn.setEnabled(bool(self._entries))
        suffix = (
            " Showing the last successfully loaded entries." if self._entries else ""
        )
        summary = " ".join(detail.split())
        if len(summary) > 360:
            summary = f"{summary[:357].rstrip()}…"
        message = "Could not load DNF history"
        if summary:
            message = f"{message}: {summary}"
        else:
            message = f"{message}."
        self.status_label.setText(f"{message}{suffix}")
        self.status_label.setStyleSheet(
            "color: #f38ba8; font-size: 11px; font-weight: bold;"
        )
        self.status_label.setToolTip(detail)

    def _populate_table(self, entries: List[HistoryEntry]):
        from PyQt6.QtCore import QSettings
        from ui.styles import get_delegate_palette

        settings = QSettings("FedoraCommunity", "Dendro")
        theme_choice = settings.value("theme", "auto", type=str)
        pal = get_delegate_palette(theme_choice)

        mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            id_item = QTableWidgetItem(f"#{entry.id}")
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

            alt_item = QTableWidgetItem(f"{entry.altered_count} pkgs")
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

    def _on_undo_clicked(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.information(
                self, "No Selection", "Please select a transaction row to undo."
            )
            return

        row_idx = selected_rows[0].row()
        id_text = self.table.item(row_idx, 0).text().replace("#", "")
        try:
            trans_id = int(id_text)
            reply = QMessageBox.question(
                self,
                "Confirm Rollback",
                f"Are you sure you want to rollback and undo DNF Transaction #{trans_id}?\n\n"
                f"This will revert the packages altered in that operation.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.undo_requested.emit(trans_id)
                self.accept()
        except ValueError:
            QMessageBox.warning(
                self,
                "Invalid Transaction ID",
                "The selected history row does not contain a valid numeric transaction ID.",
            )
