# ui/main_window.py
"""
Primary Application Window and MVC Controller for the Fedora Package Manager.
Orchestrates background query workers, lazy tree loading, and Polkit transactions.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set
from PyQt6.QtCore import QModelIndex, QPoint, Qt, QThreadPool
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
    """Main Application Controller Window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fedora Package Tree (Dendro)")
        self.resize(1200, 800)
        self.setStyleSheet(MODERN_DARK_THEME)

        # Thread Pool & Worker Lifecycle Tracking
        self.thread_pool = QThreadPool.globalInstance()
        self.current_query_worker: Optional[PackageQueryWorker] = None
        self.current_orphan_worker: Optional[OrphanQueryWorker] = None
        self.active_dep_workers: Dict[str, DependencyTreeWorker] = {}
        self.transaction_runner: Optional[PolkitTransactionRunner] = None
        self._all_packages_cache: List[PackageInfo] = []

        self._init_ui()
        self._connect_signals()

        # Initial Package Loading
        self._load_packages()

    def _init_ui(self):
        # Root Central Container
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

        # 3. Category Sidebar
        self.sidebar = CategorySidebar()
        self.main_splitter.addWidget(self.sidebar)

        # 4. Central Workspace (Vertical Splitter: TreeView + Collapsible Transaction Drawer)
        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical)

        # 4a. Tree View Setup
        self.tree_view = QTreeView()
        self.tree_view.setObjectName("PackageTreeView")
        self.tree_model = DependencyTreeModel(self)
        self.proxy_model = PackageFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.tree_model)
        self.tree_view.setModel(self.proxy_model)

        # Attach custom delegate for badges and pill rendering
        self.tree_delegate = PackageTreeItemDelegate(self.tree_view)
        self.tree_view.setItemDelegate(self.tree_delegate)

        # Configure Tree Columns Layout
        self._configure_tree_columns()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.workspace_splitter.addWidget(self.tree_view)

        # 4b. Slide-Out Transaction Drawer
        self.transaction_drawer = TransactionDrawer()
        self.transaction_drawer.hide()
        self.workspace_splitter.addWidget(self.transaction_drawer)

        self.main_splitter.addWidget(self.workspace_splitter)

        # Splitter Layout Ratios
        self.main_splitter.setSizes([220, 980])
        self.workspace_splitter.setSizes([700, 0])

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

        self.tree_view.setColumnWidth(DependencyTreeModel.COL_NAME, 320)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_STATUS, 150)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_VERSION, 160)
        self.tree_view.setColumnWidth(DependencyTreeModel.COL_SIZE, 90)

    def _connect_signals(self):
        # Header Connections
        self.header.search_changed.connect(self.proxy_model.set_search_query)
        self.header.apply_clicked.connect(self._on_header_apply_clicked)

        # Sidebar Connections
        self.sidebar.category_selected.connect(self.proxy_model.set_category_filter)

        # Tree View Connections
        self.tree_view.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree_view.expanded.connect(self._on_tree_node_expanded)
        self.tree_model.queue_state_changed.connect(self._sync_queue_states)

        # Transaction Drawer Connections
        self.transaction_drawer.closed.connect(self._close_transaction_drawer)
        self.transaction_drawer.cancel_requested.connect(self._on_drawer_cancel)
        self.transaction_drawer.commit_requested.connect(self._on_drawer_commit)

    # -------------------------------------------------------------------------
    # Asynchronous Package & Orphan Loading
    # -------------------------------------------------------------------------
    def _load_packages(self):
        """Dispatches asynchronous workers to fetch RPM database and orphan packages in parallel."""
        if self.current_query_worker:
            self.current_query_worker.cancel()
        if self.current_orphan_worker:
            self.current_orphan_worker.cancel()

        self.status_bar.showMessage("Reading RPM package database...")
        self.current_query_worker = PackageQueryWorker(category="all", search_query="")
        self.current_query_worker.signals.packages_loaded.connect(self._on_packages_loaded)
        self.current_query_worker.signals.status_update.connect(self.status_bar.showMessage)
        self.current_query_worker.signals.error_occurred.connect(self._on_query_error)
        self.thread_pool.start(self.current_query_worker)

        # Start orphan scanning asynchronously in background
        self.current_orphan_worker = OrphanQueryWorker()
        self.current_orphan_worker.signals.orphans_loaded.connect(self._on_orphans_loaded)
        self.thread_pool.start(self.current_orphan_worker)

    def _on_packages_loaded(self, packages: List[PackageInfo]):
        self._all_packages_cache = packages
        self.tree_model.set_packages(packages)
        self._update_sidebar_counts(packages)
        self.status_bar.showMessage(f"Loaded {len(packages)} packages.")

    def _on_orphans_loaded(self, orphans: Set[str]):
        self.tree_model.update_orphans(orphans)
        self.sidebar.update_category_counts({"orphans": len(orphans)})

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

    def _on_query_error(self, message: str):
        self.status_bar.showMessage(f"Error: {message}")
        QMessageBox.critical(self, "Query Error", message)

    # -------------------------------------------------------------------------
    # Asynchronous Lazy-Loaded Dependency Resolution
    # -------------------------------------------------------------------------
    def _on_tree_node_expanded(self, proxy_index: QModelIndex):
        """Triggered when a user clicks expand on a tree branch."""
        source_index = self.proxy_model.mapToSource(proxy_index)
        if not source_index.isValid():
            return

        item: TreeItem = source_index.internalPointer()
        if not item.is_dependency and not item.dependencies_loaded and not item.is_loading_dependencies:
            pkg_name = item.name
            item.is_loading_dependencies = True
            self.status_bar.showMessage(f"Resolving dependencies for '{pkg_name}'...")

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
                queue_act.triggered.connect(lambda: self.tree_model.toggle_queue_state(source_index))
                menu.addAction(queue_act)
            elif state in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE):
                cancel_act = QAction("Cancel Pending Change", self)
                cancel_act.triggered.connect(lambda: self.tree_model.toggle_queue_state(source_index))
                menu.addAction(cancel_act)

            menu.addSeparator()

        copy_name_act = QAction("Copy Package Name", self)
        copy_name_act.triggered.connect(lambda: self._copy_to_clipboard(item.name))
        menu.addAction(copy_name_act)

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def _copy_to_clipboard(self, text: str):
        clipboard: Optional[QClipboard] = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(text)
            self.status_bar.showMessage(f"Copied '{text}' to clipboard.", 2500)

    def _sync_queue_states(self):
        """Updates header badge and sidebar queue counts."""
        installs, removals = self.tree_model.get_queued_packages()
        total_queued = len(installs) + len(removals)

        self.header.update_queue_badge(total_queued)

        current_counts = {
            "queued": total_queued,
            "orphans": sum(1 for item in self.tree_model.root_item.child_items if getattr(item.payload, "is_orphan", False))
        }
        self.sidebar.update_category_counts(current_counts)

    # -------------------------------------------------------------------------
    # Transaction Drawer & Polkit Execution
    # -------------------------------------------------------------------------
    def _on_header_apply_clicked(self):
        installs, removals = self.tree_model.get_queued_packages()
        if not installs and not removals:
            return

        self.transaction_drawer.set_transaction_preview(installs, removals)
        self.transaction_drawer.show()
        self.workspace_splitter.setSizes([450, 300])

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
    # Safe Cleanup on Window Close
    # -------------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent):
        """Ensures all background workers and Polkit subprocesses are cleanly terminated."""
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
