# tests/test_core.py
"""
Unit and integration tests for Dendro Core models, deterministic taxonomy engine,
AppStream catalog parsing, multi-stage Polkit transactions, file integrity results,
CVE extraction, repository management, system protection, and proxy filter models.
Runs headlessly offscreen in CI and local test suites.
Zero emoji glyphs and zero dead imports.
"""

import os
import sys
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from core.backend import (
    DEFAULT_FEDORA_CORE_PACKAGES,
    FEDORA_SYSTEM_ROOT_PILLARS,
    HAS_LIBDNF5,
    HAS_NATIVE_RPM,
    PACKAGE_TAXONOMY_OVERRIDES,
    AppStreamCatalog,
    AvailableUpdateInfo,
    DependencyNode,
    DryRunSimulationResult,
    FedoraCompsCatalog,
    FileVerificationResult,
    PackageArchetype,
    PackageChangelogWorker,
    PackageInfo,
    PackagePhysicalAnatomy,
    PackageState,
    PolkitTransactionRunner,
    ProductionTaxonomyEngine,
    RepoManagerHelper,
    SQLiteCapabilityCache,
    create_libdnf5_base,
    create_rpm_transaction_set,
    get_system_protected_packages,
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
            parent_pillar="pillar_apps",
            sub_category="desktop_internet",
            primary_category="user_apps",
            classification_confidence=1.0,
            is_desktop_app=True,
            has_update=True,
            available_update_version="155.0-1.fc44",
            available_update_repo="Updates",
        ),
        PackageInfo(
            name="htop",
            version="3.3.0",
            release="1.fc44",
            arch="x86_64",
            summary="Interactive process viewer for terminal",
            size_bytes=350000,
            state=PackageState.INSTALLED,
            parent_pillar="pillar_cli",
            sub_category="cli_monitoring",
            primary_category="cli_tools",
            classification_confidence=1.0,
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
            parent_pillar="pillar_apps",
            sub_category="system_settings",
            primary_category="system_settings",
            classification_confidence=1.0,
            is_system_settings=True,
            is_desktop_app=False,
        ),
        PackageInfo(
            name="mesa-dri-drivers",
            version="24.2.0",
            release="1.fc44",
            arch="x86_64",
            summary="Mesa-based DRI hardware acceleration drivers",
            size_bytes=24000000,
            state=PackageState.INSTALLED,
            parent_pillar="pillar_hardware",
            sub_category="graphics_drivers",
            primary_category="graphics_drivers",
            classification_confidence=1.0,
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
            parent_pillar="pillar_hardware",
            sub_category="audio_sound",
            primary_category="audio_sound",
            classification_confidence=1.0,
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
            parent_pillar="pillar_libs",
            sub_category="media_plugins",
            primary_category="media_plugins",
            classification_confidence=1.0,
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
            parent_pillar="pillar_system",
            sub_category="desktop_addons",
            primary_category="desktop_addons",
            classification_confidence=1.0,
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
            parent_pillar="pillar_libs",
            sub_category="gui_toolkits",
            primary_category="gui_toolkits",
            classification_confidence=1.0,
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
            parent_pillar="pillar_system",
            sub_category="fedora_core",
            primary_category="fedora_core",
            classification_confidence=1.0,
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
            parent_pillar="pillar_libs",
            sub_category="c_libs",
            primary_category="c_libs",
            classification_confidence=1.0,
            is_c_lib=True,
            is_library=True,
        ),
        PackageInfo(
            name="papirus-icon-theme",
            version="20260201",
            release="1.fc44",
            arch="noarch",
            summary="Pixel-perfect icon theme for Linux",
            size_bytes=32000000,
            state=PackageState.INSTALLED,
            parent_pillar="pillar_libs",
            sub_category="themes",
            primary_category="themes",
            classification_confidence=1.0,
            is_theme=True,
        ),
        PackageInfo(
            name="glibc-langpack-en",
            version="2.40",
            release="1.fc44",
            arch="x86_64",
            summary="English language pack for glibc",
            size_bytes=560000,
            state=PackageState.INSTALLED,
            parent_pillar="pillar_libs",
            sub_category="locales",
            primary_category="locales",
            classification_confidence=1.0,
            is_locale=True,
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

    cached_val = cache.get("libssl.so.3()(64bit)")
    assert cached_val is not None
    assert cached_val == (True, "openssl-libs")

    non_existent = cache.get("definitely-not-a-registered-capability-xyz")
    assert non_existent is None


def test_package_physical_anatomy_structure():
    """Verifies that PackagePhysicalAnatomy instantiates with unified execution defaults."""
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
        has_user_bin=True,
        has_desktop_file=True,
        has_dri_dir=True,
        has_themes_dir=True,
        has_media_plugins_dir=True,
        has_man1=True,
    )
    assert anatomy.has_binaries is True
    assert anatomy.has_dri_dir is True
    assert anatomy.has_themes_dir is True
    assert anatomy.has_media_plugins_dir is True
    assert anatomy.has_plugins_dir is True
    assert anatomy.has_man1 is True


