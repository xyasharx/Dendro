# dendro/core/models.py
"""
Data models, tree representations, and proxy filter models for Dendro.
Features lazy-loading dependency trees, Qt User Roles, natural version sorting,
multi-criteria search syntax parsing, and two-tier hierarchical category filtering.
Zero font emoji glyphs to prevent Fontconfig shaping failures.
"""

from __future__ import annotations

from typing import Any, Final

from PyQt6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    pyqtSignal,
    pyqtSlot,
)

from dendro.core.backend import (
    AvailableUpdateInfo,
    DependencyNode,
    PackageInfo,
    PackageState,
)
from dendro.core.i18n import tr

# =============================================================================
# Custom Qt User Roles
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
    IsUserInstalledRole: Final[int] = Qt.ItemDataRole.UserRole + 9
    HasUpdateRole: Final[int] = Qt.ItemDataRole.UserRole + 10
    ParentPillarRole: Final[int] = Qt.ItemDataRole.UserRole + 11
    SubCategoryRole: Final[int] = Qt.ItemDataRole.UserRole + 12
    IsProtectedRole: Final[int] = Qt.ItemDataRole.UserRole + 13


# =============================================================================
# TreeItem: Multi-Level Hierarchical Data Node
# =============================================================================


class TreeItem:
    __slots__ = (
        "_row",
        "child_items",
        "dependencies_loaded",
        "is_dependency",
        "is_loading_dependencies",
        "is_reverse_dep",
        "parent_item",
        "payload",
    )

    def __init__(
        self,
        data_payload: PackageInfo | DependencyNode | str,
        parent: TreeItem | None = None,
        is_dependency: bool = False,
        is_reverse_dep: bool = False,
        row: int = 0,
    ):
        self.parent_item: TreeItem | None = parent
        self.child_items: list[TreeItem] = []
        self.payload: PackageInfo | DependencyNode | str = data_payload
        self.is_dependency: bool = is_dependency
        self.is_reverse_dep: bool = is_reverse_dep
        self.dependencies_loaded: bool = is_dependency
        if isinstance(data_payload, PackageInfo):
            self.dependencies_loaded = not getattr(
                data_payload, "has_dependencies", True
            )
        self.is_loading_dependencies: bool = False
        self._row: int = row

    def append_child(self, child: TreeItem):
        child.parent_item = self
        child._row = len(self.child_items)
        self.child_items.append(child)

    def clear_children(self):
        for child in self.child_items:
            child.parent_item = None
            child.clear_children()
        self.child_items.clear()

    def child(self, row: int) -> TreeItem | None:
        if 0 <= row < len(self.child_items):
            return self.child_items[row]
        return None

    def child_count(self) -> int:
        return len(self.child_items)

    def row(self) -> int:
        if self.parent_item is not None:
            if (
                0 <= self._row < len(self.parent_item.child_items)
                and self.parent_item.child_items[self._row] is self
            ):
                return self._row
            try:
                self._row = self.parent_item.child_items.index(self)
                return self._row
            except ValueError:
                return 0
        return self._row

    def get_root_package_item(self) -> TreeItem | None:
        curr: TreeItem = self
        while curr.parent_item is not None and curr.parent_item.parent_item is not None:
            curr = curr.parent_item
        return curr

    @property
    def name(self) -> str:
        if isinstance(self.payload, PackageInfo):
            return self.payload.name
        if isinstance(self.payload, DependencyNode):
            return self.payload.resolved_package_name
        return str(self.payload)

    @property
    def version(self) -> str:
        if isinstance(self.payload, PackageInfo):
            return self.payload.full_version
        if isinstance(self.payload, DependencyNode):
            if self.is_reverse_dep:
                return "dependent"
            # Show the real installed version when it is known. When it is not,
            # show only the requested constraint: "satisfied" is a resolution
            # state, not a version, and it made a filled-in VERSION column read
            # as data where there is none.
            if self.payload.installed_version:
                if self.payload.version_constraint:
                    return (
                        f"{self.payload.installed_version} "
                        f"({self.payload.version_constraint})"
                    )
                return self.payload.installed_version
            return self.payload.version_constraint
        return ""

    @property
    def summary(self) -> str:
        if isinstance(self.payload, PackageInfo):
            return self.payload.summary
        if isinstance(self.payload, DependencyNode):
            if self.is_reverse_dep:
                return tr("Depends on: {requirement}").format(
                    requirement=self.payload.raw_requirement
                )
            return tr("Required by: {requirement}").format(
                requirement=self.payload.raw_requirement
            )
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
        if isinstance(self.payload, DependencyNode):
            return (
                PackageState.INSTALLED
                if self.payload.is_satisfied
                else PackageState.MISSING
            )
        return PackageState.AVAILABLE


