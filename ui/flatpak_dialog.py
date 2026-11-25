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
from typing import Final

from PyQt6.QtCore import QRunnable, Qt, QThreadPool, pyqtSlot
from PyQt6.QtGui import QCloseEvent, QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.flatpak_cancel import CancellableRunner
from core.flatpak_inventory import (
    FlatpakEntry,
    FlatpakInventory,
    FlatpakInventoryWorker,
)
from core.flatpak_progress import FlatpakProgress, progress_line_handler
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
    run_flatpak_user_action,
)
from core.i18n import tr
from ui.fonts import DETAIL_PT_DELTA, apply_labelled_fonts, scaled_font
from ui.icons import theme_icon
from ui.table_rows import WholeRowFitter

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
    'other app needs it. To update runtimes, run "flatpak update" in a terminal. '
    'To remove runtimes that no app uses, run "flatpak uninstall --unused".'
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


COLUMN_HEADERS: Final[tuple[str, ...]] = (
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
_SIZE_COLUMN = COLUMN_HEADERS.index("Installed size")
_APP_ID_COLUMN = COLUMN_HEADERS.index("Application ID")
_NAME_COLUMN = COLUMN_HEADERS.index("Name")
# Header/cell padding (7px each side) plus a small safety margin.
_CELL_PADDING = 22
# Letter spacing the stylesheet applies to header sections.
_HEADER_LETTER_SPACING = 0.3
# A sorted heading draws its arrow at the trailing edge, over the label.
_SORT_INDICATOR_WIDTH = 14


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
        self.setWindowTitle(tr("Uninstall Flatpak App"))
        layout = QVBoxLayout(self)

        title = QLabel(f"Uninstall {entry.name}?")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        describe = (
            describe_system_uninstall if is_system_app(entry) else describe_uninstall
        )
        info = QLabel(describe(entry, delete_data=False))
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(info)

        self.delete_box = QCheckBox(
            tr("Also delete this app's data. This cannot be undone.")
        )
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
        cancel_button = QPushButton(tr("Cancel"))
        cancel_button.setDefault(True)
        cancel_button.clicked.connect(self.reject)
        uninstall_button = QPushButton(tr("Uninstall"))
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


def _count_label(count: int, noun: str) -> str:
    """Render ``1 app`` / ``3 apps`` instead of a placeholder ``app(s)``."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class FlatpakInventoryDialog(QDialog):
    """Shows one Flatpak inventory at a time; updates apps and uninstalls user apps."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("Flatpak Apps & Runtimes"))
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
        self._close_after_action = False
        self._warned_non_cancellable_close = False
        # The main window owns the shared gate that serializes host operations.
        # Without it, system-wide updates are refused rather than run unguarded.
        self.host_operation_gate = getattr(parent, "host_operation_gate", None)
        self._init_ui()
        apply_labelled_fonts(self)
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

        title = QLabel(tr("Flatpak Apps & Runtimes"))
        title.setObjectName("DialogTitleLabel")
        self.refresh_btn = QPushButton(tr("Refresh"))
        refresh_icon = theme_icon("view-refresh")
        if not refresh_icon.isNull():
            self.refresh_btn.setIcon(refresh_icon)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        self.uninstall_btn = QPushButton(tr("Uninstall…"))
        self.uninstall_btn.setObjectName("FlatpakUninstallButton")
        uninstall_icon = theme_icon("edit-delete", "user-trash")
        if not uninstall_icon.isNull():
            self.uninstall_btn.setIcon(uninstall_icon)
        self.uninstall_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.uninstall_btn.setToolTip(_UNINSTALL_TIP)
        self.uninstall_btn.clicked.connect(self._on_uninstall_clicked)
        self.update_btn = QPushButton(tr("Update"))
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
        self.cancel_btn = QPushButton(tr("Cancel"))
        self.cancel_btn.setObjectName("FlatpakCancelButton")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setToolTip(
            tr(
                "Stop this system-wide change. It may be partly applied, so refresh afterwards."
            )
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
        notice.setFont(scaled_font(DETAIL_PT_DELTA))
        layout.addWidget(notice)

        self.status_label = QLabel("")
        self.status_label.setObjectName("FlatpakStatusLabel")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setFont(scaled_font(DETAIL_PT_DELTA))
        layout.addWidget(self.status_label)

        # Shown only while an update runs. Flatpak's own percentage drives the bar; a
        # busy (moving) bar is used while Flatpak reports no percentage.
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("FlatpakProgressBar")
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.progress_steps_label = QLabel("")
        self.progress_steps_label.setObjectName("FlatpakProgressSteps")
        self.progress_steps_label.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_steps_label.setWordWrap(True)
        self.progress_steps_label.setFont(scaled_font(DETAIL_PT_DELTA))
        self.progress_steps_label.setVisible(False)
        layout.addWidget(self.progress_steps_label)

        # Explains why the selected row cannot be changed. Kept apart from the
        # status line so it never hides the result of the last action.
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("FlatpakSelectionHint")
        self.hint_label.setTextFormat(Qt.TextFormat.PlainText)
        self.hint_label.setWordWrap(True)
        self.hint_label.setFont(scaled_font(DETAIL_PT_DELTA))
        self.hint_label.setVisible(False)
        layout.addWidget(self.hint_label)

        self.table = QTableWidget(0, len(COLUMN_HEADERS))
        self.table.setObjectName("FlatpakInventoryTable")
        self.table.setHorizontalHeaderLabels([tr(text) for text in COLUMN_HEADERS])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self._update_controls)
        header = self.table.horizontalHeader()
        # Size every column from its *content*, never from its heading: the nine
        # headings together are wider than the dialog, so ResizeToContents pads
        # each column to its (now upper-cased) label and cuts off the last one.
        # Headings get an elided tooltip from headerData instead.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self._row_fitter = WholeRowFitter(self.table)
        self.table.installEventFilter(self._row_fitter)
        layout.addWidget(self.table, stretch=1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_btn = QPushButton(tr("Close"))
        close_btn.clicked.connect(self.close)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

    def _can_close_now(self) -> bool:
        if self._pending not in {"uninstall", "update"}:
            return True
        if self._cancel is not None:
            if not self._cancel_requested:
                self._on_cancel_clicked()
            self._close_after_action = True
            self.status_label.setText(
                tr("Cancelling active Flatpak operation before closing…")
            )
            self.status_label.setToolTip("")
            return False
        if not self._warned_non_cancellable_close:
            self._warned_non_cancellable_close = True
            self.status_label.setText(
                tr(
                    "A Flatpak operation is still running. Wait for it to finish, "
                    "or click Close again to close this window."
                )
            )
            self.status_label.setToolTip("")
            return False
        return True

    def accept(self) -> None:
        if self._can_close_now():
            super().accept()

    def reject(self) -> None:
        if self._can_close_now():
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._can_close_now():
            event.accept()
            return
        event.ignore()

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
        self._close_after_action = False
        self._warned_non_cancellable_close = False
        self.status_label.setText(tr("Loading Flatpak apps and runtimes…"))
        self.status_label.setToolTip("")
        self._update_controls()
        return self._request_serial

    def _on_refresh_clicked(self) -> None:
        self._last_result = ""
        start_flatpak_refresh(self, QThreadPool.globalInstance())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # ``_fit_table_columns`` used to run only when an inventory arrived, so
        # a table that was still empty (or whose Flatpak backend is unavailable)
        # kept Qt's arbitrary default of 100px per column and cut the headings.
        self._fit_table_columns()

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
            counts = (
                f"{_count_label(apps, 'app')}, "
                f"{_count_label(runtimes, 'runtime')} installed."
            )
        else:
            counts = "No Flatpak apps or runtimes are installed."
        self.status_label.setText(f"{self._last_result} {counts}".strip())
        self.status_label.setToolTip("")
        self._update_controls()
        self._fit_table_columns()
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
        self._close_after_action = False
        self._warned_non_cancellable_close = False
        gerund = "Uninstalling" if operation == "uninstall" else "Updating"
        self.status_label.setText(f"{gerund} {entry.name}…")
        self.status_label.setToolTip("")
        if operation == "update":
            self._show_progress_busy()
        else:
            self._hide_progress()
        self._update_controls()
        return self._request_serial

    def _show_progress_busy(self) -> None:
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self.progress_steps_label.setText("")
        self.progress_steps_label.setVisible(False)

    def _hide_progress(self) -> None:
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_steps_label.setText("")
        self.progress_steps_label.setVisible(False)

    @pyqtSlot(int, object)
    def show_update_progress_if_current(
        self, serial: int, snapshot: FlatpakProgress
    ) -> bool:
        """Show one progress snapshot of the pending update. Stale ones are dropped."""
        if serial != self._request_serial or self._pending != "update":
            return False
        overall = snapshot.overall_percent
        if overall is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(overall)
        self.progress_bar.setVisible(True)
        steps = snapshot.steps_text
        self.progress_steps_label.setText(steps)
        self.progress_steps_label.setVisible(bool(steps))
        # After Cancel is clicked, keep the "Cancelling…" line rather than overwrite it.
        if not self._cancel_requested:
            self.status_label.setText(snapshot.status_text)
        return True

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
        self.status_label.setText(tr("Cancelling…"))
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
        self._warned_non_cancellable_close = False
        self._hide_progress()
        self._last_result = message
        close_now = self._close_after_action
        self._close_after_action = False
        self.status_label.setText(f"{message} Refreshing the list…")
        self.status_label.setToolTip("")
        self._update_controls()
        if close_now:
            self.close()
            return True
        start_flatpak_refresh(self, QThreadPool.globalInstance())
        return True

    def _finish_failed(self, serial: int, operation: str, detail: str) -> bool:
        if serial != self._request_serial or self._pending != operation:
            return False
        self._pending = None
        self._clear_cancel()
        self._warned_non_cancellable_close = False
        self._hide_progress()
        close_now = self._close_after_action
        self._close_after_action = False
        self.status_label.setText(detail)
        self.status_label.setToolTip(detail)
        self._update_controls()
        if close_now:
            self.close()
            return True
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

    def _fit_table_columns(self) -> None:
        """Size inventory columns for both their heading and their widest cell.

        Qt's ``ResizeToContents`` measures with the style's full header padding,
        which made nine upper-cased headings wider than the dialog and cut the
        last column out of view. Measuring here keeps every heading readable and
        hands the remaining width to the name and application-ID columns.
        """
        metrics = QFontMetrics(self.table.font())
        header = self.table.horizontalHeader()
        # The stylesheet paints headings in a bold weight, so measuring the
        # header's own (regular) font understated every heading and clipped the
        # longest ones. Measure the weight that is actually drawn.
        heading_font = QFont(header.font())
        heading_font.setWeight(QFont.Weight.Bold)
        header_metrics = QFontMetrics(heading_font)
        needed: list[int] = []
        for column in range(self.table.columnCount()):
            width = 0
            for row in range(self.table.rowCount()):
                item = self.table.item(row, column)
                if item is not None:
                    width = max(width, metrics.horizontalAdvance(item.text()))
            heading = self.table.horizontalHeaderItem(column)
            if heading is not None:
                # The stylesheet upper-cases headings and adds 0.3px of letter
                # spacing, neither of which QFontMetrics accounts for.
                label = heading.text().upper()
                width = max(
                    width,
                    header_metrics.horizontalAdvance(label)
                    + int(_HEADER_LETTER_SPACING * len(label)),
                )
            needed.append(width + _CELL_PADDING)
        # Qt draws the sort arrow inside the sorted heading, on top of the text.
        # Without room for it the first column rendered as "KIND▾" overlapping.
        sorted_column = header.sortIndicatorSection()
        if 0 <= sorted_column < len(needed):
            needed[sorted_column] += _SORT_INDICATOR_WIDTH
        available = self.table.viewport().width()
        if available <= 0:
            return
        total = sum(needed)
        if total < available:
            surplus = available - total
            share, remainder = divmod(surplus, 2)
            needed[_NAME_COLUMN] += share + remainder
            needed[_APP_ID_COLUMN] += share
        elif total > available:
            # A wider desktop font can make the headings alone exceed the
            # viewport. Take the difference back from the two flexible columns,
            # never from a column that is already at its heading's own width.
            excess = total - available
            for column in (_APP_ID_COLUMN, _NAME_COLUMN):
                if excess <= 0:
                    break
                floor = needed[column] // 2
                give = min(excess, max(0, needed[column] - floor))
                needed[column] -= give
                excess -= give
        for column, width in enumerate(needed):
            header.resizeSection(column, width)

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
        # Rows are a fixed height and the table's height comes from the dialog,
        # so the bottom edge used to cut through the last visible row.
        self._row_fitter.fit()
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
    if operation == "update":
        # Per-user updates stream Flatpak's output for the progress bar. They are not
        # cancellable (no cancel callback is given), matching the earlier behaviour.
        runner = CancellableRunner()
        worker = FlatpakUserActionWorker(
            serial,
            entry,
            operation=operation,
            action=lambda: run_flatpak_user_action(entry, operation, runner=runner),
        )
        runner.set_on_line(progress_line_handler(worker.report_progress))
        worker.signals.progress.connect(dialog.show_update_progress_if_current)
    else:
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
    runner.set_on_line(progress_line_handler(worker.report_progress))
    worker.signals.progress.connect(dialog.show_update_progress_if_current)
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
