# tests/test_core.py
"""
Unit and smoke tests for Dendro Core models and DAG tree structures.
Runs headlessly in CI environments using offscreen Qt platform.
"""

import sys
import pytest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QModelIndex

from core.backend import PackageInfo, DependencyNode, PackageState
from core.models import DependencyTreeModel, PackageFilterProxyModel, CustomUserRoles, TreeItem


@pytest.fixture(scope="session")
def qapp():
    """Create a single QApplication instance for Qt test runtime."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def sample_packages():
    """Generates sample package datasets for model testing."""
    return [
        PackageInfo(
            name="neovim",
            version="0.9.5",
            release="2.fc40",
            arch="x86_64",
            summary="Vim-fork focused on extensibility and usability",
            group="Development/Editors",
            size_bytes=18450000,
            state=PackageState.INSTALLED,
            is_orphan=False
        ),
        PackageInfo(
            name="libtree",
            version="3.1.1",
            release="1.fc40",
            arch="x86_64",
            summary="Tree like tool for shared libraries",
            group="Development/Tools",
            size_bytes=42000,
            state=PackageState.INSTALLED,
            is_orphan=True
        ),
        PackageInfo(
            name="htop",
            version="3.3.0",
            release="1.fc40",
            arch="x86_64",
            summary="Interactive process viewer",
            group="Applications/System",
            size_bytes=3200000,
            state=PackageState.AVAILABLE,
            is_orphan=False
        )
    ]


def test_tree_model_population(qapp, sample_packages):
    """Tests if packages populate correctly into DependencyTreeModel."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    # Validate row count
    assert model.rowCount() == 3
    assert model.columnCount() == DependencyTreeModel.COL_COUNT

    # Validate top-level data access
    idx_name = model.index(0, DependencyTreeModel.COL_NAME)
    assert idx_name.data(Qt.ItemDataRole.DisplayRole) == "neovim"
    assert idx_name.data(CustomUserRoles.RawSizeRole) == 18450000

    idx_status = model.index(0, DependencyTreeModel.COL_STATUS)
    assert idx_status.data(Qt.ItemDataRole.DisplayRole) == "Installed"


def test_dependency_sub_tree_attachment(qapp, sample_packages):
    """Tests recursive attachment of resolved dependencies."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    # Create dummy dependency branch
    deps = [
        DependencyNode(
            raw_requirement="libmsgpack-c.so.2()(64bit)",
            resolved_package_name="msgpack-c",
            version_constraint=">= 2.1.0",
            is_satisfied=True,
            is_cycle=False,
            sub_dependencies=[
                DependencyNode(
                    raw_requirement="glibc",
                    resolved_package_name="glibc",
                    is_satisfied=True,
                    is_cycle=False
                )
            ]
        )
    ]

    model.attach_dependencies("neovim", deps)

    parent_idx = model.index(0, DependencyTreeModel.COL_NAME)
    assert model.rowCount(parent_idx) == 1

    # Inspect child node
    child_idx = model.index(0, DependencyTreeModel.COL_NAME, parent_idx)
    assert child_idx.data(Qt.ItemDataRole.DisplayRole) == "msgpack-c"
    assert child_idx.data(CustomUserRoles.IsDependencyRole) is True

    # Inspect nested sub-dependency
    grandchild_idx = model.index(0, DependencyTreeModel.COL_NAME, child_idx)
    assert grandchild_idx.data(Qt.ItemDataRole.DisplayRole) == "glibc"


def test_proxy_model_filtering(qapp, sample_packages):
    """Tests category and real-time search filtering."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    assert proxy.rowCount() == 3

    # Filter by search string
    proxy.set_search_query("viewer")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "htop"

    # Filter by category
    proxy.set_search_query("")
    proxy.set_category_filter("orphans")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "libtree"


def test_queue_state_toggling(qapp, sample_packages):
    """Tests install/remove queue toggling and summary calculation."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    # Toggle removal for index 0 (neovim: installed -> queued remove)
    idx_neovim = model.index(0, 0)
    model.toggle_queue_state(idx_neovim)

    # Toggle install for index 2 (htop: available -> queued install)
    idx_htop = model.index(2, 0)
    model.toggle_queue_state(idx_htop)

    installs, removals = model.get_queued_packages()
    assert "htop" in installs
    assert "neovim" in removals