# =============================================================================
# Status labels
# =============================================================================

# Every label is painted inside a fixed-width status pill and is upper-cased by
# the delegate, so these are also the strings the tree measures when it decides
# how wide the STATUS column has to be. Keep them short enough to fit without
# eliding ("MISSING DEP" and "TO UPGRADE" are the widest at the default font).
STATUS_LABELS: Final[dict[PackageState, str]] = {
    PackageState.QUEUED_INSTALL: "To Install",
    PackageState.QUEUED_REMOVE: "To Remove",
    PackageState.QUEUED_UPGRADE: "To Upgrade",
    PackageState.INSTALLED: "Installed",
    PackageState.MISSING: "Missing Dep",
    PackageState.AVAILABLE: "Available",
}

# The two extra states the delegate can paint, which have no PackageState of
# their own: an out-of-date installed package and a reverse dependency.
STATUS_EXTRA_LABELS: Final[tuple[str, ...]] = ("Update", "Required By")

# =============================================================================
# DependencyTreeModel: Primary Tree Presentation Model
# =============================================================================


#: Message ids for the package tree's column headers, in column order.
COLUMN_HEADERS: Final[tuple[str, ...]] = (
    "Package / Capability",
    "Status",
    "Version / Upgrade Path",
    "Size",
    "Summary",
)


