# dendro/ui/main_window.py
"""
Primary Application Window and MVC Controller for Dendro (Fedora Package Manager).

Coordinates asynchronous RPM workers, idiomatic Qt Model-View data fetching,
and privileged Polkit execution with zero UI blocking.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set
from PyQt6.QtCore import QModelIndex, QPersistentModelIndex, QPoint, Qt, QThreadPool
from PyQt6.QtGui import QAction, QClipboard, QCloseEvent, QGuiApplication
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
    OrphanQueryWorker,
    PackageInfo,
    PackageQueryWorker,
    PackageState,
    PolkitTransactionRunner,
)
from core.models import (
    DependencyTreeModel,
    PackageFilterProxyModel,
    TreeItem,
)
from ui.delegates import PackageTreeItemDelegate
from ui.header import HeaderBar
from ui.sidebar import CategorySidebar
from ui.styles import MODERN_DARK_THEME
from ui.transaction_drawer import TransactionDrawer


class MainWindow(QMainWindow):
    """Main Application Window & Central MVC Controller."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fedora Package Tree (Dendro)")
        self.resize(1240, 820)
        self.setStyleSheet(MODERN_DARK_THEME)

        # Thread Pool & Worker Management
        self.thread_pool = QThreadPool.globalInstance()
        self.current_query_worker: Optional[PackageQueryWorker] = None
        self.current_orphan_worker: Optional[OrphanQueryWorker] = None
        self.active_dep_workers: Dict[str, DependencyTreeWorker] = {}
        self.transaction_runner: Optional[PolkitTransactionRunner] = None
        self._all_packages_cache: List[PackageInfo] = []

        self._init_ui()
        self._connect_signals()
        self._load_packages()

    def _init_ui(self):
        root_widget = QWidget()
        self.setCentralWidget(root_widget)
        root_layout = QVBoxLayout(root_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Top Header Bar
        self.header = HeaderBar()
        root_layout.addWidget(self.header)

        # 2. Main Horizontal Splitter (Sidebar + Central Workspace)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(self.main_splitter, stretch=1)

        # 3. Category Navigation Sidebar
        self.sidebar = CategorySidebar()
        self.main_splitter.addWidget(self.sidebar)

        # 4. Central Workspace (Vertical Splitter: TreeView + Collapsible Transaction Drawer)
        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical)

        # 4a. Package Tree View Setup
        self.tree_view = QTreeView()
        self.tree_view.setObjectName("PackageTreeView")
        self.tree_model = DependencyTreeModel(self)
        self.proxy_model = PackageFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.tree_model)
        self.tree_view.setModel(self.proxy_model)

        # Attach custom delegate for zero-allocation pill & badge painting
        self.tree_delegate = PackageTreeItemDelegate(self.tree_view)
        self.tree_view.setItemDelegate(self.tree_delegate)

        self._configure_tree_columns()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.workspace_splitter.addWidget(self.tree_view)

        # 4b. Slide-out Polkit Transaction Drawer
        self.transaction_drawer = TransactionDrawer()
        self.transaction_drawer.hide()
        self.workspace_splitter.addWidget(self.transaction_drawer)

        self.main_splitter.addWidget(self.workspace_splitter)

        # Configure Splitter Layout Ratios
        self.main_splitter.setSizes([220, 1020])
        self.workspace_splitter.setSizes([720, 0])

        # 5. Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready.")

    def _configure_tree_columns(self):
        header = self.tree_view.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(DependencyTreeModel.COL_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_STATUS, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_VERSION, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_SIZE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DependencyTreeModel.COL_SUMMARY, QHeaderView.ResizeMode.Stretch)

        self.tree_view.setColumnWidth(DependencyTreeModel.COL_NAME, 340)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_STATUS, 150)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_VERSION, 170)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_SIZE, 95)

    def _connect_signals(self):
        # Header Connections
        self.header.search_changed.connect(self.proxy_model.set_search_query)
        self.header.apply_clicked.connect(self._on_header_apply_clicked)

        # Sidebar Connections
        self.sidebar.category_selected.connect(self.proxy_model.set_category_filter)

        # Model Connections (Pure MVC Lazy Loading Pattern)
        self.tree_model.fetch_dependencies_requested.connect(self._on_fetch_dependencies_requested)
        self.tree_model.queue_state_changed.connect(self._sync_queue_states)

        # Tree View Context Menu
        self.tree_view.customContextMenuRequested.connect(self._on_tree_context_menu)

        # Transaction Drawer Connections
        self.transaction_drawer.closed.connect(self._close_transaction_drawer)
        self.transaction_drawer.cancel_requested.connect(self._on_drawer_cancel)
        self.transaction_drawer.commit_requested.connect(self._on_drawer_commit)

    # -------------------------------------------------------------------------
    # Asynchronous Database Queries
    # -------------------------------------------------------------------------
    def _load_packages(self):
        """Dispatches parallel background workers for database loading & orphan scanning."""
        if self.current_query_worker:
            self.current_query_worker.cancel()
        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()

        self.status_bar.showMessage("Reading system RPM package database...")
        self.current_query_worker = PackageQueryWorker(category="all", search_query="")
        self.current_query_worker.signals.packages_loaded.connect(self._on_packages_loaded)
        self.current_query_worker.signals.status_update.connect(self.status_bar.showMessage)
        self.current_query_worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(self.current_query_worker)

        # Background orphan scanner
        self.current_orphan_worker = OrphanQueryWorker()
        self.current_orphan_worker.signals.orphans_loaded.connect(self._on_orphans_loaded)
        self.thread_pool.start(self.current_orphan_worker)

    def _on_packages_loaded(self, packages: List[PackageInfo]):
        self._all_packages_cache = packages
        self.tree_model.set_packages(packages)
        self._update_sidebar_counts(packages)
        self.status_bar.showMessage(f"Loaded {len(packages)} packages.")
        self.current_query_worker = None

    def _on_orphans_loaded(self, orphans: Set[str]):
        self.tree_model.update_orphans(orphans)
        self.sidebar.update_category_counts({"orphans": len(orphans)})
        self.current_orphan_worker = None

    def _update_sidebar_counts(self, packages: List[PackageInfo]):
        counts = {
            "all": len(packages),
            "installed": sum(1 for p in packages if p.state == PackageState.INSTALLED),
            "development": sum(1 for p in packages if "Development" in p.group),
            "system": sum(1 for p in packages if "System" in p.group or "Base" in p.group),
            "orphans": sum(1 for p in packages if p.is_orphan),
            "queued": 0,
        }
        self.sidebar.update_category_counts(counts)

    def _on_query_error(self, pkg_name: str, message: str):
        """Safely handles query worker failures."""
        self.status_bar.showMessage(f"Error: {message}")
        if pkg_name:
            self.tree_model.reset_loading_state(pkg_name)
            if pkg_name in self.active_dep_workers:
                del self.active_dep_workers[pkg_name]

        QMessageBox.critical(self, "Query Error", message)

    # -------------------------------------------------------------------------
    # Asynchronous DAG Dependency Resolution (Model Triggered)
    # -------------------------------------------------------------------------
    def _on_fetch_dependencies_requested(self, pkg_name: str, _target_index: QPersistentModelIndex):
        """Triggered automatically by the Qt Model when fetchMore is executed."""
        if pkg_name in self.active_dep_workers:
            return  # Already resolving

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

    # -------------------------------------------------------------------------
    # Context Menu & Queue Interactions
    # -------------------------------------------------------------------------
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
                queue_act.triggered.connect(
                    lambda checked=False, idx=source_index: self.tree_model.toggle_queue_state(idx)
                )
                menu.addAction(queue_act)
            elif state in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE):
                cancel_act = QAction("Cancel Pending Change", self)
                cancel_act.triggered.connect(
                    lambda checked=False, idx=source_index: self.tree_model.toggle_queue_state(idx)
                )
                menu.addAction(cancel_act)

            menu.addSeparator()

        copy_name_act = QAction("Copy Package Name", self)
        copy_name_act.triggered.connect(lambda checked=False, text=item.name: self._copy_to_clipboard(text))
        menu.addAction(copy_name_act)

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def _copy_to_clipboard(self, text: str):
        clipboard: Optional[QClipboard] = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(text)
            self.status_bar.showMessage(f"Copied '{text}' to clipboard.", 2500)

    def _sync_queue_states(self):
        """Synchronizes header badges and sidebar counts with model state."""
        installs, removals = self.tree_model.get_queued_packages()
        total_queued = len(installs) + len(removals)

        self.header.update_queue_badge(total_queued)

        current_counts = {
            "queued": total_queued,
            "orphans": sum(1 for item in self.tree_model.root_item.child_items if getattr(item.payload, "is_orphan", False))
        }
        self.sidebar.update_category_counts(current_counts)

    # -------------------------------------------------------------------------
    # Polkit Execution & Transaction Drawer
    # -------------------------------------------------------------------------
    def _on_header_apply_clicked(self):
        installs, removals = self.tree_model.get_queued_packages()
        if not installs and not removals:
            return

        self.transaction_drawer.set_transaction_preview(installs, removals)
        self.transaction_drawer.show()
        self.workspace_splitter.setSizes([450, 320])

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

    # -------------------------------------------------------------------------
    # Graceful Shutdown
    # -------------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent):
        """Ensures all background workers and Polkit subprocesses cleanly exit."""
        if self.current_query_worker:
            self.current_query_worker.cancel()

        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()

        for worker in self.active_dep_workers.values():
            worker.cancel()

        if self.transaction_runner:
            self.transaction_runner.cancel_transaction()

        self.thread_pool.waitForDone(1000)
        event.accept()