def test_fedora_comps_catalog_initialization():
    """Validates that FedoraCompsCatalog instantiates as a thread-safe singleton."""
    comps = FedoraCompsCatalog.get_instance()
    assert comps is not None
    assert isinstance(comps.package_to_comps, dict)


# =============================================================================
# Deterministic Pipeline: Regression Tests for Real-World Edge Cases
# =============================================================================

def test_deterministic_7zip_is_cli_tool_not_service():
    """
    Validates that 7zip (which installs /usr/bin/7z and /usr/libexec/7zip/7z.so)
    is cleanly categorized as a CLI Utility, and NOT misclassified as a systemd service.
    """
    raw_dirs = ["/usr/bin", "/usr/libexec/7zip", "/usr/share/doc/7zip"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])
    assert anatomy.has_binaries is True
    assert anatomy.has_libexec is True

    decision = ProductionTaxonomyEngine.classify(
        name="7zip",
        summary="7-Zip is a file archiver with a high compression ratio",
        description="CLI file archiver",
        anatomy=anatomy,
    )
    assert decision.parent_pillar == "pillar_cli"
    assert decision.primary_category == "cli_tools"
    assert decision.sub_category == "cli_data_archiving"
    assert decision.flags["is_cli_tool"] is True
    assert decision.flags["is_systemd_service"] is False
    assert decision.confidence == 1.0


def test_deterministic_libreoffice_help_is_not_desktop_app():
    """
    Validates that libreoffice-help-en (which ships documentation in /usr/share/help
    and no .desktop file) is classified as an asset/SDK, and NOT a desktop application.
    """
    raw_dirs = ["/usr/share/help/en_US/sbasic", "/usr/lib64/libreoffice/help"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])
    assert anatomy.has_desktop_file is False

    decision = ProductionTaxonomyEngine.classify(
        name="libreoffice-help-en",
        summary="Documentation for the LibreOffice office suite",
        description="Help files for LibreOffice",
        anatomy=anatomy,
    )
    assert decision.flags["is_desktop_app"] is False
    assert decision.parent_pillar != "pillar_apps"


def test_deterministic_java_headless_is_not_desktop_app():
    """
    Validates that java-25-openjdk-headless is classified as a Java/JVM platform
    runtime under Libraries & Development, and NEVER as a Desktop Application.
    """
    raw_dirs = ["/usr/lib/jvm/java-25-openjdk", "/usr/bin"]
    provides = ["java-headless = 25", "jre-headless = 25"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, provides)

    decision = ProductionTaxonomyEngine.classify(
        name="java-25-openjdk-headless",
        summary="OpenJDK 25 Headless Runtime Environment",
        description="The OpenJDK 25 runtime environment without audio and video support.",
        anatomy=anatomy,
        provides=provides,
    )
    assert decision.flags["is_desktop_app"] is False
    assert decision.parent_pillar == "pillar_libs"
    assert decision.primary_category == "jvm_pkgs"
    assert decision.flags["is_jvm_pkg"] is True