class DependencyTreeModel(QAbstractItemModel):
    COL_NAME: Final[int] = 0
    COL_STATUS: Final[int] = 1
    COL_VERSION: Final[int] = 2
    COL_SIZE: Final[int] = 3
    COL_SUMMARY: Final[int] = 4
    COL_COUNT: Final[int] = 5

    queue_state_changed = pyqtSignal()
    fetch_dependencies_requested = pyqtSignal(str, object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        from dendro.core.settings import get_app_settings

        self.root_item = TreeItem("ROOT")
        # Name lookup remains for name-only dependency navigation. Mutating
        # actions and inspector synchronization use the full RPM identity map
        # below so multilib or install-only rows cannot collapse into one item.
        self._package_lookup: dict[str, TreeItem] = {}
        self._package_identity_lookup: dict[str, TreeItem] = {}
        self._package_name_counts: dict[str, int] = {}
        self._package_name_arch_counts: dict[str, int] = {}
        settings = get_app_settings()
        self._show_epoch: bool = settings.value("show_epoch", False, type=bool)

    def set_show_epoch(self, show: bool):
        """Toggles epoch display and refreshes the version column instantly."""
        if self._show_epoch != show:
            self._show_epoch = show
            if self.root_item.child_count() > 0:
                top_left = self.index(0, self.COL_VERSION)
                bottom_right = self.index(
                    self.root_item.child_count() - 1, self.COL_VERSION
                )
                self.dataChanged.emit(
                    top_left, bottom_right, [Qt.ItemDataRole.DisplayRole]
                )

    def clear(self):
        self.beginResetModel()
        self.root_item.clear_children()
        self._package_lookup.clear()
        self._package_identity_lookup.clear()
        self._package_name_counts.clear()
        self._package_name_arch_counts.clear()
        self.endResetModel()

    def set_packages(self, packages: list[PackageInfo]):
        self.beginResetModel()
        self.root_item.clear_children()
        self._package_lookup.clear()
        self._package_identity_lookup.clear()
        self._package_name_counts = {}
        self._package_name_arch_counts = {}
        for pkg in packages:
            self._package_name_counts[pkg.name] = (
                self._package_name_counts.get(pkg.name, 0) + 1
            )
            self._package_name_arch_counts[pkg.name_arch] = (
                self._package_name_arch_counts.get(pkg.name_arch, 0) + 1
            )

        for idx, pkg in enumerate(packages):
            item = TreeItem(
                data_payload=pkg, parent=self.root_item, is_dependency=False, row=idx
            )
            self.root_item.child_items.append(item)
            self._package_lookup[pkg.name] = item
            self._package_identity_lookup[pkg.full_nevra] = item

        self.endResetModel()

    def item_for_package(
        self, package: PackageInfo, *, allow_name_arch_fallback: bool = False
    ) -> TreeItem | None:
        """Return the tree item for this concrete RPM record.

        A bare package name is not a stable identity: RPM permits multiple
        architectures and install-only versions under it. A name-and-arch
        fallback is useful only after a successful ordinary upgrade replaced
        the selected version during a database reload.
        """
        item = self._package_identity_lookup.get(package.full_nevra)
        if item is not None or not allow_name_arch_fallback:
            return item
        for candidate in self.root_item.child_items:
            if (
                isinstance(candidate.payload, PackageInfo)
                and candidate.payload.name_arch == package.name_arch
            ):
                return candidate
        return None

    def update_orphans(self, orphan_nevras: set[str]):
        """Update orphan flags from exact installed RPM identities in one batch."""
        changed = False
        for item in self.root_item.child_items:
            if isinstance(item.payload, PackageInfo):
                is_orphan = item.payload.full_nevra in orphan_nevras
                if item.payload.is_orphan != is_orphan:
                    item.payload.is_orphan = is_orphan
                    changed = True

        if changed and self.root_item.child_count() > 0:
            top_left = self.index(0, 0)
            bottom_right = self.index(
                self.root_item.child_count() - 1, self.COL_COUNT - 1
            )
            self.dataChanged.emit(
                top_left,
                bottom_right,
                [CustomUserRoles.IsOrphanRole, Qt.ItemDataRole.DisplayRole],
            )

    def update_user_installed(self, user_installed_nevras: set[str]):
        """Update user-installed flags from exact installed RPM identities."""
        changed = False
        for item in self.root_item.child_items:
            if isinstance(item.payload, PackageInfo):
                is_user = item.payload.full_nevra in user_installed_nevras
                if item.payload.is_user_installed != is_user:
                    item.payload.is_user_installed = is_user
                    changed = True

        if changed and self.root_item.child_count() > 0:
            top_left = self.index(0, 0)
            bottom_right = self.index(
                self.root_item.child_count() - 1, self.COL_COUNT - 1
            )
            self.dataChanged.emit(
                top_left,
                bottom_right,
                [CustomUserRoles.IsUserInstalledRole, Qt.ItemDataRole.DisplayRole],
            )

    def update_available_upgrades(
        self, updates_map: dict[str, AvailableUpdateInfo], replace: bool = False
    ):
        """Marks packages with available upgrades safely in a single batch."""
        changed = False
        for item in self.root_item.child_items:
            if isinstance(item.payload, PackageInfo):
                up_info = updates_map.get(item.payload.name_arch)
                if up_info is not None:
                    target_ver = up_info.full_version
                    if (
                        not item.payload.has_update
                        or item.payload.available_update_version != target_ver
                    ):
                        item.payload.has_update = True
                        item.payload.available_update_version = target_ver
                        item.payload.available_update_repo = up_info.repository
                        changed = True
                elif replace and item.payload.has_update:
                    item.payload.has_update = False
                    item.payload.available_update_version = ""
                    item.payload.available_update_repo = ""
                    changed = True

        if changed and self.root_item.child_count() > 0:
            top_left = self.index(0, 0)
            bottom_right = self.index(
                self.root_item.child_count() - 1, self.COL_COUNT - 1
            )
            self.dataChanged.emit(
                top_left,
                bottom_right,
                [CustomUserRoles.HasUpdateRole, Qt.ItemDataRole.DisplayRole],
            )

    def hasChildren(self, parent: QModelIndex | None = None) -> bool:
        if parent is None:
            parent = QModelIndex()
        if not parent.isValid():
            return self.root_item.child_count() > 0

        item: TreeItem = parent.internalPointer()
        if item is None:
            return False

        if item.is_dependency or not isinstance(item.payload, PackageInfo):
            return item.child_count() > 0

        # If this package is known to have 0 dependencies, never draw an expander arrow
        if not getattr(item.payload, "has_dependencies", True):
            return False

        if not item.dependencies_loaded:
            return True
        return item.child_count() > 0

    def canFetchMore(self, parent: QModelIndex) -> bool:
        if not parent.isValid():
            return False

        item: TreeItem = parent.internalPointer()
        if (
            item is None
            or item.is_dependency
            or not isinstance(item.payload, PackageInfo)
        ):
            return False

        # Cannot fetch more if it has zero dependencies
        if not getattr(item.payload, "has_dependencies", True):
            return False

        return (not item.dependencies_loaded) and (not item.is_loading_dependencies)

    def fetchMore(self, parent: QModelIndex):
        if not parent.isValid():
            return

        item: TreeItem = parent.internalPointer()
        if (
            item is None
            or item.is_dependency
            or not isinstance(item.payload, PackageInfo)
        ):
            return

        if not item.dependencies_loaded and not item.is_loading_dependencies:
            item.is_loading_dependencies = True
            pindex = QPersistentModelIndex(parent)
            self.fetch_dependencies_requested.emit(item.name, pindex)

    @pyqtSlot(str, list, object)
    def attach_dependencies(
        self,
        pkg_name: str,
        dependencies: list[DependencyNode],
        target_index: QPersistentModelIndex | None = None,
    ):
        parent_item: TreeItem | None = None
        parent_index = QModelIndex()

        if target_index is not None:
            # An explicit persistent index identifies the exact tree instance
            # that requested this asynchronous lookup. Never fall back by name
            # if a model reset invalidated it; that would attach stale data to a
            # freshly reloaded package with the same name.
            if not target_index.isValid():
                return
            parent_index = QModelIndex(target_index)
            parent_item = parent_index.internalPointer()
        else:
            parent_item = self._package_lookup.get(pkg_name)
            if parent_item:
                parent_index = self.createIndex(parent_item.row(), 0, parent_item)

        if parent_item is None:
            return

        if parent_item.child_count() > 0:
            self.beginRemoveRows(parent_index, 0, parent_item.child_count() - 1)
            parent_item.clear_children()
            self.endRemoveRows()

        parent_item.dependencies_loaded = True
        parent_item.is_loading_dependencies = False

        if not dependencies:
            if isinstance(parent_item.payload, PackageInfo):
                parent_item.payload.has_dependencies = False
            self.dataChanged.emit(parent_index, parent_index)
            return

        self.beginInsertRows(parent_index, 0, len(dependencies) - 1)
        for idx, dep in enumerate(dependencies):
            child = TreeItem(
                data_payload=dep,
                parent=parent_item,
                is_dependency=True,
                is_reverse_dep=dep.is_reverse,
                row=idx,
            )
            parent_item.append_child(child)
        self.endInsertRows()

    @pyqtSlot(str, list)
    def attach_reverse_dependencies(
        self, root_pkg_name: str, reverse_deps: list[DependencyNode]
    ):
        self.attach_dependencies(root_pkg_name, reverse_deps, None)

    def reset_loading_state(self, root_pkg_name: str):
        parent_item = self._package_lookup.get(root_pkg_name)
        if parent_item:
            parent_item.is_loading_dependencies = False

    def reset_loading_state_for_index(
        self, target_index: QPersistentModelIndex
    ) -> None:
        """Clear only the exact asynchronous dependency request's loading flag."""
        if not target_index.isValid():
            return
        item = QModelIndex(target_index).internalPointer()
        if isinstance(item, TreeItem):
            item.is_loading_dependencies = False

    def toggle_queue_state(self, index: QModelIndex):
        if not index.isValid():
            return

        item: TreeItem = index.internalPointer()
        if item is None:
            return

        if isinstance(item.payload, PackageInfo):
            current_state = item.payload.state

            # If package has a pending update, prioritize upgrading over removal
            if item.payload.has_update:
                if current_state == PackageState.INSTALLED:
                    item.payload.state = PackageState.QUEUED_UPGRADE
                elif (
                    current_state == PackageState.QUEUED_UPGRADE
                    or current_state == PackageState.QUEUED_REMOVE
                ):
                    item.payload.state = PackageState.INSTALLED
            else:
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
                [Qt.ItemDataRole.DisplayRole, CustomUserRoles.PackageStateRole],
            )
            self.queue_state_changed.emit()

    def merge_remote_packages(self, remote_packages: list[PackageInfo]) -> int:
        """
        Merges uninstalled remote repository packages into the root tree on demand.
        Skips a package name-and-architecture already present locally or queued.
        """
        known_name_arch = {
            item.payload.name_arch
            for item in self.root_item.child_items
            if isinstance(item.payload, PackageInfo)
        }
        new_items: list[PackageInfo] = []
        for pkg in remote_packages:
            # Remote query records normally carry an architecture. Treat an
            # incomplete record conservatively as name-only rather than adding
            # a second, ambiguous row beside an installed package.
            if not pkg.arch and pkg.name in self._package_lookup:
                continue
            if pkg.name_arch not in known_name_arch:
                new_items.append(pkg)
                known_name_arch.add(pkg.name_arch)

        if not new_items:
            return 0

        start_row = self.root_item.child_count()
        self.beginInsertRows(QModelIndex(), start_row, start_row + len(new_items) - 1)
        for offset, pkg in enumerate(new_items):
            item = TreeItem(
                data_payload=pkg,
                parent=self.root_item,
                is_dependency=False,
                row=start_row + offset,
            )
            self.root_item.child_items.append(item)
            # Preserve an installed/name navigation target when a search adds
            # an alternate remote architecture under the same package name.
            self._package_lookup.setdefault(pkg.name, item)
            self._package_identity_lookup[pkg.full_nevra] = item
            self._package_name_counts[pkg.name] = (
                self._package_name_counts.get(pkg.name, 0) + 1
            )
            self._package_name_arch_counts[pkg.name_arch] = (
                self._package_name_arch_counts.get(pkg.name_arch, 0) + 1
            )
        self.endInsertRows()
        if self.root_item.child_count() > 0:
            self.dataChanged.emit(
                self.index(0, self.COL_NAME),
                self.index(self.root_item.child_count() - 1, self.COL_NAME),
                [Qt.ItemDataRole.DisplayRole],
            )
        return len(new_items)

    def clear_all_queued(self):
        """Discards all staged changes (installs, removals, upgrades) in a single batch."""
        changed = False
        for item in self.root_item.child_items:
            if isinstance(item.payload, PackageInfo):
                if item.payload.state in (
                    PackageState.QUEUED_REMOVE,
                    PackageState.QUEUED_UPGRADE,
                ):
                    item.payload.state = PackageState.INSTALLED
                    changed = True
                elif item.payload.state == PackageState.QUEUED_INSTALL:
                    item.payload.state = PackageState.AVAILABLE
                    changed = True

        if changed and self.root_item.child_count() > 0:
            top_left = self.index(0, 0)
            bottom_right = self.index(
                self.root_item.child_count() - 1, self.COL_COUNT - 1
            )
            self.dataChanged.emit(
                top_left,
                bottom_right,
                [Qt.ItemDataRole.DisplayRole, CustomUserRoles.PackageStateRole],
            )
            self.queue_state_changed.emit()

    def get_queued_packages(self) -> tuple[list[str], list[str], list[str]]:
        """Return exact DNF selectors staged for install, removal, and upgrade.

        Removal uses the selected record's full NEVRA. Install and upgrade use
        name-and-architecture selectors so DNF may choose the appropriate
        repository version without affecting another installed architecture.
        """
        installs: list[str] = []
        removals: list[str] = []
        upgrades: list[str] = []

        for item in self.root_item.child_items:
            if isinstance(item.payload, PackageInfo):
                if item.payload.state == PackageState.QUEUED_INSTALL:
                    installs.append(item.payload.transaction_spec)
                elif item.payload.state == PackageState.QUEUED_REMOVE:
                    removals.append(item.payload.transaction_spec)
                elif item.payload.state == PackageState.QUEUED_UPGRADE:
                    upgrades.append(item.payload.transaction_spec)

        return installs, removals, upgrades

    def queued_removal_packages(self) -> list[PackageInfo]:
        """Return selected removal records for post-transaction cache cleanup."""
        return [
            item.payload
            for item in self.root_item.child_items
            if isinstance(item.payload, PackageInfo)
            and item.payload.state == PackageState.QUEUED_REMOVE
        ]

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        if parent is None:
            parent = QModelIndex()
        if parent.column() > 0:
            return 0
        if not parent.isValid():
            parent_item = self.root_item
        else:
            parent_item = parent.internalPointer()

        if parent_item is None:
            return 0
        return parent_item.child_count()

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return self.COL_COUNT

    def retranslate(self) -> None:
        """Refresh translated column headers and status cells after a locale change."""
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, self.COL_COUNT - 1)
        row_count = self.root_item.child_count()
        if row_count > 0:
            self.dataChanged.emit(
                self.index(0, self.COL_STATUS),
                self.index(row_count - 1, self.COL_SIZE),
                [Qt.ItemDataRole.DisplayRole],
            )

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(COLUMN_HEADERS)
        ):
            # Translated per call rather than stored translated: the model
            # outlives a language change, and a view asks for the header as it
            # paints.
            return tr(COLUMN_HEADERS[section])
        return None

    def index(
        self, row: int, column: int, parent: QModelIndex | None = None
    ) -> QModelIndex:
        if parent is None:
            parent = QModelIndex()
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        if not parent.isValid():
            parent_item = self.root_item
        else:
            parent_item = parent.internalPointer()

        if parent_item is None:
            return QModelIndex()

        child_item = parent_item.child(row)
        if child_item is not None:
            return self.createIndex(row, column, child_item)
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()

        child_item: TreeItem = index.internalPointer()
        if child_item is None:
            return QModelIndex()

        parent_item: TreeItem | None = child_item.parent_item
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
        if item is None:
            return None

        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == self.COL_NAME:
                if (
                    isinstance(item.payload, PackageInfo)
                    and self._package_name_counts.get(item.payload.name, 0) > 1
                ):
                    if (
                        self._package_name_arch_counts.get(item.payload.name_arch, 0)
                        > 1
                    ):
                        return item.payload.full_nevra
                    return item.payload.name_arch
                return item.name
            if col == self.COL_STATUS:
                if item.is_reverse_dep:
                    return tr("Required By")
                if (
                    isinstance(item.payload, PackageInfo)
                    and item.payload.has_update
                    and item.payload.state == PackageState.INSTALLED
                ):
                    return tr("Update")
                # These labels are painted inside a fixed-width status pill and
                # are upper-cased by the delegate. Keep them short enough that
                # the default column never elides them ("MISSING DEP" and
                # "TO UPGRADE" are the widest at the default font).
                return tr(STATUS_LABELS.get(item.state, "Unknown"))
            if col == self.COL_VERSION:
                if isinstance(item.payload, PackageInfo):
                    curr_v = item.payload.format_version(self._show_epoch)
                    if (
                        item.payload.has_update
                        and item.payload.available_update_version
                    ):
                        target_v = item.payload.available_update_version
                        if self._show_epoch and ":" not in target_v:
                            target_v = f"0:{target_v}"
                        elif not self._show_epoch and ":" in target_v:
                            target_v = target_v.split(":", 1)[1]
                        return f"{curr_v} -> {target_v}"
                    return curr_v
                return item.version
            if col == self.COL_SIZE:
                return item.size_str
            if col == self.COL_SUMMARY:
                return item.summary

        elif role == CustomUserRoles.PackageStateRole:
            return item.state
        elif role == CustomUserRoles.IsDependencyRole:
            return item.is_dependency
        elif role == CustomUserRoles.IsReverseDepRole:
            return item.is_reverse_dep
        elif role == CustomUserRoles.IsOrphanRole:
            return getattr(item.payload, "is_orphan", False)
        elif role == CustomUserRoles.IsUserInstalledRole:
            return getattr(item.payload, "is_user_installed", False)
        elif role == CustomUserRoles.HasUpdateRole:
            return getattr(item.payload, "has_update", False)
        elif role == CustomUserRoles.RawSizeRole:
            return getattr(item.payload, "size_bytes", 0)
        elif role == CustomUserRoles.PackageInfoRole:
            return item.payload if isinstance(item.payload, PackageInfo) else None
        elif role == CustomUserRoles.IsCycleRole:
            return getattr(item.payload, "is_cycle", False)
        elif role == CustomUserRoles.ParentPillarRole:
            return getattr(item.payload, "parent_pillar", "")
        elif role == CustomUserRoles.SubCategoryRole:
            return getattr(item.payload, "sub_category", "")
        elif role == CustomUserRoles.IsProtectedRole:
            return getattr(item.payload, "is_protected", False)

        return None


from dendro.core.package_filter_model import (  # noqa: E402
    MAINTENANCE_CATEGORIES,
    NATURAL_VERSION_SPLIT_PATTERN,
    SIZE_CONSTRAINT_PATTERN,
    PackageFilterProxyModel,
    SearchToken,
    query_has_explicit_category_filter,
)

__all__ = (
    "COLUMN_HEADERS",
    "MAINTENANCE_CATEGORIES",
    "NATURAL_VERSION_SPLIT_PATTERN",
    "SIZE_CONSTRAINT_PATTERN",
    "STATUS_EXTRA_LABELS",
    "STATUS_LABELS",
    "CustomUserRoles",
    "DependencyTreeModel",
    "PackageFilterProxyModel",
    "SearchToken",
    "TreeItem",
    "query_has_explicit_category_filter",
)
