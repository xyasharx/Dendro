# dendro/ui/history_dialog.py
"""
DNF Transaction History Viewer for Fedora Linux.
Provides read-only transaction browsing, package-scoped queries, inline
action-grouped package diffs, recently changed packages with applied security
advisories, approximate reverse-action queue previews via Goal.resolve, and
clipboard terminal-command helpers.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from PyQt6.QtCore import QEvent, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dendro.core.backend import (
    HistoryEntry,
    HistoryPackageChange,
    RecentChangesSummary,
    RecentPackageChange,
)
from dendro.core.daemon_locale import (
    history_action_message,
    history_reason_message,
    recent_change_message,
)
from dendro.core.dnf5daemon_types import can_build_reverse_transaction
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

DIFF_COLUMN_HEADERS: Final[tuple[str, ...]] = (
    "Package",
    "Version / Change",
    "Repository",
    "Reason",
)

RECENT_COLUMN_HEADERS: Final[tuple[str, ...]] = (
    "Package",
    "Change",
    "Version / Transition",
    "Applied Advisories",
    "Date & Time",
    "Summary",
)

_DIFF_ACTION_ORDER: Final[tuple[str, ...]] = (
    "Upgrade",
    "Install",
    "Downgrade",
    "Reinstall",
    "Remove",
)

_RECENT_SCOPE_OPTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("latest", "Latest Transaction"),
    ("last_7d", "Last 7 Days"),
    ("last_30d", "Last 30 Days"),
    ("all", "All Recorded History"),
)


def _coerce_int(value: object, default: int = 0) -> int:
    """Safely convert numeric or string-encoded integer fields for display."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                return int(stripped)
            except ValueError:
                return default
    return default


def _format_unix_timestamp(timestamp: int) -> str:
    if timestamp <= 0:
        return "—"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "—"


