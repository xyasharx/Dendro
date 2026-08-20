# tests/test_core.py
"""
Unit and integration tests for Dendro Core models and DAG tree structures.
Runs headlessly in CI environments using offscreen Qt platform.
"""

import sys
import pytest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QModelIndex

from core.backend import PackageInfo, DependencyNode, PackageState, PackageQueryWorker
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
    """
    Generates mock package datasets with modern versioning
    for deterministic model testing.
    """
    return [
        PackageInfo(
            name="neovim",
            version="0.12.4",
            release="1.fc42",
            arch="x86_64",
            summary="Vim-fork focused on extensibility and usability",
            group="Development/Editors",
            size_bytes=24500000,
            state=PackageState.INSTALLED,
            is_orphan=False,
            is_user_installed=True
        ),
        PackageInfo(
            name="libtree",
            version="3.1.1",
            release="1.fc42",
            arch="x86_64",
            summary="Tree like tool for shared libraries",
            group="Development/Tools",
            size_bytes=42000,
            state=PackageState.INSTALLED,
            is_orphan=True,
            is_user_installed=False
        ),
        PackageInfo(
            name="htop",
            version="3.5.3",
            release="1.fc42",
            arch="x86_64",
            summary="Interactive process viewer",
            group="Applications/System",
            size_bytes=3200000,
            state=PackageState.AVAILABLE,
            is_orphan=False,
            is_user_installed=False
        )
    ]


def test_tree_model_population(qapp, sample_packages):
    """Tests if packages populate correctly into DependencyTreeModel."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    assert model.rowCount() == 3
    assert model.columnCount() == DependencyTreeModel.COL_COUNT

    idx_name = model.index(0, DependencyTreeModel.COL_NAME)
    assert idx_name.data(Qt.ItemDataRole.DisplayRole) == "neovim"
    assert idx_name.data(CustomUserRoles.RawSizeRole) == 24500000

    idx_status = model.index(0, DependencyTreeModel.COL_STATUS)
    assert idx_status.data(Qt.ItemDataRole.DisplayRole) == "Installed"


def test_dependency_sub_tree_attachment(qapp, sample_packages):
    """Tests recursive attachment of resolved dependencies."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

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

    child_idx = model.index(0, DependencyTreeModel.COL_NAME, parent_idx)
    assert child_idx.data(Qt.ItemDataRole.DisplayRole) == "msgpack-c"
    assert child_idx.data(CustomUserRoles.IsDependencyRole) is True

    grandchild_idx = model.index(0, DependencyTreeModel.COL_NAME, child_idx)
    assert grandchild_idx.data(Qt.ItemDataRole.DisplayRole) == "glibc"


def test_proxy_model_filtering(qapp, sample_packages):
    """Tests category and real-time search filtering."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    # تست دسته‌بندی پیش‌فرض (همه پکیج‌ها)
    assert proxy.rowCount() == 3

    # تست فیلتر پکیج‌های اصلی (User-Installed)
    proxy.set_category_filter("installed")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "neovim"

    # تست فیلتر با جستجوی متنی
    proxy.set_category_filter("all")
    proxy.set_search_query("viewer")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "htop"

    # تست فیلتر پکیج‌های یتیم (Orphans)
    proxy.set_search_query("")
    proxy.set_category_filter("orphans")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "libtree"


def test_queue_state_toggling(qapp, sample_packages):
    """Tests install/remove queue toggling and summary calculation."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    idx_neovim = model.index(0, 0)
    model.toggle_queue_state(idx_neovim)

    idx_htop = model.index(2, 0)
    model.toggle_queue_state(idx_htop)

    installs, removals = model.get_queued_packages()
    assert "htop" in installs
    assert "neovim" in removals


def test_dynamic_live_system_packages(qapp):
    """
    Integration Test: Dynamically queries the real RPM database
    present in the running Fedora system / CI environment.
    """
    worker = PackageQueryWorker(category="all", search_query="")
    packages = worker._query_cli_subprocess()

    # سیستم فدورا باید حداقل دارای پکیج‌های پایه‌ای مثل rpm یا python3 باشد
    assert len(packages) > 0

    # بررسی اینکه بسته‌ها ساختار و نسخه واقعی سیستم را دارند
    pkg_names = {p.name for p in packages}
    assert "rpm" in pkg_names or "python3" in pkg_names

    # استخراج نسخه واقعی و پویای بسته از سیستم
    target_pkg = next(p for p in packages if p.name in ("rpm", "python3"))
    assert target_pkg.version != ""
    assert target_pkg.release != ""
