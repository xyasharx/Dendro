# dendro/ui/flatpak_dialog.py
"""
Dialog listing Flatpak apps and runtimes on an RPM host.

Flatpak entries are shown separately from RPM packages. The dialog can uninstall
apps in the current user's Flatpak installation, and nothing else. Worker results
arrive through queued connections to slots on this dialog, so every widget update
runs on the UI thread, and results from superseded requests are ignored.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThreadPool, pyqtSlot
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
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
from core.flatpak_uninstall import (
    FlatpakUninstallWorker,
    describe_uninstall,
    is_user_app,
)
from ui.icons import theme_icon

SCOPE_NOTICE = (
    "Flatpak apps and runtimes are managed by Flatpak, not by DNF. They are not part "
    "of RPM categories, dependency trees, or DNF updates. You can uninstall apps "
    "installed for your own user account. Installing, updating, and system-wide "
    "changes are not available here."
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
_APP_ID_COLUMN = _COLUMNS.index("Application ID")


class _SizeItem(QTableWidgetItem):
    """Sorts by numeric byte count instead of by the displayed text."""

    def __init__(self, text: str, size_bytes: int | None):
        super().__init__(text)
        self._size_bytes = -1 if size_bytes is None else size_bytes

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, _SizeItem):
            return self._size_bytes < other._size_bytes
        return super().__lt__(other)


def confirm_uninstall(entry: FlatpakEntry, parent: QWidget) -> bool | None:
    """Ask before uninstalling. Returns ``delete_data`` if confirmed, else ``None``.

    Cancel is the default button, and data deletion is a separate, unticked box.
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Uninstall Flatpak App")
    box.setText(f"Uninstall {entry.name}?")
    box.setInformativeText(describe_uninstall(entry, delete_data=False))
    delete_box = QCheckBox("Also delete this app's data. This cannot be undone.")
    box.setCheckBox(delete_box)
    cancel_button = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    uninstall_button = box.addButton(
        "Uninstall", QMessageBox.ButtonRole.DestructiveRole
    )
    box.setDefaultButton(cancel_button)
    box.exec()
    if box.clickedButton() is not uninstall_button:
        return None
    return delete_box.isChecked()


