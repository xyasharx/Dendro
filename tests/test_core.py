# tests/test_core.py
"""
Unit and integration tests for Dendro Core models, full categories, and DAG tree structures.
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
            name="firefox",
            version="154.0",
            release="1.fc44",
            arch="x86_64",
            summary="Mozilla Firefox Web Browser",
            group="Applications/Internet",
            size_bytes=82000000,
            state=PackageState.INSTALLED,
            is_orphan=False,
            is_user_app=True,
            is_fedora_core=False,
            is_c_lib=False,
            is_firmware=False,
            is_font=False,
            is_locale=False,
            is_devel=False,
            is_theme=False,
            is_library=False
        ),
        PackageInfo(
            name="kernel",
            version="7.1.8",
            release="1.fc44",
            arch="x86_64",
            summary="The Linux Kernel",
            group="System Environment/Kernel",
            size_bytes=150000000,
            state=PackageState.INSTALLED,
            is_orphan=False,
            is_user_app=False,
            is_fedora_core=True,
            is_c_lib=False,
            is_firmware=False,
            is_font=False,
            is_locale=False,
            is_devel=False,
            is_theme=False,
            is_library=False
        ),
        PackageInfo(
            name="libpng",
            version="1.6.58",
            release="1.fc44",
            arch="x86_64",
            summary="A library of functions for manipulating PNG image format files",
            group="System Environment/Libraries",
            size_bytes=420000,
            state=PackageState.INSTALLED,
            is_orphan=True,
            is_user_app=False,
            is_fedora_core=False,
            is_c_lib=True,
            is_firmware=False,
            is_font=False,
            is_locale=False,
            is_devel=False,
            is_theme=False,
            is_library=True
        )
    ]


def test_tree_model_population(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    assert model.rowCount() == 3
    assert model.columnCount() == DependencyTreeModel.COL_COUNT

    idx_name = model.index(0, DependencyTreeModel.COL_NAME)
    assert idx_name.data(Qt.ItemDataRole.DisplayRole) == "firefox"


def test_dependency_sub_tree_attachment(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    deps = [
        DependencyNode(
            raw_requirement="libpng16.so.16()(64bit)",
            resolved_package_name="libpng",
            version_constraint=">= 1.6.0",
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

    model.attach_dependencies("firefox", deps)

    parent_idx = model.index(0, DependencyTreeModel.COL_NAME)
    assert model.rowCount(parent_idx) == 1

    child_idx = model.index(0, DependencyTreeModel.COL_NAME, parent_idx)
    assert child_idx.data(Qt.ItemDataRole.DisplayRole) == "libpng"


def test_proxy_model_filtering(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    assert proxy.rowCount() == 3

    # تست فیلتر برنامه‌های کاربر
    proxy.set_category_filter("user_apps")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    # تست فیلتر ستون‌های اصلی فدورا
    proxy.set_category_filter("fedora_core")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "kernel"

    # تست فیلتر کتابخانه‌های C/C++
    proxy.set_category_filter("c_libs")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "libpng"

    # تست بسته‌های بدون استفاده (Orphans)
    proxy.set_category_filter("orphans")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "libpng"


def test_queue_state_toggling(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    idx_firefox = model.index(0, 0)
    model.toggle_queue_state(idx_firefox)

    installs, removals = model.get_queued_packages()
    assert "firefox" in removals
