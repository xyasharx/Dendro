"""Widget, focus-rendering, and dialog-flow checks for UX & accessibility fixes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QModelIndex, QRect, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QStyle,
    QStyleOptionViewItem,
    QTabWidget,
)

from dendro.core.backend import HistoryEntry, PackageInfo, PackageState
from dendro.core.dnf5daemon import (
    Dnf5DaemonError,
    DnfRepository,
    TransactionItem,
    TransactionPlan,
    TransactionRequest,
)
from dendro.core.models import DependencyTreeModel
from dendro.ui.delegates import DendroTreeView, PackageTreeItemDelegate
from dendro.ui.history_dialog import DnfHistoryDialog
from dendro.ui.main_window_transactions import MainWindowTransactionsMixin
from dendro.ui.repo_dialog import RepoManagerDialog
from dendro.ui.settings_dialog import DendroSettingsDialog
from dendro.ui.sidebar import CategorySidebar, SidebarItemDelegate
from dendro.ui.styles import get_theme_stylesheet
from dendro.ui.theme_palettes import THEMES_CONFIG
from dendro.ui.transaction_plan_dialog import TransactionPlanReviewDialog


@pytest.mark.parametrize("theme_key", ("dark", "light"))
def test_danger_button_renders_expected_high_contrast_colors(
    qtbot, theme_key: str
) -> None:
    """DangerButton renders its accessible danger palette rather than muted border tint."""
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(get_theme_stylesheet(theme_key))

    plan = TransactionPlan(
        token="danger-test",
        session_path="/org/rpm/dnf/v0/session/danger",
        request=TransactionRequest.from_values(removals=["systemd"]),
        items=(
            TransactionItem(
                object_type="Package",
                action="Remove",
                reason="User",
                attributes={},
                package={
                    "name": "systemd",
                    "version": "1",
                    "release": "1.fc45",
                    "arch": "x86_64",
                },
            ),
        ),
        resolver_result=0,
        warnings=(),
        critical_removals=("systemd",),
    )
    dialog = TransactionPlanReviewDialog(plan)
    qtbot.addWidget(dialog)
    dialog.show()
    for _ in range(6):
        app.processEvents()
    try:
        btn = dialog.execute_button
        assert btn.objectName() == "DangerButton"
        assert btn.autoDefault() is False

        img = btn.grab().toImage()
        # Sample top interior padding away from both rounded corners and button text.
        top_bg = img.pixelColor(10, 4).name()
        assert top_bg == QColor(THEMES_CONFIG[theme_key]["danger_bg"]).name()
    finally:
        dialog.close()
        app.processEvents()


def test_repo_dialog_inline_operation_panel_lifecycle_and_whole_row_fit(qtbot) -> None:
    """RepoManagerDialog keeps progress, logs, cancel state, and errors inline."""
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(get_theme_stylesheet("dark"))

    dialog = RepoManagerDialog()
    qtbot.addWidget(dialog)
    dialog.set_repositories(
        [
            DnfRepository(
                identifier=f"repo-{idx}",
                name=f"Repository {idx}",
                enabled=True,
                baseurl="https://example.test/repo",
                repo_file=f"/etc/yum.repos.d/repo-{idx}.repo",
            )
            for idx in range(25)
        ]
    )
    dialog.resize(880, 620)
    dialog.show()
    app.processEvents()

    assert dialog.operation_panel.isHidden()

    # Non-cancellable DNF5 maintenance call disables Cancel upfront with explanation.
    dialog.start_operation(
        "Cleaning DNF package cache through DNF5…",
        cancellable=False,
        cancel_tooltip="DNF5 daemon maintenance calls cannot be cancelled mid-flight.",
    )
    app.processEvents()
    assert not dialog.operation_panel.isHidden()
    assert not dialog.btn_cancel_operation.isEnabled()
    assert "cannot be cancelled" in dialog.btn_cancel_operation.toolTip()
    assert dialog.operation_progress.minimum() == 0
    assert dialog.operation_progress.maximum() == 0

    dialog.append_operation_log("Step 1: preparing\rStep 2: cleaning cache\n")
    assert "Step 2: cleaning cache" in dialog.operation_log.toPlainText()

    # Failure shows inline log and keeps dialog open without modal popups.
    dialog.finish_operation(
        False, "Failed to clean DNF cache", detail="Polkit authorization cancelled."
    )
    app.processEvents()
    assert not dialog.operation_panel.isHidden()
    assert dialog.operation_progress.isHidden()
    assert dialog.btn_cancel_operation.isHidden()
    assert not dialog.btn_dismiss_operation.isHidden()
    assert "Polkit authorization cancelled." in dialog.operation_log.toPlainText()

    # Cancellable repo add/delete enables Cancel and emits cancel_operation_requested.
    cancelled: list[bool] = []
    dialog.cancel_operation_requested.connect(lambda: cancelled.append(True))
    dialog.start_operation("Downloading repo…", cancellable=True)
    app.processEvents()
    assert dialog.btn_cancel_operation.isEnabled()
    dialog.btn_cancel_operation.click()
    assert cancelled == [True]

    dialog.finish_operation(True, "Repository configuration file added.")
    for _ in range(4):
        app.processEvents()
    assert dialog.operation_log.isHidden()
    assert not dialog.btn_dismiss_operation.isHidden()
    dialog.btn_dismiss_operation.click()
    for _ in range(6):
        app.processEvents()
    dialog._row_fitter.fit()
    for _ in range(4):
        app.processEvents()
    assert dialog.operation_panel.isHidden()

    row_height = dialog.table.rowHeight(0)
    assert row_height > 0
    assert dialog.table.viewport().height() % row_height == 0


def test_repo_dialog_operations_stay_inline_and_suppress_duplicate_modals() -> None:
    """MainWindowTransactionsMixin keeps repo ops inline and avoids duplicate popups."""

    class _FakeRepoDialog:
        def __init__(self) -> None:
            self.started: list[tuple[str, bool, str]] = []
            self.logs: list[str] = []
            self.finished: list[tuple[bool, str, str]] = []
            self.cleared = False

        def start_operation(
            self,
            description: str,
            *,
            cancellable: bool = True,
            cancel_tooltip: str = "",
        ) -> None:
            self.started.append((description, cancellable, cancel_tooltip))

        def append_operation_log(self, text: str) -> None:
            self.logs.append(text)

        def finish_operation(
            self, success: bool, summary: str, *, detail: str = ""
        ) -> None:
            self.finished.append((success, summary, detail))

        def clear_repository_address(self) -> None:
            self.cleared = True

    class _FakeDrawer:
        def __init__(self) -> None:
            self.shown = False
            self.logs: list[str] = []
            self.started = 0
            self.finished: list[bool] = []

        def isHidden(self) -> bool:
            return not self.shown

        def show(self) -> None:
            self.shown = True

        def start_execution_mode(self) -> None:
            self.started += 1

        def finish_execution_mode(self, success: bool) -> None:
            self.finished.append(success)

        def append_log(self, text: str) -> None:
            self.logs.append(text)

    class _FakeRunner:
        def __init__(self, should_fail: bool = False) -> None:
            self.is_running = False
            self.cancelled = False
            self.should_fail = should_fail

        def add_from_url(self, _url: str) -> None:
            if self.should_fail:
                raise Dnf5DaemonError("Bad URL", "Invalid endpoint")
            self.is_running = True

        def delete(self, _path: str) -> None:
            self.is_running = True

        def cancel(self) -> None:
            self.cancelled = True

    class _FakeController:
        def __init__(self) -> None:
            self.is_busy = False
            self.has_ready_plan = False
            self.cleaned = 0
            self.toggled: list[tuple[str, bool]] = []

        def clean_cache(self) -> None:
            self.is_busy = True
            self.cleaned += 1

        def set_repository_enabled(self, repo_id: str, enable: bool) -> None:
            self.is_busy = True
            self.toggled.append((repo_id, enable))

    class _Harness(MainWindowTransactionsMixin):
        def __init__(self) -> None:
            self._active_repo_dialog = _FakeRepoDialog()
            self._repo_dialog_op_kind = ""
            self._repo_inline_error_handled = False
            self.transaction_drawer = _FakeDrawer()
            self.workspace_splitter = SimpleNamespace(sizes_set=[])
            self.workspace_splitter.setSizes = self.workspace_splitter.sizes_set.append
            self.repository_addition_runner = _FakeRunner()
            self.repository_deletion_runner = _FakeRunner()
            self.transaction_controller = _FakeController()
            self.status_bar = SimpleNamespace(messages=[])
            self.status_bar.showMessage = self.status_bar.messages.append
            self.reloaded = 0
            self.popups: list[tuple[str, str]] = []
            self._pending_transaction_kind = ""
            self._last_transaction_upgrades = []
            self._last_transaction_removals = []

        def _load_repositories_from_daemon(self) -> None:
            self.reloaded += 1

        def _show_transaction_error(self, title: str, detail: str) -> None:
            self.popups.append((title, detail))

        def _continue_close_if_requested(self) -> None:
            pass

    h = _Harness()
    h._on_repo_add_requested("https://example.test/repo.repo")
    assert h.transaction_drawer.shown is False
    assert h._active_repo_dialog.started[-1][1] is True
    h._on_repo_runner_log_received("Downloading repo file...\n")
    assert h._active_repo_dialog.logs == ["Downloading repo file...\n"]
    assert h.transaction_drawer.logs == ["Downloading repo file...\n"]
    h._on_repo_dialog_cancel_requested()
    assert h.repository_addition_runner.cancelled is True
    h.repository_addition_runner.is_running = False
    h._on_repository_addition_finished(False, "Cancelled by user")
    assert h.popups == []

    h._on_clean_cache_requested()
    assert h.transaction_drawer.shown is False
    assert h._active_repo_dialog.started[-1][1] is False
    h._on_transaction_log_received("Requesting auth...\n")
    assert "Requesting auth...\n" in h._active_repo_dialog.logs
    h.transaction_controller.is_busy = False
    h._on_maintenance_finished("clean DNF cache", False, "Polkit cancelled")
    h._on_dnf_operation_failed("Authorization failed", "Polkit cancelled")
    assert h.popups == []
    assert h._active_repo_dialog.finished[-1] == (
        False,
        "Failed to clean DNF cache",
        "Polkit cancelled",
    )

    h._on_repo_remove_requested("rpmfusion", "/etc/yum.repos.d/rpmfusion.repo")
    assert h.transaction_drawer.shown is False
    h._clear_active_repo_dialog(h._active_repo_dialog)
    assert h.transaction_drawer.shown is True
    assert h.workspace_splitter.sizes_set[-1] == [450, 320]
    h.repository_deletion_runner.is_running = False
    h._on_repository_deletion_finished(False, "Helper failed")
    assert h.popups == [("Repository deletion failed", "Helper failed")]


def test_history_dialog_read_only_inspection_and_clipboard_copy(qtbot) -> None:
    """DnfHistoryDialog inspects transactions and copies undo commands without closing."""
    dialog = DnfHistoryDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    QApplication.processEvents()

    assert dialog.windowTitle() == "DNF Transaction History"
    assert "without a rollback RPC" in dialog.capability_notice_label.text()
    assert not hasattr(dialog, "undo_btn")

    entries = [
        HistoryEntry(
            id=105,
            command_line="dnf install neovim ripgrep",
            date_time="2026-11-16 14:30",
            action="Install",
            altered_count=4,
            return_code=0,
        ),
        HistoryEntry(
            id=104,
            command_line="dnf remove nano",
            date_time="2026-11-15 09:12",
            action="Remove",
            altered_count=1,
            return_code=0,
        ),
    ]
    dialog.set_history_entries(entries)
    QApplication.processEvents()

    # Row 0 is auto-selected so inspect & copy actions are immediately ready.
    selected = dialog.table.selectionModel().selectedRows()
    assert len(selected) == 1
    assert selected[0].row() == 0
    assert dialog.inspect_btn.isEnabled()
    assert dialog.copy_undo_btn.isEnabled()

    # Inspect opens inline detail panel without closing the dialog.
    dialog.inspect_btn.click()
    QApplication.processEvents()
    assert dialog.isVisible()
    assert not dialog.detail_panel.isHidden()
    assert "#105" in dialog.detail_title_label.text()
    detail_text = dialog.detail_command_view.toPlainText()
    assert "dnf install neovim ripgrep" in detail_text
    assert "sudo dnf history info 105" in detail_text
    assert "sudo dnf history undo 105" in detail_text

    # Switching selection updates the inline detail panel automatically.
    dialog.table.selectRow(1)
    QApplication.processEvents()
    assert "#104" in dialog.detail_title_label.text()
    assert "sudo dnf history undo 104" in dialog.detail_command_view.toPlainText()

    # Copying undo command writes valid DNF5 syntax and keeps dialog open.
    dialog.copy_undo_btn.click()
    QApplication.processEvents()
    assert dialog.isVisible()
    clipboard = QApplication.clipboard()
    if clipboard is not None:
        assert clipboard.text() == "sudo dnf history undo 104"
    assert dialog._copied_undo_command == "sudo dnf history undo 104"
    assert "sudo dnf history undo 104" in dialog.status_label.text()

    # Non-DNF5 hosts disable copying undo commands while keeping inspection enabled.
    dialog.set_undo_supported(False, tooltip="Disabled on Fedora Atomic.")
    assert not dialog.copy_undo_btn.isEnabled()
    assert dialog.inspect_btn.isEnabled()


@pytest.mark.parametrize("theme_key", ("dark", "light"))
def test_tree_view_multi_column_focus_and_active_vs_inactive_selection(
    qtbot, theme_key: str
) -> None:
    """PackageTreeItemDelegate paints row-wide focus rings and distinct inactive state."""
    view = DendroTreeView()
    qtbot.addWidget(view)
    assert view.allColumnsShowFocus() is True

    model = DependencyTreeModel()
    model.set_packages(
        [
            PackageInfo(
                name="ripgrep",
                version="14.1.1",
                release="1.fc45",
                arch="x86_64",
                summary="Fast search tool",
                description="Fast search tool",
                repo="fedora",
                state=PackageState.INSTALLED,
                size_bytes=4096,
            )
        ]
    )
    view.setModel(model)
    delegate = PackageTreeItemDelegate(view)
    delegate.set_theme(theme_key)
    accent_hex = QColor(THEMES_CONFIG[theme_key]["accent"]).name()

    def _paint_cell(col: int, state: QStyle.StateFlag, pos) -> QImage:
        img = QImage(120, 34, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 120, 34)
        opt.state = state
        opt.viewItemPosition = pos
        opt.widget = None
        painter = QPainter(img)
        delegate.paint(painter, opt, model.index(0, col))
        painter.end()
        return img

    # Row-wide focus ring spans Beginning, Middle, and End columns.
    active_focus = (
        QStyle.StateFlag.State_Enabled
        | QStyle.StateFlag.State_Selected
        | QStyle.StateFlag.State_HasFocus
        | QStyle.StateFlag.State_Active
    )
    img_begin = _paint_cell(
        0, active_focus, QStyleOptionViewItem.ViewItemPosition.Beginning
    )
    img_mid = _paint_cell(3, active_focus, QStyleOptionViewItem.ViewItemPosition.Middle)
    img_end = _paint_cell(4, active_focus, QStyleOptionViewItem.ViewItemPosition.End)

    assert img_begin.pixelColor(1, 17).name() == accent_hex
    assert img_mid.pixelColor(60, 1).name() == accent_hex
    assert img_mid.pixelColor(60, 32).name() == accent_hex
    assert img_end.pixelColor(118, 17).name() == accent_hex

    # Active selected fill clearly differs from unfocused/inactive selected fill.
    img_active_sel = _paint_cell(
        4,
        QStyle.StateFlag.State_Enabled
        | QStyle.StateFlag.State_Selected
        | QStyle.StateFlag.State_Active,
        QStyleOptionViewItem.ViewItemPosition.End,
    )
    img_inactive_sel = _paint_cell(
        4,
        QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Selected,
        QStyleOptionViewItem.ViewItemPosition.End,
    )
    assert (
        img_active_sel.pixelColor(10, 6).name()
        != img_inactive_sel.pixelColor(10, 6).name()
    )


@pytest.mark.parametrize("theme_key", ("dark", "light"))
def test_sidebar_delegate_focus_ring_and_active_vs_inactive_selection(
    qtbot, theme_key: str
) -> None:
    """SidebarItemDelegate renders keyboard focus ring and differentiates inactive selection."""
    sidebar = CategorySidebar()
    qtbot.addWidget(sidebar)
    delegate = SidebarItemDelegate(sidebar)
    delegate.set_theme(theme_key)
    accent_hex = QColor(THEMES_CONFIG[theme_key]["accent"]).name()

    row_idx: QModelIndex = sidebar.model().index(1, 0)

    def _paint_sidebar(state: QStyle.StateFlag) -> QImage:
        img = QImage(220, 32, QImage.Format.Format_ARGB32)
        img.fill(QColor(THEMES_CONFIG[theme_key]["bg_surface"]))
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 220, 32)
        opt.state = state
        opt.widget = None
        painter = QPainter(img)
        delegate.paint(painter, opt, row_idx)
        painter.end()
        return img

    img_focused = _paint_sidebar(
        QStyle.StateFlag.State_Enabled
        | QStyle.StateFlag.State_Selected
        | QStyle.StateFlag.State_HasFocus
        | QStyle.StateFlag.State_Active
    )
    img_active_unfocused = _paint_sidebar(
        QStyle.StateFlag.State_Enabled
        | QStyle.StateFlag.State_Selected
        | QStyle.StateFlag.State_Active
    )
    img_inactive = _paint_sidebar(
        QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Selected
    )

    # Focused item paints top accent focus ring at x=110, y=2.
    assert img_focused.pixelColor(110, 2).name() == accent_hex
    assert img_active_unfocused.pixelColor(110, 2).name() != accent_hex
    # Active vs inactive selection fill differs at pill interior.
    assert (
        img_active_unfocused.pixelColor(200, 16).name()
        != img_inactive.pixelColor(200, 16).name()
    )


@pytest.mark.parametrize("theme_key", ("dark", "light"))
def test_checkbox_focus_remains_unclipped_at_7pt_in_repo_table(
    qapp, theme_key: str
) -> None:
    """Focused checkboxes in RepoManagerDialog cells never clip even at 7pt font."""
    prev_font = QFont(qapp.font())
    qapp.setFont(QFont("DejaVu Sans", 7))
    qapp.setStyleSheet(get_theme_stylesheet(theme_key))
    dialog = RepoManagerDialog()
    try:
        dialog.set_repositories(
            [
                DnfRepository(
                    identifier="fedora",
                    name="Fedora",
                    enabled=True,
                    baseurl="https://example.test",
                    repo_file="/etc/yum.repos.d/fedora.repo",
                )
            ]
        )
        dialog.show()
        for _ in range(6):
            qapp.processEvents()
        cell = dialog.table.cellWidget(0, 0)
        assert cell is not None
        cb = cell.findChild(QCheckBox)
        assert cb is not None
        cb.setFocus(Qt.FocusReason.TabFocusReason)
        qapp.processEvents()
        geom = cb.geometry()
        assert geom.top() >= 0
        assert geom.bottom() < cell.height()
    finally:
        dialog.close()
        qapp.setFont(prev_font)
        qapp.processEvents()


def test_focused_tab_keeps_single_contiguous_top_accent_band(qapp) -> None:
    """Focusing a tab bar does not break its top accent band or leak to bottom."""
    qapp.setStyleSheet(get_theme_stylesheet("dark"))
    dialog = DendroSettingsDialog()
    dialog.show()
    dialog.resize(760, 636)
    for _ in range(8):
        qapp.processEvents()
    try:
        tabs = dialog.findChild(QTabWidget)
        assert tabs is not None
        bar = tabs.tabBar()
        bar.setFocus(Qt.FocusReason.TabFocusReason)
        qapp.processEvents()
        rect = bar.tabRect(0)
        image = bar.grab().toImage()
        accent = QColor(THEMES_CONFIG["dark"]["accent"]).name()
        x = rect.center().x()
        accents = [
            y
            for y in range(min(rect.height(), image.height()))
            if image.pixelColor(x, y).name() == accent
        ]
        assert accents and accents[0] <= 2
        assert accents == list(range(accents[0], accents[0] + len(accents)))
    finally:
        dialog.close()
        qapp.processEvents()
