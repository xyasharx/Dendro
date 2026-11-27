# dendro/core/package_filter_model.py
"""Filter and natural-sorting proxy model for Dendro's package tree."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from PyQt6.QtCore import QModelIndex, QObject, QSortFilterProxyModel, Qt

import dendro.core.models as _package_tree_models
from dendro.core.package_types import PackageInfo, PackageState
from dendro.core.taxonomy import category_matches, category_spec

if TYPE_CHECKING:
    from dendro.core.models import DependencyTreeModel, TreeItem

# Precompiled regular expressions for sorting and size parsing
SIZE_CONSTRAINT_PATTERN: Final[re.Pattern] = re.compile(
    r"^([><]=?|=)\s*(\d+(?:\.\d+)?)\s*([kmgtp]?b?)$"
)
NATURAL_VERSION_SPLIT_PATTERN: Final[re.Pattern] = re.compile(r"(\d+)")
MAINTENANCE_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"queued", "updates_available", "orphans"}
)


def query_has_explicit_category_filter(search_term: str) -> bool:
    """Return True when ``search_term`` includes a non-empty category/pillar token."""
    for raw_token in search_term.strip().split():
        if ":" not in raw_token:
            continue
        key, value = raw_token.split(":", 1)
        if value and key.lower() in (
            "cat",
            "category",
            "sub",
            "subcategory",
            "pillar",
            "parent",
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class SearchToken:
    """One search expression pre-parsed once per user query."""

    key: str | None
    value: str
    normalized_value: str
    size_constraint: tuple[str, int] | None = None


class PackageFilterProxyModel(QSortFilterProxyModel):
    MAINTENANCE_CATEGORIES: Final[frozenset[str]] = MAINTENANCE_CATEGORIES

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.setDynamicSortFilter(True)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setAutoAcceptChildRows(False)
        self._category: str = "user_apps"
        self._search_term: str = ""
        self._search_all_packages: bool = True
        # Splitting once per query avoids allocating/parsing the same tokens for
        # every visible tree row during filtering.
        self._search_tokens: tuple[SearchToken, ...] = ()

    @property
    def category_filter(self) -> str:
        """Currently active sidebar/category identifier."""
        return self._category

    @property
    def search_query(self) -> str:
        """Currently active trimmed search bar query."""
        return self._search_term

    @property
    def search_all_packages(self) -> bool:
        """Whether searches on regular categories expand across all packages."""
        return self._search_all_packages

    def effective_search_scope_all(self) -> bool:
        """Return True when the active filter evaluates across all categories."""
        if not self._search_term:
            return self._category == "all"
        if self._category == "all":
            return True
        if self._category in self.MAINTENANCE_CATEGORIES:
            return False
        return self._search_all_packages

    def set_search_all_packages(self, search_all: bool) -> None:
        enabled = bool(search_all)
        if enabled == self._search_all_packages:
            return
        scoped = self._begin_row_filter_change()
        self._search_all_packages = enabled
        self._end_row_filter_change(scoped)

    def _begin_row_filter_change(self) -> bool:
        if (
            hasattr(self, "beginFilterChange")
            and hasattr(self, "endFilterChange")
            and hasattr(QSortFilterProxyModel, "Direction")
        ):
            self.beginFilterChange()
            return True
        return False

    def _end_row_filter_change(self, scoped: bool) -> None:
        if scoped:
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.invalidateFilter()

    def refresh_filter(self) -> None:
        """Re-evaluate row filtering after source-model metadata changes."""
        scoped = self._begin_row_filter_change()
        self._end_row_filter_change(scoped)

    def set_category_filter(self, category: str):
        normalized = category.lower()
        scoped = self._begin_row_filter_change()
        self._category = normalized
        self._end_row_filter_change(scoped)

    def set_search_query(self, search_term: str):
        normalized = search_term.strip()
        tokens = tuple(self._parse_search_token(token) for token in normalized.split())
        scoped = self._begin_row_filter_change()
        self._search_term = normalized
        self._search_tokens = tokens
        self._end_row_filter_change(scoped)

    def _parse_search_token(self, token: str) -> SearchToken:
        if ":" not in token:
            return SearchToken(None, token, token.lower())
        key, value = token.split(":", 1)
        normalized_key = key.lower()
        return SearchToken(
            normalized_key,
            value,
            value.lower(),
            self._parse_size_constraint(value) if normalized_key == "size" else None,
        )

    def _matches_category(self, pkg: PackageInfo) -> bool:
        """Use the same registry predicate as sidebar counts and search filters."""
        return category_matches(pkg, self._category)

    def _parse_size_constraint(self, val_str: str) -> tuple[str, int] | None:
        match = SIZE_CONSTRAINT_PATTERN.match(val_str.lower())
        if not match:
            return None
        op, num_str, unit = match.groups()
        try:
            num = float(num_str)
        except ValueError:
            return None
        multiplier = 1
        if unit.startswith("k"):
            multiplier = 1024
        elif unit.startswith("m"):
            multiplier = 1024 * 1024
        elif unit.startswith("g"):
            multiplier = 1024 * 1024 * 1024
        elif unit.startswith("t"):
            multiplier = 1024 * 1024 * 1024 * 1024
        elif unit.startswith("p"):
            multiplier = 1024 * 1024 * 1024 * 1024 * 1024
        try:
            return op, int(num * multiplier)
        except OverflowError:
            # An arbitrarily large user-entered decimal is not a valid filter;
            # let the normal invalid-filter path show no matches instead of
            # allowing an input event to raise from the GUI thread.
            return None

    def _matches_search(self, item: TreeItem, root_pkg: PackageInfo) -> bool:
        if not self._search_term:
            return True

        for token in self._search_tokens:
            if token.key is not None:
                key = token.key
                val_lower = token.normalized_value

                # Keep incomplete filter prefixes non-destructive while the
                # header offers completion. A non-empty invalid expression,
                # however, must never silently behave like no filter at all.
                if not token.value:
                    continue

                if key == "size":
                    constraint = token.size_constraint
                    if constraint is None:
                        return False
                    op, target_bytes = constraint
                    pkg_size = root_pkg.size_bytes
                    if (
                        (op == ">" and not (pkg_size > target_bytes))
                        or (op == ">=" and not (pkg_size >= target_bytes))
                        or (op == "<" and not (pkg_size < target_bytes))
                        or (op == "<=" and not (pkg_size <= target_bytes))
                        or (op == "=" and pkg_size != target_bytes)
                    ):
                        return False

                elif key == "repo":
                    if val_lower not in root_pkg.repository.lower():
                        return False

                elif key == "license":
                    if val_lower not in root_pkg.license.lower():
                        return False

                elif key == "arch":
                    if val_lower != root_pkg.arch.lower():
                        return False

                elif key in ("cat", "category"):
                    # Exact registered category IDs share the sidebar/filter
                    # semantics.  Retain substring matching for old free-form
                    # searches such as ``cat:python``.
                    if category_spec(val_lower) is not None:
                        if not category_matches(root_pkg, val_lower):
                            return False
                    elif (
                        val_lower not in root_pkg.primary_category.lower()
                        and val_lower
                        not in getattr(root_pkg, "sub_category", "").lower()
                    ):
                        return False

                elif key in ("sub", "subcategory"):
                    if val_lower not in getattr(root_pkg, "sub_category", "").lower():
                        return False

                elif key in ("pillar", "parent"):
                    if val_lower not in getattr(root_pkg, "parent_pillar", "").lower():
                        return False

                elif key == "tag":
                    if not any(val_lower in t.lower() for t in root_pkg.secondary_tags):
                        return False

                elif key in ("evidence", "source"):
                    if (
                        val_lower
                        not in getattr(root_pkg, "classification_evidence", "").lower()
                    ):
                        return False

                elif key in ("scope", "metadata"):
                    if (
                        val_lower
                        not in getattr(
                            root_pkg, "classification_input_scope", ""
                        ).lower()
                    ):
                        return False

                elif key in ("type", "interface"):
                    if (
                        (
                            val_lower in ("gui", "desktop", "app")
                            and not getattr(root_pkg, "is_desktop_app", False)
                        )
                        or (
                            val_lower in ("cli", "terminal", "tool")
                            and not getattr(root_pkg, "is_cli_tool", False)
                        )
                        or (
                            val_lower in ("service", "daemon", "systemd")
                            and not getattr(root_pkg, "is_systemd_service", False)
                        )
                        or (
                            val_lower in ("lib", "library", "c_lib")
                            and not getattr(root_pkg, "is_c_lib", False)
                        )
                        or val_lower
                        not in {
                            "gui",
                            "desktop",
                            "app",
                            "cli",
                            "terminal",
                            "tool",
                            "service",
                            "daemon",
                            "systemd",
                            "lib",
                            "library",
                            "c_lib",
                        }
                    ):
                        return False

                elif key == "status":
                    if (
                        (
                            val_lower in ("update", "upgradable", "upgrade")
                            and not getattr(root_pkg, "has_update", False)
                        )
                        or (
                            val_lower == "orphan"
                            and not getattr(root_pkg, "is_orphan", False)
                        )
                        or (
                            val_lower
                            in (
                                "user",
                                "userinstalled",
                                "manual",
                            )
                            and not getattr(root_pkg, "is_user_installed", False)
                        )
                        or (
                            val_lower == "queued"
                            and root_pkg.state
                            not in (
                                PackageState.QUEUED_INSTALL,
                                PackageState.QUEUED_REMOVE,
                                PackageState.QUEUED_UPGRADE,
                            )
                        )
                        or (
                            val_lower == "installed"
                            and root_pkg.state
                            not in (
                                PackageState.INSTALLED,
                                PackageState.QUEUED_REMOVE,
                                PackageState.QUEUED_UPGRADE,
                            )
                        )
                        or (
                            val_lower in ("available", "remote")
                            and root_pkg.state != PackageState.AVAILABLE
                        )
                        or (
                            val_lower == "layered"
                            and getattr(root_pkg, "ostree_origin", "")
                            not in ("layered", "local")
                        )
                        or (
                            val_lower == "override"
                            and getattr(root_pkg, "ostree_origin", "") != "override"
                        )
                        or (
                            val_lower == "base"
                            and getattr(root_pkg, "ostree_origin", "") != "base"
                        )
                        or val_lower
                        not in {
                            "update",
                            "upgradable",
                            "upgrade",
                            "orphan",
                            "user",
                            "userinstalled",
                            "manual",
                            "queued",
                            "installed",
                            "available",
                            "remote",
                            "layered",
                            "override",
                            "base",
                        }
                    ):
                        return False
                else:
                    return False

                continue

            term = token.normalized_value
            item_match = (term in item.name.lower()) or (term in item.summary.lower())
            root_match = (
                (term in root_pkg.name.lower())
                or (term in root_pkg.summary.lower())
                or (term in root_pkg.description.lower())
                or any(term in t.lower() for t in root_pkg.secondary_tags)
            )

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
        tree_model_cls = _package_tree_models.DependencyTreeModel
        model: DependencyTreeModel | None = self.sourceModel()
        if model is None:
            return False

        index_name = model.index(source_row, tree_model_cls.COL_NAME, source_parent)
        if not index_name.isValid():
            return False

        item: TreeItem = index_name.internalPointer()
        if item is None:
            return False

        root_item = item.get_root_package_item()
        if not root_item or not isinstance(root_item.payload, PackageInfo):
            return False

        root_pkg: PackageInfo = root_item.payload

        # When a search query is active and global search scope is enabled,
        # search across all packages except inside dedicated maintenance channels
        # (queued, updates, orphans). Users can toggle scope to the current
        # category explicitly from the header search scope chip.
        is_maintenance_channel = self._category in self.MAINTENANCE_CATEGORIES
        if (
            self._search_term
            and not is_maintenance_channel
            and self._search_all_packages
        ):
            # Let explicit 'cat:', 'pillar:', or 'sub:' search syntax govern category matching
            pass
        elif not self._matches_category(root_pkg):
            return False

        return self._matches_search(item, root_pkg)

    @staticmethod
    def _natural_version_keys(version_str: str) -> list[tuple[int, int, str]]:
        """Build comparable natural-sort keys without unbounded integer conversion."""
        keys: list[tuple[int, int, str]] = []
        for component in NATURAL_VERSION_SPLIT_PATTERN.split(version_str or ""):
            if component.isdigit():
                normalized = component.lstrip("0") or "0"
                # Length then lexical comparison preserves numeric ordering for
                # arbitrarily long RPM version components without hitting
                # Python's integer-string safety limit.
                keys.append((0, len(normalized), normalized))
            else:
                keys.append((1, 0, component.lower()))
        return keys

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        tree_model_cls = _package_tree_models.DependencyTreeModel
        roles_cls = _package_tree_models.CustomUserRoles
        col = left.column()

        if col == tree_model_cls.COL_SIZE:
            left_size = left.data(roles_cls.RawSizeRole) or 0
            right_size = right.data(roles_cls.RawSizeRole) or 0
            return int(left_size) < int(right_size)

        if col == tree_model_cls.COL_VERSION:
            left_ver = str(left.data(Qt.ItemDataRole.DisplayRole) or "")
            right_ver = str(right.data(Qt.ItemDataRole.DisplayRole) or "")
            return self._natural_version_keys(left_ver) < self._natural_version_keys(
                right_ver
            )

        left_val = str(left.data(Qt.ItemDataRole.DisplayRole) or "").lower()
        right_val = str(right.data(Qt.ItemDataRole.DisplayRole) or "").lower()
        return left_val < right_val


__all__ = (
    "MAINTENANCE_CATEGORIES",
    "NATURAL_VERSION_SPLIT_PATTERN",
    "SIZE_CONSTRAINT_PATTERN",
    "PackageFilterProxyModel",
    "SearchToken",
    "query_has_explicit_category_filter",
)
