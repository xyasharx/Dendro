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
from typing import Dict, List, Optional, Set
from PyQt6.QtCore import (
    QItemSelection,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QSettings,
    QTimer,
    Qt,
    QThreadPool,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QClipboard,
    QCloseEvent,
    QColor,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.backend import (
    AvailableUpdateInfo,
    DependencyNode,
    DependencyTreeWorker,
    DnfHistoryWorker,
    LocalRpmInspectionResult,
    OrphanQueryWorker,
    PackageChangelogWorker,
    PackageFilesWorker,
    PackageInfo,
    PackageQueryWorker,
    PackageState,
    PackageVerifyWorker,
    ReverseDependencyWorker,
    RemotePackageSearchWorker,
    SystemUpdatesCheckWorker,
    UserInstalledQueryWorker,
    SQLiteCapabilityCache,
    get_system_protected_packages,
    inspect_local_rpm_file,
    is_running_in_flatpak,
)
from core.dnf5daemon import (
    Dnf5DaemonClient,
    Dnf5DaemonError,
    RepositoryListWorker,
    TransactionPlan,
    TransactionRequest,
)
from core.operation_gate import HostOperationGate
from core.repository_addition import RepositoryAdditionRunner
from core.repository_deletion import RepositoryDeletionRunner
from core.transaction_controller import DnfTransactionController
from core.models import (
    DependencyTreeModel,
    PackageFilterProxyModel,
    TreeItem,
)
from ui.delegates import DendroTreeView, PackageTreeItemDelegate
from ui.transaction_plan_dialog import TransactionPlanReviewDialog
from ui.header import HeaderBar
from ui.history_dialog import DnfHistoryDialog
from ui.inspector_panel import PackageInspectorPanel
from ui.repo_dialog import RepoManagerDialog
from ui.sidebar import CategorySidebar
from ui.styles import (
    THEMES_CONFIG,
    get_delegate_palette,
    get_resolved_theme_key,
    get_theme_stylesheet,
)
from ui.transaction_drawer import TransactionDrawer


LOGGER = logging.getLogger(__name__)


# =============================================================================
# Local .rpm File Inspection & Installation Dialog
# =============================================================================

class LocalRpmInstallDialog(QDialog):
    """
    Dedicated dialog for inspecting and installing a local standalone .rpm file.
    Shows package metadata, version comparison if already installed, and dependency requirements.
    """

    install_requested = pyqtSignal(str)

    def __init__(self, result: LocalRpmInspectionResult, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"Install Package: {result.name}")
        self.resize(620, 380)
        self.result = result
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # Header Info Card
        header_card = QFrame()
        header_card.setObjectName("AICard")
        card_layout = QHBoxLayout(header_card)
        card_layout.setContentsMargins(12, 10, 12, 10)

        pkg_icon = QLabel()
        icon = QIcon.fromTheme("package-x-generic") or QIcon.fromTheme("application-x-rpm")
        if not icon.isNull():
            pkg_icon.setPixmap(icon.pixmap(36, 36))
        card_layout.addWidget(pkg_icon)

        title_layout = QVBoxLayout()
        title_lbl = QLabel(f"<b>{self.result.name}</b> {self.result.version}-{self.result.release}")
        title_lbl.setStyleSheet("font-size: 15px; font-weight: bold;")
        summary_lbl = QLabel(self.result.summary or "Local RPM Package")
        summary_lbl.setStyleSheet("color: #a6adc8; font-size: 12px;")
        title_layout.addWidget(title_lbl)
        title_layout.addWidget(summary_lbl)
        card_layout.addLayout(title_layout, stretch=1)

        layout.addWidget(header_card)

        # Status & Comparison Notice
        if self.result.is_already_installed:
            status_box = QLabel(
                f"<b>Status:</b> Already installed (Currently installed: <code>{self.result.installed_version}</code>)."
            )
            status_box.setStyleSheet(
                "background-color: rgba(243, 139, 168, 0.15); border: 1px solid #f38ba8; "
                "color: #f38ba8; border-radius: 6px; padding: 8px;"
            )
        else:
            status_box = QLabel("<b>Status:</b> Not currently installed on this system.")
            status_box.setStyleSheet(
                "background-color: rgba(166, 227, 161, 0.15); border: 1px solid #a6e3a1; "
                "color: #a6e3a1; border-radius: 6px; padding: 8px;"
            )
        layout.addWidget(status_box)

        # Package Details Grid
        grid = QGridLayout()
        grid.setSpacing(6)
        size_mb = self.result.size_bytes / (1024 * 1024)
        grid.addWidget(QLabel("<b>Architecture:</b>"), 0, 0)
        grid.addWidget(QLabel(self.result.arch), 0, 1)
        grid.addWidget(QLabel("<b>Package Size:</b>"), 0, 2)
        grid.addWidget(QLabel(f"{size_mb:.2f} MB"), 0, 3)
        grid.addWidget(QLabel("<b>License:</b>"), 1, 0)
        grid.addWidget(QLabel(self.result.license or "Unknown"), 1, 1)
        grid.addWidget(QLabel("<b>File Path:</b>"), 2, 0)
        path_lbl = QLabel(self.result.file_path)
        path_lbl.setWordWrap(True)
        grid.addWidget(path_lbl, 2, 1, 1, 3)
        layout.addLayout(grid)

        # Requirements preview console
        req_title = QLabel("Package Dependencies (Requirements):")
        req_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(req_title)

        from PyQt6.QtGui import QFontDatabase
        req_box = QTextEdit()
        req_box.setObjectName("LocalRpmReqBox")
        req_box.setReadOnly(True)
        req_box.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        req_box.setPlainText("\n".join(self.result.requires[:40]) or "No explicit dependencies.")
        layout.addWidget(req_box, stretch=1)

        # Action Buttons
        btn_bar = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)

        self.btn_install = QPushButton("Install Local Package")
        self.btn_install.setObjectName("ApplyButton")
        self.btn_install.setIcon(QIcon.fromTheme("system-software-install") or QIcon.fromTheme("dialog-ok-apply"))
        self.btn_install.clicked.connect(self._on_install_clicked)

        btn_bar.addWidget(btn_cancel)
        btn_bar.addStretch(1)
        btn_bar.addWidget(self.btn_install)
        layout.addLayout(btn_bar)

    def _on_install_clicked(self):
        self.install_requested.emit(self.result.file_path)
        self.accept()


