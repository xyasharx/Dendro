# tests/test_core.py
"""
Unit and integration tests for Dendro Core models, top-down decision engine,
strict path anchoring (firefox font guard, libreoffice driver guard, 7zip security guard,
pipewire audio guard, kio addon disambiguation), and fine-grained proxy filters.
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
    """Provides sample packages across the fine-grained categories."""
    return [
        PackageInfo(
            name="firefox",
            version="154.0",
            release="1.fc44",
            arch="x86_64",
            summary="Mozilla Firefox Web Browser",
            size_bytes=82000000,
            state=PackageState.INSTALLED,
            primary_category="desktop_app",
            classification_confidence=0.98,
            is_desktop_app=True,
        ),
        PackageInfo(
            name="htop",
            version="3.3.0",
            release="1.fc44",
            arch="x86_64",
            summary="Interactive process viewer for terminal",
            size_bytes=350000,
            state=PackageState.INSTALLED,
            primary_category="cli_tool",
            classification_confidence=0.97,
            is_cli_tool=True,
        ),
        PackageInfo(
            name="bluedevil",
            version="6.2.0",
            release="1.fc44",
            arch="x86_64",
            summary="Bluetooth management tools and KCM settings for KDE",
            size_bytes=890000,
            state=PackageState.INSTALLED,
            primary_category="system_settings",
            classification_confidence=0.95,
            is_system_settings=True,
        ),
        PackageInfo(
            name="mesa-dri-drivers",
            version="24.2.0",
            release="1.fc44",
            arch="x86_64",
            summary="Mesa-based DRI hardware acceleration drivers",
            size_bytes=24000000,
            state=PackageState.INSTALLED,
            primary_category="graphics_driver",
            classification_confidence=0.99,
            is_graphics_driver=True,
        ),
        PackageInfo(
            name="pipewire",
            version="1.2.0",
            release="1.fc44",
            arch="x86_64",
            summary="Media Sharing Server and Audio Router",
            size_bytes=4200000,
            state=PackageState.INSTALLED,
            primary_category="audio_sound",
            classification_confidence=0.99,
            is_audio_sound=True,
        ),
        PackageInfo(
            name="vlc-plugins-freeworld",
            version="3.0.21",
            release="1.fc44",
            arch="x86_64",
            summary="Freeworld codecs and decoders for VLC media player",
            size_bytes=1200000,
            state=PackageState.INSTALLED,
            primary_category="media_plugin",
            classification_confidence=0.96,
            is_media_plugin=True,
        ),
        PackageInfo(
            name="kf5-kio-core",
            version="5.116.0",
            release="1.fc44",
            arch="x86_64",
            summary="Network transparent I/O framework plugins and workers",
            size_bytes=3100000,
            state=PackageState.INSTALLED,
            primary_category="desktop_addon",
            classification_confidence=0.95,
            is_desktop_addon=True,
        ),
        PackageInfo(
            name="python3-tkinter",
            version="3.14.0",
            release="1.fc44",
            arch="x86_64",
            summary="A GUI toolkit for Python with Tcl/Tk bindings",
            size_bytes=2100000,
            state=PackageState.INSTALLED,
            primary_category="gui_toolkit",
            classification_confidence=0.95,
            is_gui_toolkit=True,
            is_python_pkg=True,
        ),
        PackageInfo(
            name="glibc",
            version="2.40",
            release="1.fc44",
            arch="x86_64",
            summary="The GNU C Library",
            size_bytes=14000000,
            state=PackageState.INSTALLED,
            primary_category="fedora_core",
            classification_confidence=0.99,
            is_fedora_core=True,
        ),
        PackageInfo(
            name="libpng",
            version="1.6.58",
            release="1.fc44",
            arch="x86_64",
            summary="PNG reference library",
            size_bytes=420000,
            state=PackageState.INSTALLED,
            primary_category="c_lib",
            classification_confidence=0.98,
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
    """Verifies natural language keyword scoring across GUI, CLI, and Toolkit domains."""
    gui_text = "A simple graphical photo viewer and canvas editor for the desktop."
    scores_gui = SemanticIntentAnalyzer.score_text(gui_text)
    assert scores_gui["gui"] > scores_gui["cli"]

    toolkit_text = "Python bindings for the Tkinter GUI toolkit and widget set."
    scores_toolkit = SemanticIntentAnalyzer.score_text(toolkit_text)
    assert scores_toolkit["toolkit"] >= 3.0


def test_package_physical_anatomy_structure():
    """Verifies that PackagePhysicalAnatomy instantiates with unified execution defaults."""
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
        has_user_bin=True,
        has_desktop_file=True,
        has_dri_dir=True,
        has_plugins_dir=True,
        has_man1=True,
    )
    assert anatomy.has_binaries is True
    assert anatomy.has_dri_dir is True
    assert anatomy.has_plugins_dir is True
    assert anatomy.has_man1 is True


# =============================================================================
# Strict Path Anchoring & Precedence Guard Tests (Firefox, LibreOffice, 7zip, PipeWire)
# =============================================================================

def test_intelligent_classifier_firefox_font_guard():
    """
    Verifies that Firefox is classified as a Desktop Application and is protected
    from being classified as a font despite internal /usr/lib64/firefox/fonts/ paths.
    """
    raw_dirs = [
        "/usr/bin",
        "/usr/lib64/firefox",
        "/usr/lib64/firefox/fonts",  # Internal bundled fonts folder
        "/usr/share/applications",
    ]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])
    assert anatomy.has_fonts_dir is False  # Must strictly only match /usr/share/fonts/
    assert anatomy.has_desktop_file is True

    decision = IntelligentPackageClassifier.classify(
        name="firefox",
        summary="Mozilla Firefox Web Browser",
        description="Firefox is a free and open-source web browser created by Mozilla.",
        anatomy=anatomy,
        appstream_desktop=True,
        appstream_console=False,
        desktop_apps_discovered={"firefox"},
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "desktop_app"
    assert decision.flags["is_desktop_app"] is True
    assert decision.flags["is_font"] is False


def test_intelligent_classifier_libreoffice_driver_guard():
    """
    Verifies that LibreOffice core is classified as an Application Suite component
    and is protected from Graphics Drivers despite internal database driver paths.
    """
    raw_dirs = [
        "/usr/bin",
        "/usr/lib64/libreoffice/program",
        "/usr/lib64/libreoffice/program/driver",  # Internal database driver folder
        "/usr/share/applications",
    ]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])
    assert anatomy.has_dri_dir is False  # Must strictly only match /usr/lib64/dri/

    decision = IntelligentPackageClassifier.classify(
        name="libreoffice-core",
        summary="Core module for LibreOffice office suite",
        description="LibreOffice is a powerful office suite with database drivers and spreadsheet tools.",
        anatomy=anatomy,
        appstream_desktop=True,
        appstream_console=False,
        desktop_apps_discovered={"libreoffice"},
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "desktop_app"
    assert decision.flags["is_desktop_app"] is True
    assert decision.flags["is_graphics_driver"] is False


def test_intelligent_classifier_7zip_security_guard():
    """
    Verifies that 7zip is classified as a Command-Line Utility and is protected
    from Security/SELinux categories despite mentions of AES-256 encryption.
    """
    raw_dirs = [
        "/usr/bin",
        "/usr/share/man/man1",
    ]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])
    assert anatomy.has_binaries is True
    assert anatomy.has_man1 is True

    decision = IntelligentPackageClassifier.classify(
        name="7zip",
        summary="7-Zip is a file archiver with a high compression ratio",
        description="Features strong AES-256 encryption in 7z and ZIP formats with high security.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=True,
        desktop_apps_discovered=set(),
        cli_apps_discovered={"7zip", "7z"},
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "cli_tool"
    assert decision.flags["is_cli_tool"] is True
    assert decision.flags["is_security_pkg"] is False


def test_intelligent_classifier_pipewire_audio():
    """
    Verifies that PipeWire is classified under Audio & Sound Architecture
    and is not hijacked as a CLI tool despite delivering /usr/bin helpers.
    """
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
        has_systemd_user=True,
        has_shared_libs_dir=True,
    )
    decision = IntelligentPackageClassifier.classify(
        name="pipewire",
        summary="Media Sharing Server and Sound Router",
        description="Next-generation multimedia server for audio and video routing.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "audio_sound"
    assert decision.flags["is_audio_sound"] is True
    assert decision.flags["is_cli_tool"] is False
    assert decision.flags["is_fedora_core"] is False


# =============================================================================
# Fine-Grained Categorization Tests (Drivers, Addons, Toolkits, Settings)
# =============================================================================

def test_intelligent_classifier_ansible_core():
    """Verifies that Ansible is classified as a CLI tool with Python secondary tagging."""
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
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
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "cli_tool"
    assert decision.confidence >= 0.85
    assert "Python" in decision.secondary_tags
    assert decision.flags["is_cli_tool"] is True


def test_intelligent_classifier_gnome_firmware_guard():
    """Verifies that gnome-firmware is kept as a Desktop App and not mislabeled as firmware."""
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
        has_user_bin=True,
        has_desktop_file=True,
        has_firmware_dir=False,
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
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "desktop_app"
    assert decision.flags["is_desktop_app"] is True
    assert decision.flags["is_firmware"] is False


def test_intelligent_classifier_python3_tkinter_toolkit():
    """Verifies that python3-tkinter is classified under GUI Toolkits, NOT Desktop Apps."""
    anatomy = PackagePhysicalAnatomy(
        has_binaries=False,
        has_desktop_file=False,
        has_python_runtime=True,
    )
    decision = IntelligentPackageClassifier.classify(
        name="python3-tkinter",
        summary="A GUI toolkit for Python with Tcl/Tk bindings",
        description="Tkinter provides object-oriented GUI widget components.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "gui_toolkit"
    assert decision.flags["is_gui_toolkit"] is True
    assert decision.flags["is_desktop_app"] is False


def test_intelligent_classifier_media_plugin_vlc():
    """Verifies that vlc-plugins-freeworld is categorized under Media Plugins, NOT Desktop Apps."""
    anatomy = PackagePhysicalAnatomy(
        has_binaries=False,
        has_desktop_file=False,
        has_plugins_dir=True,
    )
    decision = IntelligentPackageClassifier.classify(
        name="vlc-plugins-freeworld",
        summary="Freeworld codecs and plugins for VLC media player",
        description="Extra decoders and demuxers for media playback.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "media_plugin"
    assert decision.flags["is_media_plugin"] is True
    assert decision.flags["is_desktop_app"] is False


def test_intelligent_classifier_bluedevil_settings():
    """Verifies that bluedevil is categorized under System Settings & Applets, NOT Desktop Apps."""
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
        has_desktop_file=True,
    )
    decision = IntelligentPackageClassifier.classify(
        name="bluedevil",
        summary="KDE Bluetooth management and KCM control settings",
        description="Configuration panel and applet for Bluetooth devices.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
        settings_apps_discovered={"bluedevil", "kcm_bluetooth"},
    )
    assert decision.primary_category == "system_settings"
    assert decision.flags["is_system_settings"] is True
    assert decision.flags["is_desktop_app"] is False


def test_intelligent_classifier_kio_desktop_addon():
    """Verifies that kf5-kio-core is categorized under Desktop Addons and NOT Media Plugins."""
    anatomy = PackagePhysicalAnatomy(
        has_binaries=False,
        has_desktop_file=False,
        has_kio_dir=True,
        has_plugins_dir=True,
    )
    decision = IntelligentPackageClassifier.classify(
        name="kf5-kio-core",
        summary="Core framework for network transparent file access",
        description="KIO worker plugins and integration protocols for KDE.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "desktop_addon"
    assert decision.flags["is_desktop_addon"] is True
    assert decision.flags["is_media_plugin"] is False
    assert decision.flags["is_desktop_app"] is False


def test_intelligent_classifier_mesa_graphics_driver():
    """Verifies that mesa-dri-drivers is separated into Graphics Drivers."""
    anatomy = PackagePhysicalAnatomy(
        has_binaries=False,
        has_dri_dir=True,
        has_shared_libs_dir=True,
    )
    decision = IntelligentPackageClassifier.classify(
        name="mesa-dri-drivers",
        summary="Mesa-based DRI hardware acceleration drivers",
        description="Direct Rendering Infrastructure drivers for AMD, Intel, and Nouveau.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "graphics_driver"
    assert decision.flags["is_graphics_driver"] is True
    assert decision.flags["is_fedora_core"] is False


# =============================================================================
# Core Model and Tree View Filter Proxy Tests
# =============================================================================

def test_tree_model_population(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    assert model.rowCount() == 10
    assert model.columnCount() == DependencyTreeModel.COL_COUNT

    idx_name = model.index(0, DependencyTreeModel.COL_NAME)
    assert idx_name.data(Qt.ItemDataRole.DisplayRole) == "firefox"


def test_fine_grained_proxy_isolation(qapp, sample_packages):
    """Verifies that the proxy model isolates fine-grained categories without bleeding."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    # 1. Desktop Apps (Only standalone user app firefox matches)
    proxy.set_category_filter("user_apps")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    # 2. CLI Tools (Only htop matches)
    proxy.set_category_filter("cli_tools")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "htop"

    # 3. System Settings & Applets (bluedevil)
    proxy.set_category_filter("system_settings")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "bluedevil"

    # 4. Graphics & 3D Drivers (mesa-dri-drivers)
    proxy.set_category_filter("graphics_drivers")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "mesa-dri-drivers"

    # 5. Audio & Sound (pipewire)
    proxy.set_category_filter("audio_sound")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "pipewire"

    # 6. Media Plugins & Codecs (vlc-plugins-freeworld)
    proxy.set_category_filter("media_plugins")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "vlc-plugins-freeworld"

    # 7. Desktop Addons & Workers (kf5-kio-core)
    proxy.set_category_filter("desktop_addons")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "kf5-kio-core"

    # 8. GUI Toolkits (python3-tkinter)
    proxy.set_category_filter("gui_toolkits")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "python3-tkinter"

    # 9. Minimal Fedora Core (glibc)
    proxy.set_category_filter("fedora_core")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "glibc"

    # 10. Shared C Library (libpng)
    proxy.set_category_filter("c_libs")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "libpng"


def test_on_demand_lazy_dependency_attachment(qapp, sample_packages):
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
    sim_result = DryRunSimulationResult(
        to_remove=["systemd", "gnome-shell", "my-custom-package"],
        has_critical_system_removal=True,
        critical_packages=["systemd", "gnome-shell"],
    )

    assert sim_result.has_critical_system_removal is True
    assert "systemd" in sim_result.critical_packages
    assert "systemd" in FEDORA_SYSTEM_ROOT_PILLARS
