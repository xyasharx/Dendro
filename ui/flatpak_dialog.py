# dendro/ui/flatpak_dialog.py
"""
Dialog listing Flatpak apps and runtimes on an RPM host.

Flatpak entries are shown separately from RPM packages. The dialog can uninstall
or update apps in the current user's Flatpak installation, and nothing else. Worker results
arrive through queued connections to slots on this dialog, so every widget update
runs on the UI thread, and results from superseded requests are ignored.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtCore import QRunnable, Qt, QThreadPool, pyqtSlot
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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
from core.flatpak_cancel import CancellableRunner
from core.flatpak_runtime_removal import (
    describe_runtime_removal,
    runtime_left_unused,
    uninstall_app_and_runtime,
)
from core.flatpak_system_uninstall import (
    FlatpakSystemUninstallWorker,
    describe_system_uninstall,
    run_flatpak_system_uninstall,
)
from core.flatpak_system_update import (
    FlatpakSystemUpdateWorker,
    is_system_app,
    run_flatpak_system_update,
)
from core.flatpak_uninstall import (
    FlatpakUserActionWorker,
    describe_uninstall,
    is_user_app,
    run_flatpak_uninstall,
)
from ui.icons import theme_icon

SCOPE_NOTICE = (
    "Flatpak apps and runtimes are managed by Flatpak, not by DNF. They are not part "
    "of RPM categories, dependency trees, or DNF updates. You can update apps installed "
    "system-wide (Flatpak may ask for an administrator password), and update or uninstall "
    "apps installed for your own user account or system-wide. Uninstalling an app can also "
    "remove its runtime, but only after you confirm and only when no other app uses it. "
    "Installing new apps and updating runtimes are not available here."
)

RUNTIME_REASON = (
    "Runtimes are shared by several apps, so Dendro does not update them or remove "
    "them on their own. A runtime is removed only with an app that uses it, when no "
    "other app needs it. To update runtimes, run \"flatpak update\" in a terminal. "
    "To remove runtimes that no app uses, run \"flatpak uninstall --unused\"."
)
UNSUPPORTED_REASON = (
    "Only apps installed for your user or system-wide can be changed here."
)
_UNINSTALL_TIP = (
    "Uninstall the selected app (your own apps, or system-wide apps with "
    "an administrator password)"
)
_UPDATE_TIP = (
    "Update the selected app. System-wide apps may ask for an administrator password."
)


def unavailable_reason(entry: FlatpakEntry | None) -> str:
    """Plain-language reason the selected row cannot be changed, or "" if it can."""
    if entry is None or is_user_app(entry) or is_system_app(entry):
        return ""
    if entry.kind == "runtime":
        return RUNTIME_REASON
    return UNSUPPORTED_REASON


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


@dataclass(frozen=True)
class UninstallChoice:
    """What the user ticked in the uninstall confirmation."""

    delete_data: bool
    remove_runtime: bool


class _UninstallConfirmDialog(QDialog):
    """Cancel is the default. Data deletion and runtime removal are separate, unticked boxes."""

    def __init__(
        self,
        entry: FlatpakEntry,
        runtime: FlatpakEntry | None,
        parent: QWidget | None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Uninstall Flatpak App")
        layout = QVBoxLayout(self)

        title = QLabel(f"Uninstall {entry.name}?")
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        describe = describe_system_uninstall if is_system_app(entry) else describe_uninstall
        info = QLabel(describe(entry, delete_data=False))
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(info)

        self.delete_box = QCheckBox("Also delete this app's data. This cannot be undone.")
        layout.addWidget(self.delete_box)

        self.runtime_box: QCheckBox | None = None
        if runtime is not None:
            layout.addWidget(QLabel(""))
            runtime_info = QLabel(describe_runtime_removal(runtime))
            runtime_info.setWordWrap(True)
            runtime_info.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(runtime_info)
            self.runtime_box = QCheckBox(
                f"Also remove the unused runtime {runtime.application_id} "
                f"{runtime.branch}"
            )
            layout.addWidget(self.runtime_box)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = QPushButton("Cancel")
        cancel_button.setDefault(True)
        cancel_button.clicked.connect(self.reject)
        uninstall_button = QPushButton("Uninstall")
        uninstall_button.setAutoDefault(False)
        uninstall_button.clicked.connect(self.accept)
        button_row.addWidget(cancel_button)
        button_row.addWidget(uninstall_button)
        layout.addLayout(button_row)

    def choice(self) -> UninstallChoice:
        return UninstallChoice(
            delete_data=self.delete_box.isChecked(),
            remove_runtime=(
                self.runtime_box is not None and self.runtime_box.isChecked()
            ),
        )


def confirm_uninstall(
    entry: FlatpakEntry,
    parent: QWidget,
    runtime: FlatpakEntry | None = None,
) -> UninstallChoice | None:
    """Ask before uninstalling. Returns the user's choices if confirmed, else None.

    ``runtime`` is set only when removing the app would leave that runtime unused.
    """
    dialog = _UninstallConfirmDialog(entry, runtime, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.choice()


class FlatpakInventoryDialog(QDialog):
    """Shows one Flatpak inventory at a time; updates apps and uninstalls user apps."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Flatpak Apps & Runtimes")
        self.resize(980, 560)
        self._request_serial = 0
        # "refresh", "uninstall", or "update" while a result is pending, else None.
        self._pending: str | None = None
        # Result of the last uninstall or update. It stays on the status line after
        # the automatic refresh, and is cleared when the user starts something new.
        self._last_result = ""
        # The inventory from the last successful refresh, used to decide whether an
        # app's runtime would be left unused.
        self._entries: tuple[FlatpakEntry, ...] = ()
        # Cancel for the running system-wide action, or None when it cannot be
        # cancelled (refresh, per-user actions). Set by begin_action.
        self._cancel: Callable[[], None] | None = None
        self._cancel_requested = False
        # The main window owns the shared gate that serializes host operations.
        # Without it, system-wide updates are refused rather than run unguarded.
        self.host_operation_gate = getattr(parent, "host_operation_gate", None)
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
        self.uninstall_btn.setToolTip(_UNINSTALL_TIP)
        self.uninstall_btn.clicked.connect(self._on_uninstall_clicked)
        self.update_btn = QPushButton(" Update")
        self.update_btn.setObjectName("FlatpakUpdateButton")
        update_icon = theme_icon("software-update-available", "view-refresh")
        if not update_icon.isNull():
            self.update_btn.setIcon(update_icon)
        self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_btn.setToolTip(_UPDATE_TIP)
        self.update_btn.clicked.connect(self._on_update_clicked)
        header_layout.addWidget(title, stretch=1)
        header_layout.addWidget(self.update_btn)
        header_layout.addWidget(self.uninstall_btn)
        self.cancel_btn = QPushButton(" Cancel")
        self.cancel_btn.setObjectName("FlatpakCancelButton")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setToolTip(
            "Stop this system-wide change. It may be partly applied, so refresh afterwards."
        )
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        header_layout.addWidget(self.cancel_btn)
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

        # Explains why the selected row cannot be changed. Kept apart from the
        # status line so it never hides the result of the last action.
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("FlatpakSelectionHint")
        self.hint_label.setTextFormat(Qt.TextFormat.PlainText)
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("font-size: 11px;")
        self.hint_label.setVisible(False)
        layout.addWidget(self.hint_label)

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
        changeable = entry is not None and (is_user_app(entry) or is_system_app(entry))
        self.uninstall_btn.setEnabled(idle and changeable)
        self.update_btn.setEnabled(idle and changeable)
        reason = unavailable_reason(entry)
        self.hint_label.setText(reason)
        self.hint_label.setVisible(bool(reason))
        # A disabled button gets its reason as a tooltip, so the user can see why.
        self.uninstall_btn.setToolTip(reason or _UNINSTALL_TIP)
        self.update_btn.setToolTip(reason or _UPDATE_TIP)
        cancelable = self._pending is not None and self._cancel is not None
        self.cancel_btn.setVisible(cancelable)
        self.cancel_btn.setEnabled(cancelable and not self._cancel_requested)

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
        self._last_result = ""
        start_flatpak_refresh(self, QThreadPool.globalInstance())

    @pyqtSlot(int, object)
    def show_inventory_if_current(
        self, serial: int, inventory: FlatpakInventory
    ) -> bool:
        if serial != self._request_serial:
            return False
        self._pending = None
        self._entries = inventory.entries
        self._fill_table(inventory.entries)
        apps = sum(1 for entry in inventory.entries if entry.kind == "app")
        runtimes = len(inventory.entries) - apps
        if inventory.entries:
            counts = f"{apps} app(s), {runtimes} runtime(s) installed."
        else:
            counts = "No Flatpak apps or runtimes are installed."
        self.status_label.setText(f"{self._last_result} {counts}".strip())
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
    # Uninstall and update lifecycle
    # ------------------------------------------------------------------
    def begin_action(
        self,
        entry: FlatpakEntry,
        operation: str,
        cancel: Callable[[], None] | None = None,
    ) -> int:
        """Start an uninstall or update; returns the serial its result must carry.

        ``cancel`` is given only for actions the user can stop (system-wide ones).
        """
        self._request_serial += 1
        self._pending = operation
        self._last_result = ""
        self._cancel = cancel
        self._cancel_requested = False
        gerund = "Uninstalling" if operation == "uninstall" else "Updating"
        self.status_label.setText(f"{gerund} {entry.name}…")
        self.status_label.setToolTip("")
        self._update_controls()
        return self._request_serial

    def begin_uninstall(self, entry: FlatpakEntry) -> int:
        return self.begin_action(entry, "uninstall")

    def begin_update(self, entry: FlatpakEntry) -> int:
        return self.begin_action(entry, "update")

    def _on_update_clicked(self) -> None:
        entry = self.selected_entry()
        if entry is None or not (is_user_app(entry) or is_system_app(entry)):
            return
        start_flatpak_update(self, QThreadPool.globalInstance(), entry)

    def _on_uninstall_clicked(self) -> None:
        entry = self.selected_entry()
        if entry is None or not (is_user_app(entry) or is_system_app(entry)):
            return
        runtime = runtime_left_unused(entry, self._entries)
        choice = confirm_uninstall(entry, self, runtime=runtime)
        if choice is None:
            return
        start_flatpak_uninstall(
            self,
            QThreadPool.globalInstance(),
            entry,
            delete_data=choice.delete_data,
            runtime=runtime if choice.remove_runtime else None,
        )

    def _on_cancel_clicked(self) -> None:
        if self._pending is None or self._cancel is None or self._cancel_requested:
            return
        self._cancel_requested = True
        self.status_label.setText("Cancelling…")
        self.status_label.setToolTip("")
        self._cancel()
        self._update_controls()

    def _clear_cancel(self) -> None:
        self._cancel = None
        self._cancel_requested = False

    def _finish_done(self, serial: int, operation: str, message: str) -> bool:
        if serial != self._request_serial or self._pending != operation:
            return False
        self._pending = None
        self._clear_cancel()
        self._last_result = message
        self.status_label.setText(f"{message} Refreshing the list…")
        self.status_label.setToolTip("")
        self._update_controls()
        start_flatpak_refresh(self, QThreadPool.globalInstance())
        return True

    def _finish_failed(self, serial: int, operation: str, detail: str) -> bool:
        if serial != self._request_serial or self._pending != operation:
            return False
        self._pending = None
        self._clear_cancel()
        self.status_label.setText(detail)
        self.status_label.setToolTip(detail)
        self._update_controls()
        # The app may be partly changed, so always re-read the real state.
        start_flatpak_refresh(self, QThreadPool.globalInstance())
        return True

    @pyqtSlot(int, str)
    def show_uninstall_done_if_current(self, serial: int, message: str) -> bool:
        return self._finish_done(serial, "uninstall", message)

    @pyqtSlot(int, str)
    def show_uninstall_failed_if_current(self, serial: int, detail: str) -> bool:
        return self._finish_failed(serial, "uninstall", detail)

    @pyqtSlot(int, str)
    def show_update_done_if_current(self, serial: int, message: str) -> bool:
        return self._finish_done(serial, "update", message)

    @pyqtSlot(int, str)
    def show_system_update_done_if_current(self, serial: int, message: str) -> bool:
        return self._finish_done(serial, "update", message)

    @pyqtSlot(int, str)
    def show_update_failed_if_current(self, serial: int, detail: str) -> bool:
        return self._finish_failed(serial, "update", detail)

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


