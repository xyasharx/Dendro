# dendro/core/models.py
from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional, Set, Tuple, Union
from PyQt6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QColor, QFont

from core.backend import DependencyNode, PackageInfo, PackageState


# =============================================================================
# نقش‌های داده‌ای سفارشی کیوت (Custom User Roles)
# =============================================================================

class CustomUserRoles:
    PackageInfoRole: Final[int] = Qt.ItemDataRole.UserRole + 1
    PackageStateRole: Final[int] = Qt.ItemDataRole.UserRole + 2
    IsDependencyRole: Final[int] = Qt.ItemDataRole.UserRole + 3
    IsOrphanRole: Final[int] = Qt.ItemDataRole.UserRole + 4
    RawSizeRole: Final[int] = Qt.ItemDataRole.UserRole + 5
    DependencyNodeRole: Final[int] = Qt.ItemDataRole.UserRole + 6
    IsCycleRole: Final[int] = Qt.ItemDataRole.UserRole + 7
    IsReverseDepRole: Final[int] = Qt.ItemDataRole.UserRole + 8


# =============================================================================
# گره درخت داده‌ها (TreeItem)
# =============================================================================

class TreeItem:
    __slots__ = (
        "parent_item",
        "child_items",
        "payload",
        "is_dependency",
        "is_reverse_dep",
        "dependencies_loaded",
        "is_loading_dependencies",
        "_row",
    )

    def __init__(
        self,
        data_payload: Union[PackageInfo, DependencyNode, str],
        parent: Optional[TreeItem] = None,
        is_dependency: bool = False,
        is_reverse_dep: bool = False,
        row: int = 0,
    ):
        self.parent_item: Optional[TreeItem] = parent
        self.child_items: List[TreeItem] = []
        self.payload: Union[PackageInfo, DependencyNode, str] = data_payload
        self.is_dependency: bool = is_dependency
        self.is_reverse_dep: bool = is_reverse_dep
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
        return self._row

    def get_root_package_item(self) -> Optional[TreeItem]:
        curr: TreeItem = self
        while curr.parent_item is not None and curr.parent_item.parent_item is not None:
            curr = curr.parent_item
        return curr

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
            if self.is_reverse_dep:
                return "dependent"
            return self.payload.version_constraint or "satisfied"
        return ""

    @property
    def summary(self) -> str:
        if isinstance(self.payload, PackageInfo):
            return self.payload.summary
        elif isinstance(self.payload, DependencyNode):
            if self.is_reverse_dep:
                return f"Depends on: {self.payload.raw_requirement}"
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


# =============================================================================
# مدل اصلی درخت وابستگی‌ها (DependencyTreeModel)
# =============================================================================