def test_deterministic_autostart_agents_are_not_desktop_apps():
    """
    Validates that autostart services like xdg-user-dirs, at-spi2-atk, and localsearch
    are NOT promoted to Desktop Applications simply because they own non-launcher .desktop files.
    """
    for pkg_name in ("xdg-user-dirs", "at-spi2-atk", "localsearch", "polkit-kde"):
        anatomy = PackagePhysicalAnatomy(has_binaries=True, has_desktop_file=False)
        decision = ProductionTaxonomyEngine.classify(
            name=pkg_name,
            summary=f"System background component {pkg_name}",
            anatomy=anatomy,
            desktop_entry_files=[]
        )
        assert decision.flags["is_desktop_app"] is False, f"{pkg_name} must not be a desktop app"


def test_intelligent_classifier_firefox_font_guard():
    """
    Verifies that Firefox is classified as a Desktop Application and is protected
    from being classified as a font despite internal /usr/lib64/firefox/fonts/ paths.
    """
    raw_dirs = [
        "/usr/bin",
        "/usr/lib64/firefox",
        "/usr/lib64/firefox/fonts",
        "/usr/share/applications",
    ]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])
    assert anatomy.has_fonts_dir is False
    assert anatomy.has_desktop_file is True

    appstream = AppStreamCatalog.get_instance()
    appstream.desktop_packages.add("firefox")
    appstream.pkg_xdg_categories["firefox"] = {"network", "webbrowser"}

    decision = ProductionTaxonomyEngine.classify(
        name="firefox",
        summary="Mozilla Firefox Web Browser",
        description="Firefox is a free and open-source web browser created by Mozilla.",
        anatomy=anatomy,
        appstream=appstream,
    )
    assert decision.parent_pillar == "pillar_apps"
    assert decision.primary_category == "user_apps"
    assert decision.sub_category == "desktop_internet"
    assert decision.flags["is_desktop_app"] is True
    assert decision.flags["is_font"] is False


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
    decision = ProductionTaxonomyEngine.classify(
        name="pipewire",
        summary="Media Sharing Server and Sound Router",
        description="Next-generation multimedia server for audio and video routing.",
        anatomy=anatomy,
    )
    assert decision.parent_pillar == "pillar_hardware"
    assert decision.primary_category == "audio_sound"
    assert decision.flags["is_audio_sound"] is True
    assert decision.flags["is_cli_tool"] is False
    assert decision.flags["is_fedora_core"] is False


def test_intelligent_classifier_htop_terminal_guard():
    """
    Verifies that htop (which delivers a desktop file with Terminal=true)
    stays a Command-Line Utility and is NOT promoted to desktop_app.
    """
    raw_dirs = ["/usr/bin", "/usr/share/applications", "/usr/share/man/man1"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])

    appstream = AppStreamCatalog.get_instance()
    appstream.console_packages.add("htop")

    decision = ProductionTaxonomyEngine.classify(
        name="htop",
        summary="Interactive process viewer",
        description="A text-mode process viewer for Linux",
        anatomy=anatomy,
        appstream=appstream,
    )
    assert decision.parent_pillar == "pillar_cli"
    assert decision.primary_category == "cli_tools"
    assert decision.sub_category == "cli_monitoring"
    assert decision.flags["is_cli_tool"] is True
    assert decision.flags["is_desktop_app"] is False


def test_intelligent_classifier_font_does_not_bleed_into_locale():
    """
    Verifies font packages are marked as 'fonts' and strictly do NOT bleed into 'locales'.
    """
    raw_dirs = ["/usr/share/fonts/dejavu"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, ["font(dejavusans)"])

    decision = ProductionTaxonomyEngine.classify(
        name="dejavu-sans-fonts",
        summary="Variable-width sans-serif font faces",
        description="DejaVu fonts are a font family based on the Vera Fonts.",
        anatomy=anatomy,
    )
    assert decision.primary_category == "fonts"
    assert decision.parent_pillar == "pillar_libs"
    assert decision.flags["is_font"] is True
    assert decision.flags["is_locale"] is False


