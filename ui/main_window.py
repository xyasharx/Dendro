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
    QFrame,
    QHeaderView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from core.backend import (
    AvailableUpdateInfo,
    DependencyNode,
    DependencyTreeWorker,
    DnfHistoryWorker,
    FileOwnerLookupWorker,
    LocalRpmInspectionWorker,
    OrphanQueryWorker,
    PackageChangelogWorker,
    PackageFilesWorker,
    PackageInfo,
    PackageQueryWorker,
    PackageScriptletsWorker,
    PackageState,
    PackageVerifyWorker,
    RemotePackageSearchWorker,
    ReverseDependencyWorker,
    SecurityAdvisoryWorker,
    SQLiteCapabilityCache,
    SystemUpdatesCheckWorker,
    UserInstalledQueryWorker,
    get_system_protected_packages,
    is_running_in_flatpak,
)
from core.dnf5daemon import (
    Dnf5DaemonClient,
    RepositoryListWorker,
)
from core.host_environment import HostEnvironment, detect_host_environment
from core.i18n import resolve_locale, tr
from core.models import (
    COLUMN_HEADERS,
    STATUS_EXTRA_LABELS,
    STATUS_LABELS,
    DependencyTreeModel,
    PackageFilterProxyModel,
    TreeItem,
)
from core.operation_gate import HostOperationGate
from core.repository_addition import RepositoryAdditionRunner
from core.repository_deletion import RepositoryDeletionRunner
from core.rpmostree_status import (
    RpmOstreeHostStatus,
    RpmOstreeOriginKind,
    RpmOstreeStatusWorker,
)
from core.settings import get_app_settings
from core.taxonomy import (
    matching_sidebar_categories,
    sidebar_category_ids,
)
from core.transaction_controller import DnfTransactionController
from ui.advisory_dialog import SecurityAdvisoryDialog
from ui.delegates import DendroTreeView, PackageTreeItemDelegate
from ui.flatpak_dialog import FlatpakInventoryDialog, start_flatpak_refresh
from ui.fonts import apply_labelled_fonts
from ui.header import HeaderBar
from ui.host_dialogs import HostModeBanner, LocalRpmInstallDialog, _field_name
from ui.i18n import install as install_locale
from ui.icons import theme_icon
from ui.inspector_panel import PackageInspectorPanel
from ui.key_import_dialog import RepositoryKeyImportDialog
from ui.main_window_inspector import MainWindowInspectorMixin
from ui.main_window_transactions import MainWindowTransactionsMixin
from ui.repo_dialog import RepoManagerDialog
from ui.sidebar import CategorySidebar
from ui.styles import _debug_log
from ui.theme_coordinator import ThemeCoordinator
from ui.transaction_drawer import TransactionDrawer
from ui.transaction_plan_dialog import TransactionPlanReviewDialog
from ui.window_placement import (
    fit_size_to_available_geometry,
    fit_to_available_geometry,
)
from ui.window_presentation import (
    refresh_window_empty_state as _refresh_window_empty_state,
)
from ui.window_presentation import (
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
        self.current_rpmostree_worker: RpmOstreeStatusWorker | None = None
        self._rpmostree_status: RpmOstreeHostStatus | None = None
        self.current_remote_search_worker: RemotePackageSearchWorker | None = None
        self.current_file_owner_worker: FileOwnerLookupWorker | None = None
        self._file_owner_request_serial = 0
        self.current_local_rpm_inspection_worker: LocalRpmInspectionWorker | None = None
        self._local_rpm_inspection_request_serial = 0
        self._package_tree_generation = 0
        self._inspector_generation = 0
        self._inspector_request_serials: dict[str, int] = {}
        self._update_request_serial = 0
        self.current_repository_worker: RepositoryListWorker | None = None
        self._repository_refresh_after_current_worker = False
        self._busy_operations: set[str] = set()
        self._owns_wait_cursor = False
        self._active_repo_dialog: RepoManagerDialog | None = None
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
        self.tree_view.setRootIsDecorated(True)
        self.tree_view.setIndentation(22)
        self.tree_view.setAnimated(True)
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

        # Right-side Inspector Panel inside a horizontal-capable host so
        # MainWindow's minimum width stays below half-screen tile bounds while
        # PackageInspectorPanel keeps its tab-bar minimum width intact.
        self.inspector_panel = PackageInspectorPanel()
        self.inspector_panel.set_host_environment(self.host_environment)
        self._inspector_host = QScrollArea()
        self._inspector_host.setObjectName("InspectorHostScroll")
        self._inspector_host.setFrameShape(QFrame.Shape.NoFrame)
        self._inspector_host.setWidgetResizable(True)
        self._inspector_host.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._inspector_host.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._inspector_host.setMinimumWidth(240)
        self._inspector_host.setAutoFillBackground(False)
        self._inspector_host.viewport().setAutoFillBackground(False)
        self._inspector_host.setWidget(self.inspector_panel)
        self.inspector_panel.setAutoFillBackground(False)
        self.main_splitter.addWidget(self._inspector_host)
        self.main_splitter.setCollapsible(1, False)
        self.workspace_splitter.setCollapsible(0, False)

        # Restore user custom splitter layouts or apply balanced defaults. The
        # inspector dock width is not a fixed preference: it is floored by the
        # width its own tab labels need at the current desktop font, so ask the
        # panel instead of hard-coding a number that can be too small.
        inspector_default = max(340, self.inspector_panel.minimumWidth())

        def balanced_sizes() -> list[int]:
            return [260, 680, inspector_default]

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
            self.transaction_drawer.append_log
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

        self.repository_deletion_runner = RepositoryDeletionRunner(
            environment_detector=lambda: _window_host_environment(self),
            host_operation_gate=self.host_operation_gate,
            parent=self,
        )
        self.repository_deletion_runner.log_received.connect(
            self.transaction_drawer.append_log
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
            self.transaction_drawer.append_log
        )
        self.repository_addition_runner.finished.connect(
            self._on_repository_addition_finished
        )

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(tr("Ready."))

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
            max(delegate.fm_badge.horizontalAdvance(label.upper()) for label in labels)
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
        QShortcut(
            QKeySequence("Space"), self, activated=self._toggle_queue_selected_row
        )
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

        # 5. Transaction Drawer
        self.transaction_drawer.closed.connect(self._close_transaction_drawer)
        self.transaction_drawer.cancel_requested.connect(self._on_drawer_cancel)
        self.transaction_drawer.commit_requested.connect(self._on_drawer_commit)

    # -------------------------------------------------------------------------
    # System Tray & Background Notification Engine
    # -------------------------------------------------------------------------
    def _init_system_tray(self):
        """Initializes system tray icon and 4-hour background update polling."""
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
        act_open = tray_menu.addAction(tr("Open Dendro"))
        act_open.triggered.connect(self._restore_from_tray)

        if self.host_environment.supports_dnf5_daemon_queries:
            act_upgrade = tray_menu.addAction(tr("Check for Updates"))
            act_upgrade.triggered.connect(self._trigger_background_update_check)
        elif (
            self.host_environment.is_immutable_host
            and self.host_environment.supports_flatpak_management
        ):
            act_flatpak = tray_menu.addAction(tr("Manage Flatpaks"))
            act_flatpak.triggered.connect(self._open_flatpak_dialog)

        tray_menu.addSeparator()
        act_quit = tray_menu.addAction(tr("Quit"))
        act_quit.triggered.connect(QApplication.instance().quit)

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
                    "Could not start the installed-package scan: "
                    f"{type(error).__name__}: {error}",
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
                        "Could not start user-installed package detection: "
                        f"{type(error).__name__}: {error}",
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
                        "Could not start unneeded-package detection: "
                        f"{type(error).__name__}: {error}",
                    )

        if environment.supports_dnf5_daemon_queries:
            self._start_update_check(force_refresh=False, notify=False)

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
            " (staged OS update pending reboot)"
            if status.has_pending_deployment
            else ""
        )
        self.status_bar.showMessage(
            f"Fedora Atomic deployment inspected: {layered_count:,} layered/overridden "
            f"RPM{'s' if layered_count != 1 else ''} detected{pending_note}.",
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

        self.status_bar.showMessage(f"Loaded {len(packages):,} packages successfully.")
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
                    f"Update check could not start: {type(error).__name__}: {error}",
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
        self.status_bar.showMessage(f"Update check failed: {detail}", 8000)

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
                else:
                    package.has_update = False
                    package.available_update_version = ""
                    package.available_update_repo = ""
            self._update_sidebar_counts(self._all_packages_cache)

        count = len(updates_map)
        self.sidebar.update_category_counts({"updates_available": count})
        self.header.update_available_updates_badge(count)
        self.proxy_model.refresh_filter()
        _refresh_window_empty_state(self)
        self._refresh_active_inspector_status()
        if count > 0:
            self.status_bar.showMessage(
                f"{count} software updates are available for your system."
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
                f"{count} package updates are available for your Fedora system.",
                QSystemTrayIcon.MessageIcon.Information,
                6000,
            )

    def _on_sidebar_category_selected(self, category: str):
        self.proxy_model.set_category_filter(category)
        is_orphans = category == "orphans"
        is_updates = category == "updates_available"

        # Show contextual actions strictly in their relevant views
        self.header.set_orphan_clean_visible(is_orphans, len(self._orphan_cache))
        self.header.set_updates_view_active(is_updates, len(self._pending_updates_map))
        _refresh_window_empty_state(self)

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

    def _show_status_for_current_worker(
        self, worker_attribute: str, worker: object, message: str
    ) -> None:
        """Do not let a cancelled predecessor overwrite current UI feedback."""
        if getattr(self, worker_attribute, None) is worker:
            self.status_bar.showMessage(message)

    def _on_query_error(self, pkg_name: str, message: str) -> None:
        """Present an auxiliary-query error without changing unrelated busy state."""
        self.status_bar.showMessage(f"Error: {message}", 8000)
        if pkg_name:
            self.tree_model.reset_loading_state(pkg_name)

    def _toggle_sidebar(self):
        """Toggles sidebar visibility or restores it if collapsed in the splitter."""
        three_pane_floor = (
            self.sidebar.minimumWidth() + 140 + self.inspector_panel.minimumWidth()
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
            inspector_w = max(self.inspector_panel.minimumWidth(), 370)
            inspector_w = min(inspector_w, max(240, available - sidebar_w - 140))
            tree_w = max(140, available - sidebar_w - inspector_w)
            self.main_splitter.setSizes([sidebar_w, tree_w, inspector_w])

    def _toggle_updates_view(self):
        """Allows toggling between Updates view and All Packages directly from the header."""
        if self.proxy_model._category == "updates_available":
            self._reset_to_all_packages()
        else:
            self._filter_to_updates()

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
        _refresh_window_empty_state(self)

        # Only inspect complete, existing system paths; ordinary package search
        # remains entirely local and immediate.
        if not (
            clean_path.startswith("/")
            and len(clean_path) > 1
            and os.path.exists(clean_path)
        ):
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
        self.status_bar.showMessage(f"Looking up the RPM owner of '{clean_path}'…")
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if worker is self.current_file_owner_worker:
                self.current_file_owner_worker = None
                self.status_bar.showMessage(
                    f"Could not start the RPM ownership lookup for '{clean_path}': "
                    f"{type(error).__name__}: {error}",
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
                f"File '{file_path}' is owned by package '{owner.full_nevra}'.", 4000
            )
            self.proxy_model.set_search_query(owner.name)
            _refresh_window_empty_state(self)
            self._navigate_to_package(owner)
        elif owner is not None:
            self.status_bar.showMessage(
                f"Could not interpret the RPM owner returned for '{file_path}'.", 8000
            )
        elif query.strip().lower().startswith("file:"):
            self.status_bar.showMessage(
                f"Dendro could not identify an installed RPM package that owns '{file_path}'.",
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
                f"Could not inspect the RPM owner for '{file_path}': {detail}",
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
        self.status_bar.showMessage(f"Searching remote repositories for '{clean}'...")

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
                "Could not start remote repository search: "
                f"{type(error).__name__}: {error}",
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
                f"No packages matching '{query}' found in enabled repositories.", 4000
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
                header.set_updates_view_active(
                    False, len(getattr(self, "_pending_updates_map", ()))
                )
        self.proxy_model.set_category_filter("all")
        self.proxy_model.set_search_query(query)
        _refresh_window_empty_state(self)
        action_hint = (
            "Select to review and install."
            if _window_host_environment(self).supports_dnf5_daemon_mutations
            else "Select to inspect package metadata."
        )
        self.status_bar.showMessage(
            f"Found {len(packages)} repository packages ({added} new). {action_hint}",
            5000,
        )

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

    def _navigate_to_package(self, package: PackageInfo | str):
        """Jump to an installed package, preserving a concrete RPM identity when known."""
        if isinstance(package, PackageInfo):
            pkg_name = package.name
            source_item = self.tree_model.item_for_package(package)
            display_name = package.full_nevra
        else:
            pkg_name = package
            if not pkg_name:
                return
            source_item = self.tree_model._package_lookup.get(pkg_name)
            display_name = pkg_name
        if not source_item:
            self.status_bar.showMessage(
                f"Package '{display_name}' is not installed locally.", 3500
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
        from ui.settings_dialog import DendroSettingsDialog

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
        status_message = self.status_bar.currentMessage()
        install_locale(resolved.locale)
        self._refresh_window_title()
        if status_message:
            # Re-run the same lookup the setter would have done, so a message
            # that has a translation appears in the new language rather than
            # keeping the text captured before the switch.
            self.status_bar.showMessage(tr(status_message))

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
                "stage package changes"
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
                    "stage package changes"
                )
                self.status_bar.showMessage(detail, 6000)
            return
        item = self.tree_model.item_for_package(package)
        if item:
            idx = self.tree_model.createIndex(item.row(), 0, item)
            self.tree_model.toggle_queue_state(idx)
            if isinstance(item.payload, PackageInfo):
                self._set_inspector_package_info(item.payload)

    def _on_discard_all_clicked(self):
        """1-click cancellation of all queued actions across the session."""
        installs, removals, upgrades = self.tree_model.get_queued_packages()
        total = len(installs) + len(removals) + len(upgrades)
        if total == 0:
            return

        reply = QMessageBox.question(
            self,
            tr("Clear Transaction Queue"),
            f"Remove all {total} queued package actions from the transaction queue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.tree_model.clear_all_queued()

    def _sync_queue_states(self):
        installs, removals, upgrades = self.tree_model.get_queued_packages()
        total_queued = len(installs) + len(removals) + len(upgrades)

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
                            f"Queue Upgrade (-> {up_ver})"
                            if up_ver
                            else "Queue Upgrade"
                        )
                        up_act = QAction(up_text, self)
                        up_act.setIcon(QIcon.fromTheme("system-software-update"))
                        up_act.triggered.connect(
                            lambda: self.tree_model.toggle_queue_state(source_index)
                        )
                        menu.addAction(up_act)

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
            elif allows_mutations and state in (
                PackageState.QUEUED_INSTALL,
                PackageState.QUEUED_REMOVE,
                PackageState.QUEUED_UPGRADE,
            ):
                cancel_act = QAction(tr("Remove from Queue"), self)
                cancel_act.setIcon(QIcon.fromTheme("edit-undo"))
                cancel_act.triggered.connect(
                    lambda: self.tree_model.toggle_queue_state(source_index)
                )
                menu.addAction(cancel_act)
                menu.addSeparator()

            if state in {
                PackageState.INSTALLED,
                PackageState.QUEUED_REMOVE,
                PackageState.QUEUED_UPGRADE,
            }:
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
                    "Verify File Integrity (rpm -V --nomtime)"
                    if environment.normalizes_rpm_mtimes
                    else "Verify File Integrity (rpm -V)"
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

    def _force_queue_removal(self, source_index: QModelIndex):
        if not _window_host_environment(self).supports_dnf5_daemon_mutations:
            return
        item: TreeItem | None = source_index.internalPointer()
        if item and isinstance(item.payload, PackageInfo):
            item.payload.state = PackageState.QUEUED_REMOVE
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
                f" (requires {child.payload.raw_requirement})"
                if isinstance(child.payload, DependencyNode)
                else ""
            )
            lines.append(f"  └── {child.name} [{child.version}]{req_note}")

        self._copy_to_clipboard("\n".join(lines))

    def _copy_to_clipboard(self, text: str):
        clipboard: QClipboard | None = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(text)
            self.status_bar.showMessage(f"Copied '{text}' to clipboard.", 2500)

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
        inspector_pane = getattr(self, "_inspector_host", self.inspector_panel)
        inspector_visible = (
            inspector_pane.isVisible() and self.inspector_panel.isVisible()
        )
        three_pane_floor = (
            self.sidebar.minimumWidth() + 140 + self.inspector_panel.minimumWidth()
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
                    inspector_pane.setVisible(inspector_visible)
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
                inspector_pane.setVisible(False)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_responsive_panes()
        if os.environ.get("DENDRO_DEBUG"):
            _debug_log(
                f"Window resize: size={event.size().width()}x{event.size().height()} "
                f"min={self.minimumWidth()}x{self.minimumHeight()} "
                f"state={self.windowState().name}"
            )

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and os.environ.get(
            "DENDRO_DEBUG"
        ):
            _debug_log(
                f"Window state changed: {event.oldState().name} -> {self.windowState().name} "
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
                    "A package or repository operation is still active. Dendro will request safe cancellation "
                    "and close only after a terminal result. Continue?",
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
            inspector_pane = getattr(self, "_inspector_host", self.inspector_panel)
            inspector_was_visible = inspector_pane.isVisible()
            self.sidebar.show()
            if saved_state is not None:
                self.main_splitter.restoreState(saved_state)
                inspector_pane.setVisible(inspector_was_visible)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("main_splitter_state", self.main_splitter.saveState())
        self.settings.setValue(
            "workspace_splitter_state", self.workspace_splitter.saveState()
        )
        self.settings.setValue("tree_header_state", self.tree_view.header().saveState())
        if self.current_query_worker:
            self.current_query_worker.cancel()
        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()
        if self.current_userinstalled_worker:
            self.current_userinstalled_worker.cancel()
        if self.current_updates_worker:
            self.current_updates_worker.cancel()
        rpmostree_worker = getattr(self, "current_rpmostree_worker", None)
        if rpmostree_worker:
            rpmostree_worker.cancel()
        if self.current_remote_search_worker:
            self.current_remote_search_worker.cancel()
        if self.current_file_owner_worker:
            self.current_file_owner_worker.cancel()
        if self.current_local_rpm_inspection_worker:
            self.current_local_rpm_inspection_worker.cancel()
        # Do not block the GUI thread with a guessed shutdown timeout.  Running
        # read-only workers own their resources and Qt disconnects their signals
        # as this window is destroyed.
        self.thread_pool.clear()
        SQLiteCapabilityCache.close_instance()
        event.accept()


__all__ = (
    "DependencyTreeWorker",
    "DnfHistoryWorker",
    "FlatpakInventoryDialog",
    "HostModeBanner",
    "LocalRpmInspectionWorker",
    "LocalRpmInstallDialog",
    "MainWindow",
    "PackageChangelogWorker",
    "PackageFilesWorker",
    "PackageScriptletsWorker",
    "PackageVerifyWorker",
    "RepoManagerDialog",
    "RepositoryKeyImportDialog",
    "RepositoryListWorker",
    "ReverseDependencyWorker",
    "SQLiteCapabilityCache",
    "SecurityAdvisoryDialog",
    "SecurityAdvisoryWorker",
    "TransactionPlanReviewDialog",
    "_field_name",
    "is_running_in_flatpak",
    "start_flatpak_refresh",
)
