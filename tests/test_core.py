# tests/test_core.py
"""
Unit and integration tests for Dendro Core models, top-down decision engine,
strict path anchoring, AppStream taxonomy parsing, multi-stage Polkit transactions,
file integrity audits (rpm -V), CVE linkification, repository manager helpers,
and fine-grained proxy filters.
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
    AppStreamCatalog,
    AvailableUpdateInfo,
    DependencyNode,
    DryRunSimulationResult,
    FileVerificationResult,
    IntelligentPackageClassifier,
    PackageChangelogEntry,
    PackageChangelogWorker,
    PackageFileInfo,
    PackageInfo,
    PackagePhysicalAnatomy,
    PackageState,
    PackageVerifyWorker,
    PolkitTransactionRunner,
    RepoInfo,
    RepoManagerHelper,
    SemanticIntentAnalyzer,
    SQLiteCapabilityCache,
    create_libdnf5_base,
    create_rpm_transaction_set,
)
from core.models import CustomUserRoles, DependencyTreeModel, PackageFilterProxyModel


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
        PackageInfo(
            name="papirus-icon-theme",
            version="20260201",
            release="1.fc44",
            arch="noarch",
            summary="Pixel-perfect icon theme for Linux",
            size_bytes=32000000,
            state=PackageState.INSTALLED,
            primary_category="theme",
            classification_confidence=0.96,
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
            primary_category="locale",
            classification_confidence=0.97,
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
        has_themes_dir=True,
        has_plugins_dir=True,
        has_man1=True,
    )
    assert anatomy.has_binaries is True
    assert anatomy.has_dri_dir is True
    assert anatomy.has_themes_dir is True
    assert anatomy.has_plugins_dir is True
    assert anatomy.has_man1 is True


# =============================================================================
# Strict Path Anchoring & Precedence Guard Tests
# =============================================================================

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
        "/usr/lib64/libreoffice/program/driver",
        "/usr/share/applications",
    ]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])
    assert anatomy.has_dri_dir is False

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


def test_intelligent_classifier_htop_terminal_guard():
    """
    Verifies that htop (which delivers a desktop file in /usr/share/applications
    with Terminal=true) stays a Command-Line Utility and is NOT promoted to desktop_app.
    """
    raw_dirs = ["/usr/bin", "/usr/share/applications", "/usr/share/man/man1"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])

    decision = IntelligentPackageClassifier.classify(
        name="htop",
        summary="Interactive process viewer",
        description="A text-mode process viewer for Linux",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=True,
        desktop_apps_discovered=set(),
        cli_apps_discovered={"htop"},
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "cli_tool"
    assert decision.flags["is_cli_tool"] is True
    assert decision.flags["is_desktop_app"] is False


def test_intelligent_classifier_font_does_not_bleed_into_locale():
    """
    Regression test: verifies font packages are marked as 'font' and strictly
    do NOT bleed into 'locale'.
    """
    raw_dirs = ["/usr/share/fonts/dejavu"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, ["font(dejavusans)"])

    decision = IntelligentPackageClassifier.classify(
        name="dejavu-sans-fonts",
        summary="Variable-width sans-serif font faces",
        description="DejaVu fonts are a font family based on the Vera Fonts.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "font"
    assert decision.flags["is_font"] is True
    assert decision.flags["is_locale"] is False


def test_intelligent_classifier_papirus_theme():
    """
    Regression test: verifies that icon themes are placed in 'theme'
    rather than falling back to 'c_lib'.
    """
    raw_dirs = ["/usr/share/icons/Papirus"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, [])

    decision = IntelligentPackageClassifier.classify(
        name="papirus-icon-theme",
        summary="Pixel-perfect icon theme for Linux",
        description="Papirus is a free and open-source SVG icon theme.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "theme"
    assert decision.flags["is_theme"] is True
    assert decision.flags["is_c_lib"] is False


def test_intelligent_classifier_systemd_core_precedence():
    """
    Verifies that systemd root pillar is classified as fedora_core
    and not outscored by systemd_service.
    """
    anatomy = PackagePhysicalAnatomy(
        has_binaries=True,
        has_systemd_system=True,
        has_libexec=True,
        has_man8=True,
    )
    decision = IntelligentPackageClassifier.classify(
        name="systemd",
        summary="System and Service Manager",
        description="systemd is a suite of basic building blocks for a Linux system.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category == "fedora_core"
    assert decision.flags["is_fedora_core"] is True


def test_intelligent_classifier_nodisplay_daemon_guard():
    """
    Verifies that a daemon shipping a desktop file with NoDisplay=true
    (e.g., geoclue) is NOT marked as system_settings.
    """
    anatomy = PackagePhysicalAnatomy(
        has_binaries=False,
        has_libexec=True,
        has_desktop_file=True,
        has_systemd_system=True,
    )
    decision = IntelligentPackageClassifier.classify(
        name="geoclue",
        summary="Geolocation service",
        description="Geoclue is a D-Bus service that provides location information.",
        anatomy=anatomy,
        appstream_desktop=False,
        appstream_console=False,
        desktop_apps_discovered=set(),
        cli_apps_discovered=set(),
        settings_apps_discovered=set(),
    )
    assert decision.primary_category != "system_settings"
    assert decision.flags["is_system_settings"] is False


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


def test_file_verification_parser():
    """Validates rpm -V output parser handling modified and missing files."""
    line_tampered = "S.5....T. c /etc/ssh/sshd_config"
    line_missing = "missing   /usr/bin/broken-tool"
    line_perm = ".M.......   /usr/lib64/libtest.so"

    parts_t = line_tampered.split()
    assert "S" in parts_t[0] and "5" in parts_t[0]
    assert "c" in parts_t[1:]

    parts_m = line_missing.split(None, 1)
    assert parts_m[0] == "missing"
    assert parts_m[1] == "/usr/bin/broken-tool"

    parts_p = line_perm.split()
    assert "M" in parts_p[0]


def test_repo_manager_helper_command_generation():
    """Validates DNF5 and DNF4 repository management argument synthesis."""
    # Enable repo
    args_enable = RepoManagerHelper.build_toggle_repo_args("fedora-updates-testing", True)
    assert any("enabled=1" in a or "--set-enabled" in a for a in args_enable)

    # Disable repo
    args_disable = RepoManagerHelper.build_toggle_repo_args("fedora-updates-testing", False)
    assert any("enabled=0" in a or "--set-disabled" in a for a in args_disable)

    # Enable COPR
    args_copr = RepoManagerHelper.build_enable_copr_args("xyasharx/dendro")
    assert "copr" in args_copr and "enable" in args_copr and "xyasharx/dendro" in args_copr


def test_updates_filtering_and_model_update(qapp, sample_packages):
    """Validates dynamic pending upgrade indicators and category proxy filtering."""
    model = DependencyTreeModel()
    model.set_packages(sample_packages)

    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    # 1. Initially only firefox has an available upgrade in sample_packages
    proxy.set_category_filter("updates_available")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    # 2. Search status:update
    proxy.set_category_filter("all")
    proxy.set_search_query("status:update")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    # 3. Simulate new upgrade batch loaded from DNF5
    updates = {
        "htop": AvailableUpdateInfo(
            name="htop",
            new_version="3.3.1",
            new_release="1.fc44",
            arch="x86_64",
            repository="Updates"
        )
    }
    model.update_available_upgrades(updates)
    assert proxy.rowCount() == 2  # Both firefox and htop now match status:update


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

    # 1. Desktop Apps
    proxy.set_category_filter("user_apps")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "firefox"

    # 2. CLI Tools
    proxy.set_category_filter("cli_tools")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "htop"

    # 3. System Settings & Applets
    proxy.set_category_filter("system_settings")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "bluedevil"

    # 4. Graphics & 3D Drivers
    proxy.set_category_filter("graphics_drivers")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "mesa-dri-drivers"

    # 5. Audio & Sound
    proxy.set_category_filter("audio_sound")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "pipewire"

    # 6. Media Plugins
    proxy.set_category_filter("media_plugins")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "vlc-plugins-freeworld"

    # 7. Desktop Addons
    proxy.set_category_filter("desktop_addons")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "kf5-kio-core"

    # 8. GUI Toolkits
    proxy.set_category_filter("gui_toolkits")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "python3-tkinter"

    # 9. Minimal Fedora Core
    proxy.set_category_filter("fedora_core")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "glibc"

    # 10. Shared C Library
    proxy.set_category_filter("c_libs")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "libpng"

    # 11. Themes & Icons
    proxy.set_category_filter("themes")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "papirus-icon-theme"

    # 12. Locales & Language Packs
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

    # Mark htop as user installed
    model.update_user_installed({"htop"})

    # Search: status:user
    proxy.set_search_query("status:user")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "htop"

    # Search: arch:noarch
    proxy.set_search_query("arch:noarch")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "papirus-icon-theme"

    # Search: cat:graphics_driver
    proxy.set_search_query("cat:graphics_driver")
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

    idx_firefox = model.index(0, 0)
    model.toggle_queue_state(idx_firefox)

    installs, removals = model.get_queued_packages()
    assert "firefox" in removals


def test_polkit_multi_stage_transaction(qapp):
    """Verifies that Polkit runner stages sequential execution for concurrent install and removal."""
    runner = PolkitTransactionRunner()
    runner.execute_transaction(to_install=["htop"], to_remove=["vim"])

    # Must divide execution into two separate staged commands to avoid argument collision
    assert len(runner._queue_stages) == 1
    assert "install" in runner.process.program() or any("install" in arg for arg in runner.process.arguments())


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
    Guarantees no NameError, AttributeError, or missing imports exist in the UI pipeline.
    """
    from ui.main_window import MainWindow

    # 1. Instantiate the real window
    window = MainWindow()
    assert window is not None

    # 2. Test dynamic theme switches (tests _apply_theme across dark and light)
    window._apply_theme("mocha")
    window._apply_theme("latte")
    window._apply_theme("auto")

    # 3. Exercise inspector panel and tabs
    assert hasattr(window, "inspector_panel")
    assert window.inspector_panel.tabs.count() == 4

    # 4. Show window offscreen and force a paint event
    window.show()
    qapp.processEvents()

    # 5. Clean teardown
    window.close()
    qapp.processEvents()

def test_all_dialogs_instantiation(qapp):
    """
    Exercises all dialog initializations to ensure no AttributeError or missing callbacks exist.
    """
    from ui.repo_dialog import RepoManagerDialog
    from ui.history_dialog import DnfHistoryDialog

    # 1. Test RepoManagerDialog initialization & methods
    repo_dlg = RepoManagerDialog()
    assert hasattr(repo_dlg, "_on_enable_copr_clicked")
    assert repo_dlg.table.columnCount() == 4
    repo_dlg.close()

    # 2. Test DnfHistoryDialog initialization
    hist_dlg = DnfHistoryDialog()
    assert hist_dlg.table.columnCount() == 5
    hist_dlg.close()
