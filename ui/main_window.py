# dendro/ui/main_window.py
from __future__ import annotations

from typing import Dict, List, Optional, Set
from PyQt6.QtCore import QItemSelection, QPersistentModelIndex, QPoint, Qt, QThreadPool
from PyQt6.QtGui import QAction, QClipboard, QCloseEvent, QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QHeaderView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from core.backend import (
    DependencyNode,
    DependencyTreeWorker,
    DnfHistoryWorker,
    DryRunSimulationResult,
    HistoryEntry,
    OrphanQueryWorker,
    PackageFileInfo,
    PackageFilesWorker,
    PackageInfo,
    PackageQueryWorker,
    PackageState,
    PolkitTransactionRunner,
    ReverseDependencyWorker,
    TransactionDryRunWorker,
    UserInstalledQueryWorker,
)
from core.models import (
    DependencyTreeModel,
    PackageFilterProxyModel,
    TreeItem,
)
from ui.delegates import ModernTreeStyle, PackageTreeItemDelegate
from ui.dry_run_dialog import DryRunSimulationDialog
from ui.header import HeaderBar
from ui.history_dialog import DnfHistoryDialog
from ui.inspector_panel import PackageInspectorPanel
from ui.sidebar import CategorySidebar
from ui.styles import MODERN_DARK_THEME
from ui.transaction_drawer import TransactionDrawer


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fedora Package Tree & Dependency Inspector (Dendro)")
        self.resize(1380, 880)
        self.setStyleSheet(MODERN_DARK_THEME)

        self.thread_pool = QThreadPool.globalInstance()
        self.current_query_worker: Optional[PackageQueryWorker] = None
        self.current_orphan_worker: Optional[OrphanQueryWorker] = None
        self.current_userinstalled_worker: Optional[UserInstalledQueryWorker] = None
        self.active_dep_workers: Dict[str, DependencyTreeWorker] = {}
        self.transaction_runner: Optional[PolkitTransactionRunner] = None
        self._all_packages_cache: List[PackageInfo] = []

        self._init_ui()
        self._setup_shortcuts()
        self._connect_signals()
        self._load_packages()

    def _init_ui(self):
        root_widget = QWidget()
        self.setCentralWidget(root_widget)
        root_layout = QVBoxLayout(root_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # نوار بالایی
        self.header = HeaderBar()
        root_layout.addWidget(self.header)

        # اسپلیتر افقی اصلی (سایدبار | مرکز | پنل بازرس راست)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(self.main_splitter, stretch=1)

        # سایدبار سمت چپ
        self.sidebar = CategorySidebar()
        self.main_splitter.addWidget(self.sidebar)

        # فضای مرکزی (درخت پکیج‌ها + دراور لاگ پایین)
        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical)

        self.tree_style = ModernTreeStyle(self)
        self.tree_view = QTreeView()
        self.tree_view.setObjectName("PackageTreeView")
        self.tree_view.setStyle(self.tree_style)
        self.tree_view.setRootIsDecorated(True)
        self.tree_view.setIndentation(24)
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

        # دراور اجرای تراکنش‌ها در پایین
        self.transaction_drawer = TransactionDrawer()
        self.transaction_drawer.hide()
        self.workspace_splitter.addWidget(self.transaction_drawer)

        self.main_splitter.addWidget(self.workspace_splitter)

        # پنل بازرس مشخصات در سمت راست
        self.inspector_panel = PackageInspectorPanel()
        self.main_splitter.addWidget(self.inspector_panel)

        # تنظیم نسبت‌های اولیه اسپلیتر
        self.main_splitter.setSizes([260, 740, 380])
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
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_VERSION, 160)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_SIZE, 95)

    def _setup_shortcuts(self):
        """تعریف کلیدهای میانبر استاندارد کیبورد برای سرعت کار فوق‌العاده"""
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: self.header.search_input.setFocus())
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._load_packages)
        QShortcut(QKeySequence("Ctrl+H"), self, activated=self._open_history_dialog)
        QShortcut(QKeySequence("Ctrl+I"), self, activated=self._toggle_inspector_panel)
        QShortcut(QKeySequence("Space"), self, activated=self._toggle_queue_selected_row)

    def _connect_signals(self):
        # سیگنال‌های هدر
        self.header.search_changed.connect(self.proxy_model.set_search_query)
        self.header.apply_clicked.connect(self._on_header_apply_clicked)
        self.header.toggle_inspector_clicked.connect(self._toggle_inspector_panel)
        self.header.history_clicked.connect(self._open_history_dialog)

        # سیگنال‌های سایدبار
        self.sidebar.category_selected.connect(self.proxy_model.set_category_filter)

        # سیگنال‌های درخت و مدل
        self.tree_model.fetch_dependencies_requested.connect(self._on_fetch_dependencies_requested)
        self.tree_model.queue_state_changed.connect(self._sync_queue_states)
        self.tree_view.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree_view.selectionModel().selectionChanged.connect(self._on_tree_selection_changed)

        # سیگنال‌های پنل بازرس
        self.inspector_panel.closed.connect(lambda: self.inspector_panel.hide())
        self.inspector_panel.package_action_requested.connect(self._on_inspector_queue_action)
        self.inspector_panel.file_inspection_requested.connect(self._on_inspect_files_requested)
        self.inspector_panel.reverse_deps_requested.connect(self._on_fetch_reverse_deps_requested)

        # سیگنال‌های دراور تراکنش
        self.transaction_drawer.closed.connect(self._close_transaction_drawer)
        self.transaction_drawer.cancel_requested.connect(self._on_drawer_cancel)
        self.transaction_drawer.commit_requested.connect(self._on_drawer_commit)

    # -------------------------------------------------------------------------
    # بارگذاری اولیه و هماهنگی ورکرها
    # -------------------------------------------------------------------------
    def _load_packages(self):
        if self.current_query_worker:
            self.current_query_worker.cancel()
        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()
        if self.current_userinstalled_worker:
            self.current_userinstalled_worker.cancel()

        self.status_bar.showMessage("Reading system RPM package database...")
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

    def _on_packages_loaded(self, packages: List[PackageInfo]):
        self._all_packages_cache = packages
        self.tree_model.set_packages(packages)
        self._update_sidebar_counts(packages)
        self.status_bar.showMessage(f"Loaded {len(packages):,} packages.")
        self.current_query_worker = None

    def _on_userinstalled_loaded(self, user_pkgs: Set[str]):
        self.tree_model.update_user_installed(user_pkgs)
        self._update_sidebar_counts(self._all_packages_cache)
        self.current_userinstalled_worker = None

    def _on_orphans_loaded(self, orphans: Set[str]):
        self.tree_model.update_orphans(orphans)
        self.sidebar.update_category_counts({"orphans": len(orphans)})
        self.current_orphan_worker = None

    def _update_sidebar_counts(self, packages: List[PackageInfo]):
        counts = {
            "all": len(packages),
            "user_apps": sum(1 for p in packages if p.is_user_app),
            "cli_tools": sum(1 for p in packages if p.is_cli_tool),
            "fedora_core": sum(1 for p in packages if p.is_fedora_core),
            "python_pkgs": sum(1 for p in packages if p.is_python_pkg),
            "rust_pkgs": sum(1 for p in packages if p.is_rust_pkg),
            "jvm_pkgs": sum(1 for p in packages if p.is_jvm_pkg),
            "nodejs_pkgs": sum(1 for p in packages if p.is_nodejs_pkg),
            "kernel_modules": sum(1 for p in packages if p.is_kernel_module),
            "systemd_services": sum(1 for p in packages if p.is_systemd_service),
            "security_pkgs": sum(1 for p in packages if p.is_security_pkg),
            "c_libs": sum(1 for p in packages if p.is_c_lib),
            "firmware": sum(1 for p in packages if p.is_firmware),
            "fonts": sum(1 for p in packages if p.is_font),
            "locales": sum(1 for p in packages if p.is_locale),
            "devel": sum(1 for p in packages if p.is_devel),
            "themes": sum(1 for p in packages if p.is_theme),
            "orphans": sum(1 for p in packages if p.is_orphan),
            "copr_repos": sum(1 for p in packages if "copr" in p.repository.lower()),
            "rpmfusion_repos": sum(1 for p in packages if "rpm fusion" in p.repository.lower()),
            "queued": 0,
        }
        self.sidebar.update_category_counts(counts)

    def _on_query_error(self, pkg_name: str, message: str):
        self.status_bar.showMessage(f"Error: {message}")
        if pkg_name:
            self.tree_model.reset_loading_state(pkg_name)
            if pkg_name in self.active_dep_workers:
                del self.active_dep_workers[pkg_name]

    # -------------------------------------------------------------------------
    # پردازش وابستگی‌ها و فایل‌ها
    # -------------------------------------------------------------------------
    def _on_fetch_dependencies_requested(self, pkg_name: str, _target_index: QPersistentModelIndex):
        if pkg_name in self.active_dep_workers:
            return

        self.status_bar.showMessage(f"Resolving dependency graph for '{pkg_name}'...")
        worker = DependencyTreeWorker(root_package=pkg_name, max_depth=3)
        worker.signals.dependencies_resolved.connect(self._on_dependencies_resolved)
        worker.signals.status_update.connect(self.status_bar.showMessage)
        worker.signals.error_occurred.connect(self._on_query_error)

        self.active_dep_workers[pkg_name] = worker
        self.thread_pool.start(worker)

    def _on_dependencies_resolved(self, root_pkg_name: str, dependencies: List[DependencyNode]):
        self.tree_model.attach_dependencies(root_pkg_name, dependencies)
        if root_pkg_name in self.active_dep_workers:
            del self.active_dep_workers[root_pkg_name]

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

    # -------------------------------------------------------------------------
    # تعاملات کاربر، پنل بازرس و صف
    # -------------------------------------------------------------------------
    def _on_tree_selection_changed(self, selected: QItemSelection, _deselected: QItemSelection):
        indexes = selected.indexes()
        if not indexes:
            return

        proxy_idx = indexes[0]
        source_idx = self.proxy_model.mapToSource(proxy_idx)
        item: TreeItem = source_idx.internalPointer()

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

    def _sync_queue_states(self):
        installs, removals = self.tree_model.get_queued_packages()
        total_queued = len(installs) + len(removals)

        self.header.update_queue_badge(total_queued)
        current_counts = {
            "queued": total_queued,
            "orphans": sum(1 for item in self.tree_model.root_item.child_items if getattr(item.payload, "is_orphan", False))
        }
        self.sidebar.update_category_counts(current_counts)

    def _on_tree_context_menu(self, position: QPoint):
        proxy_index = self.tree_view.indexAt(position)
        if not proxy_index.isValid():
            return

        source_index = self.proxy_model.mapToSource(proxy_index)
        item: TreeItem = source_index.internalPointer()

        menu = QMenu(self)

        if not item.is_dependency:
            state = item.state
            if state in (PackageState.INSTALLED, PackageState.AVAILABLE):
                action_text = "Queue Removal" if state == PackageState.INSTALLED else "Queue Installation"
                queue_act = QAction(action_text, self)
                queue_act.triggered.connect(lambda: self.tree_model.toggle_queue_state(source_index))
                menu.addAction(queue_act)
            elif state in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE):
                cancel_act = QAction("Cancel Pending Change", self)
                cancel_act.triggered.connect(lambda: self.tree_model.toggle_queue_state(source_index))
                menu.addAction(cancel_act)

            menu.addSeparator()

            rev_deps_act = QAction("🔍 Show Reverse Dependents (What Requires This)", self)
            rev_deps_act.triggered.connect(lambda: self._on_fetch_reverse_deps_requested(item.name))
            menu.addAction(rev_deps_act)

            menu.addSeparator()

        copy_name_act = QAction("📋 Copy Package Name", self)
        copy_name_act.triggered.connect(lambda: self._copy_to_clipboard(item.name))
        menu.addAction(copy_name_act)

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def _copy_to_clipboard(self, text: str):
        clipboard: Optional[QClipboard] = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(text)
            self.status_bar.showMessage(f"Copied '{text}' to clipboard.", 2500)

    # -------------------------------------------------------------------------
    # تاریخچه DNF و شبیه‌سازی Dry-Run
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
        
        # اجرای rollback
        self.transaction_runner.log_received.emit(f"🔒 Requesting authorization to rollback Transaction #{trans_id}...\n")
        self.transaction_runner.execute_transaction([], [f"history undo {trans_id}"])

    def _on_header_apply_clicked(self):
        installs, removals = self.tree_model.get_queued_packages()
        if not installs and not removals:
            return

        self.status_bar.showMessage("Simulating transaction impact (Dry-run)...")
        worker = TransactionDryRunWorker(to_install=installs, to_remove=removals)
        worker.signals.dry_run_finished.connect(self._on_dry_run_finished)
        worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(worker)

    def _on_dry_run_finished(self, result: DryRunSimulationResult):
        sim_dialog = DryRunSimulationDialog(result=result, parent=self)
        if sim_dialog.exec() == DryRunSimulationDialog.DialogCode.Accepted:
            installs, removals = self.tree_model.get_queued_packages()
            self.transaction_drawer.set_transaction_preview(installs, removals)
            self.transaction_drawer.show()
            self.workspace_splitter.setSizes([450, 320])
            self._on_drawer_commit()

    def _close_transaction_drawer(self):
        self.transaction_drawer.hide()
        self.workspace_splitter.setSizes([750, 0])

    def _on_drawer_cancel(self):
        if self.transaction_runner and self.transaction_runner.process and self.transaction_runner.process.state() == self.transaction_runner.process.ProcessState.Running:
            self.transaction_runner.cancel_transaction()
        else:
            self._close_transaction_drawer()

    def _on_drawer_commit(self):
        installs, removals = self.tree_model.get_queued_packages()
        if not installs and not removals:
            return

        self.transaction_drawer.start_execution_mode()
        self.status_bar.showMessage("Authenticating with Polkit...")

        self.transaction_runner = PolkitTransactionRunner(self)
        self.transaction_runner.log_received.connect(self.transaction_drawer.append_log)
        self.transaction_runner.progress_percent.connect(self.transaction_drawer.set_progress)
        self.transaction_runner.transaction_finished.connect(self._on_transaction_finished)
        self.transaction_runner.execute_transaction(installs, removals)

    def _on_transaction_finished(self, success: bool, exit_code: int):
        self.transaction_drawer.finish_execution_mode(success)
        if success:
            self.status_bar.showMessage("Transaction completed successfully.")
            self._load_packages()
            self._sync_queue_states()
        else:
            self.status_bar.showMessage(f"Transaction failed or cancelled (Exit code: {exit_code}).")

    def closeEvent(self, event: QCloseEvent):
        """لغو تمام ورکرها برای خروج پایدار و تمیز"""
        if self.current_query_worker:
            self.current_query_worker.cancel()
        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()
        if self.current_userinstalled_worker:
            self.current_userinstalled_worker.cancel()

        for worker in list(self.active_dep_workers.values()):
            worker.cancel()
        self.active_dep_workers.clear()

        if self.transaction_runner:
            self.transaction_runner.cancel_transaction()

        self.thread_pool.clear()
        self.thread_pool.waitForDone(200)
        event.accept()
