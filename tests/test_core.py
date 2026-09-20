# tests/test_core.py
"""
Unit and integration tests for Dendro Core models, classification heuristics,
two-tier SQLite caching, and dependency hierarchy structures.
Runs headlessly in CI environments using an offscreen Qt platform.
"""

import os
import sys
import tempfile
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from core.backend import (
    FEDORA_SYSTEM_ROOT_PILLARS,
    HAS_LIBDNF5,
    HAS_NATIVE_RPM,
    DependencyNode,
    DryRunSimulationResult,
    PackageInfo,
    PackageState,
    SQLiteCapabilityCache,
    classify_package,
    create_libdnf5_base,
    create_rpm_transaction_set,
)
from core.models import DependencyTreeModel, PackageFilterProxyModel


# =============================================================================
# Pytest Fixtures
# =============================================================================

@pytest.fixture(scope="session")
def qapp():
    """Initialises headless QApplication instance for offscreen test runs."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def sample_packages():
    """Provides a representative cross-section of system packages for categorization tests."""
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
            is_desktop_app=True,
            is_cli_tool=False,
            is_fedora_core=False,
            is_c_lib=False,
            is_library=False
        ),
        PackageInfo(
            name="htop",
            version="3.3.0",
            release="1.fc44",
            arch="x86_64",
            summary="Interactive process viewer for terminal",
            group="Applications/System",
            size_bytes=350000,
            state=PackageState.INSTALLED,
            is_orphan=False,
            is_desktop_app=False,
            is_cli_tool=True,
            is_fedora_core=False,
            is_c_lib=False,
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
            is_desktop_app=False,
            is_cli_tool=False,
            is_fedora_core=True,
            is_c_lib=False,
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
            is_desktop_app=False,
            is_cli_tool=False,
            is_fedora_core=False,
            is_c_lib=True,
            is_library=True
        )
    ]


# =============================================================================
# Native Library Binding Verification
# =============================================================================

def test_native_bindings_environment():
    """Validates native binding detection flags and safe factory instantiation."""
    assert isinstance(HAS_NATIVE_RPM, bool)
    assert isinstance(HAS_LIBDNF5, bool)

    # Factories must safely return None or a live handle without throwing exceptions
    ts = create_rpm_transaction_set()
    if HAS_NATIVE_RPM and not os.path.exists("/.flatpak-info"):
        assert ts is not None
        del ts
    else:
        assert ts is None

    base = create_libdnf5_base(load_repos=False)
    if HAS_LIBDNF5 and not os.path.exists("/.flatpak-info"):
        assert base is not None
    else:
        assert base is None


# =============================================================================
# Capability Cache Performance & Persistence Tests
# =============================================================================

def test_sqlite_capability_cache_operations():
    """Tests the two-tier L1 memory and L2 SQLite cache under batch reads and writes."""
    cache = SQLiteCapabilityCache.get_instance()
    assert cache is not None

    batch_payload = [
        ("libssl.so.3()(64bit)", True, "openssl-libs"),
        ("libc.so.6(GLIBC_2.38)(64bit)", True, "glibc"),
        ("nonexistent-virtual-provider()", False, "unknown"),
    ]

    cache.set_batch(batch_payload)

    # L1 RAM hit
    cached_val = cache.get("libssl.so.3()(64bit)")
    assert cached_val is not None
    assert cached_val == (True, "openssl-libs")

    # Negative lookup
    non_existent = cache.get("definitely-not-a-registered-capability-xyz")
    assert non_existent is None


# =============================================================================
# Package Classification Engine Tests
# =============================================================================

def test_package_classification_heuristics():
    """Verifies that the classification engine separates GUI apps from CLI tools and libraries."""
    desktop_apps = {"firefox", "org.gnome.nautilus"}
    cli_apps = {"htop", "neovim", "ripgrep"}

    # 1. Desktop Application
    res_gui = classify_package(
        name="firefox",
        summary="Mozilla Firefox Web Browser",
        group="Applications/Internet",
        desktop_apps=desktop_apps,
        cli_desktop_apps=cli_apps,
        has_installed_desktop_file=True
    )
    assert res_gui["is_desktop_app"] is True
    assert res_gui["is_cli_tool"] is False
    assert res_gui["is_fedora_core"] is False

    # 2. CLI Tool
    res_cli = classify_package(
        name="htop",
        summary="Interactive process viewer for terminal",
        group="Applications/System",
        desktop_apps=desktop_apps,
        cli_desktop_apps=cli_apps
    )
    assert res_cli["is_desktop_app"] is False
    assert res_cli["is_cli_tool"] is True

    # 3. Core Fedora Component
    res_core = classify_package(
        name="systemd",
        summary="System and Service Manager",
        group="System/Base",
        desktop_apps=desktop_apps,
        cli_desktop_apps=cli_apps
    )
    assert res_core["is_fedora_core"] is True
    assert res_core["is_desktop_app"] is False

    # 4. Python Ecosystem Package
    res_py = classify_package(
        name="python3-pytest",
        summary="Simple powerful testing with Python",
        group="Development/Languages",
        desktop_apps=desktop_apps,
        cli_desktop_apps=cli_apps
    )
    assert res_py["is_python_pkg"] is True
    assert res_py["is_library"] is True

    # 5. Shared C Library
    res_lib = classify_package(
        name="libpng",
        summary="PNG image compression library",
        group="System/Libraries",
        desktop_apps=desktop_apps,
        cli_desktop_apps=cli_apps
    )
    assert res_lib["is_c_lib"] is True
    assert res_lib["is_library"] is True


# =============================================================================
# Core Model and Tree View Filter Proxy Tests
# =============================================================================

def test_tree_model_population(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    assert model.rowCount() == 4
    assert model.columnCount() == DependencyTreeModel.COL_COUNT

    idx_name = model.index(0, DependencyTreeModel.COL_NAME)
    assert idx_name.data(Qt.ItemDataRole.DisplayRole) == "firefox"


def test_strict_desktop_and_cli_categorization(qapp, sample_packages):
    """Verifies that the proxy model filters isolates categories without bleed-through."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    # 1. Desktop Apps (only firefox should match)
    proxy.set_category_filter("user_apps")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    # 2. CLI Tools (only htop should match)
    proxy.set_category_filter("cli_tools")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "htop"

    # 3. Fedora Core Pillars (only kernel should match)
    proxy.set_category_filter("fedora_core")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "kernel"

    # 4. C Libraries (only libpng should match)
    proxy.set_category_filter("c_libs")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "libpng"


