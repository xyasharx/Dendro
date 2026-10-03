# dendro/ui/main_window.py
"""
Main application window controller for Dendro:
Manages asynchronous thread pools, native librpm queries, system update checks,
in-app system upgrades (dnf upgrade), batch orphan removal (dnf autoremove),
direct local .rpm package file inspection and installation,
system tray notifications, dynamic theming, and Polkit elevation.
Exclusively utilizes native FreeDesktop vector icons with zero font emoji glyphs.
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional, Set
from PyQt6.QtCore import (
    QItemSelection,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QSettings,
    QSize,
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
    DryRunSimulationResult,
    FileVerificationResult,
    HistoryEntry,
    LocalRpmInspectionResult,
    OrphanQueryWorker,
    PackageChangelogEntry,
    PackageChangelogWorker,
    PackageFileInfo,
    PackageFilesWorker,
    PackageInfo,
    PackageQueryWorker,
    PackageState,
    PackageVerifyWorker,
    PolkitTransactionRunner,
    RepoInfo,
    RepoManagerHelper,
    ReverseDependencyWorker,
    SystemUpdatesCheckWorker,
    TransactionDryRunWorker,
    UserInstalledQueryWorker,
    inspect_local_rpm_file,
)
from core.models import (
    DependencyTreeModel,
    PackageFilterProxyModel,
    TreeItem,
)
from ui.delegates import DendroTreeView, PackageTreeItemDelegate
from ui.dry_run_dialog import DryRunSimulationDialog
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
            status_box.setStyleSheet("background-color: rgba(243, 139, 168, 0.15); border: 1px solid #f38ba8; "
                                     "color: #f38ba8; border-radius: 6px; padding: 8px;")
        else:
            status_box = QLabel("<b>Status:</b> Not currently installed on this system.")
            status_box.setStyleSheet("background-color: rgba(166, 227, 161, 0.15); border: 1px solid #a6e3a1; "
                                     "color: #a6e3a1; border-radius: 6px; padding: 8px;")
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
        req_title.setStyleSheet("font-weight: bold; font-size: 11px; color: #a6adc8;")
        layout.addWidget(req_title)

        req_box = QTextEdit()
        req_box.setReadOnly(True)
        req_box.setPlainText("\n".join(self.result.requires[:40]) or "No explicit dependencies.")
        req_box.setStyleSheet("background-color: #11111b; font-family: monospace; font-size: 11px;")
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
        self.resize(1380, 880)

        # Persistent user settings
        self.settings = QSettings("FedoraCommunity", "Dendro")
        self.current_theme = self.settings.value("theme", "auto", type=str)

        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(16)

        # Background worker tracking
        self.current_query_worker: Optional[PackageQueryWorker] = None
        self.current_orphan_worker: Optional[OrphanQueryWorker] = None
        self.current_userinstalled_worker: Optional[UserInstalledQueryWorker] = None
        self.current_updates_worker: Optional[SystemUpdatesCheckWorker] = None
        self.transaction_runner: Optional[PolkitTransactionRunner] = None

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

        # Handle local .rpm file opened via CLI argument or desktop file association
        if len(sys.argv) > 1 and sys.argv[1].endswith(".rpm") and os.path.isfile(sys.argv[1]):
            QTimer.singleShot(400, lambda: self.open_local_rpm(sys.argv[1]))

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

        # Dedicated tree view with native vector chevron branches (no QProxyStyle override)
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

        # Proportions: Sidebar (290px), Center Workspace (720px), Inspector (370px)
        self.main_splitter.setSizes([290, 720, 370])
        self.workspace_splitter.setSizes([720, 0])

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready.")

        self.proxy_model.set_category_filter("user_apps")

    def _configure_tree_columns(self):
        header = self.tree_view.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(DependencyTreeModel.COL_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_STATUS, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_VERSION, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_SIZE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_SUMMARY, QHeaderView.ResizeMode.Stretch)

        self.tree_view.setColumnWidth(DependencyTreeModel.COL_NAME, 320)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_STATUS, 140)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_VERSION, 175)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_SIZE, 110)

        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self.tree_view.setSortingEnabled(True)
        self.tree_view.sortByColumn(DependencyTreeModel.COL_NAME, Qt.SortOrder.AscendingOrder)

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: self.header.search_input.setFocus())
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._load_packages)
        QShortcut(QKeySequence("Ctrl+H"), self, activated=self._open_history_dialog)
        QShortcut(QKeySequence("Ctrl+I"), self, activated=self._toggle_inspector_panel)
        QShortcut(QKeySequence("Space"), self, activated=self._toggle_queue_selected_row)

    def _connect_signals(self):
        # 1. Header Bar
        self.header.search_changed.connect(self.proxy_model.set_search_query)
        self.header.reload_clicked.connect(self._load_packages)
        self.header.apply_clicked.connect(self._on_header_apply_clicked)
        self.header.toggle_inspector_clicked.connect(self._toggle_inspector_panel)
        self.header.history_clicked.connect(self._open_history_dialog)
        self.header.theme_selected.connect(self._on_theme_selected)
        self.header.repos_clicked.connect(self._open_repo_dialog)
        self.header.updates_clicked.connect(self._filter_to_updates)
        self.header.upgrade_system_clicked.connect(self._on_system_upgrade_requested)
        self.header.clean_orphans_clicked.connect(self._on_clean_all_orphans_clicked)
        self.header.discard_btn.clicked.connect(self._on_discard_all_clicked)
        self.header.refresh_updates_clicked.connect(self._on_force_refresh_updates_clicked)
        self.header.clean_cache_clicked.connect(self._on_clean_cache_clicked)

        # 2. Sidebar Navigation
        self.sidebar.category_selected.connect(self._on_sidebar_category_selected)

        # 3. Tree View & Models
        self.tree_model.fetch_dependencies_requested.connect(self._on_fetch_dependencies_requested)
        self.tree_model.queue_state_changed.connect(self._sync_queue_states)
        self.tree_view.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree_view.selectionModel().selectionChanged.connect(self._on_tree_selection_changed)
        self.tree_view.expanded.connect(self._on_tree_item_expanded)

        # 4. Package Inspector Panel
        self.inspector_panel.closed.connect(lambda: self.inspector_panel.hide())
        self.inspector_panel.package_action_requested.connect(self._on_inspector_queue_action)
        self.inspector_panel.file_inspection_requested.connect(self._on_inspect_files_requested)
        self.inspector_panel.file_verification_requested.connect(self._on_verify_package_files_requested)
        self.inspector_panel.changelog_requested.connect(self._on_fetch_changelog_requested)
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

        # Load Dendro's own logo (checks installed theme first, then local repo files)
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

        # Background update check timer (Checks every 4 hours)
        self.bg_update_timer = QTimer(self)
        self.bg_update_timer.setInterval(4 * 60 * 60 * 1000)
        self.bg_update_timer.timeout.connect(self._trigger_background_update_check)
        self.bg_update_timer.start()

    def _restore_from_tray(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._restore_from_tray()

    def _trigger_background_update_check(self):
        """Silently checks for updates in the background and sends a desktop notification."""
        if self.current_updates_worker is not None:
            return
        worker = SystemUpdatesCheckWorker()
        worker.signals.system_updates_loaded.connect(self._on_background_updates_result)
        self.thread_pool.start(worker)

    def _on_background_updates_result(self, updates_map: Dict[str, AvailableUpdateInfo]):
        self._on_updates_loaded(updates_map)
        count = len(updates_map)
        if count > 0 and hasattr(self, "tray_icon") and self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                "Dendro - Software Updates",
                f"{count} package updates are available for your Fedora system.",
                QSystemTrayIcon.MessageIcon.Information,
                6000
            )

    # -------------------------------------------------------------------------
    # Local .rpm File Inspection & Installation
    # -------------------------------------------------------------------------
    def open_local_rpm(self, file_path: str):
        """Inspects a standalone .rpm package file and opens the install prompt."""
        self.status_bar.showMessage(f"Inspecting local package: {os.path.basename(file_path)}...")
        result = inspect_local_rpm_file(file_path)
        if not result:
            QMessageBox.warning(self, "Invalid RPM File", f"Could not parse RPM package header:\n{file_path}")
            return

        dlg = LocalRpmInstallDialog(result, self)
        dlg.install_requested.connect(self._on_install_local_rpm_requested)
        dlg.exec()

    def _on_install_local_rpm_requested(self, file_path: str):
        self.transaction_drawer.start_execution_mode()
        self.transaction_drawer.show()
        self.workspace_splitter.setSizes([450, 320])

        self.transaction_runner = PolkitTransactionRunner(self)
        self.transaction_runner.log_received.connect(self.transaction_drawer.append_log)
        self.transaction_runner.progress_percent.connect(self.transaction_drawer.set_progress)
        self.transaction_runner.transaction_finished.connect(self._on_transaction_finished)
        self.transaction_runner.execute_install_local_rpm(file_path)

    # -------------------------------------------------------------------------
    # Theme Coordination & Dynamic Synchronization
    # -------------------------------------------------------------------------
    def _init_theming(self):
        self._apply_theme(self.current_theme)
        self.header.set_active_theme(self.current_theme)

        app = QGuiApplication.instance()
        if app and hasattr(app, "styleHints"):
            app.styleHints().colorSchemeChanged.connect(self._on_system_color_scheme_changed)

    def _apply_theme(self, theme_choice: str):
        stylesheet = get_theme_stylesheet(theme_choice)
        app = QApplication.instance()
        if app:
            app.setStyleSheet(stylesheet)
        else:
            self.setStyleSheet(stylesheet)

        resolved_key = get_resolved_theme_key(theme_choice)
        theme_cfg = THEMES_CONFIG.get(resolved_key, {})
        is_dark = (theme_cfg.get("is_dark", "true") == "true")

        # Synchronize QApplication palette for FreeDesktop SVG currentColor adaptation
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

        # High-contrast icon themes
        dark_candidates = ["breeze-dark", "Papirus-Dark", "Adwaita-Dark", "Adwaita", "breeze", "hicolor"]
        light_candidates = ["breeze", "Papirus", "Adwaita", "hicolor"]
        target_candidates = dark_candidates if is_dark else light_candidates

        for cand in target_candidates:
            if QIcon.hasThemeIcon("view-refresh") or QIcon.hasThemeIcon("system-search"):
                QIcon.setThemeName(cand)
                break

        pal_colors = get_delegate_palette(theme_choice)

        # Synchronize chevron branch color and viewport palette directly
        self.tree_view.set_chevron_color(pal_colors["accent"])

        tree_pal = self.tree_view.palette()
        tree_pal.setColor(QPalette.ColorRole.Base, pal_colors["bg_base"])
        tree_pal.setColor(QPalette.ColorRole.Window, pal_colors["bg_base"])
        tree_pal.setColor(QPalette.ColorRole.Text, pal_colors["text_main"])
        self.tree_view.setPalette(tree_pal)
        self.tree_view.viewport().setPalette(tree_pal)

        self.tree_delegate.set_theme(theme_choice)
        self.sidebar.set_theme(theme_choice)
        self.inspector_panel.set_theme(theme_choice)

        self.tree_view.viewport().update()
        self.sidebar.viewport().update()
        self.settings.setValue("theme", theme_choice)

    def _on_theme_selected(self, theme_key: str):
        self.current_theme = theme_key
        self._apply_theme(theme_key)

    def _on_system_color_scheme_changed(self):
        if self.current_theme == "auto":
            self._apply_theme("auto")

    # -------------------------------------------------------------------------
    # Package Loading & Multi-Threaded Cache Reconciliation
    # -------------------------------------------------------------------------
    def _load_packages(self):
        if self.current_query_worker:
            self.current_query_worker.cancel()
        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()
        if self.current_userinstalled_worker:
            self.current_userinstalled_worker.cancel()
        if self.current_updates_worker:
            self.current_updates_worker.cancel()

        self.status_bar.showMessage("Reading system RPM package database & AppStream catalog...")

        self.current_query_worker = PackageQueryWorker(category="all", search_query="")
        self.current_query_worker.signals.packages_loaded.connect(self._on_packages_loaded)
        self.current_query_worker.signals.status_update.connect(self.status_bar.showMessage)
        self.current_query_worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(self.current_query_worker)

        self.current_userinstalled_worker = UserInstalledQueryWorker()
        self.current_userinstalled_worker.signals.userinstalled_loaded.connect(self._on_userinstalled_loaded)
        self.thread_pool.start(self.current_userinstalled_worker)

        self.current_orphan_worker = OrphanQueryWorker()
        self.current_orphan_worker.signals.orphans_loaded.connect(self._on_orphans_loaded)
        self.thread_pool.start(self.current_orphan_worker)

        self.current_updates_worker = SystemUpdatesCheckWorker()
        self.current_updates_worker.signals.system_updates_loaded.connect(self._on_updates_loaded)
        self.thread_pool.start(self.current_updates_worker)

    def _on_packages_loaded(self, packages: List[PackageInfo]):
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
                    p.available_update_version = f"{up.new_version}-{up.new_release}"
                    p.available_update_repo = up.repository

        self.tree_model.set_packages(packages)
        self._update_sidebar_counts(packages)
        self.status_bar.showMessage(f"Loaded {len(packages):,} packages successfully.")
        self.current_query_worker = None

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

    def _on_updates_loaded(self, updates_map: Dict[str, AvailableUpdateInfo]):
        self._pending_updates_map = updates_map
        self.tree_model.update_available_upgrades(updates_map)
        if self._all_packages_cache:
            for p in self._all_packages_cache:
                up = updates_map.get(p.name)
                if up:
                    p.has_update = True
                    p.available_update_version = f"{up.new_version}-{up.new_release}"
                    p.available_update_repo = up.repository
            self._update_sidebar_counts(self._all_packages_cache)

        count = len(updates_map)
        self.sidebar.update_category_counts({"updates_available": count})
        self.header.update_available_updates_badge(count)
        self.proxy_model.invalidateFilter()
        if count > 0:
            self.status_bar.showMessage(f"{count} software updates are available for your system.")
        self.current_updates_worker = None

    def _on_sidebar_category_selected(self, category: str):
        self.proxy_model.set_category_filter(category)
        is_orphans = (category == "orphans")
        is_updates = (category == "updates_available")
        is_storage = (category == "storage_audit")

        # Show contextual actions strictly in their relevant views
        self.header.set_orphan_clean_visible(is_orphans, len(self._orphan_cache))
        self.header.set_updates_view_active(is_updates, len(self._pending_updates_map))
        self.header.set_storage_audit_active(is_storage)

        # Automatic sort by size descending on storage audit selection
        if is_storage:
            self.tree_view.sortByColumn(DependencyTreeModel.COL_SIZE, Qt.SortOrder.DescendingOrder)
            total_bytes = sum(p.size_bytes for p in self._all_packages_cache if p.state == PackageState.INSTALLED)
            total_gb = total_bytes / (1024 ** 3)
            self.status_bar.showMessage(f"Storage Footprint: {total_gb:.2f} GB across {len(self._all_packages_cache):,} installed packages.")

    def _filter_to_updates(self):
        """Switches the view to show the list of available updates for review."""
        self.proxy_model.set_category_filter("updates_available")
        for row in range(self.sidebar.count()):
            item = self.sidebar.item(row)
            if item and item.data(Qt.ItemDataRole.UserRole) == "updates_available":
                self.sidebar.setCurrentRow(row)
                break

    def _on_system_upgrade_requested(self):
        """Executes full system upgrade (dnf5 upgrade) via Polkit elevation."""
        if not self._pending_updates_map:
            QMessageBox.information(self, "System Up to Date", "Your system is already up to date.")
            return

        up_count = len(self._pending_updates_map)
        reply = QMessageBox.question(
            self,
            "Upgrade System Packages",
            f"There are {up_count} package updates available for your system.\n\n"
            "Do you want to proceed with a full system upgrade ('dnf upgrade')?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.transaction_drawer.start_execution_mode()
            self.transaction_drawer.show()
            self.workspace_splitter.setSizes([450, 320])

            self.transaction_runner = PolkitTransactionRunner(self)
            self.transaction_runner.log_received.connect(self.transaction_drawer.append_log)
            self.transaction_runner.progress_percent.connect(self.transaction_drawer.set_progress)
            self.transaction_runner.transaction_finished.connect(self._on_transaction_finished)
            self.transaction_runner.execute_system_upgrade()

    def _on_clean_all_orphans_clicked(self):
        """Batch-removes all unneeded leaf dependencies (dnf5 autoremove)."""
        if not self._orphan_cache:
            QMessageBox.information(self, "No Leaf Orphans", "No unneeded leaf packages found.")
            return

        count = len(self._orphan_cache)
        reply = QMessageBox.question(
            self,
            "Clean Unneeded Leaf Packages",
            f"Are you sure you want to remove all {count} unneeded leaf packages?\n\n"
            "This will execute 'dnf autoremove' via Polkit elevation to free up disk space.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.transaction_drawer.start_execution_mode()
            self.transaction_drawer.show()
            self.workspace_splitter.setSizes([450, 320])

            self.transaction_runner = PolkitTransactionRunner(self)
            self.transaction_runner.log_received.connect(self.transaction_drawer.append_log)
            self.transaction_runner.progress_percent.connect(self.transaction_drawer.set_progress)
            self.transaction_runner.transaction_finished.connect(self._on_transaction_finished)
            self.transaction_runner.execute_autoremove()

    def _on_force_refresh_updates_clicked(self):
        """Forces live metadata download from remote mirrors (--refresh)."""
        if self.current_updates_worker is not None:
            return
        self.status_bar.showMessage("Bypassing cache and querying remote Fedora mirrors (--refresh)...")
        self.current_updates_worker = SystemUpdatesCheckWorker(force_refresh=True)
        self.current_updates_worker.signals.system_updates_loaded.connect(self._on_updates_loaded)
        self.thread_pool.start(self.current_updates_worker)

    def _on_clean_cache_clicked(self):
        """Frees disk space by clearing downloaded RPMs and expired repodata (dnf clean all)."""
        reply = QMessageBox.question(
            self,
            "Clean DNF Package Cache",
            "Are you sure you want to clean the DNF cache?\n\n"
            "This will remove all downloaded RPM packages and expired repository metadata from /var/cache/libdnf5.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.transaction_drawer.start_execution_mode()
            self.transaction_drawer.show()
            self.workspace_splitter.setSizes([450, 320])

            self.transaction_runner = PolkitTransactionRunner(self)
            self.transaction_runner.log_received.connect(self.transaction_drawer.append_log)
            self.transaction_runner.progress_percent.connect(self.transaction_drawer.set_progress)
            self.transaction_runner.transaction_finished.connect(self._on_transaction_finished)
            self.transaction_runner.execute_clean_cache()

    def _update_sidebar_counts(self, packages: List[PackageInfo]):
        counts: Dict[str, int] = {tag: 0 for _, tag, is_hdr in self.sidebar.CATEGORIES_CONFIG if tag}
        counts["all"] = len(packages)
        counts["updates_available"] = len(self._pending_updates_map)
        counts["storage_audit"] = sum(1 for p in packages if p.state == PackageState.INSTALLED)

        for p in packages:
            sub = getattr(p, "sub_category", "")
            if sub in counts:
                counts[sub] += 1

            pillar = getattr(p, "parent_pillar", "")
            if pillar in counts:
                counts[pillar] += 1

            if p.is_desktop_app:
                counts["user_apps"] += 1
            if p.is_cli_tool:
                counts["cli_tools"] += 1
            if p.is_system_settings:
                counts["system_settings"] += 1

            if p.is_graphics_driver:
                counts["graphics_drivers"] += 1
            if p.is_audio_sound:
                counts["audio_sound"] += 1
            if p.is_kernel_module:
                counts["kernel_modules"] += 1
            if p.is_firmware:
                counts["firmware"] += 1

            if p.is_fedora_core:
                counts["fedora_core"] += 1
            if p.is_systemd_service:
                counts["systemd_services"] += 1
            if p.is_security_pkg:
                counts["security_pkgs"] += 1
            if p.is_desktop_addon:
                counts["desktop_addons"] += 1

            if p.is_c_lib:
                counts["c_libs"] += 1
            if p.is_devel:
                counts["devel"] += 1
            if p.is_gui_toolkit:
                counts["gui_toolkits"] += 1
            if p.is_media_plugin:
                counts["media_plugins"] += 1
            if p.is_python_pkg:
                counts["python_pkgs"] += 1
            if p.is_rust_pkg:
                counts["rust_pkgs"] += 1
            if p.is_jvm_pkg:
                counts["jvm_pkgs"] += 1
            if p.is_nodejs_pkg:
                counts["nodejs_pkgs"] += 1
            if p.is_font:
                counts["fonts"] += 1
            if p.is_theme:
                counts["themes"] += 1
            if p.is_locale:
                counts["locales"] += 1

            if p.is_user_installed:
                counts["user_installed"] += 1
            if p.is_orphan:
                counts["orphans"] += 1

            repo_l = p.repository.lower()
            if "copr" in repo_l:
                counts["copr_repos"] += 1
            if "rpm fusion" in repo_l:
                counts["rpmfusion_repos"] += 1

        self.sidebar.update_category_counts(counts)

    def _on_query_error(self, pkg_name: str, message: str):
        self.status_bar.showMessage(f"Error: {message}")
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
        worker = ReverseDependencyWorker(target_package=pkg_name)
        worker.signals.reverse_dependencies_resolved.connect(self.inspector_panel.set_reverse_dependencies)
        worker.signals.reverse_dependencies_resolved.connect(self.tree_model.attach_reverse_dependencies)
        worker.signals.status_update.connect(self.status_bar.showMessage)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    def _on_inspect_files_requested(self, pkg_name: str):
        worker = PackageFilesWorker(package_name=pkg_name)
        worker.signals.package_files_loaded.connect(self.inspector_panel.set_package_files)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    def _on_fetch_changelog_requested(self, pkg_name: str):
        worker = PackageChangelogWorker(package_name=pkg_name)
        worker.signals.package_changelog_loaded.connect(self.inspector_panel.set_package_changelog)
        self.thread_pool.start(worker)

    def _on_verify_package_files_requested(self, pkg_name: str):
        worker = PackageVerifyWorker(package_name=pkg_name)
        worker.signals.package_verification_finished.connect(self.inspector_panel.set_package_verification)
        self.thread_pool.start(worker)

    # -------------------------------------------------------------------------
    # Software Repositories Manager Dialog
    # -------------------------------------------------------------------------
    def _open_repo_dialog(self):
        dlg = RepoManagerDialog(self)
        dlg.repo_toggle_requested.connect(self._on_repo_toggle_requested)
        dlg.enable_copr_requested.connect(self._on_enable_copr_requested)
        dlg.load_repositories()
        dlg.exec()

    def _on_repo_toggle_requested(self, repo_id: str, enable: bool):
        args = RepoManagerHelper.build_toggle_repo_args(repo_id, enable)
        self.transaction_drawer.start_execution_mode()
        self.transaction_drawer.show()
        self.workspace_splitter.setSizes([450, 320])

        self.transaction_runner = PolkitTransactionRunner(self)
        self.transaction_runner.log_received.connect(self.transaction_drawer.append_log)
        self.transaction_runner.transaction_finished.connect(self._on_transaction_finished)
        self.transaction_runner.execute_custom_command(args)

    def _on_enable_copr_requested(self, copr_spec: str):
        args = RepoManagerHelper.build_enable_copr_args(copr_spec)
        self.transaction_drawer.start_execution_mode()
        self.transaction_drawer.show()
        self.workspace_splitter.setSizes([450, 320])

        self.transaction_runner = PolkitTransactionRunner(self)
        self.transaction_runner.log_received.connect(self.transaction_drawer.append_log)
        self.transaction_runner.transaction_finished.connect(self._on_transaction_finished)
        self.transaction_runner.execute_custom_command(args)

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
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.tree_model.clear_all_queued()

    def _sync_queue_states(self):
        installs, removals, upgrades = self.tree_model.get_queued_packages()
        total_queued = len(installs) + len(removals) + len(upgrades)

        self.header.update_queue_badge(total_queued)
        current_counts = {
            "queued": total_queued,
            "orphans": len(self._orphan_cache)
        }
        self.sidebar.update_category_counts(current_counts)

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
            if state in (PackageState.INSTALLED, PackageState.AVAILABLE):
                action_text = "Queue Removal" if state == PackageState.INSTALLED else "Queue Installation"
                queue_act = QAction(action_text, self)
                queue_act.setIcon(QIcon.fromTheme("list-remove" if state == PackageState.INSTALLED else "list-add"))
                queue_act.triggered.connect(lambda: self.tree_model.toggle_queue_state(source_index))
                menu.addAction(queue_act)
            elif state in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE):
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

            menu.addSeparator()

        copy_name_act = QAction("Copy Package Name", self)
        copy_name_act.setIcon(QIcon.fromTheme("edit-copy"))
        copy_name_act.triggered.connect(lambda: self._copy_to_clipboard(item.name))
        menu.addAction(copy_name_act)

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

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
        self.status_bar.showMessage(f"Rolling back DNF Transaction #{trans_id}...")
        self.transaction_drawer.start_execution_mode()
        self.transaction_drawer.show()
        self.workspace_splitter.setSizes([450, 320])

        self.transaction_runner = PolkitTransactionRunner(self)
        self.transaction_runner.log_received.connect(self.transaction_drawer.append_log)
        self.transaction_runner.progress_percent.connect(self.transaction_drawer.set_progress)
        self.transaction_runner.transaction_finished.connect(self._on_transaction_finished)
        self.transaction_runner.execute_custom_command(["history", "undo", "-y", str(trans_id)])

    def _on_header_apply_clicked(self):
        installs, removals, upgrades = self.tree_model.get_queued_packages()
        if not installs and not removals and not upgrades:
            if self._pending_updates_map:
                self._on_system_upgrade_requested()
            return

        self.status_bar.showMessage("Simulating transaction impact (Dry-run)...")
        worker = TransactionDryRunWorker(
            to_install=installs,
            to_remove=removals,
            to_upgrade=upgrades
        )
        worker.signals.dry_run_finished.connect(self._on_dry_run_finished)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    def _on_dry_run_finished(self, result: DryRunSimulationResult):
        sim_dialog = DryRunSimulationDialog(result=result, parent=self)
        if sim_dialog.exec() == DryRunSimulationDialog.DialogCode.Accepted:
            installs, removals, upgrades = self.tree_model.get_queued_packages()
            self.transaction_drawer.set_transaction_preview(installs, removals)
            self.transaction_drawer.show()
            self.workspace_splitter.setSizes([450, 320])
            self._on_drawer_commit()

    def _close_transaction_drawer(self):
        self.transaction_drawer.hide()
        self.workspace_splitter.setSizes([750, 0])

    def _on_drawer_cancel(self):
        if (
            self.transaction_runner
            and self.transaction_runner.process
            and self.transaction_runner.process.state() == self.transaction_runner.process.ProcessState.Running
        ):
            self.transaction_runner.cancel_transaction()
        else:
            self._close_transaction_drawer()

    def _on_drawer_commit(self):
        installs, removals, upgrades = self.tree_model.get_queued_packages()
        if not installs and not removals and not upgrades:
            return

        self.transaction_drawer.start_execution_mode()
        self.status_bar.showMessage("Authenticating with Polkit...")

        self.transaction_runner = PolkitTransactionRunner(self)
        self.transaction_runner.log_received.connect(self.transaction_drawer.append_log)
        self.transaction_runner.progress_percent.connect(self.transaction_drawer.set_progress)
        self.transaction_runner.transaction_finished.connect(self._on_transaction_finished)
        self.transaction_runner.execute_transaction(installs, removals, upgrades)

    def _on_transaction_finished(self, success: bool, exit_code: int):
        self.transaction_drawer.finish_execution_mode(success)
        if success:
            self.status_bar.showMessage("Transaction completed successfully.")
            self._load_packages()
            self._sync_queue_states()
        else:
            self.status_bar.showMessage(f"Transaction failed or cancelled (Exit code: {exit_code}).")

    def closeEvent(self, event: QCloseEvent):
        if self.current_query_worker:
            self.current_query_worker.cancel()
        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()
        if self.current_userinstalled_worker:
            self.current_userinstalled_worker.cancel()
        if self.current_updates_worker:
            self.current_updates_worker.cancel()

        if hasattr(self, "bg_update_timer"):
            self.bg_update_timer.stop()

        if self.transaction_runner:
            self.transaction_runner.cancel_transaction()

        self.thread_pool.clear()
        self.thread_pool.waitForDone(1500)
        event.accept()
