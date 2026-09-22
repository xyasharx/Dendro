# tests/test_core.py
"""
Unit and integration tests for Dendro Core models, multi-tier classification,
two-tier SQLite caching, and dependency hierarchy structures.
Runs headlessly in CI and local environments using the Qt offscreen platform.
"""

import os
import sys
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
    RPMDirectoryFootprint,
    SQLiteCapabilityCache,
    classify_package_advanced,
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
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
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
            is_library=False,
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
            is_library=False,
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
            is_library=False,
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
            is_library=True,
        ),
    ]


# =============================================================================
# Native Library Binding Verification
# =============================================================================

def test_native_bindings_environment():
    """Validates native binding detection flags and safe factory instantiation."""
    assert isinstance(HAS_NATIVE_RPM, bool)
    assert isinstance(HAS_LIBDNF5, bool)

    # Factories must safely return a handle or None without crashing
    ts = create_rpm_transaction_set()
    if ts is not None:
        del ts

    base = create_libdnf5_base(load_repos=False)
    if HAS_LIBDNF5 and not os.path.exists("/.flatpak-info"):
        if base is not None:
            assert hasattr(base, "get_repo_sack")


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
# Tier 2 & 3 Classification Engine Tests
# =============================================================================

def test_rpm_directory_footprint_creation():
    """Verifies that RPMDirectoryFootprint defaults and properties instantiate correctly."""
    footprint = RPMDirectoryFootprint(
        has_bin=True,
        has_desktop_file=True,
        has_systemd_unit=False,
        has_headers=False,
        has_shared_libs=True,
    )
    assert footprint.has_bin is True
    assert footprint.has_desktop_file is True
    assert footprint.has_systemd_unit is False
    assert footprint.has_headers is False
    assert footprint.has_shared_libs is True


def test_classify_python_cli_tool_disambiguation():
    """
    Verifies that Python-based CLI applications (e.g. Ansible, Certbot, yt-dlp)
    are recognized as Command-Line Tools while retaining their Python ecosystem tag.
    """
    footprint = RPMDirectoryFootprint(has_bin=True, has_desktop_file=False)

    res = classify_package_advanced(
        name="ansible-core",
        summary="A radically simple IT automation system",
        footprint=footprint,
        appstream_desktop=False,
        appstream_console=True,  # Confirmed via AppStream
        desktop_apps_discovered=set(),
        cli_apps_discovered={"ansible-core"},
    )

    assert res["is_cli_tool"] is True
    assert res["is_python_pkg"] is True
    assert res["is_desktop_app"] is False
    assert res["is_c_lib"] is False


def test_classify_pure_python_library():
    """
    Verifies that pure Python libraries without binaries (e.g. urllib3, requests)
    are classified as libraries and NOT as CLI tools.
    """
    footprint = RPMDirectoryFootprint(has_bin=False, has_desktop_file=False)

    res = classify_package_advanced(
        name="python3-urllib3",
        summary="HTTP library with thread-safe connection pooling",
        footprint=footprint,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
    )

    assert res["is_python_pkg"] is True
    assert res["is_cli_tool"] is False
    assert res["is_desktop_app"] is False
    assert res["is_library"] is True


def test_classify_desktop_app_with_appstream_truth():
    """
    Verifies that packages listed in AppStream desktop catalog
    are tagged as Desktop Apps even if the package name differs from the .desktop file.
    """
    footprint = RPMDirectoryFootprint(has_bin=True, has_desktop_file=True)

    res = classify_package_advanced(
        name="celluloid",
        summary="Simple GTK+ frontend for mpv",
        footprint=footprint,
        appstream_desktop=True,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
    )

    assert res["is_desktop_app"] is True
    assert res["is_cli_tool"] is False


def test_classify_systemd_daemon_service():
    """
    Verifies that system daemons with systemd units or /usr/sbin binaries
    are identified as systemd services and not as desktop apps or user CLI tools.
    """
    footprint = RPMDirectoryFootprint(has_sbin=True, has_bin=False, has_systemd_unit=True)

    res = classify_package_advanced(
        name="sssd",
        summary="System Security Services Daemon",
        footprint=footprint,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
    )

    assert res["is_systemd_service"] is True
    assert res["is_cli_tool"] is False
    assert res["is_desktop_app"] is False


def test_classify_shared_c_library():
    """
    Verifies that shared C libraries (/usr/lib64/*.so) are classified
    cleanly under c_libs when no user binaries are provided.
    """
    footprint = RPMDirectoryFootprint(has_shared_libs=True, has_bin=False)

    res = classify_package_advanced(
        name="libsqlite3",
        summary="Shared library for the SQLite database engine",
        footprint=footprint,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
    )

    assert res["is_c_lib"] is True
    assert res["is_library"] is True
    assert res["is_cli_tool"] is False
    assert res["is_desktop_app"] is False


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
    """Verifies that the proxy model filters isolate categories cleanly."""
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
            is_cycle=False,
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
        critical_packages=["systemd", "gnome-shell"],
    )

    assert sim_result.has_critical_system_removal is True
    assert "systemd" in sim_result.critical_packages
    assert "systemd" in FEDORA_SYSTEM_ROOT_PILLARS