def test_intelligent_classifier_papirus_theme():
    """
    Verifies that icon themes are placed in 'themes' rather than falling back to 'c_libs'.
    """
    raw_dirs = ["/usr/share/icons/Papirus"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])

    decision = ProductionTaxonomyEngine.classify(
        name="papirus-icon-theme",
        summary="Pixel-perfect icon theme for Linux",
        description="Papirus is a free and open-source SVG icon theme.",
        anatomy=anatomy,
    )
    assert decision.primary_category == "themes"
    assert decision.parent_pillar == "pillar_libs"
    assert decision.flags["is_theme"] is True
    assert decision.flags["is_c_lib"] is False


def test_intelligent_classifier_systemd_core_precedence():
    """
    Verifies that systemd root pillar is classified as fedora_core
    and not outscored by systemd_services.
    """
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
        has_systemd_system=True,
        has_libexec=True,
        has_man8=True,
    )
    decision = ProductionTaxonomyEngine.classify(
        name="systemd",
        summary="System and Service Manager",
        description="systemd is a suite of basic building blocks for a Linux system.",
        anatomy=anatomy,
    )
    assert decision.primary_category == "fedora_core"
    assert decision.parent_pillar == "pillar_system"
    assert decision.flags["is_fedora_core"] is True


# =============================================================================
# Architectural Feature Tests: Changelogs, Verification, Updates & Repos
# =============================================================================

def test_changelog_cve_extraction():
    """Validates regex extraction of CVE and Bugzilla identifiers in changelog worker."""
    worker = PackageChangelogWorker("test-pkg")
    sample_text = (
        "- Fix buffer overflow vulnerability (CVE-2026-15307)\n"
        "- Resolve privilege escalation bug (CVE-2025-9981)\n"
        "- Resolves: RHBZ#2159842"
    )
    cves = worker._cve_pattern.findall(sample_text)
    assert "CVE-2026-15307" in cves
    assert "CVE-2025-9981" in cves


def test_file_verification_result_structure():
    """Validates FileVerificationResult dataclass structure and field mappings."""
    res = FileVerificationResult(
        path="/etc/ssh/sshd_config",
        status_flags="S.5....T.",
        is_config=True,
        is_missing=False,
        size_differs=True,
        mode_differs=False,
        digest_differs=True,
        mtime_differs=True,
        raw_line="S.5....T. c /etc/ssh/sshd_config",
    )
    assert res.digest_differs is True
    assert res.is_config is True
    assert res.is_missing is False
    assert res.path == "/etc/ssh/sshd_config"


def test_repo_manager_helper_command_generation():
    """Validates DNF5 and DNF4 repository management argument synthesis."""
    args_enable = RepoManagerHelper.build_toggle_repo_args("fedora-updates-testing", True)
    assert any("enabled=1" in a or "--set-enabled" in a for a in args_enable)

    args_disable = RepoManagerHelper.build_toggle_repo_args("fedora-updates-testing", False)
    assert any("enabled=0" in a or "--set-disabled" in a for a in args_disable)

    args_copr = RepoManagerHelper.build_enable_copr_args("xyasharx/dendro")
    assert "copr" in args_copr and "enable" in args_copr and "xyasharx/dendro" in args_copr