# =============================================================================
# Main Application Window
# =============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fedora Package Tree & Dependency Inspector (Dendro)")

        # Persistent user settings
        self.settings = QSettings("FedoraCommunity", "Dendro")
        self.current_theme = self.settings.value("theme", "auto", type=str)
        # Qt 6.8+ can synchronize native window decorations with a custom
        # application theme. Track the override we requested to avoid creating
        # a color-scheme signal loop while the app follows the system theme.
        self._native_color_scheme_override: Optional[Qt.ColorScheme] = None
        self._applying_theme = False
        self._pending_system_theme_key: Optional[str] = None
        self._system_theme_update_queued = False

        # Restore saved window geometry or calculate clean initial dimensions in a single step
        saved_geom = self.settings.value("geometry")
        if saved_geom:
            self.restoreGeometry(saved_geom)
        else:
            screen = QGuiApplication.primaryScreen()
            if screen:
                avail = screen.availableGeometry()
                self.resize(min(1280, int(avail.width() * 0.85)), min(800, int(avail.height() * 0.85)))
            else:
                self.resize(1200, 760)

        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(16)

        # Background worker tracking
        self.current_query_worker: Optional[PackageQueryWorker] = None
        self.current_orphan_worker: Optional[OrphanQueryWorker] = None
        self.current_userinstalled_worker: Optional[UserInstalledQueryWorker] = None
        self.current_updates_worker: Optional[SystemUpdatesCheckWorker] = None
        self.current_remote_search_worker: Optional[RemotePackageSearchWorker] = None
        self._update_request_serial = 0
        self.current_repository_worker: Optional[RepositoryListWorker] = None
        self._repository_refresh_after_current_worker = False
        self._busy_operations: Set[str] = set()
        self._owns_wait_cursor = False
        self._active_repo_dialog: Optional[RepoManagerDialog] = None
        self._close_after_transaction = False
        self._pending_transaction_kind = ""
        self._last_transaction_upgrades: List[str] = []
        self._last_transaction_removals: List[str] = []

        # Result caches to prevent asynchronous race conditions
        self._all_packages_cache: List[PackageInfo] = []
        self._user_installed_cache: Set[str] = set()
        self._orphan_cache: Set[str] = set()
        self._pending_updates_map: Dict[str, AvailableUpdateInfo] = {}

        self._init_ui()
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

        # 1. Header Toolbar
        self.header = HeaderBar()
        root_layout.addWidget(self.header)

        # 2. Main Horizontal Splitter
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(self.main_splitter, stretch=1)

        # Left Sidebar Navigation
        self.sidebar = CategorySidebar()
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

        # Right-side Inspector Panel
        self.inspector_panel = PackageInspectorPanel()
        self.main_splitter.addWidget(self.inspector_panel)

        # Restore user custom splitter layouts or apply balanced defaults
        saved_main_splitter = self.settings.value("main_splitter_state")
        if saved_main_splitter:
            self.main_splitter.restoreState(saved_main_splitter)
        else:
            self.main_splitter.setSizes([260, 680, 340])

        saved_workspace_splitter = self.settings.value("workspace_splitter_state")
        if saved_workspace_splitter:
            self.workspace_splitter.restoreState(saved_workspace_splitter)
        else:
            self.workspace_splitter.setSizes([680, 0])

        # One serialized DNF5-daemon lifecycle.  It is intentionally not a
        # command queue: a resolved session capability is the only thing that
        # can proceed to execution.
        self.dnf_client = Dnf5DaemonClient()
        self.host_operation_gate = HostOperationGate()
        self.transaction_controller = DnfTransactionController(
            self.dnf_client,
            get_system_protected_packages,
            thread_pool=self.thread_pool,
            host_operation_gate=self.host_operation_gate,
            parent=self,
        )
        self.transaction_controller.log_received.connect(self.transaction_drawer.append_log)
        self.transaction_controller.progress_changed.connect(self.transaction_drawer.set_progress)
        self.transaction_controller.plan_ready.connect(self._on_dnf_plan_ready)
        self.transaction_controller.plan_discarded.connect(self._on_dnf_plan_discarded)
        self.transaction_controller.transaction_finished.connect(self._on_transaction_finished)
        self.transaction_controller.maintenance_finished.connect(self._on_maintenance_finished)
        self.transaction_controller.operation_failed.connect(self._on_dnf_operation_failed)

        self.repository_deletion_runner = RepositoryDeletionRunner(
            host_operation_gate=self.host_operation_gate,
            parent=self,
        )
        self.repository_deletion_runner.log_received.connect(self.transaction_drawer.append_log)
        self.repository_deletion_runner.finished.connect(self._on_repository_deletion_finished)
        self.repository_addition_runner = RepositoryAdditionRunner(
            host_operation_gate=self.host_operation_gate,
            thread_pool=self.thread_pool,
            parent=self,
        )
        self.repository_addition_runner.log_received.connect(self.transaction_drawer.append_log)
        self.repository_addition_runner.finished.connect(self._on_repository_addition_finished)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready.")

        self.proxy_model.set_category_filter("user_apps")

    def _open_transaction_drawer(self):
        """Prepares and opens the execution drawer with consistent geometry."""
        self.transaction_drawer.start_execution_mode()
        self.transaction_drawer.show()
        self.workspace_splitter.setSizes([450, 320])

    def _configure_tree_columns(self):
        header = self.tree_view.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(DependencyTreeModel.COL_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_STATUS, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_VERSION, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_SIZE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_SUMMARY, QHeaderView.ResizeMode.Stretch)

        # Responsive baseline widths (~565px total instead of 745px, preventing laptop column push-off)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_NAME, 230)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_STATUS, 110)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_VERSION, 140)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_SIZE, 85)

        # Restore user custom column widths and order from persistent settings if available
        saved_header_state = self.settings.value("tree_header_state")
        if saved_header_state:
            header.restoreState(saved_header_state)

        # Allow user to right-click table header to show/hide specific columns
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_context_menu)

        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self.tree_view.setSortingEnabled(True)
        self.tree_view.sortByColumn(DependencyTreeModel.COL_NAME, Qt.SortOrder.AscendingOrder)

    def _on_header_context_menu(self, pos: QPoint):
        """Header context menu to selectively toggle column visibility on demand."""
        header = self.tree_view.header()
        menu = QMenu(self)
        col_names = ["Package / Capability", "Status", "Version / Upgrade Path", "Size", "Summary"]

        for col_idx, col_name in enumerate(col_names):
            act = QAction(col_name, menu)
            act.setCheckable(True)
            act.setChecked(not header.isSectionHidden(col_idx))
            # Column 0 (Name) must always remain visible
            if col_idx == 0:
                act.setEnabled(False)
            act.toggled.connect(lambda checked, idx=col_idx: header.setSectionHidden(idx, not checked))
            menu.addAction(act)

        menu.exec(header.mapToGlobal(pos))

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: self.header.search_input.setFocus())
        QShortcut(QKeySequence("Ctrl+B"), self, activated=self._toggle_sidebar)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._load_packages)
        QShortcut(QKeySequence("Ctrl+H"), self, activated=self._open_history_dialog)
        QShortcut(QKeySequence("Ctrl+I"), self, activated=self._toggle_inspector_panel)
        QShortcut(QKeySequence("Space"), self, activated=self._toggle_queue_selected_row)
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
        self.header.theme_toggle_clicked.connect(self._on_theme_toggle_clicked)
        self.header.repos_clicked.connect(self._open_repo_dialog)
        self.header.settings_clicked.connect(self._open_settings_dialog)
        self.header.updates_clicked.connect(self._toggle_updates_view)
        self.header.upgrade_system_clicked.connect(self._on_system_upgrade_requested)
        self.header.clean_orphans_clicked.connect(self._on_clean_all_orphans_clicked)
        self.header.discard_btn.clicked.connect(self._on_discard_all_clicked)
        self.header.refresh_updates_clicked.connect(self._on_force_refresh_updates_clicked)

        # 2. Sidebar Navigation
        self.sidebar.category_selected.connect(self._on_sidebar_category_selected)

        # 3. Tree View & Models
        self.tree_model.fetch_dependencies_requested.connect(self._on_fetch_dependencies_requested)
        self.tree_model.queue_state_changed.connect(self._sync_queue_states)
        self.tree_view.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree_view.selectionModel().selectionChanged.connect(self._on_tree_selection_changed)
        self.tree_view.expanded.connect(self._on_tree_item_expanded)
        self.tree_view.doubleClicked.connect(self._on_tree_item_double_clicked)

        # 4. Package Inspector Panel
        self.inspector_panel.closed.connect(lambda: self.inspector_panel.hide())
        self.inspector_panel.package_action_requested.connect(self._on_inspector_queue_action)
        self.inspector_panel.package_navigate_requested.connect(self._navigate_to_package)
        self.inspector_panel.file_inspection_requested.connect(self._on_inspect_files_requested)
        self.inspector_panel.file_verification_requested.connect(self._on_verify_package_files_requested)
        self.inspector_panel.changelog_requested.connect(self._on_fetch_changelog_requested)
        self.inspector_panel.scriptlets_requested.connect(self._on_fetch_scriptlets_requested)
        self.inspector_panel.reverse_deps_requested.connect(self._on_fetch_reverse_deps_requested)

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
                os.path.join(base_dir, "data", "icons", "256x256", "io.github.xyasharx.Dendro.png"),
                os.path.join(base_dir, "data", "icons", "128x128", "io.github.xyasharx.Dendro.png"),
                os.path.join(base_dir, "io.github.xyasharx.Dendro.svg"),
                "/usr/share/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg",
                "/usr/share/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png",
            ]:
                if os.path.isfile(icon_path):
                    app_icon = QIcon(icon_path)
                    break

        self.tray_icon.setIcon(app_icon)
        self.tray_icon.setToolTip("Dendro Package Manager")

        tray_menu = QMenu()
        act_open = tray_menu.addAction("Open Dendro")
        act_open.triggered.connect(self._restore_from_tray)

        act_upgrade = tray_menu.addAction("Check for Updates")
        act_upgrade.triggered.connect(self._trigger_background_update_check)

        tray_menu.addSeparator()
        act_quit = tray_menu.addAction("Quit")
        act_quit.triggered.connect(QApplication.instance().quit)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()


    def _restore_from_tray(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._restore_from_tray()

    def _trigger_background_update_check(self):
        """Run one user-requested, daemon-backed update check from the tray."""
        self._start_update_check(force_refresh=False, notify=True)

    # -------------------------------------------------------------------------
    # Local .rpm File Inspection & Installation
    # -------------------------------------------------------------------------
    def open_local_rpm(self, file_path: str):
        """Inspects a standalone .rpm package file and opens the install prompt."""
        if is_running_in_flatpak():
            QMessageBox.information(
                self,
                "Local RPM Installation Is Unavailable",
                "Flatpak Dendro does not expose the host filesystem or run host commands. "
                "Open this RPM with an RPM-host Dendro installation instead.",
            )
            return

        self.status_bar.showMessage(f"Inspecting local package: {os.path.basename(file_path)}...")
        result = inspect_local_rpm_file(file_path)
        if not result:
            QMessageBox.warning(self, "Invalid RPM File", f"Could not parse RPM package header:\n{file_path}")
            return

        dlg = LocalRpmInstallDialog(result, self)
        dlg.install_requested.connect(self._on_install_local_rpm_requested)
        dlg.exec()

    def _on_install_local_rpm_requested(self, file_path: str):
        try:
            request = TransactionRequest.from_values(installs=[file_path])
        except Dnf5DaemonError as error:
            self._show_transaction_error(error.title, error.detail)
            return
        self._begin_transaction_resolution(request, "local_rpm")

    # -------------------------------------------------------------------------
    # Theme Coordination & Dynamic Synchronization
    # -------------------------------------------------------------------------
    def _init_theming(self):
        self._apply_theme(self.current_theme)

        # 1. Native Qt 6 colorSchemeChanged signal (Wayland & X11)
        app = QGuiApplication.instance()
        if app and hasattr(app, "styleHints"):
            app.styleHints().colorSchemeChanged.connect(self._on_system_color_scheme_changed)

        # 2. Direct FreeDesktop Portal D-Bus SettingChanged event subscription (Zero Polling)
        try:
            from PyQt6.QtDBus import QDBusConnection
            bus = QDBusConnection.sessionBus()
            if bus.isConnected():
                bus.connect(
                    "org.freedesktop.portal.Desktop",
                    "/org/freedesktop/portal/desktop",
                    "org.freedesktop.portal.Settings",
                    "SettingChanged",
                    self._on_portal_setting_changed
                )
        except Exception:
            # Appearance portal support is optional. It cannot affect package
            # management, but a failed subscription should remain diagnosable.
            LOGGER.debug("Could not subscribe to portal color-scheme changes.", exc_info=True)

    def _on_portal_setting_changed(self, namespace: str, key: str, value: object):
        """Queue one update from the exact new portal value, when available."""
        if namespace != "org.freedesktop.appearance" or key != "color-scheme":
            return

        from ui.styles import _debug_log, portal_color_scheme_to_theme_key

        theme_key = portal_color_scheme_to_theme_key(value)
        _debug_log(
            f"D-Bus Portal SettingChanged event received: {namespace}.{key} -> {theme_key or 'unknown'}"
        )
        if self.current_theme == "auto" and not self._applying_theme:
            self._queue_system_theme_update(theme_key)

    def _queue_system_theme_update(self, theme_key: Optional[str] = None) -> None:
        """Apply a desktop-theme event after Qt has finished its own update."""
        if self.current_theme != "auto" or self._applying_theme:
            return
        if theme_key in ("dark", "light"):
            self._pending_system_theme_key = theme_key
        if self._system_theme_update_queued:
            return

        self._system_theme_update_queued = True
        QTimer.singleShot(0, self._apply_queued_system_theme)

    def _apply_queued_system_theme(self) -> None:
        """Coalesce portal and Qt notifications into one stylesheet update."""
        self._system_theme_update_queued = False
        theme_key = self._pending_system_theme_key
        self._pending_system_theme_key = None
        if self.current_theme == "auto" and not self._applying_theme:
            self._apply_theme("auto", resolved_key=theme_key)

    def _sync_native_color_scheme(self, theme_choice: str, resolved_key: str) -> None:
        """Tell Qt which scheme to use for its native window decorations.

        A stylesheet changes Dendro's widgets but not necessarily a Wayland/X11
        client-side title bar. Qt 6.8 introduced this public process-local hint;
        older PyQt6 releases simply retain their existing platform behaviour.
        """
        app = QGuiApplication.instance()
        if app is None:
            return

        style_hints = app.styleHints()
        set_color_scheme = getattr(style_hints, "setColorScheme", None)
        if not callable(set_color_scheme):
            return

        choice = (theme_choice or "auto").lower()
        if choice == "auto":
            # Qt follows the system by default.  In Auto mode, clear only
            # an explicit Dendro override; a fresh automatic application does
            # not need a native color-scheme write.
            if self._native_color_scheme_override is None:
                return
            target_scheme = Qt.ColorScheme.Unknown
        else:
            target_scheme = Qt.ColorScheme.Dark if resolved_key == "dark" else Qt.ColorScheme.Light

        if self._native_color_scheme_override == target_scheme:
            return

        try:
            set_color_scheme(target_scheme)
            self._native_color_scheme_override = (
                None if target_scheme == Qt.ColorScheme.Unknown else target_scheme
            )
        except (RuntimeError, TypeError):
            LOGGER.debug("Could not synchronize the native window color scheme.", exc_info=True)

    def _apply_theme(self, theme_choice: str, *, resolved_key: Optional[str] = None):
        if self._applying_theme:
            return

        self._applying_theme = True
        try:
            if resolved_key not in ("dark", "light"):
                resolved_key = get_resolved_theme_key(theme_choice)
            self._sync_native_color_scheme(theme_choice, resolved_key)

            stylesheet = get_theme_stylesheet(resolved_key)
            app = QApplication.instance()
            if app:
                app.setStyleSheet(stylesheet)
            else:
                self.setStyleSheet(stylesheet)

            theme_cfg = THEMES_CONFIG.get(resolved_key, {})
            is_dark = (theme_cfg.get("is_dark", "true") == "true")

            self.header.update_theme_toggle_icon(is_dark)

            if app and theme_cfg:
                pal = QPalette()
                window_col = QColor(theme_cfg["bg_surface"])
                text_col = QColor(theme_cfg["text_primary"])
                base_col = QColor(theme_cfg["bg_input"])
                accent_col = QColor(theme_cfg["accent"])
                accent_txt = QColor(theme_cfg["accent_text"])

                pal.setColor(QPalette.ColorRole.Window, window_col)
                pal.setColor(QPalette.ColorRole.WindowText, text_col)
                pal.setColor(QPalette.ColorRole.Base, base_col)
                pal.setColor(QPalette.ColorRole.Text, text_col)
                pal.setColor(QPalette.ColorRole.Button, window_col)
                pal.setColor(QPalette.ColorRole.ButtonText, text_col)
                pal.setColor(QPalette.ColorRole.Highlight, accent_col)
                pal.setColor(QPalette.ColorRole.HighlightedText, accent_txt)
                app.setPalette(pal)

            dark_candidates = ["breeze-dark", "Papirus-Dark", "Adwaita-Dark", "Adwaita", "breeze", "hicolor"]
            light_candidates = ["breeze", "Papirus", "Adwaita", "hicolor"]
            target_candidates = dark_candidates if is_dark else light_candidates

            for cand in target_candidates:
                if QIcon.hasThemeIcon("view-refresh") or QIcon.hasThemeIcon("system-search"):
                    QIcon.setThemeName(cand)
                    break

            pal_colors = get_delegate_palette(resolved_key)

            self.tree_view.set_chevron_color(pal_colors["accent"])

            tree_pal = self.tree_view.palette()
            tree_pal.setColor(QPalette.ColorRole.Base, pal_colors["bg_base"])
            tree_pal.setColor(QPalette.ColorRole.Window, pal_colors["bg_base"])
            tree_pal.setColor(QPalette.ColorRole.Text, pal_colors["text_main"])
            self.tree_view.setPalette(tree_pal)
            self.tree_view.viewport().setPalette(tree_pal)

            self.tree_delegate.set_theme(resolved_key)
            self.sidebar.set_theme(resolved_key)
            self.inspector_panel.set_theme(resolved_key)

            self.tree_view.viewport().update()
            self.sidebar.viewport().update()
            self.settings.setValue("theme", theme_choice)
        finally:
            self._applying_theme = False

    def _on_theme_toggle_clicked(self):
        """Flips between Dark and Light mode on 1 click."""
        resolved = get_resolved_theme_key(self.current_theme)
        new_theme = "light" if resolved == "dark" else "dark"
        self.current_theme = new_theme
        self._apply_theme(new_theme)

    def _on_system_color_scheme_changed(self, *args):
        """Queue an automatic-theme refresh after Qt applies its new palette."""
        from ui.styles import _debug_log

        scheme = args[0] if args else Qt.ColorScheme.Unknown
        _debug_log(f"Qt colorSchemeChanged signal received: {scheme}")
        # The portal's SettingChanged value is authoritative when it arrives.
        # This fallback deliberately re-resolves after the event loop rather
        # than letting a potentially stale Qt signal overwrite that value.
        self._queue_system_theme_update()
    # -------------------------------------------------------------------------
    # Package Loading & Multi-Threaded Cache Reconciliation
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

        self._set_busy_state(True, "package-load")
        self.status_bar.showMessage("Reading system RPM database...")

        # 1. Load local RPM packages first for sub-second startup display.
        # Capture the worker identity so a cancelled predecessor cannot replace
        # a newer reload with a stale result.
        worker = PackageQueryWorker(category="all", search_query="")
        self.current_query_worker = worker
        worker.signals.packages_loaded.connect(
            lambda packages, source=worker: self._on_packages_loaded(source, packages)
        )
        worker.signals.status_update.connect(self.status_bar.showMessage)
        worker.signals.error_occurred.connect(
            lambda package, detail, source=worker: self._on_package_query_error(source, package, detail)
        )
        self.thread_pool.start(worker)

    def _start_auxiliary_queries(self):
        """Dispatches slower DNF5 queries after packages are already rendered on screen."""
        self.current_userinstalled_worker = UserInstalledQueryWorker()
        self.current_userinstalled_worker.signals.userinstalled_loaded.connect(self._on_userinstalled_loaded)
        self.current_userinstalled_worker.signals.status_update.connect(self.status_bar.showMessage)
        self.current_userinstalled_worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(self.current_userinstalled_worker)

        self.current_orphan_worker = OrphanQueryWorker()
        self.current_orphan_worker.signals.orphans_loaded.connect(self._on_orphans_loaded)
        self.current_orphan_worker.signals.status_update.connect(self.status_bar.showMessage)
        self.current_orphan_worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(self.current_orphan_worker)

        self._start_update_check(force_refresh=False, notify=False)

    def _on_packages_loaded(self, worker: PackageQueryWorker, packages: List[PackageInfo]) -> None:
        if worker is not self.current_query_worker:
            return
        self._all_packages_cache = packages

        if self._user_installed_cache:
            for p in packages:
                p.is_user_installed = (p.name in self._user_installed_cache)
        if self._orphan_cache:
            for p in packages:
                p.is_orphan = (p.name in self._orphan_cache)
        if self._pending_updates_map:
            for p in packages:
                up = self._pending_updates_map.get(p.name)
                if up:
                    p.has_update = True
                    p.available_update_version = up.full_version
                    p.available_update_repo = up.repository

        self.tree_model.set_packages(packages)
        self._update_sidebar_counts(packages)
        self._sync_queue_states()

        # Synchronize InspectorPanel with the updated database state
        if self.inspector_panel._current_package:
            fresh_item = self.tree_model._package_lookup.get(self.inspector_panel._current_package.name)
            if fresh_item and isinstance(fresh_item.payload, PackageInfo):
                self.inspector_panel.set_package_info(fresh_item.payload)
            else:
                # Package was removed from the system; clear the inspector
                self.inspector_panel.clear()

        self.status_bar.showMessage(f"Loaded {len(packages):,} packages successfully.")
        self._set_busy_state(False, "package-load")
        self.current_query_worker = None

        # Render is complete; dispatch background auxiliary queries without blocking the UI
        self._start_auxiliary_queries()

    def _on_userinstalled_loaded(self, user_pkgs: Set[str]):
        self._user_installed_cache = user_pkgs
        self.tree_model.update_user_installed(user_pkgs)
        if self._all_packages_cache:
            for p in self._all_packages_cache:
                p.is_user_installed = (p.name in user_pkgs)
            self._update_sidebar_counts(self._all_packages_cache)
        # Invalidate filter so user-installed view refreshes if active
        self.proxy_model.invalidateFilter()
        self.current_userinstalled_worker = None

    def _on_orphans_loaded(self, orphans: Set[str]):
        self._orphan_cache = orphans
        self.tree_model.update_orphans(orphans)
        if self._all_packages_cache:
            for p in self._all_packages_cache:
                p.is_orphan = (p.name in orphans)
        self.sidebar.update_category_counts({"orphans": len(orphans)})
        # Invalidate filter so newly loaded leaf orphans become visible immediately
        self.proxy_model.invalidateFilter()
        if self.proxy_model._category == "orphans":
            self.header.set_orphan_clean_visible(True, len(orphans))
        self.current_orphan_worker = None

    def _start_update_check(self, *, force_refresh: bool, notify: bool) -> None:
        """Start one identified update request and ignore stale worker callbacks."""
        if self.current_updates_worker is not None:
            return

        self._update_request_serial += 1
        request_serial = self._update_request_serial
        worker = SystemUpdatesCheckWorker(force_refresh=force_refresh)
        self.current_updates_worker = worker
        worker.signals.status_update.connect(self.status_bar.showMessage)
        worker.signals.error_occurred.connect(
            lambda _package, detail, source=worker, serial=request_serial: self._on_update_check_failed(source, serial, detail)
        )
        worker.signals.system_updates_loaded.connect(
            lambda updates, source=worker, serial=request_serial, show_notification=notify: self._on_updates_loaded(
                source, serial, updates, show_notification
            )
        )
        self.thread_pool.start(worker)

    def _on_update_check_failed(self, worker: SystemUpdatesCheckWorker, request_serial: int, detail: str) -> None:
        if request_serial != self._update_request_serial or worker is not self.current_updates_worker:
            return
        self.current_updates_worker = None
        self.status_bar.showMessage(f"Update check failed: {detail}", 8000)

    def _on_updates_loaded(
        self,
        worker: SystemUpdatesCheckWorker,
        request_serial: int,
        updates_map: Dict[str, AvailableUpdateInfo],
        show_notification: bool,
    ) -> None:
        if request_serial != self._update_request_serial or worker is not self.current_updates_worker:
            return
        self.current_updates_worker = None
        self._pending_updates_map = updates_map
        self.tree_model.update_available_upgrades(updates_map, replace=True)
        if self._all_packages_cache:
            for package in self._all_packages_cache:
                update = updates_map.get(package.name)
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
        self.proxy_model.invalidateFilter()
        if count > 0:
            self.status_bar.showMessage(f"{count} software updates are available for your system.")
        else:
            self.status_bar.showMessage("Your system is completely up to date.")

        if show_notification and count > 0 and hasattr(self, "tray_icon") and self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                "Dendro - Software Updates",
                f"{count} package updates are available for your Fedora system.",
                QSystemTrayIcon.MessageIcon.Information,
                6000,
            )

    def _on_sidebar_category_selected(self, category: str):
        self.proxy_model.set_category_filter(category)
        is_orphans = (category == "orphans")
        is_updates = (category == "updates_available")

        # Show contextual actions strictly in their relevant views
        self.header.set_orphan_clean_visible(is_orphans, len(self._orphan_cache))
        self.header.set_updates_view_active(is_updates, len(self._pending_updates_map))

    def _filter_to_updates(self):
        """Switches the view to show the list of available updates for review."""
        for row in range(self.sidebar.count()):
            item = self.sidebar.item(row)
            if item and item.data(Qt.ItemDataRole.UserRole) == "updates_available":
                self.sidebar.setCurrentRow(row)
                break
        # Explicitly notify controller so header contextual actions (Upgrade All, Check Mirrors) become visible
        self._on_sidebar_category_selected("updates_available")

    def _on_system_upgrade_requested(self):
        """Resolve a full system upgrade through the daemon before any authentication."""
        if not self._pending_updates_map:
            QMessageBox.information(self, "System Up to Date", "Your system is already up to date.")
            return

        up_count = len(self._pending_updates_map)
        reply = QMessageBox.question(
            self,
            "Review System Upgrade",
            f"There are {up_count} package updates available for your system.\n\n"
            "Dendro will ask the DNF5 daemon to resolve the full upgrade first. "
            "You can review the exact package plan before authenticating.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._begin_transaction_resolution(
                TransactionRequest.from_values(upgrade_all=True),
                "system_upgrade",
            )

    def _on_clean_all_orphans_clicked(self):
        """Resolve explicit currently-unneeded packages; never invoke a command runner."""
        if not self._orphan_cache:
            QMessageBox.information(self, "No Leaf Orphans", "No unneeded leaf packages found.")
            return

        count = len(self._orphan_cache)
        reply = QMessageBox.question(
            self,
            "Review Unneeded Package Removal",
            f"Dendro found {count} currently unneeded packages.\n\n"
            "The DNF5 daemon will resolve the explicit removal set and show all dependent changes before authentication.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._begin_transaction_resolution(
                TransactionRequest.from_values(removals=sorted(self._orphan_cache)),
                "orphan_cleanup",
            )

    def _on_force_refresh_updates_clicked(self):
        """Expire metadata through the DNF5 daemon, then reload upgrade candidates."""
        self._start_update_check(force_refresh=True, notify=False)

    def _update_sidebar_counts(self, packages: List[PackageInfo]):
        counts: Dict[str, int] = {tag: 0 for _, tag, is_hdr in self.sidebar.CATEGORIES_CONFIG if tag}
        counts["all"] = len(packages)
        counts["updates_available"] = len(self._pending_updates_map)

        for p in packages:
            # 1. Granular subcategories (excluding composite parent tags)
            sub = getattr(p, "sub_category", "")
            if sub in counts and sub not in ("user_apps", "cli_tools", "pillar_hardware", "pillar_system", "pillar_libs"):
                counts[sub] += 1

            # 2. Top-level Pillar channels
            pillar = getattr(p, "parent_pillar", "")
            if pillar in counts:
                counts[pillar] += 1

            # 3. Dedicated interface categories
            if p.is_desktop_app and not p.is_system_settings:
                counts["user_apps"] += 1
            if p.is_cli_tool:
                counts["cli_tools"] += 1

            # 4. Provenance & Maintenance Facets
            if p.is_user_installed:
                counts["user_installed"] += 1
            if p.is_orphan:
                counts["orphans"] += 1

            repo_l = p.repository.lower()
            if "copr" in repo_l:
                counts["copr_repos"] += 1
            if "rpm fusion" in repo_l:
                counts["rpmfusion_repos"] += 1

        installs, removals, upgrades = self.tree_model.get_queued_packages()
        counts["queued"] = len(installs) + len(removals) + len(upgrades)

        self.sidebar.update_category_counts(counts)

    def _on_package_query_error(self, worker: PackageQueryWorker, pkg_name: str, message: str) -> None:
        """Finish only the package-load cursor token for its current worker."""
        if worker is not self.current_query_worker:
            return
        self.current_query_worker = None
        self._set_busy_state(False, "package-load")
        self._on_query_error(pkg_name, message)

    def _on_query_error(self, pkg_name: str, message: str) -> None:
        """Present an auxiliary-query error without changing unrelated busy state."""
        self.status_bar.showMessage(f"Error: {message}", 8000)
        if pkg_name:
            self.tree_model.reset_loading_state(pkg_name)

    # -------------------------------------------------------------------------
    # Dependency & Reverse Dependency Resolution
    # -------------------------------------------------------------------------
    def _on_tree_item_expanded(self, proxy_index: QModelIndex):
        source_index = self.proxy_model.mapToSource(proxy_index)
        if not source_index.isValid():
            return
        item: TreeItem = source_index.internalPointer()
        if (
            item
            and isinstance(item.payload, PackageInfo)
            and not item.dependencies_loaded
            and not item.is_loading_dependencies
        ):
            item.is_loading_dependencies = True
            pindex = QPersistentModelIndex(source_index)
            self._on_fetch_dependencies_requested(item.name, pindex)

    def _on_fetch_dependencies_requested(self, pkg_name: str, target_index: QPersistentModelIndex):
        worker = DependencyTreeWorker(root_package=pkg_name, max_depth=1, target_index=target_index)
        worker.signals.dependencies_resolved.connect(self._on_dependencies_resolved)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    def _on_dependencies_resolved(
        self,
        root_pkg_name: str,
        dependencies: List[DependencyNode],
        target_index: Optional[QPersistentModelIndex]
    ):
        self.tree_model.attach_dependencies(root_pkg_name, dependencies, target_index)

    def _on_fetch_reverse_deps_requested(self, pkg_name: str):
        # Only populate the Inspector panel's list; do NOT overwrite the main window's dependency tree
        worker = ReverseDependencyWorker(target_package=pkg_name)
        worker.signals.reverse_dependencies_resolved.connect(self.inspector_panel.set_reverse_dependencies)
        worker.signals.status_update.connect(self.status_bar.showMessage)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    def _toggle_sidebar(self):
        """Toggles sidebar visibility or restores it if collapsed in the splitter."""
        if self.sidebar.isVisible():
            sizes = self.main_splitter.sizes()
            if sizes[0] == 0:
                self.main_splitter.setSizes([290, sizes[1] - 290, sizes[2]])
            else:
                self.sidebar.hide()
        else:
            self.sidebar.show()
            self.main_splitter.setSizes([290, 720, 370])

    def _toggle_updates_view(self):
        """Allows toggling between Updates view and All Packages directly from the header."""
        if self.proxy_model._category == "updates_available":
            self._reset_to_all_packages()
        else:
            self._filter_to_updates()

    def _on_search_query_changed(self, query: str):
        """Handles standard search filtering and automatic rpm -qf file-path owner queries."""
        clean = query.strip()
        clean_path = clean.removeprefix("file:").strip()

        # Only execute ownership query on complete, existing system paths (avoiding jumping on '/' or partial paths)
        if clean_path.startswith("/") and len(clean_path) > 1 and os.path.exists(clean_path):
            from core.backend import find_package_owning_file
            owner = find_package_owning_file(clean_path)
            if owner:
                self.status_bar.showMessage(f"File '{clean_path}' is owned by package '{owner}'.", 4000)
                self.proxy_model.set_search_query(owner)
                self._navigate_to_package(owner)
                return

        self.proxy_model.set_search_query(query)

    def _on_remote_search_requested(self, query: str):
        """Dispatches an on-demand background query for uninstalled packages in DNF repositories."""
        clean = query.strip()
        if len(clean) < 2:
            self.status_bar.showMessage("Please enter at least 2 characters to search remote repositories.", 3500)
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
            lambda query, packages, source=worker: self._on_remote_search_finished(source, query, packages)
        )
        worker.signals.error_occurred.connect(
            lambda package, detail, source=worker: self._on_remote_search_failed(source, package, detail)
        )
        self.thread_pool.start(worker)

    def _on_remote_search_finished(
        self,
        worker: RemotePackageSearchWorker,
        query: str,
        packages: List[PackageInfo],
    ) -> None:
        if worker is not self.current_remote_search_worker:
            return
        self.current_remote_search_worker = None
        self._set_busy_state(False, "remote-search")
        if not packages:
            self.status_bar.showMessage(f"No packages matching '{query}' found in enabled repositories.", 4000)
            return

        added = self.tree_model.merge_remote_packages(packages)
        self.proxy_model.set_category_filter("all")
        self.proxy_model.set_search_query(query)
        self.status_bar.showMessage(
            f"Found {len(packages)} repository packages ({added} new). Select to review and install.", 5000
        )

    def _on_remote_search_failed(self, worker: RemotePackageSearchWorker, pkg_name: str, message: str) -> None:
        if worker is not self.current_remote_search_worker:
            return
        self.current_remote_search_worker = None
        self._set_busy_state(False, "remote-search")
        self._on_query_error(pkg_name, message)

    def _reset_to_all_packages(self):
        """Returns to the default all packages view and clears category filters."""
        self.header.search_input.clear()
        for row in range(self.sidebar.count()):
            item = self.sidebar.item(row)
            if item and item.data(Qt.ItemDataRole.UserRole) == "all":
                self.sidebar.setCurrentRow(row)
                break
        self._on_sidebar_category_selected("all")

    def _on_inspect_files_requested(self, pkg_name: str):
        worker = PackageFilesWorker(package_name=pkg_name)
        worker.signals.package_files_loaded.connect(self.inspector_panel.set_package_files)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    def _on_fetch_changelog_requested(self, pkg_name: str):
        worker = PackageChangelogWorker(package_name=pkg_name)
        worker.signals.package_changelog_loaded.connect(self.inspector_panel.set_package_changelog)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    def _on_fetch_scriptlets_requested(self, pkg_name: str):
        from core.backend import PackageScriptletsWorker
        worker = PackageScriptletsWorker(package_name=pkg_name)
        worker.signals.package_scriptlets_loaded.connect(self.inspector_panel.set_package_scriptlets)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    def _on_tree_item_double_clicked(self, proxy_index: QModelIndex):
        source_index = self.proxy_model.mapToSource(proxy_index)
        if not source_index.isValid():
            return
        item: TreeItem = source_index.internalPointer()
        if item and item.is_dependency and isinstance(item.payload, DependencyNode):
            target_pkg = item.payload.resolved_package_name
            self._navigate_to_package(target_pkg)

    def _navigate_to_package(self, pkg_name: str):
        """Jumps directly to a target package in the tree, adjusting filters if needed."""
        if not pkg_name:
            return

        source_item = self.tree_model._package_lookup.get(pkg_name)
        if not source_item:
            self.status_bar.showMessage(f"Package '{pkg_name}' is not installed locally.", 3500)
            return

        source_idx = self.tree_model.createIndex(source_item.row(), 0, source_item)
        proxy_idx = self.proxy_model.mapFromSource(source_idx)

        # If hidden by active filter, reset category to show the destination package
        if not proxy_idx.isValid():
            self._reset_to_all_packages()
            proxy_idx = self.proxy_model.mapFromSource(source_idx)

        if proxy_idx.isValid():
            self.tree_view.setCurrentIndex(proxy_idx)
            self.tree_view.scrollTo(proxy_idx, DendroTreeView.ScrollHint.PositionAtCenter)
            self.inspector_panel.set_package_info(source_item.payload)
            if not self.inspector_panel.isVisible():
                self.inspector_panel.show()

    def _on_verify_package_files_requested(self, pkg_name: str):
        worker = PackageVerifyWorker(package_name=pkg_name)
        worker.signals.package_verification_finished.connect(self.inspector_panel.set_package_verification)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    # -------------------------------------------------------------------------
    # Preferences and About Dialog
    # -------------------------------------------------------------------------
    def _open_settings_dialog(self):
        from ui.settings_dialog import DendroSettingsDialog
        dlg = DendroSettingsDialog(self)
        dlg.epoch_display_toggled.connect(self.tree_model.set_show_epoch)
        dlg.exec()

    # -------------------------------------------------------------------------
    # Software Repositories Manager Dialog
    # -------------------------------------------------------------------------
    def _open_repo_dialog(self):
        dialog = RepoManagerDialog(self)
        self._active_repo_dialog = dialog
        if self.repository_addition_runner.is_running or self.repository_deletion_runner.is_running:
            dialog.set_busy(True)
        dialog.repo_toggle_requested.connect(self._on_repo_toggle_requested)
        dialog.repo_add_requested.connect(self._on_repo_add_requested)
        dialog.repo_remove_requested.connect(self._on_repo_remove_requested)
        dialog.clean_cache_requested.connect(self._on_clean_cache_requested)
        dialog.refresh_requested.connect(self._load_repositories_from_daemon)
        dialog.finished.connect(lambda _result: self._clear_active_repo_dialog(dialog))
        self._load_repositories_from_daemon()
        dialog.exec()

    def _clear_active_repo_dialog(self, dialog: RepoManagerDialog):
        if self._active_repo_dialog is dialog:
            self._active_repo_dialog = None

    def _load_repositories_from_daemon(self):
        dialog = self._active_repo_dialog
        if dialog is None:
            return
        if self.current_repository_worker is not None:
            self._repository_refresh_after_current_worker = True
            return
        try:
            # Construct Qt's D-Bus signal receiver on the GUI thread before the
            # list worker runs, and report an absent host capability in-dialog.
            self.dnf_client.ensure_available()
        except Dnf5DaemonError as error:
            dialog.set_load_error(error.title, error.detail)
            return
        dialog.set_loading(True)
        worker = RepositoryListWorker(self.dnf_client)
        self.current_repository_worker = worker
        worker.signals.loaded.connect(self._on_daemon_repositories_loaded)
        worker.signals.failed.connect(self._on_daemon_repositories_failed)
        self.thread_pool.start(worker)

    def _on_daemon_repositories_loaded(self, repositories: object):
        self.current_repository_worker = None
        refresh_after_current_worker = self._repository_refresh_after_current_worker
        self._repository_refresh_after_current_worker = False
        dialog = self._active_repo_dialog
        if dialog is not None:
            dialog.set_repositories(repositories)
        if refresh_after_current_worker:
            self._load_repositories_from_daemon()

    def _on_daemon_repositories_failed(self, title: str, detail: str):
        self.current_repository_worker = None
        refresh_after_current_worker = self._repository_refresh_after_current_worker
        self._repository_refresh_after_current_worker = False
        dialog = self._active_repo_dialog
        if dialog is not None:
            dialog.set_load_error(title, detail)
        if refresh_after_current_worker:
            self._load_repositories_from_daemon()

    def _on_repo_add_requested(self, repository_url: str):
        dialog = self._active_repo_dialog
        if dialog is not None:
            dialog.set_busy(True)
        self._open_transaction_drawer()
        try:
            self.repository_addition_runner.add_from_url(repository_url)
        except Dnf5DaemonError as error:
            if dialog is not None:
                dialog.set_busy(False)
            self.transaction_drawer.finish_execution_mode(False)
            self._show_transaction_error(error.title, error.detail)

    def _on_repo_remove_requested(self, _repo_id: str, repo_file: str):
        dialog = self._active_repo_dialog
        if dialog is not None:
            dialog.set_busy(True)
        self._open_transaction_drawer()
        try:
            self.repository_deletion_runner.delete(repo_file)
        except Dnf5DaemonError as error:
            if dialog is not None:
                dialog.set_busy(False)
            self.transaction_drawer.finish_execution_mode(False)
            self._show_transaction_error(error.title, error.detail)

    def _on_clean_cache_requested(self):
        try:
            self._open_transaction_drawer()
            self.transaction_controller.clean_cache()
            if self._active_repo_dialog is not None:
                self._active_repo_dialog.set_busy(True)
        except Dnf5DaemonError as error:
            self.transaction_drawer.finish_execution_mode(False)
            self._show_transaction_error(error.title, error.detail)

    def _on_repo_toggle_requested(self, repo_id: str, enable: bool):
        try:
            self._open_transaction_drawer()
            self.transaction_controller.set_repository_enabled(repo_id, enable)
            if self._active_repo_dialog is not None:
                self._active_repo_dialog.set_busy(True)
        except Dnf5DaemonError as error:
            self.transaction_drawer.finish_execution_mode(False)
            self._show_transaction_error(error.title, error.detail)

    # -------------------------------------------------------------------------
    # UI Interactions, Selection & Context Menu
    # -------------------------------------------------------------------------
    def _on_tree_selection_changed(self, selected: QItemSelection, _deselected: QItemSelection):
        indexes = selected.indexes()
        if not indexes:
            return

        proxy_idx = indexes[0]
        source_idx = self.proxy_model.mapToSource(proxy_idx)
        if not source_idx.isValid():
            return

        item: TreeItem = source_idx.internalPointer()
        if item is None:
            return

        root_item = item.get_root_package_item()
        if root_item and isinstance(root_item.payload, PackageInfo):
            self.inspector_panel.set_package_info(root_item.payload)
            if not self.inspector_panel.isVisible():
                self.inspector_panel.show()

    def _toggle_inspector_panel(self):
        if self.inspector_panel.isVisible():
            self.inspector_panel.hide()
        else:
            self.inspector_panel.show()

    def _toggle_queue_selected_row(self):
        indexes = self.tree_view.selectionModel().selectedRows()
        if indexes:
            source_idx = self.proxy_model.mapToSource(indexes[0])
            self.tree_model.toggle_queue_state(source_idx)

    def _on_inspector_queue_action(self, pkg_name: str):
        item = self.tree_model._package_lookup.get(pkg_name)
        if item:
            idx = self.tree_model.createIndex(item.row(), 0, item)
            self.tree_model.toggle_queue_state(idx)
            if isinstance(item.payload, PackageInfo):
                self.inspector_panel.set_package_info(item.payload)

    def _on_discard_all_clicked(self):
        """1-click cancellation of all staged changes across the system."""
        installs, removals, upgrades = self.tree_model.get_queued_packages()
        total = len(installs) + len(removals) + len(upgrades)
        if total == 0:
            return

        reply = QMessageBox.question(
            self,
            "Discard All Pending Changes",
            f"Are you sure you want to cancel all {total} staged package changes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
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

        # Keep inspector panel action buttons synchronized if active package was queued/un-queued
        if self.inspector_panel._current_package:
            item = self.tree_model._package_lookup.get(self.inspector_panel._current_package.name)
            if item and isinstance(item.payload, PackageInfo):
                self.inspector_panel.set_package_info(item.payload)

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

        if not item.is_dependency:
            state = item.state
            has_up = isinstance(item.payload, PackageInfo) and item.payload.has_update
            if state in (PackageState.INSTALLED, PackageState.AVAILABLE):
                if state == PackageState.INSTALLED:
                    if has_up:
                        up_ver = item.payload.available_update_version
                        up_text = f"Queue Upgrade (-> {up_ver})" if up_ver else "Queue Upgrade"
                        up_act = QAction(up_text, self)
                        up_act.setIcon(QIcon.fromTheme("system-software-update"))
                        up_act.triggered.connect(lambda: self.tree_model.toggle_queue_state(source_index))
                        menu.addAction(up_act)

                    rm_act = QAction("Queue Removal", self)
                    rm_act.setIcon(QIcon.fromTheme("list-remove"))
                    rm_act.triggered.connect(lambda: self._force_queue_removal(source_index))
                    menu.addAction(rm_act)
                else:
                    in_act = QAction("Queue Installation", self)
                    in_act.setIcon(QIcon.fromTheme("list-add"))
                    in_act.triggered.connect(lambda: self.tree_model.toggle_queue_state(source_index))
                    menu.addAction(in_act)
            elif state in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE, PackageState.QUEUED_UPGRADE):
                cancel_act = QAction("Cancel Pending Change", self)
                cancel_act.setIcon(QIcon.fromTheme("edit-undo"))
                cancel_act.triggered.connect(lambda: self.tree_model.toggle_queue_state(source_index))
                menu.addAction(cancel_act)

            menu.addSeparator()

            rev_deps_act = QAction("Show Reverse Dependents (What Requires This)", self)
            rev_deps_act.setIcon(QIcon.fromTheme("system-search") or QIcon.fromTheme("edit-find"))
            rev_deps_act.triggered.connect(lambda: self._on_fetch_reverse_deps_requested(item.name))
            menu.addAction(rev_deps_act)

            verify_act = QAction("Verify File Integrity (rpm -V)", self)
            verify_act.setIcon(QIcon.fromTheme("security-high") or QIcon.fromTheme("system-run"))
            verify_act.triggered.connect(lambda: self._on_verify_package_files_requested(item.name))
            menu.addAction(verify_act)

            copy_chain_act = QAction("Copy Dependency Chain", self)
            copy_chain_act.setIcon(QIcon.fromTheme("view-list-tree") or QIcon.fromTheme("edit-copy"))
            copy_chain_act.triggered.connect(lambda: self._copy_dependency_chain(source_index))
            menu.addAction(copy_chain_act)

            menu.addSeparator()

        copy_name_act = QAction("Copy Package Name", self)
        copy_name_act.setIcon(QIcon.fromTheme("edit-copy"))
        copy_name_act.triggered.connect(lambda: self._copy_to_clipboard(item.name))
        menu.addAction(copy_name_act)

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def _force_queue_removal(self, source_index: QModelIndex):
        item: Optional[TreeItem] = source_index.internalPointer()
        if item and isinstance(item.payload, PackageInfo):
            item.payload.state = PackageState.QUEUED_REMOVE
            self.tree_model.dataChanged.emit(source_index, source_index)
            self.tree_model.queue_state_changed.emit()

    def _copy_dependency_chain(self, source_index: QModelIndex):
        """Formats the dependency tree of the selected item into plain indented text."""
        item: Optional[TreeItem] = source_index.internalPointer()
        if not item:
            return

        lines: List[str] = [f"{item.name} {item.version}"]
        for child in item.child_items:
            req_note = f" (requires {child.payload.raw_requirement})" if isinstance(child.payload, DependencyNode) else ""
            lines.append(f"  └── {child.name} [{child.version}]{req_note}")

        self._copy_to_clipboard("\n".join(lines))

    def _copy_to_clipboard(self, text: str):
        clipboard: Optional[QClipboard] = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(text)
            self.status_bar.showMessage(f"Copied '{text}' to clipboard.", 2500)

    # -------------------------------------------------------------------------
    # DNF History & Dry-Run Simulation
    # -------------------------------------------------------------------------
    def _open_history_dialog(self):
        dialog = DnfHistoryDialog(self)
        dialog.undo_requested.connect(self._on_undo_transaction_requested)
        dialog.refresh_requested.connect(lambda: self._load_dnf_history(dialog))
        self._load_dnf_history(dialog)
        dialog.exec()

    def _load_dnf_history(self, dialog: DnfHistoryDialog):
        self.status_bar.showMessage("Loading DNF transaction history...")
        worker = DnfHistoryWorker()
        worker.signals.history_loaded.connect(dialog.set_history_entries)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    def _on_undo_transaction_requested(self, trans_id: int):
        QMessageBox.information(
            self,
            "History Undo Is Not Available",
            f"DNF5's system daemon does not expose a typed history-undo API for transaction #{trans_id}. "
            "Dendro does not fall back to a privileged command runner. Use a terminal or a future daemon API explicitly designed for history rollback.",
        )

    def _on_header_apply_clicked(self):
        installs, removals, upgrades = self.tree_model.get_queued_packages()
        if not installs and not removals and not upgrades:
            if self._pending_updates_map:
                self._on_system_upgrade_requested()
            return
        try:
            request = TransactionRequest.from_values(
                installs=installs,
                removals=removals,
                upgrades=upgrades,
            )
        except Dnf5DaemonError as error:
            self._show_transaction_error(error.title, error.detail)
            return
        self._begin_transaction_resolution(request, "staged")

    def _begin_transaction_resolution(self, request: TransactionRequest, transaction_kind: str) -> None:
        try:
            self._pending_transaction_kind = transaction_kind
            self._last_transaction_upgrades = list(request.upgrades)
            self._last_transaction_removals = list(request.removals)
            self._open_transaction_drawer()
            self.status_bar.showMessage("Resolving transaction through Fedora's DNF5 daemon…")
            self.transaction_controller.resolve(request)
        except Dnf5DaemonError as error:
            self._pending_transaction_kind = ""
            self._last_transaction_upgrades = []
            self._last_transaction_removals = []
            self.transaction_drawer.finish_execution_mode(False)
            self._show_transaction_error(error.title, error.detail)

    def _on_dnf_plan_ready(self, plan: object) -> None:
        if not isinstance(plan, TransactionPlan):
            self._show_transaction_error("Invalid transaction plan", "The DNF5 daemon controller returned an invalid plan object.")
            return
        if not plan.has_changes:
            QMessageBox.information(
                self,
                "No Package Changes",
                "DNF resolved the request successfully, but no package changes are required.",
            )
            self.transaction_controller.discard_ready_plan()
            return

        dialog = TransactionPlanReviewDialog(plan=plan, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.transaction_controller.discard_ready_plan()
            return

        installs, removals, upgrades = plan.categorized_names()
        self.transaction_drawer.set_transaction_preview(installs, removals, upgrades)
        self.transaction_drawer.start_execution_mode()
        try:
            self.status_bar.showMessage("Awaiting DNF5 daemon authorization…")
            self.transaction_controller.execute_ready_plan()
        except Dnf5DaemonError as error:
            self.transaction_drawer.finish_execution_mode(False)
            self._show_transaction_error(error.title, error.detail)

    def _on_dnf_plan_discarded(self) -> None:
        if not self.transaction_controller.is_busy:
            self.transaction_drawer.finish_execution_mode(False)
            self.status_bar.showMessage("Transaction plan discarded.")
        self._continue_close_if_requested()

    def _close_transaction_drawer(self):
        if self.transaction_controller.is_busy or self.transaction_controller.has_ready_plan:
            self.status_bar.showMessage("A DNF5 operation is still active. Use Cancel to request a safe daemon cancellation.")
            return
        if self.repository_addition_runner.is_running:
            self.status_bar.showMessage(
                "Repository-file addition is still active. Use Cancel to request cancellation."
            )
            return
        if self.repository_deletion_runner.is_running:
            self.status_bar.showMessage("Repository-file deletion is still active. Use Cancel to request termination.")
            return
        self.transaction_drawer.hide()
        self.workspace_splitter.setSizes([750, 0])

    def _on_drawer_cancel(self):
        if self.repository_addition_runner.is_running:
            self.repository_addition_runner.cancel()
            return
        if self.repository_deletion_runner.is_running:
            self.repository_deletion_runner.cancel()
            return
        if self.transaction_controller.is_busy or self.transaction_controller.has_ready_plan:
            self.transaction_controller.request_cancellation()
            return
        self._close_transaction_drawer()

    def _on_drawer_commit(self):
        if not self.transaction_controller.has_ready_plan:
            return
        self.transaction_drawer.start_execution_mode()
        try:
            self.transaction_controller.execute_ready_plan()
        except Dnf5DaemonError as error:
            self.transaction_drawer.finish_execution_mode(False)
            self._show_transaction_error(error.title, error.detail)

    def _on_transaction_finished(self, success: bool, detail: str):
        self.transaction_drawer.finish_execution_mode(success)
        if success:
            self.status_bar.showMessage("Transaction completed successfully.")
            if self._pending_transaction_kind == "system_upgrade":
                self._pending_updates_map.clear()
            else:
                for package in self._last_transaction_upgrades:
                    self._pending_updates_map.pop(package, None)
                for package in self._last_transaction_removals:
                    self._pending_updates_map.pop(package, None)
                    self._orphan_cache.discard(package)
                    self._user_installed_cache.discard(package)

            remaining_updates = len(self._pending_updates_map)
            self.header.update_available_updates_badge(remaining_updates)
            self.sidebar.update_category_counts({
                "updates_available": remaining_updates,
                "orphans": len(self._orphan_cache),
            })
            # Dependency provider facts are derived from the installed RPM DB.
            # Drop both cache tiers before the reload observes the new transaction.
            SQLiteCapabilityCache.get_instance().invalidate()
            self.tree_model.clear_all_queued()
            self._sync_queue_states()
            self._load_packages()
            self.proxy_model.invalidateFilter()
            if detail:
                self.status_bar.showMessage("Transaction completed, but DNF session cleanup reported an error. See the transaction log.")
        else:
            self.status_bar.showMessage(f"Transaction failed or was cancelled: {detail}")
        self._pending_transaction_kind = ""
        self._continue_close_if_requested()

    def _on_maintenance_finished(self, action: str, success: bool, detail: str):
        self.transaction_drawer.finish_execution_mode(success)
        dialog = self._active_repo_dialog
        if dialog is not None:
            dialog.set_busy(False)
            self._load_repositories_from_daemon()
        self.status_bar.showMessage(
            f"DNF5 daemon completed: {action}." if success else f"DNF5 daemon failed to {action}: {detail}"
        )
        self._continue_close_if_requested()

    def _on_repository_addition_finished(self, success: bool, detail: str):
        self.transaction_drawer.finish_execution_mode(success)
        dialog = self._active_repo_dialog
        if dialog is not None:
            dialog.set_busy(False)
        if success:
            self.status_bar.showMessage("Repository configuration file added.")
            if dialog is not None:
                dialog.clear_repository_address()
        else:
            self.status_bar.showMessage(detail)
            self._show_transaction_error("Repository addition failed", detail)
        # The helper has reached a terminal state and released the shared host
        # gate. Refresh even after failure: a terminal filesystem error can
        # occur after an atomic directory entry mutation, and the daemon is the
        # authoritative source for what Dendro displays.
        if dialog is not None:
            self._load_repositories_from_daemon()
        self._continue_close_if_requested()

    def _on_repository_deletion_finished(self, success: bool, detail: str):
        self.transaction_drawer.finish_execution_mode(success)
        dialog = self._active_repo_dialog
        if dialog is not None:
            dialog.set_busy(False)
        if success:
            self.status_bar.showMessage("Repository configuration file deleted.")
        else:
            self.status_bar.showMessage(detail)
            self._show_transaction_error("Repository deletion failed", detail)
        if dialog is not None:
            self._load_repositories_from_daemon()
        self._continue_close_if_requested()

    def _on_dnf_operation_failed(self, title: str, detail: str):
        self.transaction_drawer.finish_execution_mode(False)
        if not self.transaction_controller.is_busy and not self.transaction_controller.has_ready_plan:
            self._pending_transaction_kind = ""
            self._last_transaction_upgrades = []
            self._last_transaction_removals = []
        self.status_bar.showMessage(f"{title}: {detail}")
        self._show_transaction_error(title, detail)
        self._continue_close_if_requested()

    def _show_transaction_error(self, title: str, detail: str):
        QMessageBox.critical(self, title, detail)

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
                    "Operation Still Active",
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

        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("main_splitter_state", self.main_splitter.saveState())
        self.settings.setValue("workspace_splitter_state", self.workspace_splitter.saveState())
        self.settings.setValue("tree_header_state", self.tree_view.header().saveState())
        if self.current_query_worker:
            self.current_query_worker.cancel()
        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()
        if self.current_userinstalled_worker:
            self.current_userinstalled_worker.cancel()
        if self.current_updates_worker:
            self.current_updates_worker.cancel()
        if self.current_remote_search_worker:
            self.current_remote_search_worker.cancel()
        # Do not block the GUI thread with a guessed shutdown timeout.  Running
        # read-only workers own their resources and Qt disconnects their signals
        # as this window is destroyed.
        self.thread_pool.clear()
        event.accept()
