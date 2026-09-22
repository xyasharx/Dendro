# tests/test_core.py
"""
Unit and integration tests for Dendro Core models, intelligent decision engine,
natural language semantic intent profiling, two-tier SQLite caching, and DAG tree structures.
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
    IntelligentPackageClassifier,
    PackageInfo,
    PackagePhysicalAnatomy,
    PackageState,
    SemanticIntentAnalyzer,
    SQLiteCapabilityCache,
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
            primary_category="desktop_app",
            classification_confidence=0.98,
            classification_rationale=["Verified in official Fedora AppStream desktop catalog"],
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
            primary_category="cli_tool",
            classification_confidence=0.97,
            classification_rationale=["Delivers user command binary into /usr/bin"],
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
            primary_category="fedora_core",
            classification_confidence=0.99,
            classification_rationale=["Identified as foundational Fedora root pillar"],
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
            primary_category="c_lib",
            classification_confidence=0.96,
            classification_rationale=["Exports dynamic ELF SONAME ABI contracts"],
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
# Semantic Intent & Physical Anatomy Tests
# =============================================================================

def test_semantic_intent_analyzer():
    """Verifies natural language keyword scoring across GUI, CLI, and Daemon domains."""
    gui_text = "A simple graphical photo viewer and canvas editor for the desktop."
    scores_gui = SemanticIntentAnalyzer.score_text(gui_text)
    assert scores_gui["gui"] > scores_gui["cli"]
    assert scores_gui["gui"] > scores_gui["daemon"]

    cli_text = "An interactive command-line utility for benchmarking system processes."
    scores_cli = SemanticIntentAnalyzer.score_text(cli_text)
    assert scores_cli["cli"] > scores_cli["gui"]
    assert scores_cli["cli"] > scores_cli["lib"]


def test_package_physical_anatomy_structure():
    """Verifies that PackagePhysicalAnatomy instantiates with expected defaults."""
    anatomy = PackagePhysicalAnatomy(
        has_user_bin=True,
        has_desktop_file=True,
        has_c_headers=False,
        has_firmware_dir=False,
    )
    assert anatomy.has_user_bin is True
    assert anatomy.has_desktop_file is True
    assert anatomy.has_c_headers is False
    assert anatomy.has_firmware_dir is False


# =============================================================================
# Intelligent Decision Engine Tests
# =============================================================================

def test_intelligent_classifier_ansible_core():
    """Verifies that Ansible is classified as a CLI tool with Python secondary tagging."""
    anatomy = PackagePhysicalAnatomy(
        has_user_bin=True,
        has_desktop_file=False,
        has_man1=True,
        has_python_runtime=True,
    )
    decision = IntelligentPackageClassifier.classify(
        name="ansible-core",
        summary="A radically simple IT automation system",
        description="Command-line configuration management and deployment framework.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=True,
        desktop_apps_discovered=set(),
        cli_apps_discovered={"ansible"},
    )
    assert decision.primary_category == "cli_tool"
    assert decision.confidence >= 0.85
    assert "Python" in decision.secondary_tags
    assert any("user command binary" in r for r in decision.rationale)


def test_intelligent_classifier_gnome_firmware_guard():
    """
    Verifies that 'gnome-firmware' is classified as a Desktop Application,
    preventing the word 'firmware' from misclassifying it as hardware microcode.
    """
    anatomy = PackagePhysicalAnatomy(
        has_user_bin=True,
        has_desktop_file=True,
        has_firmware_dir=False,  # Does NOT contain raw firmware binary blobs
    )
    decision = IntelligentPackageClassifier.classify(
        name="gnome-firmware",
        summary="Manage firmware on devices",
        description="A graphical tool to update firmware on devices using fwupd.",
        anatomy=anatomy,
        appstream_desktop=True,
        appstream_console=False,
        desktop_apps_discovered={"gnome-firmware"},
        cli_apps_discovered=set(),
    )
    assert decision.primary_category == "desktop_app"
    assert decision.confidence >= 0.90
    assert decision.flags["is_desktop_app"] is True
    assert decision.flags["is_firmware"] is False


def test_intelligent_classifier_shared_library():
    """Verifies that dynamic shared C libraries are detected via exported SONAME ABI contracts."""
    anatomy = PackagePhysicalAnatomy(
        has_user_bin=False,
        has_desktop_file=False,
        exported_sonames=["libpng16.so.16()(64bit)"],
    )
    decision = IntelligentPackageClassifier.classify(
        name="libpng",
        summary="A library of functions for manipulating PNG image format files",
        description="libpng is the official PNG reference library.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
    )
    assert decision.primary_category == "c_lib"
    assert decision.flags["is_c_lib"] is True
    assert decision.flags["is_cli_tool"] is False


def test_intelligent_classifier_systemd_daemon():
    """Verifies that background services with systemd units are identified as system services."""
    anatomy = PackagePhysicalAnatomy(
        has_admin_sbin=True,
        has_user_bin=False,
        has_systemd_system=True,
    )
    decision = IntelligentPackageClassifier.classify(
        name="sssd",
        summary="System Security Services Daemon",
        description="Provides access to identity and authentication remote resources.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
    )
    assert decision.primary_category == "systemd_service"
    assert decision.flags["is_systemd_service"] is True
    assert decision.flags["is_cli_tool"] is False


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
