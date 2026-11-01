# dendro/ui/flatpak_dialog.py
"""
Read-only dialog listing Flatpak apps and runtimes on an RPM host.

Flatpak entries are shown separately from RPM packages. The dialog cannot
install, remove, or update anything. Worker results arrive through queued
connections to slots on this dialog, so every widget update runs on the UI
thread, and results from superseded refreshes are ignored.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QThreadPool, Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.flatpak_inventory import (
    FlatpakEntry,
    FlatpakInventory,
    FlatpakInventoryWorker,
)
from ui.icons import theme_icon

READ_ONLY_NOTICE = (
    "Read-only view. Flatpak apps and runtimes are managed by Flatpak, not by DNF. "
    "They are not part of RPM categories, dependency trees, or DNF updates, "
    "and this view cannot install, remove, or update them."
)

_COLUMNS = (
    "Kind",
    "Name",
    "Application ID",
    "Version",
    "Branch",
    "Arch",
    "Origin",
    "Installation",
    "Installed size",
)
_SIZE_COLUMN = _COLUMNS.index("Installed size")


def format_installed_size(size_bytes: Optional[int]) -> str:
    """Format a byte count in decimal units, matching Flatpak's own convention."""
    if size_bytes is None:
        return "—"
    if size_bytes < 1000:
        return f"{size_bytes} B"
    value = float(size_bytes)
    for unit in ("kB", "MB", "GB"):
        value /= 1000
        if value < 1000:
            return f"{value:.1f} {unit}"
    return f"{value / 1000:.1f} TB"


class _SizeItem(QTableWidgetItem):
    """Sorts by numeric byte count instead of by the displayed text."""

    def __init__(self, text: str, size_bytes: Optional[int]):
        super().__init__(text)
        self._size_bytes = -1 if size_bytes is None else size_bytes

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, _SizeItem):
            return self._size_bytes < other._size_bytes
        return super().__lt__(other)


class FlatpakInventoryDialog(QDialog):
    """Shows one Flatpak inventory at a time, read-only."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Flatpak Apps & Runtimes")
        self.resize(980, 560)
        self._request_serial = 0
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header_layout = QHBoxLayout()
        icon = QLabel()
        flatpak_icon = theme_icon("application-x-flatpak", "system-software-install")
        if not flatpak_icon.isNull():
            icon.setPixmap(flatpak_icon.pixmap(20, 20))
            header_layout.addWidget(icon)

        title = QLabel("Flatpak Apps & Runtimes")
        title.setStyleSheet("font-weight: bold; color: #89b4fa;")
        self.refresh_btn = QPushButton(" Refresh")
        refresh_icon = theme_icon("view-refresh")
        if not refresh_icon.isNull():
            self.refresh_btn.setIcon(refresh_icon)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_layout.addWidget(title, stretch=1)
        header_layout.addWidget(self.refresh_btn)
        layout.addLayout(header_layout)

        notice = QLabel(READ_ONLY_NOTICE)
        notice.setObjectName("FlatpakReadOnlyNotice")
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setStyleSheet("font-size: 11px;")
        layout.addWidget(notice)

        self.status_label = QLabel("")
        self.status_label.setObjectName("FlatpakStatusLabel")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setObjectName("FlatpakInventoryTable")
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        layout.addWidget(self.table, stretch=1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

    # ------------------------------------------------------------------
    # Refresh lifecycle (all methods run on the UI thread)
    # ------------------------------------------------------------------
    def begin_loading(self) -> int:
        """Start a refresh; returns the serial that its result must carry."""
        self._request_serial += 1
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("Loading Flatpak apps and runtimes…")
        self.status_label.setToolTip("")
        return self._request_serial

    def _on_refresh_clicked(self) -> None:
        start_flatpak_refresh(self, QThreadPool.globalInstance())

    @pyqtSlot(int, object)
    def show_inventory_if_current(
        self, serial: int, inventory: FlatpakInventory
    ) -> bool:
        if serial != self._request_serial:
            return False
        self.refresh_btn.setEnabled(True)
        self._fill_table(inventory.entries)
        apps = sum(1 for entry in inventory.entries if entry.kind == "app")
        runtimes = len(inventory.entries) - apps
        if inventory.entries:
            self.status_label.setText(
                f"{apps} app(s), {runtimes} runtime(s) installed."
            )
        else:
            self.status_label.setText("No Flatpak apps or runtimes are installed.")
        self.status_label.setToolTip("")
        return True

    @pyqtSlot(int, str)
    def show_error_if_current(self, serial: int, detail: str) -> bool:
        if serial != self._request_serial:
            return False
        self.refresh_btn.setEnabled(True)
        self.table.setRowCount(0)
        self.status_label.setText(detail)
        self.status_label.setToolTip(detail)
        return True

    def _fill_table(self, entries: tuple[FlatpakEntry, ...]) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            size_text = format_installed_size(entry.installed_size_bytes)
            values = (
                entry.kind.capitalize(),
                entry.name,
                entry.application_id,
                entry.version,
                entry.branch,
                entry.arch,
                entry.origin,
                entry.installation,
                size_text,
            )
            for column, text in enumerate(values):
                if column == _SIZE_COLUMN:
                    item: QTableWidgetItem = _SizeItem(text, entry.installed_size_bytes)
                else:
                    item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(text)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)


def start_flatpak_refresh(
    dialog: FlatpakInventoryDialog,
    thread_pool: QThreadPool,
) -> None:
    """Begin one identified refresh and route its single result to the dialog."""
    serial = dialog.begin_loading()
    worker = FlatpakInventoryWorker(serial)
    worker.signals.loaded.connect(dialog.show_inventory_if_current)
    worker.signals.failed.connect(dialog.show_error_if_current)
    try:
        thread_pool.start(worker)
    except Exception as error:  # noqa: BLE001 - surface any failure to start
        dialog.show_error_if_current(
            serial,
            f"Could not start Flatpak loading: {type(error).__name__}: {error}",
        )
