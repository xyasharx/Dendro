# core/models.py
"""
High-Performance Model Layer for hierarchical dependency visualization and filtering.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Union
from PyQt6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont

from core.backend import DependencyNode, PackageInfo, PackageState


# Custom Data Roles for Delegates and Views
class CustomUserRoles:
    PackageInfoRole = Qt.ItemDataRole.UserRole + 1
    PackageStateRole = Qt.ItemDataRole.UserRole + 2
    IsDependencyRole = Qt.ItemDataRole.UserRole + 3
    IsOrphanRole = Qt.ItemDataRole.UserRole + 4
    RawSizeRole = Qt.ItemDataRole.UserRole + 5
    DependencyNodeRole = Qt.ItemDataRole.UserRole + 6
    IsCycleRole = Qt.ItemDataRole.UserRole + 7


class TreeItem:
    """
    Represents an item in the dependency tree.
    Holds references to parent/children and either a PackageInfo or DependencyNode.
    """

    def __init__(
        self,
        data_payload: Union[PackageInfo, DependencyNode, str],
        parent: Optional[TreeItem] = None,
        is_dependency: bool = False,
    ):
        self.parent_item: Optional[TreeItem] = parent
        self.child_items: List[TreeItem] = []
        self.payload: Union[PackageInfo, DependencyNode, str] = data_payload
        self.is_dependency: bool = is_dependency
        
        # State tracking for lazy loading
        self.dependencies_loaded: bool = False
        self.is_loading_dependencies: bool = False

    def append_child(self, child: TreeItem):
        child.parent_item = self
        self.child_items.append(child)

    def clear_children(self):
        self.child_items.clear()

    def child(self, row: int) -> Optional[TreeItem]:
        if 0 <= row < len(self.child_items):
            return self.child_items[row]
        return None

    def child_count(self) -> int:
        return len(self.child_items)

    def row(self) -> int:
        if self.parent_item:
            return self.parent_item.child_items.index(self)
        return 0

    @property
    def name(self) -> str:
        if isinstance(self.payload, PackageInfo):
            return self.payload.name
        elif isinstance(self.payload, DependencyNode):
            return self.payload.resolved_package_name
        return str(self.payload)

    @property
    def version(self) -> str:
        if isinstance(self.payload, PackageInfo):
            return self.payload.full_version
        elif isinstance(self.payload, DependencyNode):
            return self.payload.version_constraint or "satisfied"
        return ""

    @property
    def summary(self) -> str:
        if isinstance(self.payload, PackageInfo):
            return self.payload.summary
        elif isinstance(self.payload, DependencyNode):
            return f"Required by capability: {self.payload.raw_requirement}"
        return ""

    @property
    def size_str(self) -> str:
        if isinstance(self.payload, PackageInfo):
            return self.payload.human_size
        return ""

    @property
    def state(self) -> PackageState:
        if isinstance(self.payload, PackageInfo):
            return self.payload.state
        elif isinstance(self.payload, DependencyNode):
            return PackageState.INSTALLED if self.payload.is_satisfied else PackageState.MISSING
        return PackageState.AVAILABLE


class DependencyTreeModel(QAbstractItemModel):
    """
    QAbstractItemModel implementation for the hierarchical package/dependency tree.
    """

    # Columns
    COL_NAME = 0
    COL_STATUS = 1
    COL_VERSION = 2
    COL_SIZE = 3
    COL_SUMMARY = 4
    COL_COUNT = 5

    # Signals
    request_dependency_fetch = pyqtSignal(str)  # Emitted when a node needs async resolution
    queue_state_changed = pyqtSignal()          # Emitted when install/remove queues change

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.root_item = TreeItem("ROOT")
        self._package_lookup: Dict[str, TreeItem] = {}

    def clear(self):
        self.beginResetModel()
        self.root_item.clear_children()
        self._package_lookup.clear()
        self.endResetModel()

    def set_packages(self, packages: List[PackageInfo]):
        """Populates top-level packages into the model."""
        self.beginResetModel()
        self.root_item.clear_children()
        self._package_lookup.clear()

        for pkg in packages:
            item = TreeItem(data_payload=pkg, parent=self.root_item, is_dependency=False)
            self.root_item.append_child(item)
            self._package_lookup[pkg.name] = item

        self.endResetModel()

    def attach_dependencies(self, root_pkg_name: str, dependencies: List[DependencyNode]):
        """
        Attaches resolved dependency sub-trees to a top-level package node.
        """
        parent_item = self._package_lookup.get(root_pkg_name)
        if not parent_item:
            return

        parent_index = self.createIndex(parent_item.row(), 0, parent_item)
        
        # Begin structure modification
        self.beginResetModel()
        parent_item.clear_children()

        def build_branch(parent_node: TreeItem, dep_nodes: List[DependencyNode]):
            for dep in dep_nodes:
                child = TreeItem(data_payload=dep, parent=parent_node, is_dependency=True)
                child.dependencies_loaded = True
                parent_node.append_child(child)
                if dep.sub_dependencies:
                    build_branch(child, dep.sub_dependencies)

        build_branch(parent_item, dependencies)
        parent_item.dependencies_loaded = True
        parent_item.is_loading_dependencies = False
        self.endResetModel()

    def toggle_queue_state(self, index: QModelIndex):
        """Toggles between Queued for Install/Remove and default state."""
        if not index.isValid():
            return

        item: TreeItem = index.internalPointer()
        if isinstance(item.payload, PackageInfo):
            current_state = item.payload.state
            if current_state == PackageState.INSTALLED:
                item.payload.state = PackageState.QUEUED_REMOVE
            elif current_state == PackageState.QUEUED_REMOVE:
                item.payload.state = PackageState.INSTALLED
            elif current_state == PackageState.AVAILABLE:
                item.payload.state = PackageState.QUEUED_INSTALL
            elif current_state == PackageState.QUEUED_INSTALL:
                item.payload.state = PackageState.AVAILABLE

            # Notify views of data change
            top_left = self.index(item.row(), 0, index.parent())
            bottom_right = self.index(item.row(), self.COL_COUNT - 1, index.parent())
            self.dataChanged.emit(top_left, bottom_right, [Qt.ItemDataRole.DisplayRole, CustomUserRoles.PackageStateRole])
            self.queue_state_changed.emit()

    def get_queued_packages(self) -> tuple[List[str], List[str]]:
        """Returns ([installs], [removals])."""
        installs: List[str] = []
        removals: List[str] = []

        for item in self.root_item.child_items:
            if isinstance(item.payload, PackageInfo):
                if item.payload.state == PackageState.QUEUED_INSTALL:
                    installs.append(item.payload.name)
                elif item.payload.state == PackageState.QUEUED_REMOVE:
                    removals.append(item.payload.name)

        return installs, removals

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.column() > 0:
            return 0
        if not parent.isValid():
            parent_item = self.root_item
        else:
            parent_item = parent.internalPointer()
        return parent_item.child_count()

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return self.COL_COUNT

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            headers = ["Package / Dependency", "Status", "Version", "Size", "Summary"]
            if 0 <= section < len(headers):
                return headers[section]
        return None

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        if not parent.isValid():
            parent_item = self.root_item
        else:
            parent_item = parent.internalPointer()

        child_item = parent_item.child(row)
        if child_item:
            return self.createIndex(row, column, child_item)
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()

        child_item: TreeItem = index.internalPointer()
        parent_item: Optional[TreeItem] = child_item.parent_item

        if parent_item == self.root_item or parent_item is None:
            return QModelIndex()

        return self.createIndex(parent_item.row(), 0, parent_item)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None

        item: TreeItem = index.internalPointer()
        col = index.column()

        # Display Text
        if role == Qt.ItemDataRole.DisplayRole:
            if col == self.COL_NAME:
                return item.name
            elif col == self.COL_STATUS:
                state = item.state
                if state == PackageState.QUEUED_INSTALL:
                    return "Queued (Install)"
                elif state == PackageState.QUEUED_REMOVE:
                    return "Queued (Remove)"
                elif state == PackageState.INSTALLED:
                    return "Installed"
                elif state == PackageState.MISSING:
                    return "Missing Dependency"
                return "Available"
            elif col == self.COL_VERSION:
                return item.version
            elif col == self.COL_SIZE:
                return item.size_str
            elif col == self.COL_SUMMARY:
                return item.summary

        # Text Formatting / Dimming
        elif role == Qt.ItemDataRole.ForegroundRole:
            if item.state == PackageState.MISSING:
                return QColor("#f38ba8")  # Red
            if item.state in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE):
                return QColor("#fab387")  # Peach/Orange
            if item.is_dependency:
                return QColor("#a6adc8")  # Subdued gray-blue for dependencies

        # Font adjustments
        elif role == Qt.ItemDataRole.FontRole:
            if not item.is_dependency and col == self.COL_NAME:
                font = QFont()
                font.setBold(True)
                return font

        # Custom Payload Roles for Item Delegates
        elif role == CustomUserRoles.PackageStateRole:
            return item.state
        elif role == CustomUserRoles.IsDependencyRole:
            return item.is_dependency
        elif role == CustomUserRoles.IsOrphanRole:
            return getattr(item.payload, "is_orphan", False)
        elif role == CustomUserRoles.RawSizeRole:
            return getattr(item.payload, "size_bytes", 0)
        elif role == CustomUserRoles.PackageInfoRole:
            return item.payload if isinstance(item.payload, PackageInfo) else None
        elif role == CustomUserRoles.IsCycleRole:
            return getattr(item.payload, "is_cycle", False)

        return None


class PackageFilterProxyModel(QSortFilterProxyModel):
    """
    High-performance proxy model handling instantaneous filtering,
    category segmentation, and numerical size sorting.
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.setDynamicSortFilter(True)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._category: str = "all"
        self._search_term: str = ""

    def set_category_filter(self, category: str):
        self._category = category.lower()
        self.invalidateFilter()

    def set_search_query(self, search_term: str):
        self._search_term = search_term.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model: DependencyTreeModel = self.sourceModel() # type: ignore
        index_name = model.index(source_row, DependencyTreeModel.COL_NAME, source_parent)
        
        if not index_name.isValid():
            return False

        item: TreeItem = index_name.internalPointer()

        # If it's a child dependency node, accept if its parent was accepted
        if item.is_dependency:
            return True

        # Category Filter Evaluation
        if isinstance(item.payload, PackageInfo):
            pkg = item.payload
            if self._category == "installed" and pkg.state != PackageState.INSTALLED:
                return False
            elif self._category == "orphans" and not pkg.is_orphan:
                return False
            elif self._category == "development" and "Development" not in pkg.group:
                return False
            elif self._category == "system" and "System" not in pkg.group and "Base" not in pkg.group:
                return False
            elif self._category == "queued" and pkg.state not in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE):
                return False

        # Search Query Evaluation (Matches name or summary)
        if self._search_term:
            name_match = self._search_term in item.name.lower()
            summary_match = self._search_term in item.summary.lower()
            if not (name_match or summary_match):
                return False

        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        """Enables accurate numerical sorting for package byte sizes."""
        if left.column() == DependencyTreeModel.COL_SIZE:
            left_size = left.data(CustomUserRoles.RawSizeRole) or 0
            right_size = right.data(CustomUserRoles.RawSizeRole) or 0
            return left_size < right_size

        return super().lessThan(left, right)