class DnfHistoryDialog(QDialog):
    """
    Read-only dialog for exploring Fedora DNF transaction history, per-transaction
    package diffs, package-scoped history, and recently changed packages with
    applied advisories.
    """

    refresh_requested = pyqtSignal()
    package_filter_requested = pyqtSignal(str)
    transaction_info_requested = pyqtSignal(int)
    recent_changes_requested = pyqtSignal(object, bool)
    package_navigate_requested = pyqtSignal(str)
    reverse_preview_requested = pyqtSignal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        initial_package: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle(tr("DNF Transaction History"))
        self.setMinimumSize(860, 560)
        self.resize(1040, 680)
        self._entries: list[HistoryEntry] = []
        self._filtered_entries: list[HistoryEntry] = []
        self._history_request_serial = 0
        self._recent_request_serial = 0
        self._recent_loaded_once = False
        self._recent_summary: RecentChangesSummary | None = None
        self._package_scope = initial_package.strip()
        self._undo_supported = True
        self._undo_disabled_reason = ""
        self._copied_undo_command: str | None = None
        self._loading_info_tx_ids: set[int] = set()
        self._failed_info_tx_ids: dict[int, str] = {}

        self._init_ui()
        apply_labelled_fonts(self)
        self._sync_package_scope_ui()
        self._update_action_buttons()

    def set_undo_supported(self, supported: bool, *, tooltip: str = "") -> None:
        """Enable or disable copying undo commands and reverse previews per host mode."""
        self._undo_supported = bool(supported)
        self._undo_disabled_reason = tooltip
        if not self._undo_supported and tooltip:
            self.capability_notice_label.setText(tooltip)
        elif self._undo_supported:
            self.capability_notice_label.setText(
                tr(
                    "Read-only history viewer: Fedora's DNF5 system daemon exposes transaction logs without a "
                    "rollback RPC. Inspect package diffs here, review recently applied advisories, or preview an "
                    "approximate reverse plan before making changes."
                )
            )
        self._update_action_buttons()

    def package_scope(self) -> str:
        return self._package_scope

    def set_package_scope(self, package_name: str) -> None:
        self._package_scope = package_name.strip()
        if hasattr(self, "package_scope_input"):
            self.package_scope_input.setText(self._package_scope)
        self._sync_package_scope_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        # ---------------------------------------------------------------------
        # 1. Header & Capability Notice
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
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)

        header_layout.addWidget(self.title_label, stretch=1)
        header_layout.addWidget(self.refresh_btn)
        layout.addLayout(header_layout)

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
                "rollback RPC. Inspect package diffs here, review recently applied advisories, or preview an "
                "approximate reverse plan before making changes."
            )
        )
        self.capability_notice_label.setObjectName("HistoryCapabilityLabel")
        self.capability_notice_label.setTextFormat(Qt.TextFormat.PlainText)
        self.capability_notice_label.setWordWrap(True)
        capability_layout.addWidget(self.capability_notice_label, stretch=1)
        layout.addWidget(capability_box)

        # ---------------------------------------------------------------------
        # 2. Tabbed Surfaces (Transactions | Recently Changed & Advisories)
        # ---------------------------------------------------------------------
        self.tabs = QTabWidget()
        self.tabs.setObjectName("HistoryTabs")

        self.transactions_tab = QWidget()
        self._init_transactions_tab()
        self.tabs.addTab(self.transactions_tab, tr("Transactions"))

        self.recent_tab = QWidget()
        self._init_recent_tab()
        self.tabs.addTab(self.recent_tab, tr("Recently Changed & Advisories"))
        self.tabs.currentChanged.connect(self._on_tab_changed)

        layout.addWidget(self.tabs, stretch=1)

        # ---------------------------------------------------------------------
        # 3. Bottom Action Bar
        # ---------------------------------------------------------------------
        bottom_layout = QHBoxLayout()

        self.inspect_btn = QPushButton(tr("Inspect Details"))
        self.inspect_btn.setIcon(
            theme_icon("dialog-information", "document-properties")
        )
        self.inspect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inspect_btn.clicked.connect(self._on_inspect_clicked)

        self.preview_reverse_btn = QPushButton(tr("Preview Reverse Action in Queue…"))
        self.preview_reverse_btn.setIcon(theme_icon("edit-undo", "view-refresh"))
        self.preview_reverse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_reverse_btn.clicked.connect(self._on_preview_reverse_clicked)

        self.copy_undo_btn = QPushButton(tr("Copy Undo Command"))
        self.copy_undo_btn.setIcon(theme_icon("edit-copy", "edit-undo"))
        self.copy_undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_undo_btn.clicked.connect(self._on_copy_undo_clicked)

        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.accept)

        bottom_layout.addWidget(self.inspect_btn)
        bottom_layout.addWidget(self.preview_reverse_btn)
        bottom_layout.addWidget(self.copy_undo_btn)
        bottom_layout.addStretch(1)
        bottom_layout.addWidget(self.close_btn)
        layout.addLayout(bottom_layout)

        self.setTabOrder(self.search_input, self.package_scope_input)
        self.setTabOrder(self.package_scope_input, self.filter_scope_btn)
        self.setTabOrder(self.filter_scope_btn, self.table)
        self.setTabOrder(self.table, self.diff_tree)
        self.setTabOrder(self.diff_tree, self.inspect_btn)
        self.setTabOrder(self.inspect_btn, self.preview_reverse_btn)
        self.setTabOrder(self.preview_reverse_btn, self.copy_undo_btn)
        self.setTabOrder(self.copy_undo_btn, self.close_btn)

    def _init_transactions_tab(self) -> None:
        tab_layout = QVBoxLayout(self.transactions_tab)
        tab_layout.setContentsMargins(6, 10, 6, 6)
        tab_layout.setSpacing(8)

        self.status_label = QLabel(tr("Loading DNF transaction history…"))
        self.status_label.setObjectName("HistoryStatusLabel")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        tab_layout.addWidget(self.status_label)

        # Search + Package-Scoped Query Bar
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        self.search_input = QLineEdit()
        self.search_input.setAccessibleName(tr("Filter transaction history"))
        self.search_input.setPlaceholderText(
            tr("Search loaded transactions by command, action, package, or date…")
        )
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter_table)
        filter_row.addWidget(self.search_input, stretch=3)

        self.package_scope_input = QLineEdit()
        self.package_scope_input.setAccessibleName(
            tr("Filter daemon history by package name")
        )
        self.package_scope_input.setPlaceholderText(
            tr("Package scope (e.g. kernel, mesa)…")
        )
        self.package_scope_input.setClearButtonEnabled(True)
        self.package_scope_input.setText(self._package_scope)
        self.package_scope_input.returnPressed.connect(self._on_apply_package_scope)
        filter_row.addWidget(self.package_scope_input, stretch=2)

        self.filter_scope_btn = QPushButton(tr("Filter Package"))
        self.filter_scope_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.filter_scope_btn.clicked.connect(self._on_apply_package_scope)
        filter_row.addWidget(self.filter_scope_btn)

        self.clear_scope_btn = QPushButton(tr("Show All"))
        self.clear_scope_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_scope_btn.clicked.connect(self._on_clear_package_scope)
        filter_row.addWidget(self.clear_scope_btn)

        tab_layout.addLayout(filter_row)

        self.package_scope_label = QLabel("")
        self.package_scope_label.setObjectName("HistoryScopeLabel")
        self.package_scope_label.setTextFormat(Qt.TextFormat.PlainText)
        self.package_scope_label.setWordWrap(True)
        self.package_scope_label.hide()
        tab_layout.addWidget(self.package_scope_label)

        # Vertical Splitter: Transaction Table | Inline Detail & Package Diff Panel
        self.history_splitter = QSplitter(Qt.Orientation.Vertical)
        self.history_splitter.setChildrenCollapsible(False)

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
        self.history_splitter.addWidget(self.table)

        # Inline Detail Panel (starts hidden; toggles on Inspect Details or double-click)
        self.detail_panel = QFrame()
        self.detail_panel.setObjectName("CoprBox")
        detail_layout = QVBoxLayout(self.detail_panel)
        detail_layout.setContentsMargins(10, 8, 10, 8)
        detail_layout.setSpacing(6)

        detail_header = QHBoxLayout()
        self.detail_title_label = QLabel("")
        self.detail_title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_title_label.setStyleSheet("font-weight: bold;")

        self.go_to_pkg_btn = QPushButton(tr("Go to Package"))
        self.go_to_pkg_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.go_to_pkg_btn.setEnabled(False)
        self.go_to_pkg_btn.clicked.connect(self._on_go_to_diff_package_clicked)

        self.btn_hide_detail = QPushButton(tr("Hide Details"))
        self.btn_hide_detail.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hide_detail.clicked.connect(self._hide_detail_panel)

        detail_header.addWidget(self.detail_title_label, stretch=1)
        detail_header.addWidget(self.go_to_pkg_btn)
        detail_header.addWidget(self.btn_hide_detail)
        detail_layout.addLayout(detail_header)

        self.detail_meta_label = QLabel("")
        self.detail_meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_meta_label.setWordWrap(True)
        detail_layout.addWidget(self.detail_meta_label)

        self.diff_status_label = QLabel("")
        self.diff_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.diff_status_label.setWordWrap(True)
        detail_layout.addWidget(self.diff_status_label)

        self.diff_tree = QTreeWidget()
        self.diff_tree.setObjectName("HistoryDiffTree")
        self.diff_tree.setAccessibleName(tr("Transaction package changes"))
        self.diff_tree.setColumnCount(len(DIFF_COLUMN_HEADERS))
        self.diff_tree.setHeaderLabels([tr(text) for text in DIFF_COLUMN_HEADERS])
        self.diff_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.diff_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.diff_tree.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.diff_tree.header().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.diff_tree.setAlternatingRowColors(True)
        self.diff_tree.setUniformRowHeights(True)
        self.diff_tree.setMinimumHeight(130)
        self.diff_tree.itemSelectionChanged.connect(self._update_go_to_package_button)
        self.diff_tree.itemDoubleClicked.connect(
            lambda item, _col: self._emit_navigate_for_diff_item(item)
        )
        self.diff_tree.itemActivated.connect(
            lambda item, _col: self._emit_navigate_for_diff_item(item)
        )
        detail_layout.addWidget(self.diff_tree, stretch=2)

        self.detail_command_view = QTextEdit()
        self.detail_command_view.setObjectName("ConsoleOutput")
        self.detail_command_view.setAccessibleName(tr("Transaction command details"))
        self.detail_command_view.setReadOnly(True)
        self.detail_command_view.setFont(scaled_fixed_font())
        min_cmd_h = max(68, self.fontMetrics().lineSpacing() * 3 + 18)
        self.detail_command_view.setMinimumHeight(min_cmd_h)
        self.detail_command_view.setMaximumHeight(max(96, min_cmd_h + 24))
        detail_layout.addWidget(self.detail_command_view)

        self.detail_panel.hide()
        self.history_splitter.addWidget(self.detail_panel)
        tab_layout.addWidget(self.history_splitter, stretch=1)

    def _init_recent_tab(self) -> None:
        tab_layout = QVBoxLayout(self.recent_tab)
        tab_layout.setContentsMargins(6, 10, 6, 6)
        tab_layout.setSpacing(8)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(8)

        self.recent_scope_label = QLabel(tr("Window:"))
        self.recent_scope_combo = QComboBox()
        self.recent_scope_combo.setAccessibleName(tr("Recent changes time window"))
        for key, label in _RECENT_SCOPE_OPTIONS:
            self.recent_scope_combo.addItem(tr(label), key)
        self.recent_scope_combo.setCurrentIndex(1)  # Last 7 Days
        self.recent_scope_combo.currentIndexChanged.connect(
            lambda _idx: self._request_recent_changes()
        )

        self.recent_include_advisory_cb = QCheckBox(
            tr("Include applied security advisories")
        )
        self.recent_include_advisory_cb.setChecked(True)
        self.recent_include_advisory_cb.toggled.connect(
            lambda _checked: self._request_recent_changes()
        )

        self.recent_advisory_only_cb = QCheckBox(tr("Advisories only"))
        self.recent_advisory_only_cb.setChecked(False)
        self.recent_advisory_only_cb.toggled.connect(
            lambda _checked: self._populate_recent_table()
        )

        self.recent_search_input = QLineEdit()
        self.recent_search_input.setAccessibleName(tr("Filter recent package changes"))
        self.recent_search_input.setPlaceholderText(
            tr("Filter by package, advisory ID, or summary…")
        )
        self.recent_search_input.setClearButtonEnabled(True)
        self.recent_search_input.textChanged.connect(
            lambda _text: self._populate_recent_table()
        )

        self.recent_refresh_btn = QPushButton(tr("Refresh Recent Changes"))
        self.recent_refresh_btn.setIcon(QIcon.fromTheme("view-refresh"))
        self.recent_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recent_refresh_btn.clicked.connect(self._request_recent_changes)

        controls_row.addWidget(self.recent_scope_label)
        controls_row.addWidget(self.recent_scope_combo)
        controls_row.addWidget(self.recent_include_advisory_cb)
        controls_row.addWidget(self.recent_advisory_only_cb)
        controls_row.addWidget(self.recent_search_input, stretch=1)
        controls_row.addWidget(self.recent_refresh_btn)
        tab_layout.addLayout(controls_row)

        self.recent_status_label = QLabel(
            tr("Switch to this tab to load recently changed packages.")
        )
        self.recent_status_label.setObjectName("HistoryRecentStatusLabel")
        self.recent_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.recent_status_label.setWordWrap(True)
        tab_layout.addWidget(self.recent_status_label)

        self.recent_table = QTableWidget(0, len(RECENT_COLUMN_HEADERS))
        self.recent_table.setObjectName("RecentChangesTable")
        self.recent_table.setAccessibleName(tr("Recently changed packages table"))
        self.recent_table.setHorizontalHeaderLabels(
            [tr(text) for text in RECENT_COLUMN_HEADERS]
        )
        self.recent_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.recent_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.recent_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.recent_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.recent_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        self.recent_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch
        )
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.setShowGrid(False)
        self.recent_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.recent_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.recent_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._recent_row_fitter = WholeRowFitter(self.recent_table)
        self.recent_table.installEventFilter(self._recent_row_fitter)
        self.recent_table.itemDoubleClicked.connect(self._on_recent_item_double_clicked)
        tab_layout.addWidget(self.recent_table, stretch=1)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(
            self, "detail_command_view"
        ):
            self.detail_command_view.setFont(scaled_fixed_font())
            min_cmd_h = max(68, self.fontMetrics().lineSpacing() * 3 + 18)
            self.detail_command_view.setMinimumHeight(min_cmd_h)
            self.detail_command_view.setMaximumHeight(max(96, min_cmd_h + 24))
            if self._filtered_entries:
                self._populate_table(self._filtered_entries)
            if self._recent_summary is not None:
                self._populate_recent_table()

    def retranslate(self) -> None:
        self.setWindowTitle(tr("DNF Transaction History"))
        self.title_label.setText(tr("System Package Transaction History"))
        self.refresh_btn.setText(tr("Refresh"))
        self.tabs.setTabText(0, tr("Transactions"))
        self.tabs.setTabText(1, tr("Recently Changed & Advisories"))
        self.search_input.setAccessibleName(tr("Filter transaction history"))
        self.search_input.setPlaceholderText(
            tr("Search loaded transactions by command, action, package, or date…")
        )
        self.package_scope_input.setAccessibleName(
            tr("Filter daemon history by package name")
        )
        self.package_scope_input.setPlaceholderText(
            tr("Package scope (e.g. kernel, mesa)…")
        )
        self.filter_scope_btn.setText(tr("Filter Package"))
        self.clear_scope_btn.setText(tr("Show All"))
        self.table.setAccessibleName(tr("DNF transaction history table"))
        self.table.setHorizontalHeaderLabels([tr(text) for text in COLUMN_HEADERS])
        self.diff_tree.setAccessibleName(tr("Transaction package changes"))
        self.diff_tree.setHeaderLabels([tr(text) for text in DIFF_COLUMN_HEADERS])
        self.go_to_pkg_btn.setText(tr("Go to Package"))
        self.btn_hide_detail.setText(tr("Hide Details"))
        self.detail_command_view.setAccessibleName(tr("Transaction command details"))
        self.preview_reverse_btn.setText(tr("Preview Reverse Action in Queue…"))
        self.copy_undo_btn.setText(tr("Copy Undo Command"))
        self.close_btn.setText(tr("Close"))

        self.recent_scope_label.setText(tr("Window:"))
        self.recent_scope_combo.setAccessibleName(tr("Recent changes time window"))
        for index, (_key, label) in enumerate(_RECENT_SCOPE_OPTIONS):
            self.recent_scope_combo.setItemText(index, tr(label))
        self.recent_include_advisory_cb.setText(
            tr("Include applied security advisories")
        )
        self.recent_advisory_only_cb.setText(tr("Advisories only"))
        self.recent_search_input.setAccessibleName(tr("Filter recent package changes"))
        self.recent_search_input.setPlaceholderText(
            tr("Filter by package, advisory ID, or summary…")
        )
        self.recent_refresh_btn.setText(tr("Refresh Recent Changes"))
        self.recent_table.setAccessibleName(tr("Recently changed packages table"))
        self.recent_table.setHorizontalHeaderLabels(
            [tr(text) for text in RECENT_COLUMN_HEADERS]
        )

        self.set_undo_supported(
            self._undo_supported, tooltip=self._undo_disabled_reason
        )
        self._sync_package_scope_ui()
        if self._entries:
            self._filter_table(self.search_input.text())
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
        if self._recent_summary is not None:
            self._populate_recent_table()
        self._update_action_buttons()

    def _sync_package_scope_ui(self) -> None:
        has_scope = bool(self._package_scope)
        self.clear_scope_btn.setEnabled(has_scope)
        if has_scope:
            self.package_scope_label.setText(
                tr("Showing transactions affecting package: {package}").format(
                    package=self._package_scope
                )
            )
            self.package_scope_label.show()
        else:
            self.package_scope_label.setText("")
            self.package_scope_label.hide()

    def _on_refresh_clicked(self) -> None:
        if self.tabs.currentWidget() is self.recent_tab:
            self._request_recent_changes()
            return
        if self._package_scope:
            self.package_filter_requested.emit(self._package_scope)
        else:
            self.refresh_requested.emit()

    def _on_apply_package_scope(self) -> None:
        pkg = self.package_scope_input.text().strip()
        self.set_package_scope(pkg)
        if pkg:
            self.package_filter_requested.emit(pkg)
        else:
            self.refresh_requested.emit()

    def _on_clear_package_scope(self) -> None:
        self.set_package_scope("")
        self.package_filter_requested.emit("")

    def _on_tab_changed(self, _index: int) -> None:
        if (
            hasattr(self, "recent_tab")
            and self.tabs.currentWidget() is self.recent_tab
            and not self._recent_loaded_once
        ):
            self._request_recent_changes()

    def requested_recent_since(self) -> int | None:
        key = self.recent_scope_combo.currentData()
        now = int(time.time())
        if key == "latest":
            return None
        if key == "last_7d":
            return max(0, now - 7 * 86400)
        if key == "last_30d":
            return max(0, now - 30 * 86400)
        if key == "all":
            return 0
        return None

    def _request_recent_changes(self) -> None:
        self._recent_loaded_once = True
        self.recent_changes_requested.emit(
            self.requested_recent_since(),
            self.recent_include_advisory_cb.isChecked(),
        )

    def set_history_loading(self) -> int:
        """Start one identified load without erasing previously loaded rows."""
        self._history_request_serial += 1
        self.status_label.setText(tr("Loading DNF transaction history…"))
        self.status_label.setStyleSheet("")
        self.status_label.setToolTip("")
        self.refresh_btn.setEnabled(False)
        self.inspect_btn.setEnabled(False)
        self.preview_reverse_btn.setEnabled(False)
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
        self._entries = list(entries)
        self._loading_info_tx_ids.clear()
        self._failed_info_tx_ids.clear()
        self._filter_table(self.search_input.text())
        self.refresh_btn.setEnabled(True)
        if entries:
            count = len(entries)
            if self._package_scope:
                self.status_label.setText(
                    tr_plural(
                        "Loaded {count} DNF transaction affecting {package}.",
                        "Loaded {count} DNF transactions affecting {package}.",
                        count,
                    ).format(
                        count=format_number(count),
                        package=self._package_scope,
                    )
                )
            else:
                self.status_label.setText(
                    tr_plural(
                        "Loaded {count} DNF transaction.",
                        "Loaded {count} DNF transactions.",
                        count,
                    ).format(count=format_number(count))
                )
        else:
            self.status_label.setText(
                tr("No DNF transactions found matching package {package}.").format(
                    package=self._package_scope
                )
                if self._package_scope
                else tr("No DNF transactions were found.")
            )
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

    def set_transaction_packages_if_current(
        self, tx_id: int, packages: Sequence[HistoryPackageChange]
    ) -> bool:
        """Populate per-package diff for a CLI-fetched transaction."""
        self._loading_info_tx_ids.discard(tx_id)
        self._failed_info_tx_ids.pop(tx_id, None)
        updated = False
        for entry in self._entries:
            if entry.id == tx_id:
                entry.packages = tuple(packages)
                updated = True
                break
        if not updated:
            return False
        selected = self._selected_entry()
        if selected is not None and selected.id == tx_id:
            if self.detail_panel.isVisible():
                self._populate_detail_panel(selected)
            self._update_action_buttons()
        return True

    def set_transaction_info_error(self, tx_id: int, detail: str) -> None:
        self._loading_info_tx_ids.discard(tx_id)
        self._failed_info_tx_ids[tx_id] = detail
        selected = self._selected_entry()
        if (
            selected is not None
            and selected.id == tx_id
            and self.detail_panel.isVisible()
        ):
            self._populate_detail_panel(selected)

    # -------------------------------------------------------------------------
    # Recent Changes & Advisories State
    # -------------------------------------------------------------------------
    def set_recent_changes_loading(self) -> int:
        self._recent_request_serial += 1
        self._recent_loaded_once = True
        self.recent_status_label.setText(
            tr("Loading recently changed packages and applied advisories…")
        )
        self.recent_status_label.setStyleSheet("")
        self.recent_status_label.setToolTip("")
        self.recent_refresh_btn.setEnabled(False)
        return self._recent_request_serial

    def set_recent_changes_if_current(
        self, request_serial: int, summary: RecentChangesSummary
    ) -> bool:
        if request_serial != self._recent_request_serial:
            return False
        self.recent_refresh_btn.setEnabled(True)
        self._recent_summary = summary
        self._populate_recent_table()
        adv_count = sum(1 for item in summary.all_changes if item.advisories)
        if summary.total_count == 0:
            self.recent_status_label.setText(
                tr("No package changes found for the selected time window.")
            )
        else:
            self.recent_status_label.setText(
                tr(
                    "{upgraded} upgraded · {installed} installed · {downgraded} downgraded · "
                    "{removed} removed ({advisory_pkgs} with applied advisories)"
                ).format(
                    upgraded=format_number(len(summary.upgraded)),
                    installed=format_number(len(summary.installed)),
                    downgraded=format_number(len(summary.downgraded)),
                    removed=format_number(len(summary.removed)),
                    advisory_pkgs=format_number(adv_count),
                )
            )
        self.recent_status_label.setStyleSheet("")
        self.recent_status_label.setToolTip("")
        return True

    def set_recent_changes_error_if_current(
        self, request_serial: int, _package_name: str, detail: str
    ) -> bool:
        if request_serial != self._recent_request_serial:
            return False
        self.recent_refresh_btn.setEnabled(True)
        summary = " ".join(detail.split())
        if len(summary) > 360:
            summary = f"{summary[:357].rstrip()}…"
        settings = get_app_settings()
        pal = get_delegate_palette(settings.value("theme", "auto", type=str))
        self.recent_status_label.setText(
            tr("Could not load recent package changes: {detail}").format(detail=summary)
            if summary
            else tr("Could not load recent package changes.")
        )
        style_text(
            self.recent_status_label,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=pal["badge_fg_missing"].name(),
        )
        self.recent_status_label.setToolTip(detail)
        return True

    def _populate_recent_table(self) -> None:
        summary = self._recent_summary
        if summary is None:
            self.recent_table.setRowCount(0)
            return

        query = self.recent_search_input.text().strip().lower()
        adv_only = self.recent_advisory_only_cb.isChecked()
        rows: list[RecentPackageChange] = []
        for item in summary.all_changes:
            if adv_only and not item.advisories:
                continue
            if query:
                adv_text = " ".join(item.advisories).lower()
                if not (
                    query in item.name.lower()
                    or query in item.name_arch.lower()
                    or query in item.evr.lower()
                    or query in item.original_evr.lower()
                    or query in item.summary.lower()
                    or query in item.repo_id.lower()
                    or query in adv_text
                ):
                    continue
            rows.append(item)

        settings = get_app_settings()
        theme_choice = settings.value("theme", "auto", type=str)
        pal = get_delegate_palette(theme_choice)
        mono_font = scaled_fixed_font()

        self.recent_table.setRowCount(len(rows))
        for row_idx, item in enumerate(rows):
            pkg_label = item.name_arch or item.name
            pkg_item = QTableWidgetItem(pkg_label)
            pkg_item.setData(Qt.ItemDataRole.UserRole, pkg_label)
            pkg_item.setToolTip(
                tr("Double-click to inspect {package} in Dendro.").format(
                    package=pkg_label
                )
            )

            change_label = recent_change_message(item.change_type)
            change_item = QTableWidgetItem(change_label)
            change_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if item.change_type == "installed":
                change_item.setForeground(pal["badge_fg_installed"])
            elif item.change_type == "removed":
                change_item.setForeground(pal["badge_fg_missing"])
            elif item.change_type == "upgraded":
                change_item.setForeground(pal["accent"])
            elif item.change_type == "downgraded":
                change_item.setForeground(pal["badge_fg_tag"])

            if item.original_evr and item.evr:
                ver_text = f"{item.original_evr} → {item.evr}"
            else:
                ver_text = item.evr or "—"
            ver_item = QTableWidgetItem(ver_text)
            ver_item.setFont(mono_font)
            ver_item.setToolTip(ver_text)

            adv_text = ", ".join(item.advisories) if item.advisories else "—"
            adv_item = QTableWidgetItem(adv_text)
            adv_item.setFont(mono_font)
            if item.advisories:
                adv_item.setForeground(pal["badge_fg_update"])
                adv_item.setToolTip("\n".join(item.advisories))

            dt_text = _format_unix_timestamp(item.transaction_time)
            dt_item = QTableWidgetItem(dt_text)
            dt_item.setFont(mono_font)

            summary_parts = [part for part in (item.summary, item.repo_id) if part]
            summary_item = QTableWidgetItem(item.summary or "—")
            if summary_parts:
                summary_item.setToolTip(" · ".join(summary_parts))

            self.recent_table.setItem(row_idx, 0, pkg_item)
            self.recent_table.setItem(row_idx, 1, change_item)
            self.recent_table.setItem(row_idx, 2, ver_item)
            self.recent_table.setItem(row_idx, 3, adv_item)
            self.recent_table.setItem(row_idx, 4, dt_item)
            self.recent_table.setItem(row_idx, 5, summary_item)

        self._recent_row_fitter.fit()

    def _on_recent_item_double_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        pkg_cell = self.recent_table.item(row, 0)
        if pkg_cell is None:
            return
        pkg_target = pkg_cell.data(Qt.ItemDataRole.UserRole)
        if isinstance(pkg_target, str) and pkg_target.strip():
            self.package_navigate_requested.emit(pkg_target.strip())

    # -------------------------------------------------------------------------
    # Transactions Table & Diff Panel
    # -------------------------------------------------------------------------
    def _populate_table(self, entries: list[HistoryEntry]) -> None:
        self._filtered_entries = list(entries)
        settings = get_app_settings()
        theme_choice = settings.value("theme", "auto", type=str)
        pal = get_delegate_palette(theme_choice)

        mono_font = scaled_fixed_font()
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            tx_id = _coerce_int(entry.id)
            id_item = QTableWidgetItem(f"#{tx_id}")
            id_item.setData(Qt.ItemDataRole.UserRole, tx_id)
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

            alt_count = _coerce_int(entry.altered_count)
            alt_item = QTableWidgetItem(
                tr("{count} pkgs").format(count=format_number(alt_count))
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

        self._row_fitter.fit()
        if entries:
            self.table.selectRow(0)
        self._update_action_buttons()

    @staticmethod
    def _entry_matches_query(entry: HistoryEntry, query: str) -> bool:
        if (
            query in entry.command_line.lower()
            or query in entry.action.lower()
            or query in entry.date_time.lower()
            or query in str(entry.id)
            or query in entry.status.lower()
            or query in entry.comment.lower()
        ):
            return True
        return any(
            query in pkg.name.lower()
            or query in pkg.name_arch.lower()
            or query in pkg.evr.lower()
            or query in pkg.original_evr.lower()
            or query in pkg.repo_id.lower()
            for pkg in entry.packages
        )

    def _filter_table(self, query: str) -> None:
        cleaned = query.strip().lower()
        if not cleaned:
            self._populate_table(self._entries)
            return

        filtered = [
            entry
            for entry in self._entries
            if self._entry_matches_query(entry, cleaned)
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
        return next(
            (entry for entry in self._entries if _coerce_int(entry.id) == trans_id),
            None,
        )

    def _update_action_buttons(self, *_args) -> None:
        entry = self._selected_entry()
        has_selection = entry is not None
        self.inspect_btn.setEnabled(has_selection)
        self.copy_undo_btn.setEnabled(self._undo_supported and has_selection)

        can_reverse = (
            self._undo_supported
            and entry is not None
            and can_build_reverse_transaction(entry.packages)
        )
        self.preview_reverse_btn.setEnabled(can_reverse)
        if not self._undo_supported:
            self.copy_undo_btn.setToolTip(self._undo_disabled_reason)
            self.preview_reverse_btn.setToolTip(self._undo_disabled_reason)
        elif entry is not None:
            self.copy_undo_btn.setToolTip(
                tr("Copy terminal command: {command}").format(
                    command=f"sudo dnf history undo {entry.id}"
                )
            )
            if can_reverse:
                self.preview_reverse_btn.setToolTip(
                    tr(
                        "Resolve an approximate reverse transaction through Goal.resolve and inspect "
                        "the resulting plan before applying any changes."
                    )
                )
            else:
                self.preview_reverse_btn.setToolTip(
                    tr(
                        "Selected transaction has no reversible install, remove, upgrade, or downgrade package records."
                    )
                )
        else:
            self.copy_undo_btn.setToolTip("")
            self.preview_reverse_btn.setToolTip("")

        if self.detail_panel.isVisible():
            if entry is not None:
                self._populate_detail_panel(entry)
            else:
                self._hide_detail_panel()
        else:
            self.inspect_btn.setText(tr("Inspect Details"))

    def _populate_diff_tree(self, entry: HistoryEntry) -> None:
        self.diff_tree.clear()
        self.go_to_pkg_btn.setEnabled(False)
        packages = entry.packages
        tx_id = _coerce_int(entry.id)
        alt_count = _coerce_int(entry.altered_count)
        if not packages:
            if tx_id in self._loading_info_tx_ids:
                self.diff_status_label.setText(
                    tr("Loading per-package details for transaction #{id}…").format(
                        id=format_number(tx_id)
                    )
                )
            elif tx_id in self._failed_info_tx_ids:
                self.diff_status_label.setText(
                    tr("Per-package diff unavailable: {detail}").format(
                        detail=self._failed_info_tx_ids[tx_id]
                    )
                )
            elif alt_count > 0:
                self.diff_status_label.setText(
                    tr(
                        "No per-package records returned by the daemon; run 'sudo dnf history info {id}' for details."
                    ).format(id=tx_id)
                )
            else:
                self.diff_status_label.setText(
                    tr("This transaction did not modify any RPM packages.")
                )
            return

        settings = get_app_settings()
        pal = get_delegate_palette(settings.value("theme", "auto", type=str))
        mono_font = scaled_fixed_font()

        grouped: dict[str, list[HistoryPackageChange]] = {}
        for pkg in packages:
            action_key = pkg.action.strip() or "Other"
            grouped.setdefault(action_key, []).append(pkg)

        ordered_keys = [key for key in _DIFF_ACTION_ORDER if key in grouped] + [
            key for key in grouped if key not in _DIFF_ACTION_ORDER
        ]

        total_pkgs = len(packages)
        self.diff_status_label.setText(
            tr_plural(
                "{count} recorded package change — double-click a package to inspect it in Dendro.",
                "{count} recorded package changes — double-click a package to inspect it in Dendro.",
                total_pkgs,
            ).format(count=format_number(total_pkgs))
        )

        for action_key in ordered_keys:
            items = grouped[action_key]
            group_label = (
                f"{history_action_message(action_key)} ({format_number(len(items))})"
            )
            group_node = QTreeWidgetItem(self.diff_tree, [group_label, "", "", ""])
            group_node.setFlags(group_node.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            action_cf = action_key.casefold()
            if action_cf in {"install", "reinstall"}:
                group_color = pal["badge_fg_installed"]
            elif action_cf in {"upgrade", "update"}:
                group_color = pal["accent"]
            elif action_cf == "downgrade":
                group_color = pal["badge_fg_tag"]
            elif action_cf in {"remove", "erase"}:
                group_color = pal["badge_fg_missing"]
            else:
                group_color = pal["text_main"]
            group_node.setForeground(0, group_color)

            for pkg in items:
                pkg_label = pkg.name_arch or pkg.name
                if pkg.original_evr and pkg.evr:
                    ver_label = f"{pkg.original_evr} → {pkg.evr}"
                else:
                    ver_label = pkg.evr or "—"
                repo_label = pkg.repo_id or "—"
                reason_label = history_reason_message(pkg.reason)
                child = QTreeWidgetItem(
                    group_node,
                    [pkg_label, ver_label, repo_label, reason_label],
                )
                child.setData(0, Qt.ItemDataRole.UserRole, pkg_label)
                child.setFont(1, mono_font)
                child.setToolTip(
                    0,
                    tr("Double-click to inspect {package} in Dendro.").format(
                        package=pkg_label
                    ),
                )
            group_node.setExpanded(True)

    def _update_go_to_package_button(self) -> None:
        items = self.diff_tree.selectedItems()
        target = items[0].data(0, Qt.ItemDataRole.UserRole) if items else None
        self.go_to_pkg_btn.setEnabled(isinstance(target, str) and bool(target.strip()))

    def _emit_navigate_for_diff_item(self, item: QTreeWidgetItem) -> None:
        target = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(target, str) and target.strip():
            self.package_navigate_requested.emit(target.strip())

    def _on_go_to_diff_package_clicked(self) -> None:
        items = self.diff_tree.selectedItems()
        if items:
            self._emit_navigate_for_diff_item(items[0])

    def _populate_detail_panel(self, entry: HistoryEntry) -> None:
        tx_id = _coerce_int(entry.id)
        alt_count = _coerce_int(entry.altered_count)
        ret_code = _coerce_int(entry.return_code)
        self.detail_panel.setProperty("activeTransactionId", tx_id)
        self.detail_title_label.setText(
            tr("Transaction #{id} — {action}").format(
                id=format_number(tx_id), action=entry.action
            )
        )
        meta_parts = [
            tr("Date: {date}").format(date=entry.date_time),
            tr("Altered: {altered}").format(altered=format_number(alt_count)),
        ]
        if entry.status:
            meta_parts.append(tr("Status: {status}").format(status=entry.status))
        if entry.releasever:
            meta_parts.append(
                tr("Release: {releasever}").format(releasever=entry.releasever)
            )
        if entry.user_id is not None:
            meta_parts.append(
                tr("User ID: {uid}").format(
                    uid=format_number(_coerce_int(entry.user_id))
                )
            )
        if entry.comment:
            meta_parts.append(tr("Comment: {comment}").format(comment=entry.comment))
        meta_parts.append(tr("Exit code: {code}").format(code=format_number(ret_code)))
        self.detail_meta_label.setText("  |  ".join(meta_parts))

        if (
            not entry.packages
            and alt_count > 0
            and tx_id not in self._loading_info_tx_ids
            and tx_id not in self._failed_info_tx_ids
        ):
            self._loading_info_tx_ids.add(tx_id)
            self.transaction_info_requested.emit(tx_id)

        self._populate_diff_tree(entry)

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
        was_hidden = not self.detail_panel.isVisible()
        self._populate_detail_panel(entry)
        self.detail_panel.show()
        if was_hidden and self.history_splitter.height() > 160:
            total = self.history_splitter.height()
            top_h = max(140, int(total * 0.42))
            self.history_splitter.setSizes([top_h, max(180, total - top_h)])
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

    def _on_preview_reverse_clicked(self) -> None:
        entry = self._selected_entry()
        if (
            entry is None
            or not self._undo_supported
            or not can_build_reverse_transaction(entry.packages)
        ):
            return
        self.reverse_preview_requested.emit(entry)

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
