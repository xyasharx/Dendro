# dendro/ui/main_window.py
"""Main application window controller for Dendro.

Package-changing operations are resolved and executed through Fedora's DNF5
system daemon.  The UI owns no root command line, retains the daemon's resolved
plan until confirmation, and exposes cancellation only where the daemon says it
is safe.  Read-only RPM-host queries remain isolated from Flatpak host access.
"""

from __future__ import annotations

import logging
import os

from PyQt6.QtCore import (
    QEvent,
    QModelIndex,
    QPoint,
    Qt,
    QThreadPool,
    QTimer,
)
from PyQt6.QtGui import (
    QAction,
    QClipboard,
    QCloseEvent,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHeaderView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from dendro.core.backend import (
    AvailableUpdateInfo,
    DependencyNode,
    DependencyTreeWorker,
    DnfHistoryInfoWorker,
    DnfHistoryWorker,
    FileOwnerLookupWorker,
    LocalRpmInspectionWorker,
    OfflineStatusQueryWorker,
    OrphanQueryWorker,
    PackageChangelogWorker,
    PackageFilesWorker,
    PackageInfo,
    PackageQueryWorker,
    PackageScriptletsWorker,
    PackageState,
    PackageVerifyWorker,
    PackageVersionsQueryWorker,
    RecentChangesWorker,
    RemotePackageSearchWorker,
    ReverseDependencyWorker,
    SecurityAdvisoryWorker,
    SQLiteCapabilityCache,
    SystemRebootWorker,
    SystemUpdatesCheckWorker,
    UserInstalledQueryWorker,
    get_system_protected_packages,
    is_running_in_flatpak,
)
from dendro.core.dnf5daemon import (
    Dnf5DaemonClient,
    OfflineTransactionStatus,
    PackageRepoVersions,
    RepositoryListWorker,
)
from dendro.core.host_environment import HostEnvironment, detect_host_environment
from dendro.core.i18n import (
    active_locale,
    format_number,
    resolve_locale,
    reverse_translate,
    tr,
    tr_plural,
)
from dendro.core.models import (
    COLUMN_HEADERS,
    QUEUED_INSTALLED_STATES,
    STATUS_EXTRA_LABELS,
    STATUS_LABELS,
    DependencyTreeModel,
    PackageFilterProxyModel,
    TreeItem,
    query_has_explicit_category_filter,
)
from dendro.core.operation_gate import HostOperationGate
from dendro.core.repository_addition import RepositoryAdditionRunner
from dendro.core.repository_deletion import RepositoryDeletionRunner
from dendro.core.rpmostree_status import (
    RpmOstreeHostStatus,
    RpmOstreeOriginKind,
    RpmOstreeStatusWorker,
)
from dendro.core.settings import get_app_settings
from dendro.core.system_reboot import RebootCapability
from dendro.core.taxonomy import (
    category_label,
    matching_sidebar_categories,
    sidebar_category_ids,
)
from dendro.core.transaction_controller import DnfTransactionController
from dendro.ui.advisory_dialog import SecurityAdvisoryDialog
from dendro.ui.delegates import DendroTreeView, PackageTreeItemDelegate
from dendro.ui.downgrade_dialog import DowngradePackageDialog
from dendro.ui.flatpak_dialog import FlatpakInventoryDialog, start_flatpak_refresh
from dendro.ui.fonts import apply_labelled_fonts
from dendro.ui.header import HeaderBar, is_file_lookup_query
from dendro.ui.host_dialogs import HostModeBanner, LocalRpmInstallDialog, _field_name
from dendro.ui.i18n import install as install_locale
from dendro.ui.icons import theme_icon
from dendro.ui.inspector_panel import PackageInspectorPanel
from dendro.ui.key_import_dialog import RepositoryKeyImportDialog
from dendro.ui.main_window_inspector import MainWindowInspectorMixin
from dendro.ui.main_window_transactions import MainWindowTransactionsMixin
from dendro.ui.offline_banner import OfflineUpdateBanner
from dendro.ui.repo_dialog import RepoManagerDialog
from dendro.ui.sidebar import CategorySidebar
from dendro.ui.styles import _debug_log, is_system_reduced_motion
from dendro.ui.theme_coordinator import ThemeCoordinator
from dendro.ui.transaction_drawer import TransactionDrawer
from dendro.ui.transaction_plan_dialog import TransactionPlanReviewDialog
from dendro.ui.window_placement import (
    fit_size_to_available_geometry,
    fit_to_available_geometry,
)
from dendro.ui.window_presentation import (
    refresh_window_empty_state as _refresh_window_empty_state,
)
from dendro.ui.window_presentation import (
    window_host_environment as _window_host_environment,
)

LOGGER = logging.getLogger(__name__)


# =============================================================================
# Local .rpm File Inspection & Installation Dialog
# =============================================================================


# =============================================================================
# Main Application Window
# =============================================================================


class MainWindow(
    MainWindowInspectorMixin,
    MainWindowTransactionsMixin,
    ThemeCoordinator,
    QMainWindow,
):
    def __init__(self, *, host_environment: HostEnvironment | None = None):
        super().__init__()
        self.host_environment: HostEnvironment = (
            host_environment
            if host_environment is not None
            else detect_host_environment()
        )
        # One id, written in one place: the constructor and a language switch
        # both come through ``_refresh_window_title``, so they cannot disagree
        # about which string this window shows. They used to: startup used this
        # long descriptive form and a switch used the tray tooltip's short name,
        # which read as the window changing identity rather than translating.
        self._refresh_window_title()

        # Persistent user settings
        self.settings = get_app_settings()
        # Appearance state belongs to the theme coordinator, which also repairs a
        # stored preference it does not recognise.
        self.init_theme_state(self.settings)

        # Restore the saved geometry, or choose an initial size that the screen
        # can hold. ``restoreGeometry`` only rescues a window that is entirely
        # off-screen, so a geometry saved on a larger display is honoured even
        # when this one cannot show all of it; ``showEvent`` fits the result to
        # the screen it actually opens on.
        saved_geom = self.settings.value("geometry")
        if saved_geom:
            self.restoreGeometry(saved_geom)
        else:
            fit_size_to_available_geometry(self, 1280, 800)

        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(16)

        # Background worker tracking
        self.current_query_worker: PackageQueryWorker | None = None
        self.current_orphan_worker: OrphanQueryWorker | None = None
        self.current_userinstalled_worker: UserInstalledQueryWorker | None = None
        self.current_updates_worker: SystemUpdatesCheckWorker | None = None
        self.current_offline_status_worker: OfflineStatusQueryWorker | None = None
        self.current_reboot_worker: SystemRebootWorker | None = None
        self.current_rpmostree_worker: RpmOstreeStatusWorker | None = None
        self._rpmostree_status: RpmOstreeHostStatus | None = None
        self.current_remote_search_worker: RemotePackageSearchWorker | None = None
        self.current_file_owner_worker: FileOwnerLookupWorker | None = None
        self._file_owner_request_serial = 0
        self.current_local_rpm_inspection_worker: LocalRpmInspectionWorker | None = None
        self._local_rpm_inspection_request_serial = 0
        self.current_package_versions_worker: PackageVersionsQueryWorker | None = None
        self._package_versions_request_serial = 0
        self._package_tree_generation = 0
        self._inspector_generation = 0
        self._inspector_request_serials: dict[str, int] = {}
        self._update_request_serial = 0
        self._offline_status_request_serial = 0
        self._offline_status: OfflineTransactionStatus | None = None
        self._reboot_capability: RebootCapability = RebootCapability.UNAVAILABLE
        self._prompt_reboot_after_offline_refresh = False
        self.current_repository_worker: RepositoryListWorker | None = None
        self._repository_refresh_after_current_worker = False
        self._busy_operations: set[str] = set()
        self._owns_wait_cursor = False
        self._tray_action_open: QAction | None = None
        self._tray_action_upgrade: QAction | None = None
        self._tray_action_flatpak: QAction | None = None
        self._tray_action_quit: QAction | None = None
        self._active_repo_dialog: RepoManagerDialog | None = None
        self._repo_dialog_op_kind = ""
        self._repo_inline_error_handled = False
        self._close_after_transaction = False
        self._sidebar_auto_collapsed = False
        self._manual_sidebar_override = False
        self._pre_collapse_splitter_state = None
        self._pending_transaction_kind = ""
        self._last_transaction_upgrades: list[str] = []
        self._last_transaction_removals: list[PackageInfo] = []

        # Result caches to prevent asynchronous race conditions
        self._catalog_loaded = False
        self._catalog_error = ""
        self._last_scope_status_message = ""
        self._all_packages_cache: list[PackageInfo] = []
        self._user_installed_cache: set[str] = set()
        self._orphan_cache: set[str] = set()
        self._pending_updates_map: dict[str, AvailableUpdateInfo] = {}

        self._init_ui()
        apply_labelled_fonts(self)
        self._setup_shortcuts()
        self._connect_signals()
        self._init_theming()
        self._init_system_tray()
        self._load_packages()

    def _init_ui(self):
        root_widget = QWidget()
        self.setCentralWidget(root_widget)
        root_layout = QVBoxLayout(root_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Header Toolbar & Optional Host Mode Banner
        self.header = HeaderBar()
        self.header.configure_host_environment(self.host_environment)
        root_layout.addWidget(self.header)

        self.host_mode_banner = HostModeBanner()
        self.host_mode_banner.configure(self.host_environment)
        self.host_mode_banner.flatpak_requested.connect(self._open_flatpak_dialog)
        root_layout.addWidget(self.host_mode_banner)

        self.offline_banner = OfflineUpdateBanner()
        root_layout.addWidget(self.offline_banner)

        # 2. Main Horizontal Splitter
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(self.main_splitter, stretch=1)

        # Left Sidebar Navigation
        self.sidebar = CategorySidebar()
        self.sidebar.configure_host_environment(self.host_environment)
        self.main_splitter.addWidget(self.sidebar)

        # Central Workspace Splitter
        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical)

        # Dedicated tree view with native vector chevron branches
        self.tree_view = DendroTreeView()
        self.tree_view.setObjectName("PackageTreeView")
        self.tree_view.setUniformRowHeights(True)
        self.tree_view.setRootIsDecorated(True)
        self.tree_view.setIndentation(22)
        self.tree_view.setAnimated(not is_system_reduced_motion(allow_blocking=False))
        self.tree_view.setExpandsOnDoubleClick(True)
        self.tree_view.setItemsExpandable(True)

        self.tree_model = DependencyTreeModel(self)
        self.proxy_model = PackageFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.tree_model)
        self.tree_view.setModel(self.proxy_model)

        self.tree_delegate = PackageTreeItemDelegate(self.tree_view)
        self.tree_view.setItemDelegate(self.tree_delegate)

        self._configure_tree_columns()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.workspace_splitter.addWidget(self.tree_view)

        # Bottom Transaction Terminal Drawer
        self.transaction_drawer = TransactionDrawer()
        self.transaction_drawer.hide()
        self.workspace_splitter.addWidget(self.transaction_drawer)

        self.main_splitter.addWidget(self.workspace_splitter)

        # Right-side Inspector Panel sitting directly in the main splitter.
        # Its pinned header stays fixed above a single inner QScrollArea while
        # allowing narrow 240px split-pane tiling without scrolling the header
        # controls off-screen.
        self.inspector_panel = PackageInspectorPanel()
        self.inspector_panel.set_host_environment(self.host_environment)
        self.inspector_panel.set_dock_compact_floor(True)
        self.inspector_panel.setAccessibleName(tr("Package details dock"))
        raw_metadata_expanded = self.settings.value("inspector/metadata_expanded", True)
        if isinstance(raw_metadata_expanded, bool):
            metadata_expanded = raw_metadata_expanded
        else:
            metadata_expanded = str(raw_metadata_expanded).strip().lower() not in (
                "false",
                "0",
                "no",
                "",
            )
        self.inspector_panel.set_metadata_expanded(metadata_expanded)
        self.main_splitter.addWidget(self.inspector_panel)
        self.main_splitter.setCollapsible(1, False)
        self.workspace_splitter.setCollapsible(0, False)

        # Restore user custom splitter layouts or apply balanced defaults. The
        # inspector dock width is not a fixed preference: it is floored by the
        # width its own tab labels (plus outer vertical scrollbar) need at the
        # current desktop font, so ask the panel instead of hard-coding.
        inspector_default = max(340, self._inspector_dock_preferred_width())

        def balanced_sizes() -> list[int]:
            tree_w = max(480, 1260 - 260 - inspector_default)
            return [260, tree_w, inspector_default]

        saved_main_splitter = self.settings.value("main_splitter_state")
        if saved_main_splitter:
            self.main_splitter.restoreState(saved_main_splitter)
            sizes = self.main_splitter.sizes()
            if len(sizes) == 3 and sizes[1] < 240 and sum(sizes) >= 600:
                self.main_splitter.setSizes(balanced_sizes())
        else:
            self.main_splitter.setSizes(balanced_sizes())

        saved_workspace_splitter = self.settings.value("workspace_splitter_state")
        if saved_workspace_splitter:
            self.workspace_splitter.restoreState(saved_workspace_splitter)
            ws_sizes = self.workspace_splitter.sizes()
            if len(ws_sizes) == 2 and ws_sizes[0] < 160:
                self.workspace_splitter.setSizes([680, 0])
        else:
            self.workspace_splitter.setSizes([680, 0])

        # One serialized DNF5-daemon lifecycle.  It is intentionally not a
        # command queue: a resolved session capability is the only thing that
        # can proceed to execution.
        self.dnf_client = Dnf5DaemonClient(
            environment_detector=lambda: _window_host_environment(self),
            key_import_prompt=self._ask_about_repository_key,
        )
        self.host_operation_gate = HostOperationGate()
        self.transaction_controller = DnfTransactionController(
            self.dnf_client,
            get_system_protected_packages,
            thread_pool=self.thread_pool,
            host_operation_gate=self.host_operation_gate,
            parent=self,
        )
        self.transaction_controller.log_received.connect(
            self._on_transaction_log_received
        )
        self.transaction_controller.progress_changed.connect(
            self.transaction_drawer.set_progress
        )
        self.transaction_controller.plan_ready.connect(self._on_dnf_plan_ready)
        self.transaction_controller.plan_discarded.connect(self._on_dnf_plan_discarded)
        self.transaction_controller.transaction_finished.connect(
            self._on_transaction_finished
        )
        self.transaction_controller.maintenance_finished.connect(
            self._on_maintenance_finished
        )
        self.transaction_controller.operation_failed.connect(
            self._on_dnf_operation_failed
        )
        self.transaction_controller.resolution_failed.connect(
            self._on_dnf_resolution_failed
        )

        self.repository_deletion_runner = RepositoryDeletionRunner(
            environment_detector=lambda: _window_host_environment(self),
            host_operation_gate=self.host_operation_gate,
            parent=self,
        )
        self.repository_deletion_runner.log_received.connect(
            self._on_repo_runner_log_received
        )
        self.repository_deletion_runner.finished.connect(
            self._on_repository_deletion_finished
        )
        self.repository_addition_runner = RepositoryAdditionRunner(
            environment_detector=lambda: _window_host_environment(self),
            host_operation_gate=self.host_operation_gate,
            thread_pool=self.thread_pool,
            parent=self,
        )
        self.repository_addition_runner.log_received.connect(
            self._on_repo_runner_log_received
        )
        self.repository_addition_runner.finished.connect(
            self._on_repository_addition_finished
        )

        self.status_bar = QStatusBar()
        self.status_bar.setAccessibleName(tr("Status bar"))
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(tr("Ready."))

        self.setTabOrder(
            self.header.apply_btn, self.host_mode_banner.flatpak_action_btn
        )
        self.setTabOrder(
            self.host_mode_banner.flatpak_action_btn,
            self.offline_banner.poweroff_after_checkbox,
        )
        self.setTabOrder(
            self.offline_banner.poweroff_after_checkbox,
            self.offline_banner.schedule_btn,
        )
        self.setTabOrder(
            self.offline_banner.schedule_btn,
            self.offline_banner.reboot_btn,
        )
        self.setTabOrder(
            self.offline_banner.reboot_btn,
            self.offline_banner.cancel_btn,
        )
        self.setTabOrder(
            self.offline_banner.cancel_btn,
            self.offline_banner.clean_btn,
        )
        self.setTabOrder(self.offline_banner.clean_btn, self.sidebar)
        self.setTabOrder(self.sidebar, self.tree_view)
        self.setTabOrder(self.tree_view, self.inspector_panel.tabs)
        self.setTabOrder(self.inspector_panel.tabs, self.transaction_drawer.cancel_btn)

        self.proxy_model.set_category_filter("user_apps")
        _refresh_window_empty_state(self)

    def _configure_tree_columns(self):
        header = self.tree_view.header()
        header.setMinimumSectionSize(64)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(
            DependencyTreeModel.COL_NAME, QHeaderView.ResizeMode.Interactive
        )
        header.setSectionResizeMode(
            DependencyTreeModel.COL_STATUS, QHeaderView.ResizeMode.Interactive
        )
        header.setSectionResizeMode(
            DependencyTreeModel.COL_VERSION, QHeaderView.ResizeMode.Interactive
        )
        header.setSectionResizeMode(
            DependencyTreeModel.COL_SIZE, QHeaderView.ResizeMode.Interactive
        )
        header.setSectionResizeMode(
            DependencyTreeModel.COL_SUMMARY, QHeaderView.ResizeMode.Stretch
        )

        # Responsive baseline widths (~585px total instead of 745px, preventing
        # laptop column push-off). The status column must fit every upper-cased
        # pill without eliding ("MISSING DEP" and "TO UPGRADE" are the widest),
        # because a truncated state label hides the actionable information.
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_NAME, 230)
        self.tree_view.setColumnWidth(
            DependencyTreeModel.COL_STATUS, self._status_column_minimum_width()
        )
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_VERSION, 140)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_SIZE, 85)

        # Restore user custom column widths and order from persistent settings if available
        saved_header_state = self.settings.value("tree_header_state")
        if saved_header_state:
            header.restoreState(saved_header_state)

        # A state saved by an older release can still be narrower than the
        # widest pill, which would silently elide "MISSING DEP" and
        # "TO UPGRADE" again.
        minimum_status_width = self._status_column_minimum_width()
        if (
            header.isSectionHidden(DependencyTreeModel.COL_STATUS) is False
            and self.tree_view.columnWidth(DependencyTreeModel.COL_STATUS)
            < minimum_status_width
        ):
            self.tree_view.setColumnWidth(
                DependencyTreeModel.COL_STATUS, minimum_status_width
            )

        # Allow user to right-click table header to show/hide specific columns
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_context_menu)

        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self.tree_view.setSortingEnabled(True)
        self.tree_view.sortByColumn(
            DependencyTreeModel.COL_NAME, Qt.SortOrder.AscendingOrder
        )

    def _status_column_minimum_width(self) -> int:
        """Width the status column needs for its widest un-elided state pill."""
        delegate = self.tree_delegate
        # 14px of horizontal pill padding, plus the 4px pill offset and a
        # little slack on each side of the cell.
        labels = (*STATUS_LABELS.values(), *STATUS_EXTRA_LABELS)
        return (
            max(
                delegate.fm_badge.horizontalAdvance(tr(label).upper())
                for label in labels
            )
            + 26
        )

    def _on_header_context_menu(self, pos: QPoint):
        """Header context menu to selectively toggle column visibility on demand."""
        header = self.tree_view.header()
        menu = QMenu(self)
        for col_idx, col_name in enumerate(COLUMN_HEADERS):
            act = QAction(tr(col_name), menu)
            act.setCheckable(True)
            act.setChecked(not header.isSectionHidden(col_idx))
            # Column 0 (Name) must always remain visible
            if col_idx == 0:
                act.setEnabled(False)
            act.toggled.connect(
                lambda checked, idx=col_idx: header.setSectionHidden(idx, not checked)
            )
            menu.addAction(act)

        menu.exec(header.mapToGlobal(pos))

    def _setup_shortcuts(self):
        QShortcut(
            QKeySequence("Ctrl+F"),
            self,
            activated=lambda: self.header.search_input.setFocus(),
        )
        QShortcut(QKeySequence("Ctrl+B"), self, activated=self._toggle_sidebar)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._load_packages)
        QShortcut(QKeySequence("Ctrl+H"), self, activated=self._open_history_dialog)
        QShortcut(QKeySequence("Ctrl+I"), self, activated=self._toggle_inspector_panel)
        space_shortcut = QShortcut(QKeySequence("Space"), self.tree_view)
        space_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        space_shortcut.activated.connect(self._toggle_queue_selected_row)
        QShortcut(QKeySequence("F11"), self, activated=self._toggle_fullscreen)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self._open_settings_dialog)

    def _toggle_fullscreen(self):
        """Toggles fullscreen display while preserving previous maximized state."""
        if self.isFullScreen():
            if getattr(self, "_was_maximized_before_fullscreen", False):
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self._was_maximized_before_fullscreen = self.isMaximized()
            self.showFullScreen()

    def _connect_signals(self):
        # 1. Header Bar
        self.header.search_changed.connect(self._on_search_query_changed)
        self.header.search_scope_toggled.connect(self._on_search_scope_toggled)
        self.header.remote_search_requested.connect(self._on_remote_search_requested)
        self.header.reload_clicked.connect(self._load_packages)
        self.header.apply_clicked.connect(self._on_header_apply_clicked)
        self.header.toggle_inspector_clicked.connect(self._toggle_inspector_panel)
        self.header.toggle_sidebar_clicked.connect(self._toggle_sidebar)
        self.header.clear_filter_clicked.connect(self._reset_to_all_packages)
        self.header.history_clicked.connect(self._open_history_dialog)
        self.header.advisories_clicked.connect(self._open_advisories_dialog)
        self.header.flatpak_clicked.connect(self._open_flatpak_dialog)
        self.header.repos_clicked.connect(self._open_repo_dialog)
        self.header.settings_clicked.connect(self._open_settings_dialog)
        self.header.updates_clicked.connect(self._toggle_updates_view)
        self.header.upgrade_system_clicked.connect(self._on_system_upgrade_requested)
        self.header.queue_security_updates_clicked.connect(
            self._on_queue_security_updates_only
        )
        self.header.clean_orphans_clicked.connect(self._on_clean_all_orphans_clicked)
        self.header.discard_btn.clicked.connect(self._on_discard_all_clicked)
        self.header.refresh_updates_clicked.connect(
            self._on_force_refresh_updates_clicked
        )

        # 2. Sidebar Navigation
        self.sidebar.category_selected.connect(self._on_sidebar_category_selected)

        # 3. Tree View & Models
        self.tree_model.fetch_dependencies_requested.connect(
            self._on_fetch_dependencies_requested
        )
        self.tree_model.queue_state_changed.connect(self._sync_queue_states)
        self.tree_view.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree_view.selectionModel().selectionChanged.connect(
            self._on_tree_selection_changed
        )
        self.tree_view.expanded.connect(self._on_tree_item_expanded)
        self.tree_view.doubleClicked.connect(self._on_tree_item_double_clicked)

        # 4. Package Inspector Panel
        self.inspector_panel.closed.connect(self._hide_inspector_panel)
        self.inspector_panel.package_action_requested.connect(
            self._on_inspector_queue_action
        )
        self.inspector_panel.reinstall_requested.connect(
            self._on_inspector_reinstall_requested
        )
        self.inspector_panel.history_requested.connect(self._open_history_dialog)
        self.inspector_panel.package_navigate_requested.connect(
            self._navigate_to_package
        )
        self.inspector_panel.file_inspection_requested.connect(
            self._on_inspect_files_requested
        )
        self.inspector_panel.file_verification_requested.connect(
            self._on_verify_package_files_requested
        )
        self.inspector_panel.changelog_requested.connect(
            self._on_fetch_changelog_requested
        )
        self.inspector_panel.scriptlets_requested.connect(
            self._on_fetch_scriptlets_requested
        )
        self.inspector_panel.reverse_deps_requested.connect(
            self._on_fetch_reverse_deps_requested
        )
        self.inspector_panel.text_copied.connect(self._show_clipboard_status)
        self.inspector_panel.metadata_expanded_changed.connect(
            self._on_inspector_metadata_expanded_changed
        )

        # 5. Transaction Drawer
        self.transaction_drawer.closed.connect(self._close_transaction_drawer)
        self.transaction_drawer.cancel_requested.connect(self._on_drawer_cancel)
        self.transaction_drawer.commit_requested.connect(self._on_drawer_commit)

        # 6. Offline Update Lifecycle Banner
        self.offline_banner.schedule_requested.connect(
            self._on_offline_schedule_requested
        )
        self.offline_banner.cancel_requested.connect(self._on_offline_cancel_requested)
        self.offline_banner.clean_requested.connect(self._on_offline_clean_requested)
        self.offline_banner.finish_action_requested.connect(
            self._on_offline_finish_action_requested
        )
        self.offline_banner.reboot_requested.connect(self._on_offline_reboot_requested)

    def _on_inspector_metadata_expanded_changed(self, expanded: bool) -> None:
        self.settings.setValue("inspector/metadata_expanded", bool(expanded))

    # -------------------------------------------------------------------------
    # System Tray Integration
    # -------------------------------------------------------------------------
    def _init_system_tray(self):
        """Initialize the system tray icon and its on-demand menu actions."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray_icon = QSystemTrayIcon(self)

        # Load Dendro's application logo
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        app_icon = QIcon.fromTheme("io.github.xyasharx.Dendro")
        if app_icon.isNull() or not app_icon.availableSizes():
            for icon_path in [
                os.path.join(
                    base_dir,
                    "data",
                    "icons",
                    "256x256",
                    "io.github.xyasharx.Dendro.png",
                ),
                os.path.join(
                    base_dir,
                    "data",
                    "icons",
                    "128x128",
                    "io.github.xyasharx.Dendro.png",
                ),
                os.path.join(base_dir, "io.github.xyasharx.Dendro.svg"),
                "/usr/share/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg",
                "/usr/share/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png",
            ]:
                if os.path.isfile(icon_path):
                    app_icon = QIcon(icon_path)
                    break

        self.tray_icon.setIcon(app_icon)
        self.tray_icon.setToolTip(tr("Dendro Package Manager"))

        tray_menu = QMenu()
        self._tray_action_open = tray_menu.addAction(tr("Open Dendro"))
        self._tray_action_open.triggered.connect(self._restore_from_tray)

        if self.host_environment.supports_dnf5_daemon_queries:
            self._tray_action_upgrade = tray_menu.addAction(tr("Check for Updates"))
            self._tray_action_upgrade.triggered.connect(
                self._trigger_background_update_check
            )
        elif (
            self.host_environment.is_immutable_host
            and self.host_environment.supports_flatpak_management
        ):
            self._tray_action_flatpak = tray_menu.addAction(tr("Manage Flatpaks"))
            self._tray_action_flatpak.triggered.connect(self._open_flatpak_dialog)

        tray_menu.addSeparator()
        self._tray_action_quit = tray_menu.addAction(tr("Quit"))
        self._tray_action_quit.triggered.connect(QApplication.instance().quit)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _restore_from_tray(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_from_tray()

    def _trigger_background_update_check(self):
        """Run one user-requested, daemon-backed update check from the tray."""
        self._start_update_check(force_refresh=False, notify=True)

    # -------------------------------------------------------------------------
    # Local .rpm File Inspection & Installation
    # -------------------------------------------------------------------------
    def _set_busy_state(self, busy: bool, operation: str = "default") -> None:
        """Track independent UI operations without clearing another operation's cursor.

        Qt maintains a process-global override-cursor stack.  A worker failure
        must therefore remove only its own operation token, rather than popping
        every cursor override in the application.  This keeps a package reload
        visibly busy while an unrelated remote search or inspector query ends.
        """
        if busy:
            self._busy_operations.add(operation)
        else:
            self._busy_operations.discard(operation)

        if self._busy_operations:
            if not self._owns_wait_cursor and QGuiApplication.overrideCursor() is None:
                QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                self._owns_wait_cursor = True
            return

        if self._owns_wait_cursor:
            QGuiApplication.restoreOverrideCursor()
            self._owns_wait_cursor = False

    def _load_packages(self):
        # Dependency workers belong to the package tree they started against.
        # A reload invalidates all of their eventual success and error callbacks.
        self._package_tree_generation += 1
        if self.current_query_worker:
            self.current_query_worker.cancel()
        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()
        if self.current_userinstalled_worker:
            self.current_userinstalled_worker.cancel()
        if self.current_updates_worker:
            self.current_updates_worker.cancel()
            self.current_updates_worker = None
            self._update_request_serial += 1
        offline_worker = getattr(self, "current_offline_status_worker", None)
        if offline_worker:
            offline_worker.cancel()
            self.current_offline_status_worker = None
            self._offline_status_request_serial += 1
            self._prompt_reboot_after_offline_refresh = False
        rpmostree_worker = getattr(self, "current_rpmostree_worker", None)
        if rpmostree_worker:
            rpmostree_worker.cancel()
            self.current_rpmostree_worker = None

        self._catalog_error = ""
        _refresh_window_empty_state(self)
        self._set_busy_state(True, "package-load")
        self.status_bar.showMessage(tr("Reading system RPM database..."))

        # 1. Load local RPM packages first for sub-second startup display.
        # Capture the worker identity so a cancelled predecessor cannot replace
        # a newer reload with a stale result.
        worker = PackageQueryWorker(category="all", search_query="")
        self.current_query_worker = worker
        worker.signals.packages_loaded.connect(
            lambda packages, source=worker: self._on_packages_loaded(source, packages)
        )
        worker.signals.status_update.connect(
            lambda message, source=worker: self._show_status_for_current_worker(
                "current_query_worker", source, message
            )
        )
        worker.signals.error_occurred.connect(
            lambda package, detail, source=worker: self._on_package_query_error(
                source, package, detail
            )
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if worker is self.current_query_worker:
                self.current_query_worker = None
                self._set_busy_state(False, "package-load")
                self._on_query_error(
                    "",
                    tr(
                        "Could not start the installed-package scan: {error_type}: {error}"
                    ).format(error_type=type(error).__name__, error=error),
                )

    def _start_auxiliary_queries(self):
        """Dispatch non-blocking host metadata queries suited to the runtime mode."""
        environment = _window_host_environment(self)

        if environment.supports_rpmostree_readonly:
            self._start_rpmostree_status_query()
            return

        if environment.supports_dnf_cli_queries:
            userinstalled_worker = UserInstalledQueryWorker()
            self.current_userinstalled_worker = userinstalled_worker
            userinstalled_worker.signals.userinstalled_loaded.connect(
                lambda user_pkgs, source=userinstalled_worker: (
                    self._on_userinstalled_loaded(source, user_pkgs)
                )
            )
            userinstalled_worker.signals.status_update.connect(
                lambda message, source=userinstalled_worker: (
                    self._show_status_for_current_worker(
                        "current_userinstalled_worker", source, message
                    )
                )
            )
            userinstalled_worker.signals.error_occurred.connect(
                lambda package, detail, source=userinstalled_worker: (
                    self._on_userinstalled_query_error(source, package, detail)
                )
            )
            try:
                self.thread_pool.start(userinstalled_worker)
            except Exception as error:
                if userinstalled_worker is self.current_userinstalled_worker:
                    self.current_userinstalled_worker = None
                    self._on_query_error(
                        "",
                        tr(
                            "Could not start user-installed package detection: {error_type}: {error}"
                        ).format(error_type=type(error).__name__, error=error),
                    )

        if environment.supports_orphan_detection:
            orphan_worker = OrphanQueryWorker()
            self.current_orphan_worker = orphan_worker
            orphan_worker.signals.orphans_loaded.connect(
                lambda orphans, source=orphan_worker: self._on_orphans_loaded(
                    source, orphans
                )
            )
            orphan_worker.signals.status_update.connect(
                lambda message, source=orphan_worker: (
                    self._show_status_for_current_worker(
                        "current_orphan_worker", source, message
                    )
                )
            )
            orphan_worker.signals.error_occurred.connect(
                lambda package, detail, source=orphan_worker: (
                    self._on_orphan_query_error(source, package, detail)
                )
            )
            try:
                self.thread_pool.start(orphan_worker)
            except Exception as error:
                if orphan_worker is self.current_orphan_worker:
                    self.current_orphan_worker = None
                    self._on_query_error(
                        "",
                        tr(
                            "Could not start unneeded-package detection: {error_type}: {error}"
                        ).format(error_type=type(error).__name__, error=error),
                    )

        if environment.supports_dnf5_daemon_queries:
            self._start_update_check(force_refresh=False, notify=False)
            self._start_offline_status_check()

    def _start_rpmostree_status_query(self) -> None:
        """Read unprivileged OSTree deployment metadata on Fedora Atomic hosts."""
        if self.current_rpmostree_worker is not None:
            self.current_rpmostree_worker.cancel()
        worker = RpmOstreeStatusWorker()
        self.current_rpmostree_worker = worker
        worker.signals.status_update.connect(
            lambda message, source=worker: self._show_status_for_current_worker(
                "current_rpmostree_worker", source, message
            )
        )
        worker.signals.loaded.connect(
            lambda status, source=worker: self._on_rpmostree_status_loaded(
                source, status
            )
        )
        worker.signals.failed.connect(
            lambda detail, source=worker: self._on_rpmostree_status_failed(
                source, detail
            )
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if worker is self.current_rpmostree_worker:
                self.current_rpmostree_worker = None
                LOGGER.info(
                    "Could not start read-only rpm-ostree inspection: %s",
                    error,
                )

    def _apply_rpmostree_status_to_packages(
        self, packages: list[PackageInfo], status: RpmOstreeHostStatus
    ) -> set[str]:
        """Annotate installed packages with OSTree base/layered provenance."""
        layered_nevras: set[str] = set()
        for package in packages:
            origin = status.classify_installed_package(
                name=package.name,
                arch=package.arch,
                nevra=package.nevra,
                full_nevra=package.full_nevra,
            )
            package.ostree_origin = origin
            is_layered = origin in {
                RpmOstreeOriginKind.LAYERED,
                RpmOstreeOriginKind.LOCAL,
                RpmOstreeOriginKind.OVERRIDE,
            }
            package.is_user_installed = is_layered
            package.is_orphan = False
            if is_layered:
                layered_nevras.add(package.full_nevra)
        return layered_nevras

    def _on_rpmostree_status_loaded(
        self, worker: RpmOstreeStatusWorker, status: object
    ) -> None:
        if worker is not self.current_rpmostree_worker:
            return
        self.current_rpmostree_worker = None
        if not isinstance(status, RpmOstreeHostStatus):
            return

        self._rpmostree_status = status
        self._orphan_cache = set()
        self._pending_updates_map = {}

        layered_nevras: set[str] = set()
        if self._all_packages_cache:
            layered_nevras = self._apply_rpmostree_status_to_packages(
                self._all_packages_cache, status
            )
            self._user_installed_cache = layered_nevras
            self.tree_model.update_user_installed(layered_nevras)
            self.tree_model.update_orphans(set())
            self._update_sidebar_counts(self._all_packages_cache)
            self.sidebar.update_category_counts({"orphans": 0, "updates_available": 0})
            self.proxy_model.refresh_filter()

            if self.inspector_panel._current_package is not None:
                fresh_item = self.tree_model.item_for_package(
                    self.inspector_panel._current_package,
                    allow_name_arch_fallback=True,
                )
                if fresh_item and isinstance(fresh_item.payload, PackageInfo):
                    self._set_inspector_package_info(fresh_item.payload)

        if hasattr(self, "host_mode_banner"):
            self.host_mode_banner.set_deployment_summary(status.summary_line())

        layered_count = len(layered_nevras)
        pending_note = (
            " " + tr("(staged OS update pending reboot)")
            if status.has_pending_deployment
            else ""
        )
        self.status_bar.showMessage(
            tr_plural(
                "Fedora Atomic deployment inspected: {count} layered/overridden RPM detected{pending}.",
                "Fedora Atomic deployment inspected: {count} layered/overridden RPMs detected{pending}.",
                layered_count,
            ).format(count=format_number(layered_count), pending=pending_note),
            6000,
        )

    def _on_rpmostree_status_failed(
        self, worker: RpmOstreeStatusWorker, detail: str
    ) -> None:
        if worker is not self.current_rpmostree_worker:
            return
        self.current_rpmostree_worker = None
        LOGGER.info("Read-only rpm-ostree deployment query unavailable: %s", detail)
        self.status_bar.showMessage(
            tr(
                "Loaded installed RPM inventory (layered deployment metadata unavailable)."
            ),
            5000,
        )

    def _on_packages_loaded(
        self, worker: PackageQueryWorker, packages: list[PackageInfo]
    ) -> None:
        if worker is not self.current_query_worker:
            return
        self._catalog_loaded = True
        self._catalog_error = ""
        self._all_packages_cache = packages

        rpmostree_status = getattr(self, "_rpmostree_status", None)
        if isinstance(rpmostree_status, RpmOstreeHostStatus):
            self._user_installed_cache = self._apply_rpmostree_status_to_packages(
                packages, rpmostree_status
            )
        else:
            if self._user_installed_cache:
                for p in packages:
                    p.is_user_installed = p.full_nevra in self._user_installed_cache
            if self._orphan_cache:
                for p in packages:
                    p.is_orphan = p.full_nevra in self._orphan_cache
        if self._pending_updates_map:
            for p in packages:
                up = self._pending_updates_map.get(p.name_arch)
                if up:
                    p.has_update = True
                    p.available_update_version = up.full_version
                    p.available_update_repo = up.repository
                    p.is_security_update = up.is_security
                    p.advisory_id = up.advisory_id
                    p.advisory_severity = up.advisory_severity

        self.tree_model.set_packages(packages)
        self._update_sidebar_counts(packages)
        self._sync_queue_states()
        _refresh_window_empty_state(self)

        # Synchronize InspectorPanel with the updated database state
        if self.inspector_panel._current_package:
            fresh_item = self.tree_model.item_for_package(
                self.inspector_panel._current_package,
                allow_name_arch_fallback=True,
            )
            if fresh_item and isinstance(fresh_item.payload, PackageInfo):
                self._set_inspector_package_info(fresh_item.payload)
            else:
                # Package was removed from the system; clear the inspector
                self._clear_inspector_panel()

        self.status_bar.showMessage(
            tr_plural(
                "Loaded {count} package successfully.",
                "Loaded {count} packages successfully.",
                len(packages),
            ).format(count=format_number(len(packages)))
        )
        self._set_busy_state(False, "package-load")
        self.current_query_worker = None

        # Render is complete; dispatch background auxiliary queries without blocking the UI
        self._start_auxiliary_queries()

    def _on_userinstalled_loaded(
        self, worker: UserInstalledQueryWorker, user_pkg_nevras: set[str]
    ) -> None:
        if worker is not self.current_userinstalled_worker:
            return
        self._user_installed_cache = user_pkg_nevras
        self.tree_model.update_user_installed(user_pkg_nevras)
        if self._all_packages_cache:
            for p in self._all_packages_cache:
                p.is_user_installed = p.full_nevra in user_pkg_nevras
            self._update_sidebar_counts(self._all_packages_cache)
        # Refresh filter so user-installed view updates if active
        self.proxy_model.refresh_filter()
        _refresh_window_empty_state(self)
        self._refresh_active_inspector_status()
        self.current_userinstalled_worker = None

    def _on_userinstalled_query_error(
        self, worker: UserInstalledQueryWorker, pkg_name: str, message: str
    ) -> None:
        if worker is not self.current_userinstalled_worker:
            return
        self.current_userinstalled_worker = None
        self._on_query_error(pkg_name, message)

    def _on_orphans_loaded(
        self, worker: OrphanQueryWorker, orphan_nevras: set[str]
    ) -> None:
        if worker is not self.current_orphan_worker:
            return
        self._orphan_cache = orphan_nevras
        self.tree_model.update_orphans(orphan_nevras)
        if self._all_packages_cache:
            for p in self._all_packages_cache:
                p.is_orphan = p.full_nevra in orphan_nevras
        self.sidebar.update_category_counts({"orphans": len(orphan_nevras)})
        # Refresh filter so newly loaded leaf orphans become visible immediately
        self.proxy_model.refresh_filter()
        _refresh_window_empty_state(self)
        if self.proxy_model._category == "orphans":
            self.header.set_orphan_clean_visible(True, len(orphan_nevras))
        self._refresh_active_inspector_status()
        self.current_orphan_worker = None

    def _on_orphan_query_error(
        self, worker: OrphanQueryWorker, pkg_name: str, message: str
    ) -> None:
        if worker is not self.current_orphan_worker:
            return
        self.current_orphan_worker = None
        self._on_query_error(pkg_name, message)

    def _start_update_check(self, *, force_refresh: bool, notify: bool) -> None:
        """Start one identified update request and ignore stale worker callbacks."""
        environment = _window_host_environment(self)
        if not environment.supports_dnf5_daemon_queries:
            self.status_bar.showMessage(
                environment.feature_unavailable("System update checks"), 6000
            )
            return
        if self.current_updates_worker is not None:
            return

        self._update_request_serial += 1
        request_serial = self._update_request_serial
        worker = SystemUpdatesCheckWorker(force_refresh=force_refresh)
        self.current_updates_worker = worker
        worker.signals.status_update.connect(
            lambda message, source=worker, serial=request_serial: (
                self._on_update_check_status(source, serial, message)
            )
        )
        worker.signals.error_occurred.connect(
            lambda _package, detail, source=worker, serial=request_serial: (
                self._on_update_check_failed(source, serial, detail)
            )
        )
        worker.signals.system_updates_loaded.connect(
            lambda updates, source=worker, serial=request_serial, show_notification=notify: (
                self._on_updates_loaded(source, serial, updates, show_notification)
            )
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if (
                request_serial == self._update_request_serial
                and worker is self.current_updates_worker
            ):
                self.current_updates_worker = None
                self.status_bar.showMessage(
                    tr("Update check could not start: {error_type}: {error}").format(
                        error_type=type(error).__name__, error=error
                    ),
                    8000,
                )

    def _on_update_check_status(
        self, worker: SystemUpdatesCheckWorker, request_serial: int, message: str
    ) -> None:
        """Keep a stale update worker from replacing newer status feedback."""
        if (
            request_serial == self._update_request_serial
            and worker is self.current_updates_worker
        ):
            self.status_bar.showMessage(message)

    def _on_update_check_failed(
        self, worker: SystemUpdatesCheckWorker, request_serial: int, detail: str
    ) -> None:
        if (
            request_serial != self._update_request_serial
            or worker is not self.current_updates_worker
        ):
            return
        self.current_updates_worker = None
        self.status_bar.showMessage(
            tr("Update check failed: {detail}").format(detail=detail), 8000
        )

    def _on_updates_loaded(
        self,
        worker: SystemUpdatesCheckWorker,
        request_serial: int,
        updates_map: dict[str, AvailableUpdateInfo],
        show_notification: bool,
    ) -> None:
        if (
            request_serial != self._update_request_serial
            or worker is not self.current_updates_worker
        ):
            return
        self.current_updates_worker = None
        self._pending_updates_map = updates_map
        self.tree_model.update_available_upgrades(updates_map, replace=True)
        if self._all_packages_cache:
            for package in self._all_packages_cache:
                update = updates_map.get(package.name_arch)
                if update:
                    package.has_update = True
                    package.available_update_version = update.full_version
                    package.available_update_repo = update.repository
                    package.is_security_update = update.is_security
                    package.advisory_id = update.advisory_id
                    package.advisory_severity = update.advisory_severity
                else:
                    package.has_update = False
                    package.available_update_version = ""
                    package.available_update_repo = ""
                    package.is_security_update = False
                    package.advisory_id = ""
                    package.advisory_severity = ""
            self._update_sidebar_counts(self._all_packages_cache)

        count = len(updates_map)
        security_count = sum(1 for update in updates_map.values() if update.is_security)
        self.sidebar.update_category_counts({"updates_available": count})
        self.header.update_available_updates_badge(count, security_count=security_count)
        if self.proxy_model.category_filter == "updates_available":
            self.header.set_updates_view_active(
                True, count, security_count=security_count
            )
        self.proxy_model.refresh_filter()
        _refresh_window_empty_state(self)
        self._refresh_active_inspector_status()
        if count > 0:
            self.status_bar.showMessage(
                tr_plural(
                    "{count} software update is available for your system.",
                    "{count} software updates are available for your system.",
                    count,
                ).format(count=format_number(count))
            )
        else:
            self.status_bar.showMessage(tr("Your system is completely up to date."))

        if (
            show_notification
            and count > 0
            and hasattr(self, "tray_icon")
            and self.tray_icon.isVisible()
        ):
            self.tray_icon.showMessage(
                tr("Dendro - Software Updates"),
                tr_plural(
                    "{count} package update is available for your Fedora system.",
                    "{count} package updates are available for your Fedora system.",
                    count,
                ).format(count=format_number(count)),
                QSystemTrayIcon.MessageIcon.Information,
                6000,
            )

    def _start_offline_status_check(
        self, *, prompt_reboot_if_ready: bool = False
    ) -> None:
        """Query ``org.rpm.dnf.v0.Offline.get_status()`` without blocking the UI."""
        environment = _window_host_environment(self)
        offline_banner = getattr(self, "offline_banner", None)
        if not environment.supports_dnf5_daemon_queries:
            self._offline_status = None
            self._reboot_capability = RebootCapability.UNAVAILABLE
            self._prompt_reboot_after_offline_refresh = False
            if offline_banner is not None:
                offline_banner.update_status(
                    None, environment, RebootCapability.UNAVAILABLE
                )
            return

        previous = getattr(self, "current_offline_status_worker", None)
        if previous is not None:
            previous.cancel()
            self.current_offline_status_worker = None
        self._offline_status_request_serial += 1
        request_serial = self._offline_status_request_serial
        if prompt_reboot_if_ready:
            self._prompt_reboot_after_offline_refresh = True

        worker = OfflineStatusQueryWorker()
        self.current_offline_status_worker = worker
        worker.signals.loaded.connect(
            lambda status, reboot_cap, source=worker, serial=request_serial: (
                self._on_offline_status_loaded(source, serial, status, reboot_cap)
            )
        )
        worker.signals.failed.connect(
            lambda title, detail, source=worker, serial=request_serial: (
                self._on_offline_status_failed(source, serial, title, detail)
            )
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if (
                request_serial == self._offline_status_request_serial
                and worker is self.current_offline_status_worker
            ):
                self.current_offline_status_worker = None
                self._prompt_reboot_after_offline_refresh = False
                LOGGER.info("Could not check DNF5 offline status: %s", error)

    def _on_offline_status_loaded(
        self,
        worker: OfflineStatusQueryWorker,
        request_serial: int,
        status: object,
        reboot_capability: object,
    ) -> None:
        if (
            request_serial != self._offline_status_request_serial
            or worker is not self.current_offline_status_worker
        ):
            return
        self.current_offline_status_worker = None
        if not isinstance(status, OfflineTransactionStatus):
            self._prompt_reboot_after_offline_refresh = False
            return
        capability = (
            reboot_capability
            if isinstance(reboot_capability, RebootCapability)
            else RebootCapability.UNAVAILABLE
        )
        environment = _window_host_environment(self)
        self._offline_status = status
        self._reboot_capability = capability
        offline_banner = getattr(self, "offline_banner", None)
        if offline_banner is not None:
            offline_banner.update_status(status, environment, capability)

        should_prompt = self._prompt_reboot_after_offline_refresh
        self._prompt_reboot_after_offline_refresh = False
        if (
            should_prompt
            and status.is_scheduled_for_boot
            and environment.supports_host_reboot_control
            and capability.can_trigger_immediately
            and not getattr(self, "_close_after_transaction", False)
        ):
            self._prompt_reboot_to_apply_offline_update(status)

    def _on_offline_status_failed(
        self,
        worker: OfflineStatusQueryWorker,
        request_serial: int,
        title: str,
        detail: str,
    ) -> None:
        if (
            request_serial != self._offline_status_request_serial
            or worker is not self.current_offline_status_worker
        ):
            return
        self.current_offline_status_worker = None
        self._prompt_reboot_after_offline_refresh = False
        LOGGER.info("Offline status check failed (%s): %s", title, detail)

    def _on_sidebar_category_selected(self, category: str):
        self.proxy_model.set_category_filter(category)
        is_orphans = category == "orphans"
        is_updates = category == "updates_available"

        # Show contextual actions strictly in their relevant views
        self.header.set_orphan_clean_visible(is_orphans, len(self._orphan_cache))
        security_count = sum(
            1 for update in self._pending_updates_map.values() if update.is_security
        )
        self.header.set_updates_view_active(
            is_updates,
            len(self._pending_updates_map),
            security_count=security_count,
        )
        self._sync_search_scope_ui()
        _refresh_window_empty_state(self)
        if self.proxy_model.search_query and not is_file_lookup_query(
            self.proxy_model.search_query
        ):
            self._update_search_scope_status_message(self.proxy_model.search_query)

    def _filter_to_updates(self):
        """Switches the view to show the list of available updates for review."""
        self.sidebar.select_category("updates_available", emit=False)
        # Explicitly notify controller so header contextual actions (Upgrade All, Check Mirrors) become visible
        self._on_sidebar_category_selected("updates_available")

    def _on_force_refresh_updates_clicked(self):
        """Expire metadata through the DNF5 daemon, then reload upgrade candidates."""
        self._start_update_check(force_refresh=True, notify=False)

    def _update_sidebar_counts(self, packages: list[PackageInfo]):
        """Count through the same registry predicate used by visible filtering."""
        counts: dict[str, int] = dict.fromkeys(sidebar_category_ids(), 0)
        for package in packages:
            for identifier in matching_sidebar_categories(package):
                counts[identifier] += 1
        self.sidebar.update_category_counts(counts)

    def _on_package_query_error(
        self, worker: PackageQueryWorker, pkg_name: str, message: str
    ) -> None:
        """Finish only the package-load cursor token for its current worker."""
        if worker is not self.current_query_worker:
            return
        self.current_query_worker = None
        self._set_busy_state(False, "package-load")
        if not getattr(self, "_catalog_loaded", False):
            self._catalog_error = message
            _refresh_window_empty_state(self)
        self._on_query_error(pkg_name, message)
        if (
            _window_host_environment(self).supports_dnf5_daemon_queries
            and self.current_offline_status_worker is None
            and self._offline_status is None
        ):
            self._start_offline_status_check()

    def _show_status_for_current_worker(
        self, worker_attribute: str, worker: object, message: str
    ) -> None:
        """Do not let a cancelled predecessor overwrite current UI feedback."""
        if getattr(self, worker_attribute, None) is worker:
            self.status_bar.showMessage(message)

    def _on_query_error(self, pkg_name: str, message: str) -> None:
        """Present an auxiliary-query error without changing unrelated busy state."""
        self.status_bar.showMessage(
            tr("Error: {message}").format(message=message), 8000
        )
        if pkg_name:
            self.tree_model.reset_loading_state(pkg_name)

    def _inspector_dock_preferred_width(self) -> int:
        preferred_fn = getattr(self.inspector_panel, "preferred_dock_width", None)
        if callable(preferred_fn):
            return int(preferred_fn())
        return self.inspector_panel.minimumWidth()

    def _toggle_sidebar(self):
        """Toggles sidebar visibility or restores it if collapsed in the splitter."""
        three_pane_floor = (
            self.sidebar.minimumWidth() + 140 + self._inspector_dock_preferred_width()
        )
        if self.sidebar.isVisible():
            sizes = self.main_splitter.sizes()
            if sizes[0] == 0:
                sidebar_w = min(290, max(180, sum(sizes) // 4))
                self.main_splitter.setSizes(
                    [sidebar_w, max(120, sizes[1] - sidebar_w), sizes[2]]
                )
            else:
                self._sidebar_auto_collapsed = False
                self._manual_sidebar_override = self.width() < three_pane_floor
                self.sidebar.hide()
        else:
            self._sidebar_auto_collapsed = False
            self._manual_sidebar_override = self.width() < three_pane_floor
            self.sidebar.show()
            available = max(600, self.main_splitter.width())
            sidebar_w = min(290, max(180, available // 4))
            inspector_w = max(self._inspector_dock_preferred_width(), 370)
            inspector_w = min(inspector_w, max(240, available - sidebar_w - 140))
            tree_w = max(140, available - sidebar_w - inspector_w)
            self.main_splitter.setSizes([sidebar_w, tree_w, inspector_w])

    def _toggle_updates_view(self):
        """Allows toggling between Updates view and All Packages directly from the header."""
        if self.proxy_model._category == "updates_available":
            self._reset_to_all_packages()
        else:
            self._filter_to_updates()

    def _sync_search_scope_ui(self) -> None:
        """Keep the header search-scope chip synchronized with proxy filter state."""
        header = getattr(self, "header", None)
        proxy_model = getattr(self, "proxy_model", None)
        if header is None or proxy_model is None:
            return
        setter = getattr(header, "set_search_scope_state", None)
        if not callable(setter):
            return
        category = str(getattr(proxy_model, "category_filter", "all") or "all")
        query = str(getattr(proxy_model, "search_query", "") or "")
        search_all = bool(getattr(proxy_model, "search_all_packages", True))
        is_regular_category = (
            bool(category)
            and category != "all"
            and category not in PackageFilterProxyModel.MAINTENANCE_CATEGORIES
        )
        setter(
            visible=bool(query.strip()) and is_regular_category,
            search_all_packages=search_all,
            category_id=category,
        )

    def _update_search_scope_status_message(self, query: str) -> None:
        """Surface active search scope in the status bar without clobbering workers."""
        if (
            getattr(self, "current_file_owner_worker", None) is not None
            or getattr(self, "current_remote_search_worker", None) is not None
        ):
            return
        status_bar = getattr(self, "status_bar", None)
        proxy_model = getattr(self, "proxy_model", None)
        if status_bar is None or proxy_model is None:
            return

        clean = query.strip()
        if not clean:
            last_msg = getattr(self, "_last_scope_status_message", "")
            current_fn = getattr(status_bar, "currentMessage", None)
            clear_fn = getattr(status_bar, "clearMessage", None)
            if (
                last_msg
                and callable(current_fn)
                and callable(clear_fn)
                and current_fn() == last_msg
            ):
                clear_fn()
            self._last_scope_status_message = ""
            return

        category = str(getattr(proxy_model, "category_filter", "all") or "all")
        if category == "all":
            return

        effective_scope_fn = getattr(proxy_model, "effective_search_scope_all", None)
        row_count_fn = getattr(proxy_model, "rowCount", None)
        if not callable(effective_scope_fn) or not callable(row_count_fn):
            return

        count = int(row_count_fn())
        if effective_scope_fn():
            if query_has_explicit_category_filter(clean):
                message = tr_plural(
                    "Filtering across all packages — {count} match.",
                    "Filtering across all packages — {count} matches.",
                    count,
                ).format(count=format_number(count))
            else:
                message = tr_plural(
                    "Searching all packages — {count} match for '{query}'.",
                    "Searching all packages — {count} matches for '{query}'.",
                    count,
                ).format(count=format_number(count), query=clean)
        else:
            cat_name = category_label(category, fallback=category)
            message = tr_plural(
                "Searching in '{category}' — {count} match.",
                "Searching in '{category}' — {count} matches.",
                count,
            ).format(category=cat_name, count=format_number(count))

        self._last_scope_status_message = message
        status_bar.showMessage(message, 6000)

    def _on_search_scope_toggled(self, search_all: bool) -> None:
        self.proxy_model.set_search_all_packages(search_all)
        self._sync_search_scope_ui()
        _refresh_window_empty_state(self)
        query = self.proxy_model.search_query
        if query and not is_file_lookup_query(query):
            self._update_search_scope_status_message(query)

    def _on_search_query_changed(self, query: str):
        """Filter immediately, then resolve an existing file path off the GUI thread."""
        self._file_owner_request_serial += 1
        request_serial = self._file_owner_request_serial
        if self.current_file_owner_worker is not None:
            self.current_file_owner_worker.cancel()
            self.current_file_owner_worker = None

        clean = query.strip()
        clean_path = clean.removeprefix("file:").strip()
        self.proxy_model.set_search_query(query)
        sync_scope_fn = getattr(self, "_sync_search_scope_ui", None)
        if callable(sync_scope_fn):
            sync_scope_fn()
        _refresh_window_empty_state(self)

        # Only inspect complete, existing system paths; ordinary package search
        # remains entirely local and immediate.
        if not (
            clean_path.startswith("/")
            and len(clean_path) > 1
            and os.path.exists(clean_path)
        ):
            if not is_file_lookup_query(query):
                update_scope_status = getattr(
                    self, "_update_search_scope_status_message", None
                )
                if callable(update_scope_status):
                    update_scope_status(query)
            return

        worker = FileOwnerLookupWorker(clean_path)
        self.current_file_owner_worker = worker
        worker.signals.resolved.connect(
            lambda path, owner, source=worker, serial=request_serial, source_query=query: (
                self._on_file_owner_lookup_resolved(
                    source, serial, source_query, path, owner
                )
            )
        )
        worker.signals.failed.connect(
            lambda path, detail, source=worker, serial=request_serial: (
                self._on_file_owner_lookup_failed(source, serial, path, detail)
            )
        )
        self.status_bar.showMessage(
            tr("Looking up the RPM owner of '{path}'…").format(path=clean_path)
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if worker is self.current_file_owner_worker:
                self.current_file_owner_worker = None
                self.status_bar.showMessage(
                    tr(
                        "Could not start the RPM ownership lookup for '{path}': {error_type}: {error}"
                    ).format(
                        path=clean_path, error_type=type(error).__name__, error=error
                    ),
                    8000,
                )

    def _on_file_owner_lookup_resolved(
        self,
        worker: FileOwnerLookupWorker,
        request_serial: int,
        query: str,
        file_path: str,
        owner: object,
    ) -> None:
        if (
            request_serial != self._file_owner_request_serial
            or worker is not self.current_file_owner_worker
        ):
            return
        self.current_file_owner_worker = None
        if isinstance(owner, PackageInfo):
            self.status_bar.showMessage(
                tr("File '{path}' is owned by package '{package}'.").format(
                    path=file_path, package=owner.full_nevra
                ),
                4000,
            )
            self.proxy_model.set_search_query(owner.name)
            sync_scope_fn = getattr(self, "_sync_search_scope_ui", None)
            if callable(sync_scope_fn):
                sync_scope_fn()
            _refresh_window_empty_state(self)
            self._navigate_to_package(owner)
        elif owner is not None:
            self.status_bar.showMessage(
                tr("Could not interpret the RPM owner returned for '{path}'.").format(
                    path=file_path
                ),
                8000,
            )
        elif query.strip().lower().startswith("file:"):
            self.status_bar.showMessage(
                tr(
                    "Dendro could not identify an installed RPM package that owns '{path}'."
                ).format(path=file_path),
                5000,
            )

    def _on_file_owner_lookup_failed(
        self,
        worker: FileOwnerLookupWorker,
        request_serial: int,
        file_path: str,
        detail: str,
    ) -> None:
        if (
            request_serial == self._file_owner_request_serial
            and worker is self.current_file_owner_worker
        ):
            self.current_file_owner_worker = None
            self.status_bar.showMessage(
                tr("Could not inspect the RPM owner for '{path}': {detail}").format(
                    path=file_path, detail=detail
                ),
                8000,
            )

    def _on_remote_search_requested(self, query: str):
        """Dispatches an on-demand background query for uninstalled packages in DNF repositories."""
        clean = query.strip()
        if len(clean) < 2:
            self.status_bar.showMessage(
                tr("Please enter at least 2 characters to search remote repositories."),
                3500,
            )
            return

        environment = _window_host_environment(self)
        if not environment.supports_dnf_cli_queries and not environment.is_flatpak:
            self.status_bar.showMessage(
                environment.feature_unavailable("Remote repository search"),
                6000,
            )
            return

        if self.current_remote_search_worker is not None:
            # The subprocess cannot be force-killed safely from the UI.  Mark
            # the old request stale, then accept only this new worker's result.
            self.current_remote_search_worker.cancel()
        self._set_busy_state(True, "remote-search")
        self.status_bar.showMessage(
            tr("Searching remote repositories for '{query}'...").format(query=clean)
        )

        worker = RemotePackageSearchWorker(query=clean)
        self.current_remote_search_worker = worker
        worker.signals.remote_search_finished.connect(
            lambda query, packages, source=worker: self._on_remote_search_finished(
                source, query, packages
            )
        )
        worker.signals.error_occurred.connect(
            lambda package, detail, source=worker: self._on_remote_search_failed(
                source, package, detail
            )
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            self._on_remote_search_failed(
                worker,
                "",
                tr(
                    "Could not start remote repository search: {error_type}: {error}"
                ).format(error_type=type(error).__name__, error=error),
            )

    def _on_remote_search_finished(
        self,
        worker: RemotePackageSearchWorker,
        query: str,
        packages: list[PackageInfo],
    ) -> None:
        if worker is not self.current_remote_search_worker:
            return
        self.current_remote_search_worker = None
        self._set_busy_state(False, "remote-search")
        if not packages:
            self.status_bar.showMessage(
                tr(
                    "No packages matching '{query}' found in enabled repositories."
                ).format(query=query),
                4000,
            )
            return

        added = self.tree_model.merge_remote_packages(packages)
        sidebar = getattr(self, "sidebar", None)
        if sidebar is not None and hasattr(sidebar, "select_category"):
            sidebar.select_category("all", emit=False)
        header = getattr(self, "header", None)
        if header is not None:
            if hasattr(header, "set_orphan_clean_visible"):
                header.set_orphan_clean_visible(
                    False, len(getattr(self, "_orphan_cache", ()))
                )
            if hasattr(header, "set_updates_view_active"):
                pending_updates = getattr(self, "_pending_updates_map", {})
                header.set_updates_view_active(
                    False,
                    len(pending_updates),
                    security_count=sum(
                        1
                        for update in pending_updates.values()
                        if getattr(update, "is_security", False)
                    ),
                )
        self.proxy_model.set_category_filter("all")
        self.proxy_model.set_search_query(query)
        sync_scope_fn = getattr(self, "_sync_search_scope_ui", None)
        if callable(sync_scope_fn):
            sync_scope_fn()
        _refresh_window_empty_state(self)
        action_hint = (
            tr("Select to review and install.")
            if _window_host_environment(self).supports_dnf5_daemon_mutations
            else tr("Select to inspect package metadata.")
        )
        found_text = tr_plural(
            "Found {count} repository package ({added} new).",
            "Found {count} repository packages ({added} new).",
            len(packages),
        ).format(count=format_number(len(packages)), added=format_number(added))
        self.status_bar.showMessage(f"{found_text} {action_hint}", 5000)

    def _on_remote_search_failed(
        self, worker: RemotePackageSearchWorker, pkg_name: str, message: str
    ) -> None:
        if worker is not self.current_remote_search_worker:
            return
        self.current_remote_search_worker = None
        self._set_busy_state(False, "remote-search")
        self._on_query_error(pkg_name, message)

    def _reset_to_all_packages(self):
        """Returns to the default all packages view and clears category filters."""
        self.header.search_input.clear()
        self.sidebar.select_category("all", emit=False)
        self._on_sidebar_category_selected("all")

    def _on_tree_item_double_clicked(self, proxy_index: QModelIndex):
        source_index = self.proxy_model.mapToSource(proxy_index)
        if not source_index.isValid():
            return
        item: TreeItem = source_index.internalPointer()
        if item and item.is_dependency and isinstance(item.payload, DependencyNode):
            target_pkg = item.payload.resolved_package_name
            self._navigate_to_package(target_pkg)

    def _find_local_package_item(self, target: str):
        """Find a ``TreeItem`` matching ``target`` (``name.arch`` or ``name``)."""
        cleaned = target.strip()
        if not cleaned:
            return None
        direct = self.tree_model._package_lookup.get(cleaned)
        if direct is not None:
            return direct
        for candidate in self.tree_model.root_item.child_items:
            if (
                isinstance(candidate.payload, PackageInfo)
                and candidate.payload.name_arch == cleaned
            ):
                return candidate
        if "." in cleaned:
            base_name = cleaned.rsplit(".", 1)[0]
            return self.tree_model._package_lookup.get(base_name)
        return None

    def _navigate_to_package(self, package: PackageInfo | str):
        """Jump to an installed package, preserving a concrete RPM identity when known."""
        if isinstance(package, PackageInfo):
            source_item = self.tree_model.item_for_package(package)
            display_name = package.full_nevra
        else:
            pkg_name = package
            if not pkg_name:
                return
            source_item = self._find_local_package_item(pkg_name)
            display_name = pkg_name
        if not source_item:
            self.status_bar.showMessage(
                tr("Package '{name}' is not installed locally.").format(
                    name=display_name
                ),
                3500,
            )
            return

        source_idx = self.tree_model.createIndex(source_item.row(), 0, source_item)
        proxy_idx = self.proxy_model.mapFromSource(source_idx)

        # If hidden by active filter, reset category to show the destination package
        if not proxy_idx.isValid():
            self._reset_to_all_packages()
            proxy_idx = self.proxy_model.mapFromSource(source_idx)

        if proxy_idx.isValid():
            self.tree_view.setCurrentIndex(proxy_idx)
            self.tree_view.scrollTo(
                proxy_idx, DendroTreeView.ScrollHint.PositionAtCenter
            )
            self._set_inspector_package_info(source_item.payload)
            if not self._inspector_panel_is_open():
                self._show_inspector_panel()

    # -------------------------------------------------------------------------
    # Preferences and About Dialog
    # -------------------------------------------------------------------------
    def _open_settings_dialog(self):
        from dendro.ui.settings_dialog import DendroSettingsDialog

        dlg = DendroSettingsDialog(
            self, host_environment=_window_host_environment(self)
        )
        dlg.epoch_display_toggled.connect(self.tree_model.set_show_epoch)
        dlg.theme_changed.connect(self._set_theme_choice)
        dlg.language_changed.connect(self._apply_language_choice)
        dlg.exec()

    def _apply_language_choice(self, preference: str) -> None:
        """Switch languages live and repaint the window in the new locale.

        ``install_locale`` retranslates every live widget that exposes a
        ``retranslate`` hook, so this method does not enumerate children: a
        hand-maintained list here would go stale the first time a panel was
        added and would never cover a dialog opened afterwards. Ordering
        matters, though. The running status message was set in the old language
        and has no widget to re-run, and the window title belongs to this
        object, so both are refreshed explicitly after the sweep.
        """

        resolved = resolve_locale(preference, os.environ.get("LANG", ""))
        previous_locale = active_locale()
        status_message = self.status_bar.currentMessage()
        was_scope_status = bool(
            status_message and status_message == self._last_scope_status_message
        )
        source_status = (
            reverse_translate(status_message, previous_locale)
            if status_message and not was_scope_status
            else ""
        )
        install_locale(resolved.locale)
        self._refresh_window_title()
        if was_scope_status and self.proxy_model.search_query:
            self._update_search_scope_status_message(self.proxy_model.search_query)
        elif source_status:
            # Re-run the lookup from the recovered English key so switching
            # between two non-English locales translates the status line too.
            self.status_bar.showMessage(tr(source_status))

    def _refresh_window_title(self) -> None:
        """Write the window title for the current language.

        The title is the window's identity, so it is one message: a language
        switch rewrites it, it never replaces it with a different name.
        """

        self.setWindowTitle(tr("Fedora Package Tree & Dependency Inspector (Dendro)"))

    # -------------------------------------------------------------------------
    # UI Interactions, Selection & Context Menu
    # -------------------------------------------------------------------------
    def _toggle_queue_selected_row(self):
        environment = _window_host_environment(self)
        if not environment.supports_dnf5_daemon_mutations:
            _title, detail = environment.dnf5_daemon_unavailable(
                tr("stage package changes")
            )
            self.status_bar.showMessage(detail, 6000)
            return
        indexes = self.tree_view.selectionModel().selectedRows()
        if indexes:
            source_idx = self.proxy_model.mapToSource(indexes[0])
            self.tree_model.toggle_queue_state(source_idx)

    def _on_inspector_queue_action(self, package: object):
        if not isinstance(package, PackageInfo):
            return
        environment = _window_host_environment(self)
        if not environment.supports_dnf5_daemon_mutations:
            if hasattr(self, "status_bar"):
                _title, detail = environment.dnf5_daemon_unavailable(
                    tr("stage package changes")
                )
                self.status_bar.showMessage(detail, 6000)
            return
        item = self.tree_model.item_for_package(package)
        if item:
            idx = self.tree_model.createIndex(item.row(), 0, item)
            self.tree_model.toggle_queue_state(idx)
            if isinstance(item.payload, PackageInfo):
                self._set_inspector_package_info(item.payload)

    def _on_inspector_reinstall_requested(self, package: object):
        if not isinstance(package, PackageInfo):
            return
        environment = _window_host_environment(self)
        if not environment.supports_dnf5_daemon_mutations:
            if hasattr(self, "status_bar"):
                _title, detail = environment.dnf5_daemon_unavailable(
                    tr("reinstall packages")
                )
                self.status_bar.showMessage(detail, 6000)
            return
        item = self.tree_model.item_for_package(package)
        if item and isinstance(item.payload, PackageInfo):
            idx = self.tree_model.createIndex(item.row(), 0, item)
            if item.payload.state == PackageState.QUEUED_REINSTALL:
                self.tree_model.set_package_queue_state(idx, PackageState.INSTALLED)
            else:
                self.tree_model.set_package_queue_state(
                    idx, PackageState.QUEUED_REINSTALL
                )
            self._refresh_active_inspector_status()

    def _on_discard_all_clicked(self):
        """1-click cancellation of all queued actions across the session."""
        specs = self.tree_model.get_queued_transaction_specs()
        total = specs.total_count
        if total == 0:
            return

        reply = QMessageBox.question(
            self,
            tr("Clear Transaction Queue"),
            tr_plural(
                "Remove {count} queued package action from the transaction queue?",
                "Remove all {count} queued package actions from the transaction queue?",
                total,
            ).format(count=format_number(total)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.tree_model.clear_all_queued()

    def _sync_queue_states(self):
        specs = self.tree_model.get_queued_transaction_specs()
        total_queued = specs.total_count

        self.header.update_queue_badge(total_queued)
        current_counts = {
            "queued": total_queued,
            "orphans": len(self._orphan_cache),
        }
        self.sidebar.update_category_counts(current_counts)
        _refresh_window_empty_state(self)

        # Keep inspector panel action buttons synchronized without discarding loaded tabs
        self._refresh_active_inspector_status()

    def _on_tree_context_menu(self, position: QPoint):
        proxy_index = self.tree_view.indexAt(position)
        if not proxy_index.isValid():
            return

        source_index = self.proxy_model.mapToSource(proxy_index)
        if not source_index.isValid():
            return

        item: TreeItem = source_index.internalPointer()
        if item is None:
            return

        menu = QMenu(self)

        environment = _window_host_environment(self)
        allows_mutations = environment.supports_dnf5_daemon_mutations

        if not item.is_dependency:
            state = item.state
            has_up = isinstance(item.payload, PackageInfo) and item.payload.has_update
            if allows_mutations and state in (
                PackageState.INSTALLED,
                PackageState.AVAILABLE,
            ):
                if state == PackageState.INSTALLED:
                    if has_up:
                        up_ver = item.payload.available_update_version
                        up_text = (
                            tr("Queue Upgrade (-> {version})").format(version=up_ver)
                            if up_ver
                            else tr("Queue Upgrade")
                        )
                        up_act = QAction(up_text, self)
                        up_act.setIcon(QIcon.fromTheme("system-software-update"))
                        up_act.triggered.connect(
                            lambda: self.tree_model.toggle_queue_state(source_index)
                        )
                        menu.addAction(up_act)

                    reinstall_act = QAction(tr("Queue Reinstall"), self)
                    reinstall_act.setIcon(theme_icon("view-refresh", "system-run"))
                    reinstall_act.triggered.connect(
                        lambda: self._queue_package_reinstall(source_index)
                    )
                    menu.addAction(reinstall_act)

                    downgrade_act = QAction(tr("Downgrade…"), self)
                    downgrade_act.setIcon(theme_icon("go-down", "view-refresh"))
                    downgrade_act.triggered.connect(
                        lambda: self._open_downgrade_dialog(source_index)
                    )
                    menu.addAction(downgrade_act)

                    sync_act = QAction(tr("Sync to Repo Version"), self)
                    sync_act.setIcon(theme_icon("emblem-synchronizing", "view-refresh"))
                    sync_act.triggered.connect(
                        lambda: self._sync_package_to_repo_version(source_index)
                    )
                    menu.addAction(sync_act)

                    rm_act = QAction(tr("Queue Removal"), self)
                    rm_act.setIcon(QIcon.fromTheme("list-remove"))
                    rm_act.triggered.connect(
                        lambda: self._force_queue_removal(source_index)
                    )
                    menu.addAction(rm_act)
                else:
                    in_act = QAction(tr("Queue Installation"), self)
                    in_act.setIcon(QIcon.fromTheme("list-add"))
                    in_act.triggered.connect(
                        lambda: self.tree_model.toggle_queue_state(source_index)
                    )
                    menu.addAction(in_act)
                menu.addSeparator()
            elif allows_mutations and (
                state == PackageState.QUEUED_INSTALL or state in QUEUED_INSTALLED_STATES
            ):
                cancel_act = QAction(tr("Remove from Queue"), self)
                cancel_act.setIcon(QIcon.fromTheme("edit-undo"))
                cancel_act.triggered.connect(
                    lambda: self.tree_model.toggle_queue_state(source_index)
                )
                menu.addAction(cancel_act)
                menu.addSeparator()

            if state == PackageState.INSTALLED or state in QUEUED_INSTALLED_STATES:
                rev_deps_act = QAction(
                    tr("Show Reverse Dependents (What Requires This)"), self
                )
                rev_deps_act.setIcon(theme_icon("system-search", "edit-find"))
                rev_deps_act.triggered.connect(
                    lambda package=item.payload: self._on_fetch_reverse_deps_requested(
                        package
                    )
                )
                menu.addAction(rev_deps_act)

                verify_label = (
                    tr("Verify File Integrity (rpm -V --nomtime)")
                    if environment.normalizes_rpm_mtimes
                    else tr("Verify File Integrity (rpm -V)")
                )
                verify_act = QAction(verify_label, self)
                verify_act.setIcon(theme_icon("security-high", "system-run"))
                verify_act.triggered.connect(
                    lambda package=item.payload: (
                        self._on_verify_package_files_requested(package)
                    )
                )
                menu.addAction(verify_act)

            copy_chain_act = QAction(tr("Copy Dependency Chain"), self)
            copy_chain_act.setIcon(theme_icon("view-list-tree", "edit-copy"))
            copy_chain_act.triggered.connect(
                lambda: self._copy_dependency_chain(source_index)
            )
            menu.addAction(copy_chain_act)

            menu.addSeparator()

        copy_name_act = QAction(tr("Copy Package Name"), self)
        copy_name_act.setIcon(QIcon.fromTheme("edit-copy"))
        copy_name_act.triggered.connect(lambda: self._copy_to_clipboard(item.name))
        menu.addAction(copy_name_act)

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def _queue_package_reinstall(self, source_index: QModelIndex) -> None:
        environment = _window_host_environment(self)
        if not environment.supports_dnf5_daemon_mutations:
            _title, detail = environment.dnf5_daemon_unavailable(
                tr("reinstall packages")
            )
            self.status_bar.showMessage(detail, 6000)
            return
        if self.tree_model.set_package_queue_state(
            source_index, PackageState.QUEUED_REINSTALL
        ):
            item: TreeItem | None = source_index.internalPointer()
            if item and isinstance(item.payload, PackageInfo):
                self.status_bar.showMessage(
                    tr("Queued {name} for reinstall.").format(name=item.payload.name),
                    4000,
                )

    def _open_downgrade_dialog(self, source_index: QModelIndex) -> None:
        environment = _window_host_environment(self)
        if not environment.supports_dnf5_daemon_mutations:
            _title, detail = environment.dnf5_daemon_unavailable(
                tr("downgrade packages")
            )
            self.status_bar.showMessage(detail, 6000)
            return
        if not environment.supports_dnf5_daemon_queries:
            self.status_bar.showMessage(
                environment.feature_unavailable(tr("Repository version inspection")),
                6000,
            )
            return
        item: TreeItem | None = source_index.internalPointer()
        if item is None or not isinstance(item.payload, PackageInfo):
            return
        package = item.payload

        prior_worker = getattr(self, "current_package_versions_worker", None)
        if prior_worker is not None:
            prior_worker.cancel()
        self._package_versions_request_serial += 1
        request_serial = self._package_versions_request_serial

        dialog_cls = self._resolve_window_hook(
            "DowngradePackageDialog", DowngradePackageDialog
        )
        dialog = dialog_cls(package, self)
        worker_cls = self._resolve_window_hook(
            "PackageVersionsQueryWorker", PackageVersionsQueryWorker
        )
        worker = worker_cls(
            package.name,
            arch=package.arch,
            installed_evr=package.evr_tuple,
        )
        self.current_package_versions_worker = worker

        def _on_loaded(pkg_name: str, versions: object) -> None:
            if (
                request_serial != self._package_versions_request_serial
                or pkg_name != package.name
                or not isinstance(versions, PackageRepoVersions)
            ):
                return
            dialog.set_versions(versions)

        def _on_failed(pkg_name: str, detail: str) -> None:
            if (
                request_serial != self._package_versions_request_serial
                or pkg_name != package.name
            ):
                return
            dialog.set_error(detail)

        worker.signals.loaded.connect(_on_loaded)
        worker.signals.failed.connect(_on_failed)
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if request_serial == self._package_versions_request_serial:
                self._package_versions_request_serial += 1
            if self.current_package_versions_worker is worker:
                self.current_package_versions_worker = None
            dialog.set_error(f"{type(error).__name__}: {error}")

        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if request_serial == self._package_versions_request_serial:
            self._package_versions_request_serial += 1
        worker.cancel()
        if self.current_package_versions_worker is worker:
            self.current_package_versions_worker = None

        if not accepted:
            return
        chosen = dialog.selected_record()
        if chosen is None:
            return
        fresh_item = self.tree_model.item_for_package(package)
        if fresh_item is None:
            return
        target_index = self.tree_model.createIndex(fresh_item.row(), 0, fresh_item)
        if self.tree_model.set_package_queue_state(
            target_index,
            PackageState.QUEUED_DOWNGRADE,
            target_spec=chosen.full_nevra,
            target_version=chosen.evr,
        ):
            self.status_bar.showMessage(
                tr("Queued downgrade of {name} to {version}.").format(
                    name=package.name, version=chosen.evr
                ),
                4000,
            )

    def _sync_package_to_repo_version(self, source_index: QModelIndex) -> None:
        environment = _window_host_environment(self)
        if not environment.supports_dnf5_daemon_mutations:
            _title, detail = environment.dnf5_daemon_unavailable(
                tr("sync packages to repository versions")
            )
            self.status_bar.showMessage(detail, 6000)
            return
        if not environment.supports_dnf5_daemon_queries:
            self.status_bar.showMessage(
                environment.feature_unavailable(tr("Repository version inspection")),
                6000,
            )
            return
        item: TreeItem | None = source_index.internalPointer()
        if item is None or not isinstance(item.payload, PackageInfo):
            return
        package = item.payload

        prior_worker = getattr(self, "current_package_versions_worker", None)
        if prior_worker is not None:
            prior_worker.cancel()
        self._package_versions_request_serial += 1
        request_serial = self._package_versions_request_serial

        worker_cls = self._resolve_window_hook(
            "PackageVersionsQueryWorker", PackageVersionsQueryWorker
        )
        worker = worker_cls(
            package.name,
            arch=package.arch,
            installed_evr=package.evr_tuple,
        )
        self.current_package_versions_worker = worker
        self.status_bar.showMessage(
            tr("Checking repository versions for {name}…").format(name=package.name)
        )

        def _on_loaded(pkg_name: str, versions: object) -> None:
            if (
                request_serial != self._package_versions_request_serial
                or pkg_name != package.name
                or not isinstance(versions, PackageRepoVersions)
            ):
                return
            if self.current_package_versions_worker is worker:
                self.current_package_versions_worker = None
            if versions.sync_target is None:
                self.status_bar.showMessage(
                    tr("No build of {name} was found in enabled repositories.").format(
                        name=package.name
                    ),
                    5000,
                )
                return
            if not versions.sync_needed:
                self.status_bar.showMessage(
                    tr(
                        "{name} ({version}) already matches the repository version."
                    ).format(name=package.name, version=package.full_version),
                    5000,
                )
                return
            fresh_item = self.tree_model.item_for_package(package)
            if fresh_item is None:
                return
            target_index = self.tree_model.createIndex(fresh_item.row(), 0, fresh_item)
            if self.tree_model.set_package_queue_state(
                target_index,
                PackageState.QUEUED_DISTRO_SYNC,
                target_spec=package.name_arch,
                target_version=versions.sync_target.evr,
            ):
                self.status_bar.showMessage(
                    tr("Queued {name} to sync to {version}.").format(
                        name=package.name, version=versions.sync_target.evr
                    ),
                    4000,
                )

        def _on_failed(pkg_name: str, detail: str) -> None:
            if (
                request_serial != self._package_versions_request_serial
                or pkg_name != package.name
            ):
                return
            if self.current_package_versions_worker is worker:
                self.current_package_versions_worker = None
            self.status_bar.showMessage(detail, 6000)

        worker.signals.loaded.connect(_on_loaded)
        worker.signals.failed.connect(_on_failed)
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if request_serial == self._package_versions_request_serial:
                self._package_versions_request_serial += 1
            if self.current_package_versions_worker is worker:
                self.current_package_versions_worker = None
            self.status_bar.showMessage(f"{type(error).__name__}: {error}", 6000)

    @staticmethod
    def _resolve_window_hook(name: str, default):
        import sys

        module = sys.modules.get(__name__)
        return getattr(module, name, default) if module is not None else default

    def _force_queue_removal(self, source_index: QModelIndex):
        if not _window_host_environment(self).supports_dnf5_daemon_mutations:
            return
        item: TreeItem | None = source_index.internalPointer()
        if item and isinstance(item.payload, PackageInfo):
            item.payload.state = PackageState.QUEUED_REMOVE
            item.payload.queued_target_spec = ""
            item.payload.queued_target_version = ""
            left_idx = self.tree_model.index(
                source_index.row(), 0, source_index.parent()
            )
            right_idx = self.tree_model.index(
                source_index.row(),
                self.tree_model.columnCount() - 1,
                source_index.parent(),
            )
            self.tree_model.dataChanged.emit(left_idx, right_idx)
            self.tree_model.queue_state_changed.emit()

    def _copy_dependency_chain(self, source_index: QModelIndex):
        """Formats the dependency tree of the selected item into plain indented text."""
        item: TreeItem | None = source_index.internalPointer()
        if not item:
            return

        lines: list[str] = [f"{item.name} {item.version}"]
        for child in item.child_items:
            req_note = (
                " ("
                + tr("requires {requirement}").format(
                    requirement=child.payload.raw_requirement
                )
                + ")"
                if isinstance(child.payload, DependencyNode)
                else ""
            )
            lines.append(f"  └── {child.name} [{child.version}]{req_note}")

        self._copy_to_clipboard("\n".join(lines))

    def _show_clipboard_status(self, text: str) -> None:
        self.status_bar.showMessage(
            tr("Copied '{text}' to clipboard.").format(text=text), 2500
        )

    def _copy_to_clipboard(self, text: str):
        clipboard: QClipboard | None = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(text)
            self._show_clipboard_status(text)

    # -------------------------------------------------------------------------
    # DNF History & Dry-Run Simulation
    # -------------------------------------------------------------------------
    def _continue_close_if_requested(self):
        if (
            self._close_after_transaction
            and not self.transaction_controller.is_busy
            and not self.transaction_controller.has_ready_plan
            and not self.repository_addition_runner.is_running
            and not self.repository_deletion_runner.is_running
        ):
            self._close_after_transaction = False
            QTimer.singleShot(0, self.close)

    def _update_responsive_panes(self) -> None:
        """Auto-collapse the sidebar on narrow tiled widths when inspector is open."""
        if not self.isVisible():
            return
        inspector_visible = self.inspector_panel.isVisible()
        three_pane_floor = (
            self.sidebar.minimumWidth() + 140 + self._inspector_dock_preferred_width()
        )
        if self.width() >= three_pane_floor:
            self._manual_sidebar_override = False
            if self._sidebar_auto_collapsed:
                self._sidebar_auto_collapsed = False
                saved_state = self._pre_collapse_splitter_state
                self._pre_collapse_splitter_state = None
                self.sidebar.show()
                if saved_state is not None:
                    self.main_splitter.restoreState(saved_state)
                    self.inspector_panel.setVisible(inspector_visible)
            return

        if inspector_visible and not self._manual_sidebar_override:
            if self.sidebar.isVisible():
                self._pre_collapse_splitter_state = self.main_splitter.saveState()
                self._sidebar_auto_collapsed = True
                self.sidebar.hide()
        elif not inspector_visible and self._sidebar_auto_collapsed:
            self._sidebar_auto_collapsed = False
            saved_state = self._pre_collapse_splitter_state
            self._pre_collapse_splitter_state = None
            self.sidebar.show()
            if saved_state is not None:
                self.main_splitter.restoreState(saved_state)
                self.inspector_panel.setVisible(False)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_responsive_panes()
        if os.environ.get("DENDRO_DEBUG"):
            state = self.windowState()
            state_label = getattr(state, "name", None) or str(state)
            _debug_log(
                f"Window resize: size={event.size().width()}x{event.size().height()} "
                f"min={self.minimumWidth()}x{self.minimumHeight()} "
                f"state={state_label}"
            )

    def retranslate(self) -> None:
        """Refresh window-owned text and status columns on live locale switches."""
        self._refresh_window_title()
        if hasattr(self, "status_bar"):
            self.status_bar.setAccessibleName(tr("Status bar"))
        if hasattr(self, "inspector_panel"):
            self.inspector_panel.setAccessibleName(tr("Package details dock"))
        if hasattr(self, "tree_model"):
            self.tree_model.retranslate()
        if hasattr(self, "tree_view") and hasattr(self, "tree_delegate"):
            min_status = self._status_column_minimum_width()
            if (
                not self.tree_view.header().isSectionHidden(
                    DependencyTreeModel.COL_STATUS
                )
                and self.tree_view.columnWidth(DependencyTreeModel.COL_STATUS)
                < min_status
            ):
                self.tree_view.setColumnWidth(
                    DependencyTreeModel.COL_STATUS, min_status
                )
        if hasattr(self, "tray_icon"):
            self.tray_icon.setToolTip(tr("Dendro Package Manager"))
        if getattr(self, "_tray_action_open", None) is not None:
            self._tray_action_open.setText(tr("Open Dendro"))
        if getattr(self, "_tray_action_upgrade", None) is not None:
            self._tray_action_upgrade.setText(tr("Check for Updates"))
        if getattr(self, "_tray_action_flatpak", None) is not None:
            self._tray_action_flatpak.setText(tr("Manage Flatpaks"))
        if getattr(self, "_tray_action_quit", None) is not None:
            self._tray_action_quit.setText(tr("Quit"))
        _refresh_window_empty_state(self)
        self._refresh_active_inspector_status()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            apply_labelled_fonts(self)
            if hasattr(self, "tree_view") and hasattr(self, "tree_delegate"):
                min_status = self._status_column_minimum_width()
                if (
                    not self.tree_view.header().isSectionHidden(
                        DependencyTreeModel.COL_STATUS
                    )
                    and self.tree_view.columnWidth(DependencyTreeModel.COL_STATUS)
                    < min_status
                ):
                    self.tree_view.setColumnWidth(
                        DependencyTreeModel.COL_STATUS, min_status
                    )
            self._update_responsive_panes()
            return
        if event.type() == QEvent.Type.WindowStateChange and os.environ.get(
            "DENDRO_DEBUG"
        ):
            old_state = event.oldState()
            new_state = self.windowState()
            old_label = getattr(old_state, "name", None) or str(old_state)
            new_label = getattr(new_state, "name", None) or str(new_state)
            _debug_log(
                f"Window state changed: {old_label} -> {new_label} "
                f"size={self.width()}x{self.height()}"
            )

    def showEvent(self, event) -> None:
        """Fit the window to this screen before the first paint.

        A geometry saved on a larger display, at a larger text scale, or under a
        desktop with a shorter panel is restored as-is: ``restoreGeometry`` only
        rescues a window that is entirely off-screen or has lost its title bar.
        The window then opens taller than the area the compositor reserved, and
        the bottom of it -- the status bar and the last row of the inspector --
        is drawn past the edge. Fitting here, once per show, corrects that
        without ever moving a window the user placed.

        Only the first show is fitted. Later ones are the user restoring a
        minimized window or leaving full screen, where a fresh correction could
        undo a deliberate resize.
        """
        super().showEvent(event)
        self._update_responsive_panes()
        if getattr(self, "_geometry_fitted", False):
            return
        self._geometry_fitted = True
        # ``showEvent`` arrives before the frame exists, so the correction is
        # deferred one turn of the event loop, when the compositor has told Qt
        # how much chrome it is drawing around the client area.
        QTimer.singleShot(0, self._fit_to_screen)

    def _fit_to_screen(self) -> None:
        """Apply :func:`fit_to_available_geometry` to this window."""
        fit_to_available_geometry(self)
        self._update_responsive_panes()
        if os.environ.get("DENDRO_DEBUG"):
            _debug_log(
                f"Window fitted: size={self.width()}x{self.height()} "
                f"min={self.minimumWidth()}x{self.minimumHeight()}"
            )

    def closeEvent(self, event: QCloseEvent):
        if (
            self.transaction_controller.is_busy
            or self.transaction_controller.has_ready_plan
            or self.repository_addition_runner.is_running
            or self.repository_deletion_runner.is_running
        ):
            if not self._close_after_transaction:
                reply = QMessageBox.question(
                    self,
                    tr("Operation Still Active"),
                    tr(
                        "A package or repository operation is still active. Dendro will request safe cancellation "
                        "and close only after a terminal result. Continue?"
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self._close_after_transaction = True
                self._on_drawer_cancel()
            event.ignore()
            return

        # Pending inspector workers have plain Python callback closures. Invalidate
        # their callbacks before Qt tears down the widgets they would otherwise use.
        self._inspector_generation += 1
        if getattr(self, "_sidebar_auto_collapsed", False):
            self._sidebar_auto_collapsed = False
            saved_state = getattr(self, "_pre_collapse_splitter_state", None)
            self._pre_collapse_splitter_state = None
            inspector_was_visible = self.inspector_panel.isVisible()
            self.sidebar.show()
            if saved_state is not None:
                self.main_splitter.restoreState(saved_state)
                self.inspector_panel.setVisible(inspector_was_visible)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("main_splitter_state", self.main_splitter.saveState())
        self.settings.setValue(
            "workspace_splitter_state", self.workspace_splitter.saveState()
        )
        self.settings.setValue("tree_header_state", self.tree_view.header().saveState())
        inspector = getattr(self, "inspector_panel", None)
        if inspector is not None:
            self.settings.setValue(
                "inspector/metadata_expanded",
                inspector.is_metadata_expanded(),
            )
        if self.current_query_worker:
            self.current_query_worker.cancel()
        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()
        if self.current_userinstalled_worker:
            self.current_userinstalled_worker.cancel()
        if self.current_updates_worker:
            self.current_updates_worker.cancel()
        offline_worker = getattr(self, "current_offline_status_worker", None)
        if offline_worker:
            offline_worker.cancel()
        rpmostree_worker = getattr(self, "current_rpmostree_worker", None)
        if rpmostree_worker:
            rpmostree_worker.cancel()
        if self.current_remote_search_worker:
            self.current_remote_search_worker.cancel()
        if self.current_file_owner_worker:
            self.current_file_owner_worker.cancel()
        if self.current_local_rpm_inspection_worker:
            self.current_local_rpm_inspection_worker.cancel()
        versions_worker = getattr(self, "current_package_versions_worker", None)
        if versions_worker:
            versions_worker.cancel()
        cancel_gsettings_fallback = getattr(
            self, "_cancel_async_gsettings_fallback", None
        )
        if callable(cancel_gsettings_fallback):
            cancel_gsettings_fallback()
        # Do not block the GUI thread with a guessed shutdown timeout.  Running
        # read-only workers own their resources and Qt disconnects their signals
        # as this window is destroyed.
        self.thread_pool.clear()
        dnf_client = getattr(self, "dnf_client", None)
        if dnf_client is not None:
            close_fn = getattr(dnf_client, "close", None)
            if callable(close_fn):
                close_fn()
        SQLiteCapabilityCache.close_instance()
        event.accept()


__all__ = (
    "DependencyTreeWorker",
    "DnfHistoryInfoWorker",
    "DnfHistoryWorker",
    "DowngradePackageDialog",
    "FlatpakInventoryDialog",
    "HostModeBanner",
    "LocalRpmInspectionWorker",
    "LocalRpmInstallDialog",
    "MainWindow",
    "OfflineStatusQueryWorker",
    "OfflineUpdateBanner",
    "PackageChangelogWorker",
    "PackageFilesWorker",
    "PackageScriptletsWorker",
    "PackageVerifyWorker",
    "PackageVersionsQueryWorker",
    "RecentChangesWorker",
    "RepoManagerDialog",
    "RepositoryKeyImportDialog",
    "RepositoryListWorker",
    "ReverseDependencyWorker",
    "SQLiteCapabilityCache",
    "SecurityAdvisoryDialog",
    "SecurityAdvisoryWorker",
    "SystemRebootWorker",
    "TransactionPlanReviewDialog",
    "_field_name",
    "is_running_in_flatpak",
    "start_flatpak_refresh",
)