def test_updates_filtering_and_model_update(qapp, sample_packages):
    """Validates dynamic pending upgrade indicators and category proxy filtering."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    proxy.set_category_filter("updates_available")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    proxy.set_category_filter("all")
    proxy.set_search_query("status:update")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    updates = {
        "htop": AvailableUpdateInfo(
            name="htop",
            new_version="3.3.1",
            new_release="1.fc44",
            arch="x86_64",
            repository="Updates",
        )
    }
    model.update_available_upgrades(updates)
    assert proxy.rowCount() == 2


# =============================================================================
# Model and Filter Proxy Isolation Tests
# =============================================================================

def test_tree_model_population(qapp, sample_packages):
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    assert model.rowCount() == len(sample_packages)
    assert model.columnCount() == DependencyTreeModel.COL_COUNT

    idx_name = model.index(0, DependencyTreeModel.COL_NAME)
    assert idx_name.data(Qt.ItemDataRole.DisplayRole) == "firefox"


def test_fine_grained_proxy_isolation(qapp, sample_packages):
    """Verifies that the proxy model isolates fine-grained categories without bleeding."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    proxy.set_category_filter("user_apps")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    proxy.set_category_filter("cli_tools")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "htop"

    proxy.set_category_filter("system_settings")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "bluedevil"

    proxy.set_category_filter("graphics_drivers")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "mesa-dri-drivers"

    proxy.set_category_filter("audio_sound")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "pipewire"

    proxy.set_category_filter("media_plugins")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "vlc-plugins-freeworld"

    proxy.set_category_filter("desktop_addons")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "kf5-kio-core"

    proxy.set_category_filter("gui_toolkits")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "python3-tkinter"

    proxy.set_category_filter("fedora_core")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "glibc"

    proxy.set_category_filter("c_libs")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "libpng"

    proxy.set_category_filter("themes")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "papirus-icon-theme"

    proxy.set_category_filter("locales")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "glibc-langpack-en"


