# tests/test_core.py
"""
Unit and integration tests for Dendro Core models, filters and DAG tree structures.
Runs headlessly in CI environments using offscreen Qt platform.
"""

import sys
import pytest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from core.backend import PackageInfo, DependencyNode, PackageState
from core.models import DependencyTreeModel, PackageFilterProxyModel, CustomUserRoles


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def sample_packages():
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
            is_user_installed=True,
            is_gui_app=False,
            is_cli_tool=True,
            is_system=False,
            is_development=True,
            is_multimedia=False,
            is_network=False,
            is_fonts=False,
            is_library=False
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
            is_user_installed=False,
            is_gui_app=False,
            is_cli_tool=False,
            is_system=False,
            is_development=True,
            is_multimedia=False,
            is_network=False,
            is_fonts=False,
            is_library=True
        ),
        PackageInfo(
            name="firefox",
            version="154.0",
            release="1.fc42",
            arch="x86_64",
            summary="Mozilla Firefox Web Browser",
            group="Applications/Internet",
            size_bytes=82000000,
            state=PackageState.INSTALLED,
            is_orphan=False,
            is_user_installed=True,
            is_gui_app=True,
            is_cli_tool=False,
            is_system=False,
            is_development=False,
            is_multimedia=False,
            is_network=True,
            is_fonts=False,
            is_library=False
        )
    ]


def test_tree_model_population(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    assert model.rowCount() == 3
    assert model.columnCount() == DependencyTreeModel.COL_COUNT

    idx_name = model.index(0, DependencyTreeModel.COL_NAME)
    assert idx_name.data(Qt.ItemDataRole.DisplayRole) == "neovim"


def test_dependency_sub_tree_attachment(qapp, sample_packages):
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


def test_proxy_model_filtering(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    assert proxy.rowCount() == 3

    # تست دسته‌بندی برنامه‌های دسکتاپ
    proxy.set_category_filter("gui_apps")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    # تست دسته‌بندی ابزارهای خط فرمان
    proxy.set_category_filter("cli_tools")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "neovim"

    # تست دسته‌بندی اینترنت و شبکه
    proxy.set_category_filter("network")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    # تست دسته‌بندی کتابخانه‌ها
    proxy.set_category_filter("libraries")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "libtree"

    # تست دسته‌بندی بسته‌های یتیم
    proxy.set_category_filter("orphans")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "libtree"


def test_queue_state_toggling(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    idx_neovim = model.index(0, 0)
    model.toggle_queue_state(idx_neovim)

    installs, removals = model.get_queued_packages()
    assert "neovim" in removals