class DependencyTreeModel(QAbstractItemModel):
    COL_NAME: Final[int] = 0
    COL_STATUS: Final[int] = 1
    COL_VERSION: Final[int] = 2
    COL_SIZE: Final[int] = 3
    COL_SUMMARY: Final[int] = 4
    COL_COUNT: Final[int] = 5

    queue_state_changed = pyqtSignal()
    fetch_dependencies_requested = pyqtSignal(str, QPersistentModelIndex)
    fetch_reverse_dependencies_requested = pyqtSignal(str, QPersistentModelIndex)

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
        self.beginResetModel()
        self.root_item.clear_children()
        self._package_lookup.clear()

        for idx, pkg in enumerate(packages):
            item = TreeItem(data_payload=pkg, parent=self.root_item, is_dependency=False, row=idx)
            self.root_item.child_items.append(item)
            self._package_lookup[pkg.name] = item

        self.endResetModel()

    def update_orphans(self, orphan_names: Set[str]):
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

    def update_user_installed(self, user_installed_names: Set[str]):
        for item in self.root_item.child_items:
            if isinstance(item.payload, PackageInfo):
                if item.payload.name in user_installed_names and not item.payload.is_library and not item.payload.is_cli_tool:
                    item.payload.is_desktop_app = True
        self.layoutChanged.emit()

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        if not parent.isValid():
            return self.root_item.child_count() > 0

        item: TreeItem = parent.internalPointer()
        if not item.dependencies_loaded:
            return True
        return item.child_count() > 0

    def canFetchMore(self, parent: QModelIndex) -> bool:
        if not parent.isValid():
            return False

        item: TreeItem = parent.internalPointer()
        return (not item.dependencies_loaded) and (not item.is_loading_dependencies)

    def fetchMore(self, parent: QModelIndex):
        if not parent.isValid():
            return

        item: TreeItem = parent.internalPointer()
        if not item.dependencies_loaded and not item.is_loading_dependencies:
            item.is_loading_dependencies = True
            self.fetch_dependencies_requested.emit(item.name, QPersistentModelIndex(parent))

    @pyqtSlot(str, list, object)
    def attach_dependencies(
        self,
        pkg_name: str,
        dependencies: List[DependencyNode],
        target_index: Optional[QPersistentModelIndex] = None
    ):
        parent_item: Optional[TreeItem] = None
        parent_index = QModelIndex()

        if target_index is not None and target_index.isValid():
            parent_index = QModelIndex(target_index)
            parent_item = parent_index.internalPointer()
        else:
            parent_item = self._package_lookup.get(pkg_name)
            if parent_item:
                parent_index = self.createIndex(parent_item.row(), 0, parent_item)

        if not parent_item:
            return

        if parent_item.child_count() > 0:
            self.beginRemoveRows(parent_index, 0, parent_item.child_count() - 1)
            parent_item.clear_children()
            self.endRemoveRows()

        parent_item.dependencies_loaded = True
        parent_item.is_loading_dependencies = False

        if not dependencies:
            self.layoutChanged.emit()
            return

        self.beginInsertRows(parent_index, 0, len(dependencies) - 1)
        for idx, dep in enumerate(dependencies):
            child = TreeItem(
                data_payload=dep,
                parent=parent_item,
                is_dependency=True,
                is_reverse_dep=dep.is_reverse,
                row=idx
            )
            parent_item.append_child(child)
        self.endInsertRows()

    @pyqtSlot(str, list)
    def attach_reverse_dependencies(self, root_pkg_name: str, reverse_deps: List[DependencyNode]):
        self.attach_dependencies(root_pkg_name, reverse_deps)

    def reset_loading_state(self, root_pkg_name: str):
        parent_item = self._package_lookup.get(root_pkg_name)
        if parent_item:
            parent_item.is_loading_dependencies = False

    def toggle_queue_state(self, index: QModelIndex):
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
            headers = ["Package / Capability", "Status", "Version / Constraint", "Size", "Summary"]
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

        if role == Qt.ItemDataRole.DisplayRole:
            if col == self.COL_NAME:
                return item.name
            elif col == self.COL_STATUS:
                if item.is_reverse_dep:
                    return "Required By"
                mapping = {
                    PackageState.QUEUED_INSTALL: "Queued (Install)",
                    PackageState.QUEUED_REMOVE: "Queued (Remove)",
                    PackageState.INSTALLED: "Installed",
                    PackageState.MISSING: "Missing Dependency",
                    PackageState.AVAILABLE: "Available"
                }
                return mapping.get(item.state, "Unknown")
            elif col == self.COL_VERSION:
                return item.version
            elif col == self.COL_SIZE:
                return item.size_str
            elif col == self.COL_SUMMARY:
                return item.summary

        elif role == Qt.ItemDataRole.ForegroundRole:
            if item.state == PackageState.MISSING:
                return QColor("#f38ba8")
            if item.state in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE):
                return QColor("#fab387")
            if item.is_dependency:
                return QColor("#a6adc8")

        elif role == Qt.ItemDataRole.FontRole:
            if not item.is_dependency and col == self.COL_NAME:
                font = QFont()
                font.setBold(True)
                return font

        elif role == CustomUserRoles.PackageStateRole:
            return item.state
        elif role == CustomUserRoles.IsDependencyRole:
            return item.is_dependency
        elif role == CustomUserRoles.IsReverseDepRole:
            return item.is_reverse_dep
        elif role == CustomUserRoles.IsOrphanRole:
            return getattr(item.payload, "is_orphan", False)
        elif role == CustomUserRoles.RawSizeRole:
            return getattr(item.payload, "size_bytes", 0)
        elif role == CustomUserRoles.PackageInfoRole:
            return item.payload if isinstance(item.payload, PackageInfo) else None
        elif role == CustomUserRoles.IsCycleRole:
            return getattr(item.payload, "is_cycle", False)

        return None


# =============================================================================
# پروکسی فیلتر و موتور جستجو و مرتب‌سازی پیشرفته (PackageFilterProxyModel)
# =============================================================================

class PackageFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.setDynamicSortFilter(True)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setRecursiveFilteringEnabled(True)
        self._category: str = "user_apps"
        self._search_term: str = ""

    def set_category_filter(self, category: str):
        self._category = category.lower()
        self.invalidateFilter()

    def set_search_query(self, search_term: str):
        self._search_term = search_term.strip()
        self.invalidateFilter()

    def _matches_category(self, pkg: PackageInfo) -> bool:
        cat = self._category
        if cat == "all":
            return True
        elif cat == "user_apps":
            return pkg.is_desktop_app
        elif cat == "cli_tools":
            return pkg.is_cli_tool
        elif cat == "fedora_core":
            return pkg.is_fedora_core
        elif cat == "python_pkgs":
            return pkg.is_python_pkg
        elif cat == "rust_pkgs":
            return pkg.is_rust_pkg
        elif cat == "jvm_pkgs":
            return pkg.is_jvm_pkg
        elif cat == "nodejs_pkgs":
            return pkg.is_nodejs_pkg
        elif cat == "kernel_modules":
            return pkg.is_kernel_module
        elif cat == "systemd_services":
            return pkg.is_systemd_service
        elif cat == "security_pkgs":
            return pkg.is_security_pkg
        elif cat == "c_libs":
            return pkg.is_c_lib
        elif cat == "firmware":
            return pkg.is_firmware
        elif cat == "fonts":
            return pkg.is_font
        elif cat == "locales":
            return pkg.is_locale
        elif cat == "devel":
            return pkg.is_devel
        elif cat == "themes":
            return pkg.is_theme
        elif cat == "orphans":
            return pkg.is_orphan
        elif cat == "queued":
            return pkg.state in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE)
        elif cat == "copr_repos":
            return "copr" in pkg.repository.lower()
        elif cat == "rpmfusion_repos":
            return "rpm fusion" in pkg.repository.lower()
        return True

    def _parse_size_constraint(self, val_str: str) -> Optional[Tuple[str, int]]:
        match = re.match(r'^([><]=?|=)\s*(\d+(?:\.\d+)?)\s*([kmgtp]?b?)$', val_str.lower())
        if not match:
            return None
        op, num_str, unit = match.groups()
        num = float(num_str)
        multiplier = 1
        if unit.startswith('k'):
            multiplier = 1024
        elif unit.startswith('m'):
            multiplier = 1024 * 1024
        elif unit.startswith('g'):
            multiplier = 1024 * 1024 * 1024
        elif unit.startswith('t'):
            multiplier = 1024 * 1024 * 1024 * 1024
        return op, int(num * multiplier)

    def _matches_search(self, item: TreeItem, root_pkg: PackageInfo) -> bool:
        if not self._search_term:
            return True

        tokens = self._search_term.split()
        for token in tokens:
            if ":" in token:
                key, val = token.split(":", 1)
                key = key.lower()
                val_lower = val.lower()

                if key == "size":
                    constraint = self._parse_size_constraint(val)
                    if constraint:
                        op, target_bytes = constraint
                        pkg_size = root_pkg.size_bytes
                        if op == ">" and not (pkg_size > target_bytes):
                            return False
                        elif op == ">=" and not (pkg_size >= target_bytes):
                            return False
                        elif op == "<" and not (pkg_size < target_bytes):
                            return False
                        elif op == "<=" and not (pkg_size <= target_bytes):
                            return False
                        elif op == "=" and not (pkg_size == target_bytes):
                            return False

                elif key == "repo":
                    if val_lower not in root_pkg.repository.lower():
                        return False

                elif key == "license":
                    if val_lower not in root_pkg.license.lower():
                        return False

                elif key == "status":
                    if val_lower == "orphan" and not root_pkg.is_orphan:
                        return False
                    elif val_lower == "queued" and root_pkg.state not in (PackageState.QUEUED_INSTALL, PackageState.QUEUED_REMOVE):
                        return False

                continue

            term = token.lower()
            item_match = (term in item.name.lower()) or (term in item.summary.lower())
            root_match = (term in root_pkg.name.lower()) or (term in root_pkg.summary.lower()) or (term in root_pkg.description.lower())

            ancestor_match = False
            curr = item.parent_item
            while curr and curr.parent_item is not None:
                if (term in curr.name.lower()) or (term in curr.summary.lower()):
                    ancestor_match = True
                    break
                curr = curr.parent_item

            if not (item_match or root_match or ancestor_match):
                return False

        return True

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model: DependencyTreeModel = self.sourceModel()
        index_name = model.index(source_row, DependencyTreeModel.COL_NAME, source_parent)

        if not index_name.isValid():
            return False

        item: TreeItem = index_name.internalPointer()

        root_item = item.get_root_package_item()
        if not root_item or not isinstance(root_item.payload, PackageInfo):
            return False

        root_pkg: PackageInfo = root_item.payload

        if not self._matches_category(root_pkg):
            return False

        return self._matches_search(item, root_pkg)

    @staticmethod
    def _natural_version_keys(version_str: str) -> List[Union[int, str]]:
        """تجزیه طبیعی رشته نگارش پکیج برای مرتب‌سازی هوشمند (مثلاً تشخیص 1.10 > 1.2)"""
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', version_str or "")]

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        col = left.column()

        # ۱. مرتب‌سازی عددی اندازه دقیق بر حسب بایت
        if col == DependencyTreeModel.COL_SIZE:
            left_size = left.data(CustomUserRoles.RawSizeRole) or 0
            right_size = right.data(CustomUserRoles.RawSizeRole) or 0
            return int(left_size) < int(right_size)

        # ۲. مرتب‌سازی هوشمند طبیعی نسخه‌ها
        elif col == DependencyTreeModel.COL_VERSION:
            left_ver = str(left.data(Qt.ItemDataRole.DisplayRole) or "")
            right_ver = str(right.data(Qt.ItemDataRole.DisplayRole) or "")
            return self._natural_version_keys(left_ver) < self._natural_version_keys(right_ver)

        # ۳. مرتب‌سازی الفبایی نام پکیج و سایر ستون‌ها
        left_val = str(left.data(Qt.ItemDataRole.DisplayRole) or "").lower()
        right_val = str(right.data(Qt.ItemDataRole.DisplayRole) or "").lower()
        return left_val < right_val
