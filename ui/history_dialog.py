# dendro/ui/history_dialog.py
from __future__ import annotations

from typing import List, Optional
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont
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


class DnfHistoryDialog(QDialog):
    """
    پنجره مدیریت و کاوش تاریخچه تراکنش‌های DNF فدورا
    با قابلیت جستجو و بازگردانی (Undo) تغییرات
    """

    undo_requested = pyqtSignal(int)      # شناسه تراکنش برای بازگردانی
    refresh_requested = pyqtSignal()      # درخواست بارگذاری مجدد تاریخچه

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("DNF Transaction History & Rollback")
        self.resize(850, 520)
        self._entries: List[HistoryEntry] = []

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # ۱. هدر و توضیحات
        header_layout = QHBoxLayout()
        title_label = QLabel("🕒 System Package Transaction History")
        title_label.setStyleSheet("font-size: 15px; font-weight: bold; color: #89b4fa;")

        self.refresh_btn = QPushButton("🔄 Refresh")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)

        header_layout.addWidget(title_label, stretch=1)
        header_layout.addWidget(self.refresh_btn)
        layout.addLayout(header_layout)

        # ۲. نوار جستجو در تاریخچه
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Filter by command, action (Install/Erase), or date...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter_table)
        layout.addWidget(self.search_input)

        # ۳. جدول نمایش تراکنش‌ها
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["ID", "Action", "Date & Time", "Altered", "Command Line"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
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

        # ۴. نوار دکمه‌های پایین
        bottom_layout = QHBoxLayout()
        
        self.undo_btn = QPushButton("↩️ Undo Selected Transaction")
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

    @pyqtSlot(list)
    def set_history_entries(self, entries: List[HistoryEntry]):
        self._entries = entries
        self._populate_table(entries)

    def _populate_table(self, entries: List[HistoryEntry]):
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            id_item = QTableWidgetItem(f"#{entry.id}")
            id_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            
            action_item = QTableWidgetItem(entry.action)
            action_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            
            # رنگ‌آمیزی بر اساس نوع عملیات
            act_lower = entry.action.lower()
            if "install" in act_lower:
                action_item.setForeground(QColor("#a6e3a1"))
            elif "erase" in act_lower or "remove" in act_lower:
                action_item.setForeground(QColor("#eba0ac"))
            elif "upgrade" in act_lower or "update" in act_lower:
                action_item.setForeground(QColor("#89b4fa"))

            dt_item = QTableWidgetItem(entry.date_time)
            alt_item = QTableWidgetItem(f"{entry.altered_count} pkgs")
            alt_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            cmd_item = QTableWidgetItem(entry.command_line)
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
            e for e in self._entries
            if query in e.command_line.lower() or query in e.action.lower() or query in e.date_time.lower() or query in str(e.id)
        ]
        self._populate_table(filtered)

    def _on_undo_clicked(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.information(self, "No Selection", "Please select a transaction row to undo.")
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
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.undo_requested.emit(trans_id)
                self.accept()
        except ValueError:
            pass