def start_flatpak_action(
    dialog: FlatpakInventoryDialog,
    thread_pool: QThreadPool,
    entry: FlatpakEntry,
    operation: str,
    *,
    delete_data: bool = False,
) -> None:
    """Begin one identified uninstall or update and route its single result."""
    serial = dialog.begin_action(entry, operation)
    worker = FlatpakUserActionWorker(
        serial, entry, operation=operation, delete_data=delete_data
    )
    if operation == "uninstall":
        done_slot = dialog.show_uninstall_done_if_current
        failed_slot = dialog.show_uninstall_failed_if_current
    else:
        done_slot = dialog.show_update_done_if_current
        failed_slot = dialog.show_update_failed_if_current
    worker.signals.succeeded.connect(done_slot)
    worker.signals.failed.connect(failed_slot)
    try:
        thread_pool.start(worker)
    except Exception as error:
        failed_slot(
            serial,
            f"Could not start {operation}: {type(error).__name__}: {error}",
        )


def start_flatpak_system_uninstall(
    dialog: FlatpakInventoryDialog,
    thread_pool: QThreadPool,
    entry: FlatpakEntry,
    *,
    delete_data: bool,
) -> None:
    """Begin one identified system-wide uninstall through Flatpak's helper and Polkit."""
    runner = CancellableRunner()
    serial = dialog.begin_action(entry, "uninstall", cancel=runner.cancel)
    gate = dialog.host_operation_gate
    worker = FlatpakSystemUninstallWorker(
        serial,
        entry,
        delete_data=delete_data,
        gate=gate,
        action=lambda: run_flatpak_system_uninstall(
            entry, delete_data=delete_data, gate=gate, runner=runner
        ),
    )
    worker.signals.succeeded.connect(dialog.show_uninstall_done_if_current)
    worker.signals.failed.connect(dialog.show_uninstall_failed_if_current)
    try:
        thread_pool.start(worker)
    except Exception as error:
        dialog.show_uninstall_failed_if_current(
            serial,
            f"Could not start uninstall: {type(error).__name__}: {error}",
        )