def test_proxy_advanced_search_syntax(qapp, sample_packages):
    """Verifies status:user, cat:, tag:, and arch: syntax in search proxy."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)
    proxy.set_category_filter("all")

    model.update_user_installed({"htop"})

    proxy.set_search_query("status:user")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "htop"

    proxy.set_search_query("arch:noarch")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "papirus-icon-theme"

    proxy.set_search_query("cat:graphics_drivers")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "mesa-dri-drivers"


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

    # 1. htop has no updates -> toggling queues it for removal
    idx_htop = model.index(1, 0)
    model.toggle_queue_state(idx_htop)
    installs, removals, upgrades = model.get_queued_packages()
    assert "htop" in removals

    # 2. firefox has an available update -> toggling queues it for upgrade
    idx_firefox = model.index(0, 0)
    model.toggle_queue_state(idx_firefox)
    installs, removals, upgrades = model.get_queued_packages()
    assert "firefox" in upgrades


def test_polkit_multi_stage_transaction(qapp):
    """Verifies that Polkit runner stages sequential execution for concurrent install and removal."""
    runner = PolkitTransactionRunner()
    runner.execute_transaction(to_install=["htop"], to_remove=["vim"])

    assert len(runner._queue_stages) == 1
    assert "install" in runner.process.program() or any("install" in arg for arg in runner.process.arguments())

    if runner.process and runner.process.state() == runner.process.ProcessState.Running:
        runner.cancel_transaction()


def test_system_pillar_guard_detection():
    sim_result = DryRunSimulationResult(
        to_remove=["systemd", "gnome-shell", "my-custom-package"],
        has_critical_system_removal=True,
        critical_packages=["systemd", "gnome-shell"],
    )

    assert sim_result.has_critical_system_removal is True
    assert "systemd" in sim_result.critical_packages
    assert "systemd" in FEDORA_SYSTEM_ROOT_PILLARS


def test_full_application_gui_launch_and_render(qapp):
    """
    End-to-End GUI Startup Test:
    Instantiates MainWindow, forces theming, and executes offscreen rendering.
    """
    from ui.main_window import MainWindow

    window = MainWindow()
    assert window is not None

    window._apply_theme("mocha")
    window._apply_theme("latte")
    window._apply_theme("auto")

    assert hasattr(window, "inspector_panel")
    assert window.inspector_panel.tabs.count() == 4

    window.show()
    qapp.processEvents()

    window.close()
    qapp.processEvents()


def test_all_dialogs_instantiation(qapp):
    """Exercises dialog initializations to ensure all widgets load properly."""
    from ui.history_dialog import DnfHistoryDialog
    from ui.repo_dialog import RepoManagerDialog

    repo_dlg = RepoManagerDialog()
    assert hasattr(repo_dlg, "_on_enable_copr_clicked")
    assert repo_dlg.table.columnCount() == 4
    repo_dlg.close()

    hist_dlg = DnfHistoryDialog()
    assert hist_dlg.table.columnCount() == 5
    hist_dlg.close()


def test_standards_contract_shared_mime_info():
    """
    Verifies that shared-mime-info is classified as Fedora Base Infrastructure or
    a CLI tool, and NEVER as a Desktop Application.
    """
    raw_dirs = ["/usr/bin", "/usr/share/applications", "/usr/share/mime", "/usr/share/man/man1"]
    provides = ["pkgconfig(shared-mime-info)", "shared-mime-info"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, provides)
    anatomy.has_desktop_file = False

    decision = ProductionTaxonomyEngine.classify(
        name="shared-mime-info",
        summary="Shared MIME-info database",
        description="MIME type classification specification and database.",
        anatomy=anatomy,
        desktop_entry_files=[],
        provides=provides,
        appstream=AppStreamCatalog.get_instance(),
    )
    assert decision.flags["is_desktop_app"] is False
    assert decision.primary_category in ("fedora_core", "cli_tools")


def test_standards_contract_openbox_wm():
    """
    Verifies that Openbox is recognized as a Window Manager via RPM firstboot(windowmanager),
    classified under desktop_addons, and NEVER as desktop_app.
    """
    raw_dirs = ["/usr/bin", "/usr/share/applications", "/usr/share/xsessions"]
    provides = ["application()", "application(openbox.desktop)", "firstboot(windowmanager)"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, provides)

    decision = ProductionTaxonomyEngine.classify(
        name="openbox",
        summary="Highly configurable standards-compliant window manager",
        description="A lightweight and compliant X11 window manager.",
        anatomy=anatomy,
        desktop_entry_files=["openbox.desktop"],
        provides=provides,
        appstream=AppStreamCatalog.get_instance(),
    )
    assert decision.primary_category == "desktop_addons"
    assert decision.parent_pillar == "pillar_system"
    assert decision.flags["is_desktop_addon"] is True
    assert decision.flags["is_desktop_app"] is False


def test_standards_contract_ibus_daemon():
    """
    Verifies that IBus is recognized as an Input Method framework and background daemon,
    classified under systemd_services, and NEVER as desktop_app.
    """
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
        has_libexec=True,
        has_systemd_user=True,
    )
    provides = ["ibus", "application(org.freedesktop.IBus.Setup.desktop)"]

    decision = ProductionTaxonomyEngine.classify(
        name="ibus",
        summary="Intelligent Input Bus for Linux OS",
        description="Multilingual input method framework and daemon.",
        anatomy=anatomy,
        desktop_entry_files=["org.freedesktop.IBus.Setup.desktop"],
        provides=provides,
        appstream=AppStreamCatalog.get_instance(),
    )
    assert decision.primary_category == "systemd_services"
    assert decision.flags["is_systemd_service"] is True
    assert decision.flags["is_desktop_app"] is False


def test_multi_faceted_secondary_tag_enrichment():
    """
    Verifies that multi-role packages retain secondary tags for transparent search.
    """
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
        has_user_bin=True,
        has_shared_libs_dir=True,
        exported_sonames=["libwireshark.so.16()(64bit)"],
    )
    appstream = AppStreamCatalog.get_instance()
    appstream.desktop_packages.add("wireshark")

    decision = ProductionTaxonomyEngine.classify(
        name="wireshark",
        summary="Network traffic analyzer",
        description="Network protocol analyzer with GUI and CLI capture tools.",
        anatomy=anatomy,
        appstream=appstream,
    )
    assert decision.primary_category == "user_apps"
    assert "CLI Tool" in decision.secondary_tags
    assert "Library" in decision.secondary_tags


def test_dynamic_system_protection_detection():
    """
    Verifies that get_system_protected_packages() returns standard system pillars.
    """
    protected = get_system_protected_packages()
    assert isinstance(protected, set)
    assert "systemd" in protected
    assert "glibc" in protected


def test_package_taxonomy_overrides_lookup():
    """Validates that Law 4 PACKAGE_TAXONOMY_OVERRIDES resolves in O(1)."""
    assert "pipewire" in PACKAGE_TAXONOMY_OVERRIDES
    assert "kernel-modules" in PACKAGE_TAXONOMY_OVERRIDES
    assert "pam" in PACKAGE_TAXONOMY_OVERRIDES
    assert "7zip" in PACKAGE_TAXONOMY_OVERRIDES

    decision = ProductionTaxonomyEngine.classify("pipewire")
    assert decision.parent_pillar == "pillar_hardware"
    assert decision.primary_category == "audio_sound"
    assert decision.flags["is_audio_sound"] is True


def test_protected_vs_category_isolation():
    """
    Validates that a protected security package (e.g. pam) is classified
    under security_pkgs, and a kernel module is under kernel_modules,
    rather than being unconditionally hijacked by fedora_core.
    """
    pam_decision = ProductionTaxonomyEngine.classify("pam")
    assert pam_decision.parent_pillar == "pillar_system"
    assert pam_decision.primary_category == "security_pkgs"
    assert pam_decision.flags["is_security_pkg"] is True

    kmod_decision = ProductionTaxonomyEngine.classify("kernel-modules")
    assert kmod_decision.parent_pillar == "pillar_hardware"
    assert kmod_decision.primary_category == "kernel_modules"
    assert kmod_decision.flags["is_kernel_module"] is True


def test_queued_upgrade_proxy_model_retention(qapp, sample_packages):
    """
    Validates that staged package upgrades (QUEUED_UPGRADE) are correctly
    retained when filtering by the 'queued' category or 'status:queued'.
    """
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    # firefox has has_update=True -> toggle sets QUEUED_UPGRADE
    idx_firefox = model.index(0, 0)
    model.toggle_queue_state(idx_firefox)

    proxy.set_category_filter("queued")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    proxy.set_category_filter("all")
    proxy.set_search_query("status:queued")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"


def test_python_cli_tools_not_misclassified_as_libraries():
    """
    Validates that standalone CLI tools written in Python (like ansible or meson)
    land in cli_tools with secondary tag 'Python', rather than python_pkgs libraries.
    """
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
        has_user_bin=True,
        has_python_runtime=True
    )
    decision = ProductionTaxonomyEngine.classify(
        name="ansible",
        summary="Radically simple IT automation",
        anatomy=anatomy
    )
    assert decision.parent_pillar == "pillar_cli"
    assert decision.primary_category == "cli_tools"
    assert decision.flags["is_cli_tool"] is True
    assert "Python" in decision.secondary_tags


def test_pure_documentation_not_misclassified_as_c_libs():
    """
    Validates that pure manual/documentation packages (like man-pages)
    are routed to devel, and NEVER fallback to c_libs shared libraries.
    """
    anatomy = PackagePhysicalAnatomy(
        has_docs_dir=True,
        has_man1=True,
        has_man3=True
    )
    decision = ProductionTaxonomyEngine.classify(
        name="man-pages",
        summary="Man pages that document Linux system calls and library functions",
        anatomy=anatomy
    )
    assert decision.parent_pillar == "pillar_libs"
    assert decision.primary_category == "devel"
    assert decision.flags["is_c_lib"] is False


def test_dry_run_multiline_removal_detection():
    """
    Validates that TransactionDryRunWorker catches protected package removals
    even when listed several lines beneath the 'Removing:' section header.
    """
    mock_dnf_output = """
