# core/models.py
"""
High-Performance Model Layer for hierarchical dependency visualization and filtering.
Implements O(1) row lookups, non-destructive row insertions, and custom sort/filter proxies.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple, Union
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


class CustomUserRoles:
    """Custom Qt User Roles for specialized painting and filtering."""
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
    Holds explicit row index to achieve O(1) row resolution during Qt paints.
    """
    __slots__ = (
        "parent_item",
        "child_items",
        "payload",
        "is_dependency",
        "dependencies_loaded",
        "is_loading_dependencies",
        "_row",
    )

    def __init__(
        self,
        data_payload: Union[PackageInfo, DependencyNode, str],
        parent: Optional[TreeItem] = None,
        is_dependency: bool = False,
        row: int = 0,
    ):
        self.parent_item: Optional[TreeItem] = parent
        self.child_items: List[TreeItem] = []
        self.payload: Union[PackageInfo, DependencyNode, str] = data_payload
        self.is_dependency: bool = is_dependency
        self.dependencies_loaded: bool = False
        self.is_loading_dependencies: bool = False
        self._row: int = row

    def append_child(self, child: TreeItem):
        child.parent_item = self
        child._row = len(self.child_items)
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
        """O(1) complexity instead of O(N) list.index lookup."""
        return self._row

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
            return f"Required by: {self.payload.raw_requirement}"
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
    """QAbstractItemModel implementation for the hierarchical package/dependency tree."""

    COL_NAME = 0
    COL_STATUS = 1
    COL_VERSION = 2
    COL_SIZE = 3
    COL_SUMMARY = 4
    COL_COUNT = 5

    queue_state_changed = pyqtSignal()

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
        """Populates top-level packages in a single batch reset with O(1) indexed rows."""
        self.beginResetModel()
        self.root_item.clear_children()
        self._package_lookup.clear()

        for idx, pkg in enumerate(packages):
            item = TreeItem(data_payload=pkg, parent=self.root_item, is_dependency=False, row=idx)
            self.root_item.child_items.append(item)
            self._package_lookup[pkg.name] = item

        self.endResetModel()

    def update_orphans(self, orphan_names: Set[str]):
        """Updates orphan states with targeted dataChanged signals."""
        for i, item in enumerate(self.root_item.child_items):
            if isinstance(item.payload, PackageInfo):
                is_orphan = item.payload.name in orphan_names
                if item.payload.is_orphan != is_orphan:
                    item.payload.is_orphan = is_orphan
                    left_idx = self.index(i, 0)
                    right_idx = self.index(i, self.COL_COUNT - 1)
                    self.dataChanged.emit(
                        left_idx,
                        right_idx,
                        [CustomUserRoles.IsOrphanRole, Qt.ItemDataRole.DisplayRole]
                    )

    def attach_dependencies(self, root_pkg_name: str, dependencies: List[DependencyNode]):
        """Attaches resolved dependencies cleanly using non-destructive row insertion."""
        parent_item = self._package_lookup.get(root_pkg_name)
        if not parent_item:
            return

        parent_index = self.createIndex(parent_item.row(), 0, parent_item)

        if parent_item.child_count() > 0:
            self.beginRemoveRows(parent_index, 0, parent_item.child_count() - 1)
            parent_item.clear_children()
            self.endRemoveRows()

        if not dependencies:
            parent_item.dependencies_loaded = True
            parent_item.is_loading_dependencies = False
            return

        def build_branch(parent_node: TreeItem, dep_nodes: List[DependencyNode]):
            for dep in dep_nodes:
                child = TreeItem(
                    data_payload=dep,
                    parent=parent_node,
                    is_dependency=True,
                    row=parent_node.child_count()
                )
                child.dependencies_loaded = True
                parent_node.append_child(child)
                if dep.sub_dependencies:
                    build_branch(child, dep.sub_dependencies)

        self.beginInsertRows(parent_index, 0, len(dependencies) - 1)
        build_branch(parent_item, dependencies)
        parent_item.dependencies_loaded = True
        parent_item.is_loading_dependencies = False
        self.endInsertRows()

    def reset_loading_state(self, root_pkg_name: str):
        """Recovers loading state in case of backend query worker exceptions."""
        parent_item = self._package_lookup.get(root_pkg_name)
        if parent_item:
            parent_item.is_loading_dependencies = False

    def toggle_queue_state(self, index: QModelIndex):
        """Toggles package state between Queued for Install/Remove and default."""
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

            top_left = self.index(item.row(), 0, index.parent())
            bottom_right = self.index(item.row(), self.COL_COUNT - 1, index.parent())
            self.dataChanged.emit(
                top_left,
                bottom_right,
                [Qt.ItemDataRole.DisplayRole, CustomUserRoles.PackageStateRole]
            )
            self.queue_state_changed.emit()

    def get_queued_packages(self) -> Tuple[List[str], List[str]]:
        """Returns lists of pending packages: ([installs], [removals])."""
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
                mapping = {
                    PackageState.QUEUED_INSTALL: "Queued (Install)",
                    PackageState.QUEUED_REMOVE: "Queued (Remove)",
                    PackageState.INSTALLED: "Installed",
                    PackageState.MISSING: "Missing Dependency",
                    PackageState.AVAILABLE: "Available"
                }
                return mapping.get(state, "Unknown")
            elif col == self.COL_VERSION:
                return item.version
            elif col == self.COL_SIZE:
                return item.size_str
            elif col == self.COL_SUMMARY:
                return item.summary

        # Text Foreground
        elif role == Qt.ItemDataRole.ForegroundRole:
            if item.state == PackageState.MISSING:
                return QColor("#f38ba8")
            if item.state in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE):
                return QColor("#fab387")
            if item.is_dependency:
                return QColor("#a6adc8")

        # Typography
        elif role == Qt.ItemDataRole.FontRole:
            if not item.is_dependency and col == self.COL_NAME:
                font = QFont()
                font.setBold(True)
                return font

        # Custom Item Roles
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
        model: DependencyTreeModel = self.sourceModel()  # type: ignore
        index_name = model.index(source_row, DependencyTreeModel.COL_NAME, source_parent)

        if not index_name.isValid():
            return False

        item: TreeItem = index_name.internalPointer()

        # Child dependencies are accepted if their parent branch is visible
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

        # Search Term Evaluation
        if self._search_term:
            name_match = self._search_term in item.name.lower()
            summary_match = self._search_term in item.summary.lower()
            if not (name_match or summary_match):
                return False

        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        """Enables accurate numerical sorting for raw byte sizes."""
        if left.column() == DependencyTreeModel.COL_SIZE:
            left_size = left.data(CustomUserRoles.RawSizeRole) or 0
            right_size = right.data(CustomUserRoles.RawSizeRole) or 0
            return left_size < right_size

        return super().lessThan(left, right)