def start_flatpak_uninstall_with_runtime(
    dialog: FlatpakInventoryDialog,
    thread_pool: QThreadPool,
    entry: FlatpakEntry,
    runtime: FlatpakEntry,
    *,
    delete_data: bool,
) -> None:
    """Uninstall one app, then its unused runtime, as one identified action."""
    gate = dialog.host_operation_gate
    runner = CancellableRunner() if is_system_app(entry) else None
    serial = dialog.begin_action(
        entry, "uninstall", cancel=runner.cancel if runner else None
    )
    if runner is not None:

        def remove_app() -> str:
            return run_flatpak_system_uninstall(
                entry, delete_data=delete_data, gate=gate, runner=runner
            )

        worker: QRunnable = FlatpakSystemUninstallWorker(
            serial,
            entry,
            delete_data=delete_data,
            gate=gate,
            action=lambda: uninstall_app_and_runtime(
                remove_app, runtime, gate=gate, runner=runner
            ),
        )
    else:

        def remove_user_app() -> str:
            return run_flatpak_uninstall(entry, delete_data=delete_data)

        worker = FlatpakUserActionWorker(
            serial,
            entry,
            operation="uninstall",
            action=lambda: uninstall_app_and_runtime(
                remove_user_app, runtime, gate=gate
            ),
        )
    worker.signals.succeeded.connect(dialog.show_uninstall_done_if_current)
    worker.signals.failed.connect(dialog.show_uninstall_failed_if_current)
    try:
        thread_pool.start(worker)
    except Exception as error:
        dialog.show_uninstall_failed_if_current(
            serial,
            f"Could not start uninstall: {type(error).__name__}: {error}",
        )