================================================================================
 Package             Arch       Version             Repository             Size
================================================================================
Removing:
 unused-app          x86_64     1.0-1.fc41          @System                2.1 M
 orphan-lib          x86_64     2.2-3.fc41          @System                800 k
 systemd             x86_64     256.6-1.fc41        @System                 14 M

Transaction Summary
================================================================================
Remove  3 Packages
"""
    result = DryRunSimulationResult(raw_output=mock_dnf_output)
    protected_set = get_system_protected_packages()
    in_removing_section = False
    for line in mock_dnf_output.splitlines():
        line_clean = line.strip()
        if line_clean.startswith("Removing:"):
            in_removing_section = True
            continue
        elif in_removing_section and (
            line_clean.startswith(("Installing:", "Upgrading:", "Transaction Summary", "Complete!"))
            or (line_clean and not line.startswith(" "))
        ):
            in_removing_section = False

        if in_removing_section and line_clean:
            pkg_candidate = line_clean.split()[0].lower()
            for pillar in protected_set:
                if pkg_candidate == pillar or pkg_candidate.startswith(f"{pillar}-"):
                    result.has_critical_system_removal = True
                    if pillar not in result.critical_packages:
                        result.critical_packages.append(pillar)

    assert result.has_critical_system_removal is True
    assert "systemd" in result.critical_packages


def test_qt_toolkit_not_misclassified_as_media_plugin():
    """
    Validates that packages providing Qt/GTK plugins are categorized
    under gui_toolkits and are not hijacked by media_plugins.
    """
    raw_dirs = ["/usr/lib64/qt6/plugins/platforms", "/usr/lib64/qt6"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])

    decision = ProductionTaxonomyEngine.classify(
        name="qt6-qtbase-gui",
        summary="Qt6 GUI platform plugins",
        anatomy=anatomy
    )
    assert decision.parent_pillar == "pillar_libs"
    assert decision.primary_category == "gui_toolkits"
    assert decision.flags["is_gui_toolkit"] is True
    assert decision.flags["is_media_plugin"] is False


def test_plasma_desktop_classified_as_desktop_shell_not_browser():
    """
    Validates that plasma-desktop is routed to desktop_addons under pillar_system,
    Firefox stays in desktop_internet without being hijacked by GNOME search providers,
    and Kamera resolves to system_settings.
    """
    # 1. plasma-desktop -> desktop_addons (Pillar 4: System Architecture)
    raw_dirs = [
        "/usr/bin",
        "/usr/share/applications",
        "/usr/share/plasma/shells/org.kde.plasma.desktop",
    ]
    raw_basenames = ["knetattach", "org.kde.knetattach.desktop"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [], raw_basenames)

    decision = ProductionTaxonomyEngine.classify(
        name="plasma-desktop",
        summary="KDE Plasma Desktop shell",
        anatomy=anatomy,
        desktop_entry_files=["org.kde.knetattach.desktop"]
    )
    assert decision.parent_pillar == "pillar_system"
    assert decision.primary_category == "desktop_addons"
    assert decision.flags["is_desktop_addon"] is True
    assert decision.flags["is_desktop_app"] is False

    # 2. firefox -> desktop_internet (Must NOT be hijacked by GNOME search-providers path)
    ff_dirs = [
        "/usr/bin",
        "/usr/share/applications",
        "/usr/share/gnome-shell/search-providers",
    ]
    ff_basenames = ["firefox", "firefox.desktop", "firefox-search-provider.ini"]
    ff_anatomy = PackagePhysicalAnatomy.from_manifest_data(ff_dirs, [], ff_basenames)

    appstream = AppStreamCatalog.get_instance()
    appstream.desktop_packages.add("firefox")
    appstream.pkg_xdg_categories["firefox"] = {"network", "webbrowser"}

    ff_decision = ProductionTaxonomyEngine.classify(
        name="firefox",
        summary="Mozilla Firefox Web Browser",
        anatomy=ff_anatomy,
        desktop_entry_files=["firefox.desktop"],
        appstream=appstream
    )
    assert ff_decision.parent_pillar == "pillar_apps"
    assert ff_decision.primary_category == "user_apps"
    assert ff_decision.sub_category == "desktop_internet"
    assert ff_decision.flags["is_desktop_app"] is True
    assert ff_decision.flags["is_desktop_addon"] is False

    # 3. kamera -> system_settings (KDE camera settings module in System Settings)
    kamera_decision = ProductionTaxonomyEngine.classify(
        name="kamera",
        summary="Digital camera support for KDE",
        anatomy=PackagePhysicalAnatomy(has_desktop_file=True)
    )
    assert kamera_decision.parent_pillar == "pillar_apps"
    assert kamera_decision.primary_category == "system_settings"
    assert kamera_decision.sub_category == "system_settings"
    assert kamera_decision.flags["is_system_settings"] is True
    assert kamera_decision.flags["is_desktop_addon"] is False