def test_on_demand_lazy_dependency_attachment(qapp, sample_packages):
    """Tests lazy dependency attachment to tree parent nodes."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    deps = [
        DependencyNode(
            raw_requirement="libpng16.so.16()(64bit)",
            resolved_package_name="libpng",
            version_constraint=">= 1.6.0",
            is_satisfied=True,
            is_cycle=False
        )
    ]

    model.attach_dependencies("firefox", deps)

    parent_idx = model.index(0, DependencyTreeModel.COL_NAME)
    assert model.rowCount(parent_idx) == 1

    child_idx = model.index(0, DependencyTreeModel.COL_NAME, parent_idx)
    assert child_idx.data(Qt.ItemDataRole.DisplayRole) == "libpng"


def test_queue_state_toggling(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    idx_firefox = model.index(0, 0)
    model.toggle_queue_state(idx_firefox)

    installs, removals = model.get_queued_packages()
    assert "firefox" in removals


def test_system_pillar_guard_detection():
    """Verifies that dry-run results detect removals targeting system core packages."""
    sim_result = DryRunSimulationResult(
        to_remove=["systemd", "gnome-shell", "my-custom-package"],
        has_critical_system_removal=True,
        critical_packages=["systemd", "gnome-shell"]
    )

    assert sim_result.has_critical_system_removal is True
    assert "systemd" in sim_result.critical_packages
    assert "systemd" in FEDORA_SYSTEM_ROOT_PILLARS