def start_flatpak_uninstall(
    dialog: FlatpakInventoryDialog,
    thread_pool: QThreadPool,
    entry: FlatpakEntry,
    *,
    delete_data: bool,
    runtime: FlatpakEntry | None = None,
) -> None:
    if runtime is not None:
        start_flatpak_uninstall_with_runtime(
            dialog, thread_pool, entry, runtime, delete_data=delete_data
        )
        return
    if is_system_app(entry):
        start_flatpak_system_uninstall(
            dialog, thread_pool, entry, delete_data=delete_data
        )
    else:
        start_flatpak_action(
            dialog, thread_pool, entry, "uninstall", delete_data=delete_data
        )


def start_flatpak_system_update(
    dialog: FlatpakInventoryDialog,
    thread_pool: QThreadPool,
    entry: FlatpakEntry,
) -> None:
    """Begin one identified system-wide update through Flatpak's helper and Polkit."""
    runner = CancellableRunner()
    serial = dialog.begin_action(entry, "update", cancel=runner.cancel)
    gate = dialog.host_operation_gate
    worker = FlatpakSystemUpdateWorker(
        serial,
        entry,
        gate=gate,
        action=lambda: run_flatpak_system_update(entry, gate=gate, runner=runner),
    )
    worker.signals.succeeded.connect(dialog.show_system_update_done_if_current)
    worker.signals.failed.connect(dialog.show_update_failed_if_current)
    try:
        thread_pool.start(worker)
    except Exception as error:
        dialog.show_update_failed_if_current(
            serial,
            f"Could not start update: {type(error).__name__}: {error}",
        )


def start_flatpak_update(
    dialog: FlatpakInventoryDialog,
    thread_pool: QThreadPool,
    entry: FlatpakEntry,
) -> None:
    if is_system_app(entry):
        start_flatpak_system_update(dialog, thread_pool, entry)
    else:
        start_flatpak_action(dialog, thread_pool, entry, "update")