class FlatpakInventoryDialog(QDialog):
    """Shows one Flatpak inventory at a time and uninstalls per-user apps."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Flatpak Apps & Runtimes")
        self.resize(980, 560)
        self._request_serial = 0
        # "refresh" or "uninstall" while a worker result is pending, else None.
        self._pending: str | None = None
        self._init_ui()
        self._update_controls()

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
        self.uninstall_btn = QPushButton(" Uninstall…")
        self.uninstall_btn.setObjectName("FlatpakUninstallButton")
        uninstall_icon = theme_icon("edit-delete", "user-trash")
        if not uninstall_icon.isNull():
            self.uninstall_btn.setIcon(uninstall_icon)
        self.uninstall_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.uninstall_btn.setToolTip(
            "Uninstall the selected app from your user Flatpak installation"
        )
        self.uninstall_btn.clicked.connect(self._on_uninstall_clicked)
        header_layout.addWidget(title, stretch=1)
        header_layout.addWidget(self.uninstall_btn)
        header_layout.addWidget(self.refresh_btn)
        layout.addLayout(header_layout)

        notice = QLabel(SCOPE_NOTICE)
        notice.setObjectName("FlatpakScopeNotice")
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
        self.table.itemSelectionChanged.connect(self._update_controls)
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
    # Control state (all methods run on the UI thread)
    # ------------------------------------------------------------------
    def selected_entry(self) -> FlatpakEntry | None:
        items = self.table.selectedItems()
        if not items:
            return None
        entry = items[0].data(Qt.ItemDataRole.UserRole)
        return entry if isinstance(entry, FlatpakEntry) else None

    def _update_controls(self) -> None:
        idle = self._pending is None
        self.refresh_btn.setEnabled(idle)
        entry = self.selected_entry()
        self.uninstall_btn.setEnabled(idle and entry is not None and is_user_app(entry))

    # ------------------------------------------------------------------
    # Refresh lifecycle
    # ------------------------------------------------------------------
    def begin_loading(self) -> int:
        """Start a refresh; returns the serial that its result must carry."""
        self._request_serial += 1
        self._pending = "refresh"
        self.status_label.setText("Loading Flatpak apps and runtimes…")
        self.status_label.setToolTip("")
        self._update_controls()
        return self._request_serial

    def _on_refresh_clicked(self) -> None:
        start_flatpak_refresh(self, QThreadPool.globalInstance())

    @pyqtSlot(int, object)
    def show_inventory_if_current(
        self, serial: int, inventory: FlatpakInventory
    ) -> bool:
        if serial != self._request_serial:
            return False
        self._pending = None
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
        self._update_controls()
        return True

    @pyqtSlot(int, str)
    def show_error_if_current(self, serial: int, detail: str) -> bool:
        if serial != self._request_serial:
            return False
        self._pending = None
        self.table.setRowCount(0)
        self.status_label.setText(detail)
        self.status_label.setToolTip(detail)
        self._update_controls()
        return True

    # ------------------------------------------------------------------
    # Uninstall lifecycle
    # ------------------------------------------------------------------
    def begin_uninstall(self, entry: FlatpakEntry) -> int:
        """Start an uninstall; returns the serial that its result must carry."""
        self._request_serial += 1
        self._pending = "uninstall"
        self.status_label.setText(f"Uninstalling {entry.name}…")
        self.status_label.setToolTip("")
        self._update_controls()
        return self._request_serial

    def _on_uninstall_clicked(self) -> None:
        entry = self.selected_entry()
        if entry is None or not is_user_app(entry):
            return
        delete_data = confirm_uninstall(entry, self)
        if delete_data is None:
            return
        start_flatpak_uninstall(
            self,
            QThreadPool.globalInstance(),
            entry,
            delete_data=delete_data,
        )

    @pyqtSlot(int, str)
    def show_uninstall_done_if_current(self, serial: int, name: str) -> bool:
        if serial != self._request_serial:
            return False
        self._pending = None
        self.status_label.setText(f"Uninstalled {name}. Refreshing the list…")
        self.status_label.setToolTip("")
        self._update_controls()
        start_flatpak_refresh(self, QThreadPool.globalInstance())
        return True

    @pyqtSlot(int, str)
    def show_uninstall_failed_if_current(self, serial: int, detail: str) -> bool:
        if serial != self._request_serial:
            return False
        self._pending = None
        self.status_label.setText(detail)
        self.status_label.setToolTip(detail)
        self._update_controls()
        # The app may be partly removed, so always re-read the real state.
        start_flatpak_refresh(self, QThreadPool.globalInstance())
        return True

    def _fill_table(self, entries: tuple[FlatpakEntry, ...]) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            size_text = entry.installed_size_text or "—"
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
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self._update_controls()


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
    except Exception as error:
        dialog.show_error_if_current(
            serial,
            f"Could not start Flatpak loading: {type(error).__name__}: {error}",
        )


def start_flatpak_uninstall(
    dialog: FlatpakInventoryDialog,
    thread_pool: QThreadPool,
    entry: FlatpakEntry,
    *,
    delete_data: bool,
) -> None:
    """Begin one identified uninstall and route its single result to the dialog."""
    serial = dialog.begin_uninstall(entry)
    worker = FlatpakUninstallWorker(serial, entry, delete_data=delete_data)
    worker.signals.succeeded.connect(dialog.show_uninstall_done_if_current)
    worker.signals.failed.connect(dialog.show_uninstall_failed_if_current)
    try:
        thread_pool.start(worker)
    except Exception as error:
        dialog.show_uninstall_failed_if_current(
            serial,
            f"Could not start uninstall: {type(error).__name__}: {error}",
        )
