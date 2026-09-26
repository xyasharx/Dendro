# dendro/core/backend.py
"""
High-performance native backend engine for Dendro.
Features native librpm and libdnf5 bindings, AppStream catalog truth analysis,
strict top-down category precedence, file integrity verification (rpm -V),
native RPM changelog extraction, available system updates checking,
software repository management, and multi-stage Polkit transactions.
"""
from __future__ import annotations

import configparser
import glob
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, Final, List, Optional, Set, Tuple, Union

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QRunnable, pyqtSignal, pyqtSlot

# Thread-safety lock for native C librpm and libdnf5 calls
RPM_GLOBAL_LOCK: Final[threading.RLock] = threading.RLock()

# =============================================================================
# Native Library Detection & Feature Flags
# =============================================================================

try:
    import rpm  # type: ignore[import-untyped]
    HAS_NATIVE_RPM: Final[bool] = True
except ImportError:
    HAS_NATIVE_RPM = False

try:
    import libdnf5  # type: ignore[import-untyped]
    import libdnf5.base  # type: ignore[import-untyped]
    import libdnf5.rpm  # type: ignore[import-untyped]
    import libdnf5.repo  # type: ignore[import-untyped]
    import libdnf5.transaction  # type: ignore[import-untyped]
    HAS_LIBDNF5: Final[bool] = True
except ImportError:
    HAS_LIBDNF5 = False


# =============================================================================
# Helper Utilities for Native RPM Extraction
# =============================================================================

def _decode_rpm_str(val: Any) -> str:
    """Safely normalises byte strings or objects from librpm headers into UTF-8 strings."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


# =============================================================================
# Environment & Host Execution Helpers
# =============================================================================

def is_running_in_flatpak() -> bool:
    """Detects if the application is running inside a Flatpak sandbox container."""
    return os.path.exists("/.flatpak-info")


def get_host_command_prefix() -> List[str]:
    """Provides the flatpak-spawn prefix when host access is required."""
    if is_running_in_flatpak() and shutil.which("flatpak-spawn"):
        return ["flatpak-spawn", "--host"]
    return []


def get_clean_env() -> Dict[str, str]:
    """Generates a sanitised environment dictionary stripped of GUI/Python library paths."""
    env = os.environ.copy()
    for var in (
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
    ):
        env.pop(var, None)
    return env


def get_dnf_binary_path() -> str:
    """Returns the host or container path to the DNF5 or DNF4 binary."""
    prefix = get_host_command_prefix()
    if prefix:
        return "/usr/bin/dnf5" if os.path.exists("/run/host/usr/bin/dnf5") else "/usr/bin/dnf"
    return shutil.which("dnf5") or shutil.which("dnf") or "/usr/bin/dnf"


def create_rpm_transaction_set() -> Optional[object]:
    """
    Creates a thread-safe read-only rpm.TransactionSet protected by RPM_GLOBAL_LOCK.
    Signature checking is bypassed to ensure low-latency lookups.
    """
    if not HAS_NATIVE_RPM or is_running_in_flatpak():
        return None
    with RPM_GLOBAL_LOCK:
        try:
            ts = rpm.TransactionSet()
            if hasattr(rpm, "_RPMVSF_NOSIGNATURES"):
                ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
            return ts
        except Exception:
            return None


def create_libdnf5_base(load_repos: bool = False) -> Optional[object]:
    """
    Instantiates an isolated libdnf5.base.Base instance safely under RPM_GLOBAL_LOCK.
    Avoids SWIG temporary OptionPath.set() which triggers upstream SIGSEGV (#2379).
    """
    if not HAS_LIBDNF5 or is_running_in_flatpak():
        return None
    with RPM_GLOBAL_LOCK:
        try:
            base = libdnf5.base.Base()

            if hasattr(base, "load_config"):
                base.load_config()
            elif hasattr(base, "load_config_from_file"):
                base.load_config_from_file("/etc/dnf/dnf.conf")

            base.setup()

            if load_repos:
                repo_sack = base.get_repo_sack()
                repo_sack.create_repos_from_system_configuration()
                repo_sack.update_and_load_enabled_repos(False)

            return base
        except Exception:
            return None


# =============================================================================
# Fedora Minimal Base Pillars & Command-Line Whitelist
# =============================================================================

FEDORA_SYSTEM_ROOT_PILLARS: Final[Set[str]] = {
    "kernel", "kernel-core", "kernel-modules", "grub2-common", "grub2-efi-x64", "dracut",
    "systemd", "systemd-udev", "systemd-libs", "glibc", "glibc-common", "coreutils",
    "bash", "sudo", "shadow-utils", "util-linux", "polkit", "pam", "chrony",
    "btrfs-progs", "e2fsprogs", "lvm2", "cryptsetup", "dosfstools", "mdadm",
    "NetworkManager", "firewalld", "selinux-policy", "audit", "iptables",
    "dnf5", "dnf", "rpm", "flatpak"
}

KNOWN_CLI_USER_TOOLS: Final[Set[str]] = {
    "neovim", "vim", "htop", "btop", "tmux", "zsh", "fish", "git",
    "curl", "wget", "ripgrep", "fd-find", "fzf", "tree", "fastfetch",
    "neofetch", "nmap", "ffmpeg", "rsync", "jq", "micro", "bat", "eza",
    "lazygit", "bwrap", "tar", "gzip", "bzip2", "xz", "zip", "unzip",
    "sed", "gawk", "grep", "findutils", "diffutils", "which", "iproute",
    "traceroute", "net-tools", "iperf3", "strace", "gdb", "valgrind",
    "7z", "7za", "p7zip", "7zip", "ranger", "mc", "ncdu", "glances"
}


# =============================================================================
# Core Data Models
# =============================================================================

class PackageState(Enum):
    INSTALLED = auto()
    AVAILABLE = auto()
    MISSING = auto()
    QUEUED_INSTALL = auto()
    QUEUED_REMOVE = auto()


@dataclass(slots=True)
class DependencyNode:
    raw_requirement: str
    resolved_package_name: str
    version_constraint: str = ""
    is_satisfied: bool = True
    is_cycle: bool = False
    is_reverse: bool = False
    sub_dependencies: List[DependencyNode] = field(default_factory=list)


@dataclass(slots=True)
class PackageFileInfo:
    path: str
    size_bytes: int = 0
    mode: str = ""
    is_dir: bool = False
    is_config: bool = False
    is_executable: bool = False


@dataclass(slots=True)
class HistoryEntry:
    id: int
    command_line: str
    date_time: str
    action: str
    altered_count: int
    return_code: int


@dataclass(slots=True)
class DryRunSimulationResult:
    to_install: List[str] = field(default_factory=list)
    to_remove: List[str] = field(default_factory=list)
    to_upgrade: List[str] = field(default_factory=list)
    total_download_size: str = "0 B"
    net_space_diff: str = "0 B"
    has_critical_system_removal: bool = False
    critical_packages: List[str] = field(default_factory=list)
    raw_output: str = ""


@dataclass(slots=True)
class PackageChangelogEntry:
    author: str
    timestamp: int
    date_str: str
    text: str
    cves: List[str] = field(default_factory=list)


@dataclass(slots=True)
class FileVerificationResult:
    path: str
    status_flags: str
    is_config: bool
    is_missing: bool
    size_differs: bool
    mode_differs: bool
    digest_differs: bool
    mtime_differs: bool
    raw_line: str


@dataclass(slots=True)
class AvailableUpdateInfo:
    name: str
    new_version: str
    new_release: str
    arch: str
    repository: str
    is_security: bool = False
    advisory_id: str = ""


@dataclass(slots=True)
class RepoInfo:
    id: str
    name: str
    enabled: bool
    repo_file: str
    is_copr: bool
    is_rpmfusion: bool
    is_core: bool
    baseurl: str = ""


@dataclass(slots=True)
class PackageInfo:
    name: str
    version: str = ""
    release: str = ""
    arch: str = ""
    summary: str = ""
    description: str = ""
    license: str = ""
    url: str = ""
    packager: str = ""
    vendor: str = ""
    build_time: str = ""
    install_time: str = ""
    group: str = "System"
    size_bytes: int = 0
    state: PackageState = PackageState.AVAILABLE

    # Intelligent Classification Insights
    primary_category: str = "General"
    classification_confidence: float = 0.0
    classification_rationale: List[str] = field(default_factory=list)
    secondary_tags: List[str] = field(default_factory=list)

    # Core & Application Flags
    is_orphan: bool = False
    is_user_installed: bool = False
    is_desktop_app: bool = False
    is_cli_tool: bool = False
    is_system_settings: bool = False
    is_fedora_core: bool = False

    # Hardware & System Architecture Flags
    is_graphics_driver: bool = False
    is_audio_sound: bool = False
    is_kernel_module: bool = False
    is_firmware: bool = False
    is_systemd_service: bool = False
    is_security_pkg: bool = False

    # Libraries, Toolkits & Plugins
    is_media_plugin: bool = False
    is_desktop_addon: bool = False
    is_gui_toolkit: bool = False
    is_c_lib: bool = False
    is_font: bool = False
    is_locale: bool = False
    is_devel: bool = False
    is_theme: bool = False
    is_library: bool = False

    # Ecosystem Flags
    is_python_pkg: bool = False
    is_rust_pkg: bool = False
    is_jvm_pkg: bool = False
    is_nodejs_pkg: bool = False

    # Repository & packaging metadata
    repository: str = "Fedora Project"

    # Pending Updates
    has_update: bool = False
    available_update_version: str = ""
    available_update_repo: str = ""

    # Hierarchy and file collections
    dependencies_loaded: bool = False
    dependencies: List[DependencyNode] = field(default_factory=list)
    reverse_dependencies: List[DependencyNode] = field(default_factory=list)
    files: List[PackageFileInfo] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)

    @property
    def full_version(self) -> str:
        return f"{self.version}-{self.release}" if self.release else self.version

    @property
    def human_size(self) -> str:
        size = float(self.size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024.0 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"


# =============================================================================
# Two-Tier Capability & Provider Cache (L1 RAM + L2 SQLite WAL)
# =============================================================================

class SQLiteCapabilityCache:
    _instance: Optional[SQLiteCapabilityCache] = None
    _lock = threading.Lock()

    def __init__(self):
        cache_dir = os.path.expanduser("~/.cache/dendro")
        os.makedirs(cache_dir, exist_ok=True)
        self.db_path = os.path.join(cache_dir, "capabilities_v5.db")
        self._memory_cache: Dict[str, Tuple[bool, str]] = {}
        self._local_storage = threading.local()
        self._init_db()

    @classmethod
    def get_instance(cls) -> SQLiteCapabilityCache:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local_storage, "conn") or self._local_storage.conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local_storage.conn = conn
        return self._local_storage.conn

    def _init_db(self):
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS capabilities (
                cap_name TEXT PRIMARY KEY,
                is_satisfied INTEGER,
                provider_name TEXT
            )
        """)
        conn.commit()

    def get(self, cap_name: str) -> Optional[Tuple[bool, str]]:
        if cap_name in self._memory_cache:
            return self._memory_cache[cap_name]

        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute("SELECT is_satisfied, provider_name FROM capabilities WHERE cap_name = ?", (cap_name,))
            row = cur.fetchone()
            if row:
                res = (bool(row[0]), str(row[1]))
                self._memory_cache[cap_name] = res
                return res
        except Exception:
            pass
        return None

    def set_batch(self, items: List[Tuple[str, bool, str]]):
        if not items:
            return
        for name, sat, prov in items:
            self._memory_cache[name] = (sat, prov)

        try:
            conn = self._get_connection()
            conn.executemany(
                "INSERT OR REPLACE INTO capabilities (cap_name, is_satisfied, provider_name) VALUES (?, ?, ?)",
                [(name, int(sat), prov) for name, sat, prov in items]
            )
            conn.commit()
        except Exception:
            pass


# =============================================================================
# Tier 1: Fedora AppStream Software Catalog Parser (Full 10-Component Support)
# =============================================================================

class AppStreamCatalog:
    """
    Parses and caches the official Fedora AppStream software catalog.
    Extracts all 10 component types defined in the Freedesktop AppStream 1.0-1.2 specification.
    """
    _instance: Optional[AppStreamCatalog] = None
    _lock = threading.Lock()

    def __init__(self):
        self.desktop_packages: Set[str] = set()
        self.console_packages: Set[str] = set()
        self.addon_packages: Set[str] = set()
        self.codec_packages: Set[str] = set()
        self.font_packages: Set[str] = set()
        self.firmware_packages: Set[str] = set()
        self.driver_packages: Set[str] = set()
        self.service_packages: Set[str] = set()
        self.localization_packages: Set[str] = set()
        self.icon_theme_packages: Set[str] = set()
        self.component_categories: Dict[str, Set[str]] = {}
        self._loaded = False
        self._load_catalog()

    @classmethod
    def get_instance(cls) -> AppStreamCatalog:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _load_catalog(self):
        if self._loaded:
            return

        catalog_dirs = [
            "/usr/share/swcatalog/xml",
            "/var/lib/swcatalog/xml",
            "/var/cache/swcatalog/xml",
        ]
        if is_running_in_flatpak():
            catalog_dirs.extend([
                "/run/host/usr/share/swcatalog/xml",
                "/run/host/var/lib/swcatalog/xml",
            ])

        for cat_dir in catalog_dirs:
            if not os.path.isdir(cat_dir):
                continue

            try:
                for file_name in os.listdir(cat_dir):
                    if not (file_name.endswith(".xml") or file_name.endswith(".xml.gz")):
                        continue

                    full_path = os.path.join(cat_dir, file_name)
                    self._parse_appstream_file(full_path)
            except Exception:
                continue

        self._loaded = True

    def _parse_appstream_file(self, file_path: str):
        try:
            open_fn = gzip.open if file_path.endswith(".gz") else open
            with open_fn(file_path, "rb") as f:
                context = ET.iterparse(f, events=("end",))
                for _, elem in context:
                    if elem.tag == "component":
                        comp_type = elem.get("type", "").lower()
                        pkgname_elem = elem.find("pkgname")

                        if pkgname_elem is None:
                            bundle_elem = elem.find("bundle")
                            if bundle_elem is not None and bundle_elem.get("type") == "package":
                                pkgname_elem = bundle_elem

                        if pkgname_elem is not None and pkgname_elem.text:
                            pkg_name = pkgname_elem.text.strip().lower()

                            if comp_type in ("desktop", "desktop-application"):
                                self.desktop_packages.add(pkg_name)
                            elif comp_type in ("console", "console-application"):
                                self.console_packages.add(pkg_name)
                            elif comp_type == "addon":
                                self.addon_packages.add(pkg_name)
                            elif comp_type == "codec":
                                self.codec_packages.add(pkg_name)
                            elif comp_type == "font":
                                self.font_packages.add(pkg_name)
                            elif comp_type == "firmware":
                                self.firmware_packages.add(pkg_name)
                            elif comp_type == "driver":
                                self.driver_packages.add(pkg_name)
                            elif comp_type == "service":
                                self.service_packages.add(pkg_name)
                            elif comp_type == "localization":
                                self.localization_packages.add(pkg_name)
                            elif comp_type in ("icon-theme", "theme"):
                                self.icon_theme_packages.add(pkg_name)

                            cats_elem = elem.find("categories")
                            if cats_elem is not None:
                                cats = {c.text.strip().lower() for c in cats_elem.findall("category") if c.text}
                                if pkg_name in self.component_categories:
                                    self.component_categories[pkg_name].update(cats)
                                else:
                                    self.component_categories[pkg_name] = cats

                        elem.clear()
        except Exception:
            pass


# =============================================================================
# Tier 2: Physical Anatomy Extractor (Strictly Anchored Directory Footprints)
# =============================================================================

@dataclass(slots=True)
class PackagePhysicalAnatomy:
    """
    Physical evidence extracted from RPM headers with strict path anchoring.
    Prevents loose substring matches.
    """
    has_binaries: bool = False
    has_user_bin: bool = False
    has_admin_sbin: bool = False
    has_libexec: bool = False
    has_desktop_file: bool = False
    has_systemd_system: bool = False
    has_systemd_user: bool = False
    has_c_headers: bool = False
    has_fonts_dir: bool = False
    has_themes_dir: bool = False
    has_docs_dir: bool = False
    has_firmware_dir: bool = False
    has_kernel_modules_dir: bool = False
    has_dri_dir: bool = False
    has_plugins_dir: bool = False
    has_kio_dir: bool = False
    has_locales_dir: bool = False
    has_shared_libs_dir: bool = False
    has_man1: bool = False
    has_man8: bool = False
    has_man3: bool = False
    has_python_runtime: bool = False

    exported_sonames: List[str] = field(default_factory=list)
    provides_pkgconfig: bool = False
    provides_font: bool = False
    provides_kmod: bool = False
    provides_appstream: bool = False

    @classmethod
    def from_manifest_data(cls, dirnames: List[str], provides: List[str]) -> PackagePhysicalAnatomy:
        anatomy = cls()
        for d in dirnames:
            d_clean = d.strip().rstrip("/")
            if not d_clean:
                continue

            # 1. Executable Binaries
            if d_clean in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
                anatomy.has_binaries = True
                anatomy.has_user_bin = True
                if d_clean in ("/usr/sbin", "/sbin"):
                    anatomy.has_admin_sbin = True

            # 2. Internal Daemons & Helpers
            elif d_clean == "/usr/libexec" or d_clean.startswith("/usr/libexec/"):
                anatomy.has_libexec = True

            # 3. Desktop Application Launchers
            elif d_clean in ("/usr/share/applications", "/usr/local/share/applications") or d_clean.startswith(("/usr/share/applications/", "/usr/local/share/applications/")):
                anatomy.has_desktop_file = True

            # 4. Systemd Units
            elif d_clean.startswith(("/usr/lib/systemd/system", "/lib/systemd/system")):
                anatomy.has_systemd_system = True
            elif d_clean.startswith("/usr/lib/systemd/user"):
                anatomy.has_systemd_user = True

            # 5. C/C++ Development Headers
            elif d_clean == "/usr/include" or d_clean.startswith("/usr/include/"):
                anatomy.has_c_headers = True

            # 6. Typography
            elif d_clean == "/usr/share/fonts" or d_clean.startswith("/usr/share/fonts/"):
                anatomy.has_fonts_dir = True

            # 7. Themes, Icons & Wallpapers
            elif any(d_clean == p or d_clean.startswith(p + "/") for p in ("/usr/share/themes", "/usr/share/icons", "/usr/share/backgrounds", "/usr/share/sounds")):
                anatomy.has_themes_dir = True

            # 8. Documentation Files (/usr/share/doc strictly)
            elif d_clean == "/usr/share/doc" or d_clean.startswith("/usr/share/doc/"):
                anatomy.has_docs_dir = True

            # 9. Hardware Microcode
            elif d_clean == "/usr/lib/firmware" or d_clean.startswith("/usr/lib/firmware/"):
                anatomy.has_firmware_dir = True

            # 10. Kernel Drivers
            elif d_clean.startswith("/usr/lib/modules/"):
                anatomy.has_kernel_modules_dir = True

            # 11. GPU 3D Acceleration Drivers
            elif d_clean in ("/usr/lib64/dri", "/usr/lib/dri") or d_clean.startswith(("/usr/lib64/dri/", "/usr/lib/dri/")):
                anatomy.has_dri_dir = True
            elif d_clean in ("/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d"):
                anatomy.has_dri_dir = True

            # 12. Plugins, Codecs & Workers
            elif any(p in d_clean for p in ("/qt5/plugins", "/qt6/plugins", "/vlc/plugins", "/gstreamer-1.0", "/plymouth")):
                anatomy.has_plugins_dir = True
                if "/kio" in d_clean:
                    anatomy.has_kio_dir = True

            # 13. System Locales
            elif d_clean == "/usr/share/locale" or d_clean.startswith("/usr/share/locale/"):
                anatomy.has_locales_dir = True

            # 14. Shared Libraries
            elif d_clean in ("/usr/lib64", "/usr/lib"):
                anatomy.has_shared_libs_dir = True

            # 15. Manual Sections (Prioritized over general man checks)
            elif d_clean.endswith("/man/man1") or "/man/man1/" in d_clean:
                anatomy.has_man1 = True
            elif d_clean.endswith("/man/man8") or "/man/man8/" in d_clean:
                anatomy.has_man8 = True
            elif d_clean.endswith("/man/man3") or "/man/man3/" in d_clean:
                anatomy.has_man3 = True
            elif d_clean == "/usr/share/man" or d_clean.startswith("/usr/share/man/"):
                anatomy.has_docs_dir = True

            # 16. Python Site-Packages
            elif "/python3" in d_clean and "site-packages" in d_clean:
                anatomy.has_python_runtime = True

        for prov in provides:
            prov_str = prov.strip()
            if not prov_str:
                continue

            if ".so" in prov_str and "(" in prov_str:
                anatomy.exported_sonames.append(prov_str)
            elif prov_str.startswith("pkgconfig("):
                anatomy.provides_pkgconfig = True
            elif prov_str.startswith("font("):
                anatomy.provides_font = True
            elif prov_str.startswith("kmod("):
                anatomy.provides_kmod = True
            elif prov_str.startswith(("appdata(", "metainfo(")):
                anatomy.provides_appstream = True

        return anatomy

    @classmethod
    def from_rpm_header(cls, header: Any) -> PackagePhysicalAnatomy:
        if not HAS_NATIVE_RPM or header is None:
            return cls()

        try:
            raw_dirs = [_decode_rpm_str(d) for d in (header[rpm.RPMTAG_DIRNAMES] or [])]
            raw_provs = [_decode_rpm_str(p) for p in (header[rpm.RPMTAG_PROVIDENAME] or [])]
            return cls.from_manifest_data(raw_dirs, raw_provs)
        except Exception:
            return cls()


# =============================================================================
# Desktop Entry Metadata Parser (Clean separation of GUI, CLI, & Settings)
# =============================================================================

def parse_installed_desktop_applications() -> Tuple[Set[str], Set[str], Set[str]]:
    gui_apps: Set[str] = set()
    cli_apps: Set[str] = set()
    settings_apps: Set[str] = set()

    search_dirs = [
        "/usr/share/applications",
        "/usr/local/share/applications",
        os.path.expanduser("~/.local/share/applications"),
        "/var/lib/flatpak/exports/share/applications",
    ]
    if is_running_in_flatpak():
        search_dirs.extend([
            "/run/host/usr/share/applications",
            "/run/host/usr/local/share/applications"
        ])

    for directory in search_dirs:
        if not os.path.isdir(directory):
            continue
        for root, _, files in os.walk(directory):
            for file in files:
                if not file.endswith(".desktop"):
                    continue

                full_path = os.path.join(root, file)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        in_entry = False
                        is_app = False
                        no_display = False
                        terminal = False
                        is_settings = False
                        exec_bin = ""

                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            if line.startswith("["):
                                in_entry = (line == "[Desktop Entry]")
                                continue

                            if in_entry and "=" in line:
                                key, val = line.split("=", 1)
                                key = key.strip()
                                val = val.strip()

                                if key == "Type":
                                    if val == "Application":
                                        is_app = True
                                elif key == "NoDisplay":
                                    if val.lower() == "true":
                                        no_display = True
                                elif key == "Terminal":
                                    if val.lower() == "true":
                                        terminal = True
                                elif key == "Categories":
                                    if any(cat in val for cat in ("Settings", "HardwareSettings", "PackageManager", "X-KDE-settings")):
                                        is_settings = True
                                elif key == "Exec" and not exec_bin:
                                    token = val.split()[0].strip('"\'')
                                    exec_bin = os.path.basename(token).lower()

                        if is_app:
                            desktop_base = os.path.splitext(file)[0].lower()
                            
                            # Clean Specification Separation
                            if is_settings:
                                settings_apps.add(desktop_base)
                                if exec_bin:
                                    settings_apps.add(exec_bin)
                            elif terminal:
                                cli_apps.add(desktop_base)
                                if exec_bin:
                                    cli_apps.add(exec_bin)
                            elif not no_display:
                                gui_apps.add(desktop_base)
                                if exec_bin:
                                    gui_apps.add(exec_bin)

                except Exception:
                    continue

    return gui_apps, cli_apps, settings_apps


# =============================================================================
# Intelligent Semantic Intent Profiler
# =============================================================================

class SemanticIntentAnalyzer:
    LEXICON_DESKTOP_GUI: Final[Dict[str, float]] = {
        "graphical": 2.0, "gui": 2.0, "desktop": 1.8, "viewer": 1.8,
        "editor": 1.5, "player": 1.8, "browser": 2.0, "client": 1.2,
        "canvas": 1.5, "window": 1.2, "calculator": 2.0, "drawing": 2.0,
    }

    LEXICON_CLI_TOOL: Final[Dict[str, float]] = {
        "command-line": 3.0, "command line": 3.0, "cli": 3.0, "terminal": 2.5,
        "console": 2.0, "utility": 1.5, "debugger": 2.2, "benchmark": 2.0,
        "interactive process": 2.5, "shell": 1.8, "generator": 1.2,
        "linter": 2.2, "formatter": 2.0, "downloader": 1.5,
    }

    LEXICON_DAEMON_SERVICE: Final[Dict[str, float]] = {
        "daemon": 3.0, "service": 2.2, "background process": 2.8, "server": 2.0,
        "monitoring": 1.5, "listener": 2.0, "supervisor": 2.0, "agent": 1.5,
        "proxy": 1.8, "broker": 2.0, "scheduler": 1.8,
    }

    LEXICON_LIBRARY: Final[Dict[str, float]] = {
        "shared library": 3.0, "library": 2.0, "c library": 2.8, "c++ library": 2.8,
        "bindings": 2.5, "api": 1.8, "wrapper": 1.8, "sdk": 1.5,
        "framework": 1.5, "header files": 2.5, "development files": 2.5,
    }

    LEXICON_TOOLKIT: Final[Dict[str, float]] = {
        "toolkit": 3.0, "widget": 2.8, "gui toolkit": 3.5, "widget set": 3.0,
        "tcl/tk": 3.0, "tkinter": 3.5, "user interface components": 2.5,
    }

    LEXICON_PLUGIN: Final[Dict[str, float]] = {
        "plugin": 3.0, "plugins": 3.0, "codec": 3.0, "extension": 2.0,
        "format plugin": 3.5, "decoders": 2.5, "encoders": 2.5,
    }

    LEXICON_SECURITY: Final[Dict[str, float]] = {
        "cryptographic": 2.5, "encryption": 2.5, "security": 2.0, "authentication": 2.5,
        "authorization": 2.5, "firewall": 2.5, "selinux": 3.0, "pam": 2.5,
    }

    @classmethod
    def score_text(cls, text: str) -> Dict[str, float]:
        if not text:
            return {"gui": 0.0, "cli": 0.0, "daemon": 0.0, "lib": 0.0, "toolkit": 0.0, "plugin": 0.0, "sec": 0.0}

        text_lower = text.lower()
        def match_score(lexicon: Dict[str, float]) -> float:
            return sum(weight for term, weight in lexicon.items() if term in text_lower)

        return {
            "gui": match_score(cls.LEXICON_DESKTOP_GUI),
            "cli": match_score(cls.LEXICON_CLI_TOOL),
            "daemon": match_score(cls.LEXICON_DAEMON_SERVICE),
            "lib": match_score(cls.LEXICON_LIBRARY),
            "toolkit": match_score(cls.LEXICON_TOOLKIT),
            "plugin": match_score(cls.LEXICON_PLUGIN),
            "sec": match_score(cls.LEXICON_SECURITY),
        }


# =============================================================================
# Multi-Factor Scored Decision Engine (Definitive Top-Down Precedence Matrix)
# =============================================================================

@dataclass(slots=True)
class ClassificationDecision:
    primary_category: str
    confidence: float
    rationale: List[str]
    secondary_tags: List[str]
    flags: Dict[str, bool]


class IntelligentPackageClassifier:
    """
    Expert decision engine enforcing strict top-down precedence:
    Hardware/Audio -> Root Pillars -> Desktop Apps -> Settings -> CLI Utilities ->
    Services -> Frameworks/Plugins -> Toolkits -> Firmware/Modules -> Themes/Fonts/Locales/Libs.
    """

    @classmethod
    def classify(
        cls,
        name: str,
        summary: str,
        description: str,
        anatomy: PackagePhysicalAnatomy,
        appstream_desktop: bool,
        appstream_console: bool,
        desktop_apps_discovered: Set[str],
        cli_apps_discovered: Set[str],
        settings_apps_discovered: Set[str],
        vendor: str = "",
        packager: str = "",
    ) -> ClassificationDecision:
        name_lower = name.lower()
        full_text = f"{summary} {description}"
        semantic = SemanticIntentAnalyzer.score_text(full_text)
        appstream = AppStreamCatalog.get_instance()

        scores: Dict[str, float] = {}
        reasons: Dict[str, List[str]] = {}

        def add_score(cat: str, delta: float, reason: str):
            scores[cat] = scores.get(cat, 0.0) + delta
            reasons.setdefault(cat, []).append(reason)

        is_libreoffice_suite = name_lower.startswith("libreoffice")

        # 1. Hardware Drivers & Audio Stack
        is_gpu = (
            not is_libreoffice_suite and (
                anatomy.has_dri_dir or
                name_lower in appstream.driver_packages or
                any(kw in name_lower for kw in ("mesa-dri-", "mesa-vulkan-", "mesa-libgbm", "mesa-va-", "vulkan-", "nvidia-", "libdrm", "xorg-x11-drv-"))
            )
        )
        is_audio = (
            not is_libreoffice_suite and
            any(kw in name_lower for kw in ("pipewire", "wireplumber", "alsa-lib", "pulseaudio", "jack-audio"))
        )

        # 2. Strict CLI Identification
        is_explicit_cli = (
            appstream_console or
            name in cli_apps_discovered or
            name_lower in cli_apps_discovered or
            name_lower in KNOWN_CLI_USER_TOOLS
        )

        # 3. Settings Identification
        is_setting = (
            name in settings_apps_discovered or
            name_lower in settings_apps_discovered or
            any(kw in name_lower for kw in ("control-center", "system-config-", "kcm_"))
        )

        # ---------------------------------------------------------------------
        # TIER 1: Dedicated Hardware Drivers & Audio Architecture
        # ---------------------------------------------------------------------
        if is_audio:
            add_score("audio_sound", 32.0, "PipeWire / ALSA audio architecture stack")

        if is_gpu:
            add_score("graphics_driver", 32.0, "GPU hardware driver / 3D DRI acceleration stack")

        # ---------------------------------------------------------------------
        # TIER 2: Fedora Minimal Base Infrastructure (Root Pillars)
        # ---------------------------------------------------------------------
        if (name in FEDORA_SYSTEM_ROOT_PILLARS or name_lower in FEDORA_SYSTEM_ROOT_PILLARS) and not is_gpu and not is_audio:
            add_score("fedora_core", 30.0, "Protected Fedora boot, core identity & package manager infrastructure")

        # ---------------------------------------------------------------------
        # TIER 3: Desktop Applications (Dominant for user GUI software)
        # ---------------------------------------------------------------------
        has_real_desktop_launcher = (
            (appstream_desktop or (anatomy.has_desktop_file and not is_explicit_cli) or name in desktop_apps_discovered or name_lower in desktop_apps_discovered or is_libreoffice_suite)
            and not is_setting
            and not is_explicit_cli
        )

        is_toolkit = (
            any(kw in name_lower for kw in ("tkinter", "pyqt5", "pyqt6", "pyside", "gtk3", "gtk4", "qt5-qtbase", "qt6-qtbase", "wxgtk", "wxwidgets")) or
            (semantic["toolkit"] >= 3.0 and not has_real_desktop_launcher)
        )
        is_plugin = (
            name_lower in appstream.codec_packages or
            any(kw in name_lower for kw in ("-plugins-", "-plugin-", "kimageformats", "imageformats", "gstreamer1-plugins-", "ffmpeg-libs", "vlc-plugin")) or
            (anatomy.has_plugins_dir and not has_real_desktop_launcher)
        )

        if has_real_desktop_launcher and not is_toolkit and not is_plugin and not is_gpu and not is_audio:
            if anatomy.has_binaries or appstream_desktop or is_libreoffice_suite:
                add_score("desktop_app", 28.0, "Verified standalone graphical desktop application launcher")

        if is_setting and not is_gpu and not is_audio:
            add_score("system_settings", 26.0, "System preferences panel or KCM control applet")

        # ---------------------------------------------------------------------
        # TIER 4: Command-Line Utilities
        # ---------------------------------------------------------------------
        is_cli_executable = (
            (anatomy.has_binaries and not anatomy.has_libexec) or
            is_explicit_cli
        )

        if is_cli_executable and not has_real_desktop_launcher and not is_setting and not is_gpu and not is_audio:
            if not anatomy.has_systemd_system and not (anatomy.has_man8 and not anatomy.has_man1 and not is_explicit_cli):
                add_score("cli_tool", 24.0, "Interactive command-line executable in system PATH")
                if anatomy.has_man1:
                    add_score("cli_tool", 2.0, "Provides Section 1 (User Commands) man page")

        # ---------------------------------------------------------------------
        # TIER 5: Systemd Daemons & Background Services
        # ---------------------------------------------------------------------
        if not has_real_desktop_launcher and not is_audio and not (name in FEDORA_SYSTEM_ROOT_PILLARS):
            if anatomy.has_systemd_system or anatomy.has_systemd_user or name_lower in appstream.service_packages:
                add_score("systemd_service", 22.0, "Delivers systemd service/socket/timer unit")
            elif anatomy.has_libexec and not anatomy.has_man1 and not is_explicit_cli:
                add_score("systemd_service", 18.0, "Internal daemon/helper binaries in /usr/libexec")
            elif anatomy.has_man8 and not anatomy.has_man1 and not is_cli_executable:
                add_score("systemd_service", 15.0, "Section 8 (System Administration) service documentation")

        # ---------------------------------------------------------------------
        # TIER 6: Frameworks, Addons, Toolkits & Plugins
        # ---------------------------------------------------------------------
        is_addon = (
            name_lower in appstream.addon_packages or
            anatomy.has_kio_dir or
            any(kw in name_lower for kw in ("kio-core", "kio-extras", "plymouth-plugin-", "gnome-shell-extension-", "kwin-script-"))
        )
        if is_addon and not has_real_desktop_launcher:
            add_score("desktop_addon", 18.0, "Desktop framework worker, shell extension, or Plymouth plugin")

        if is_plugin and not is_gpu and not has_real_desktop_launcher and not is_addon:
            add_score("media_plugin", 18.0, "Media format decoders, codec plugins, or player extensions")

        if is_toolkit and not has_real_desktop_launcher:
            add_score("gui_toolkit", 18.0, "GUI widget toolkit or windowing library bindings")

        # ---------------------------------------------------------------------
        # TIER 7: Firmware & Kernel Modules
        # ---------------------------------------------------------------------
        if (anatomy.has_firmware_dir or name_lower in appstream.firmware_packages) and not has_real_desktop_launcher:
            add_score("firmware", 20.0, "Hardware binary microcode in /usr/lib/firmware")

        if (anatomy.has_kernel_modules_dir or anatomy.provides_kmod) and not has_real_desktop_launcher:
            add_score("kernel_module", 20.0, "Compiled kernel driver module in /usr/lib/modules")

        # ---------------------------------------------------------------------
        # TIER 8: Themes, Fonts, Locales, Devel & C Libraries
        # ---------------------------------------------------------------------
        if (anatomy.has_themes_dir or name_lower in appstream.icon_theme_packages or any(kw in name_lower for kw in ("-theme", "-icon-theme", "-backgrounds", "-wallpapers"))) and not has_real_desktop_launcher:
            add_score("theme", 16.0, "Desktop themes, icon packs, sound themes, or wallpapers")

        if (anatomy.has_fonts_dir or anatomy.provides_font or name_lower in appstream.font_packages or name_lower.endswith(("-fonts", "-font"))) and not has_real_desktop_launcher:
            add_score("font", 16.0, "Typography assets in /usr/share/fonts")

        is_locale_pkg = (
            name_lower in appstream.localization_packages or
            name_lower.startswith(("glibc-langpack-", "langpacks-")) or
            name_lower.endswith(("-lang", "-langpack")) or
            (anatomy.has_locales_dir and not anatomy.has_binaries and not anatomy.exported_sonames)
        )
        if is_locale_pkg and not has_real_desktop_launcher:
            add_score("locale", 16.0, "Localization, translations, and locale dictionaries")

        if (anatomy.has_c_headers or anatomy.provides_pkgconfig or name_lower.endswith(("-devel", "-static"))) and not has_real_desktop_launcher:
            add_score("devel", 15.0, "C/C++ header interfaces (/usr/include) or pkgconfig file")
        elif anatomy.has_docs_dir and not anatomy.has_binaries and not anatomy.exported_sonames and not has_real_desktop_launcher:
            add_score("devel", 12.0, "Software manual and documentation package")

        if anatomy.exported_sonames and not is_gpu and not is_audio and not is_plugin and not is_toolkit and not has_real_desktop_launcher:
            add_score("c_lib", 14.0, f"Exports {len(anatomy.exported_sonames)} dynamic ELF SONAME ABI contracts")

        is_security_component = "selinux" in name_lower or any(kw in name_lower for kw in ("firewalld", "pam-", "shadow-utils", "audit"))
        if is_security_component and not is_cli_executable and not has_real_desktop_launcher and not (name in FEDORA_SYSTEM_ROOT_PILLARS):
            add_score("security_pkg", 16.0, "Dedicated system security, PAM, or SELinux policy component")

        # ---------------------------------------------------------------------
        # Resolution & Confidence Scoring
        # ---------------------------------------------------------------------
        valid_candidates = {cat: score for cat, score in scores.items() if score > 0}
        if not valid_candidates:
            if has_real_desktop_launcher:
                primary = "desktop_app"
            elif is_audio:
                primary = "audio_sound"
            elif is_gpu:
                primary = "graphics_driver"
            elif anatomy.has_binaries:
                primary = "cli_tool"
            elif is_toolkit:
                primary = "gui_toolkit"
            elif is_addon:
                primary = "desktop_addon"
            elif is_plugin:
                primary = "media_plugin"
            elif anatomy.has_themes_dir:
                primary = "theme"
            elif anatomy.has_locales_dir:
                primary = "locale"
            elif anatomy.has_libexec or anatomy.has_systemd_system:
                primary = "systemd_service"
            else:
                primary = "c_lib"
            reasons[primary] = ["Fallback classification based on physical footprint"]
            valid_candidates[primary] = 1.0

        primary_category = max(valid_candidates.items(), key=lambda item: item[1])[0]
        top_score = valid_candidates[primary_category]

        if top_score >= 25.0:
            confidence = min(0.99, 0.90 + (top_score / 100.0) * 0.1)
        else:
            confidence = min(0.94, max(0.65, top_score / (top_score + 4.0)))

        # Secondary Tags
        secondary_tags: List[str] = []
        is_python = (
            name_lower.startswith(("python3-", "python-", "pytest-"))
            or anatomy.has_python_runtime
            or name_lower in ("ansible", "ansible-core", "certbot", "yt-dlp", "black", "flake8", "meson", "pip")
        )
        is_rust = (
            name_lower.startswith(("rust-", "cargo-"))
            or name_lower in ("ripgrep", "bat", "eza", "fd-find")
        )
        is_jvm = name_lower.startswith(("java-", "openjdk-"))
        is_node = name_lower.startswith(("nodejs-", "npm-"))

        if is_python: secondary_tags.append("Python")
        if is_rust: secondary_tags.append("Rust")
        if is_jvm: secondary_tags.append("Java/JVM")
        if is_node: secondary_tags.append("Node.js")

        flags = {
            "is_desktop_app": (primary_category == "desktop_app"),
            "is_cli_tool": (primary_category == "cli_tool"),
            "is_system_settings": (primary_category == "system_settings"),
            "is_graphics_driver": (primary_category == "graphics_driver"),
            "is_audio_sound": (primary_category == "audio_sound"),
            "is_media_plugin": (primary_category == "media_plugin"),
            "is_desktop_addon": (primary_category == "desktop_addon"),
            "is_gui_toolkit": (primary_category == "gui_toolkit"),
            "is_fedora_core": (primary_category == "fedora_core" or (name in FEDORA_SYSTEM_ROOT_PILLARS and not is_gpu and not is_audio)),
            "is_c_lib": (primary_category == "c_lib"),
            "is_systemd_service": (primary_category == "systemd_service"),
            "is_firmware": (primary_category == "firmware"),
            "is_kernel_module": (primary_category == "kernel_module"),
            "is_font": (primary_category == "font"),
            "is_devel": (primary_category == "devel"),
            "is_locale": (primary_category == "locale"),
            "is_theme": (primary_category == "theme"),
            "is_python_pkg": is_python,
            "is_rust_pkg": is_rust,
            "is_jvm_pkg": is_jvm,
            "is_nodejs_pkg": is_node,
            "is_security_pkg": (primary_category == "security_pkg" or "selinux" in name_lower),
            "is_library": primary_category in ("c_lib", "devel", "font", "firmware", "gui_toolkit", "media_plugin", "desktop_addon"),
        }

        return ClassificationDecision(
            primary_category=primary_category,
            confidence=round(confidence, 2),
            rationale=reasons.get(primary_category, ["Classified by top-down decision engine"]),
            secondary_tags=secondary_tags,
            flags=flags,
        )


# =============================================================================
# Unified Backend Signals
# =============================================================================

class BackendSignals(QObject):
    packages_loaded = pyqtSignal(list)
    orphans_loaded = pyqtSignal(set)
    userinstalled_loaded = pyqtSignal(set)
    dependencies_resolved = pyqtSignal(str, list, object)
    reverse_dependencies_resolved = pyqtSignal(str, list)
    package_files_loaded = pyqtSignal(str, list)
    package_details_loaded = pyqtSignal(object)
    history_loaded = pyqtSignal(list)
    dry_run_finished = pyqtSignal(object)
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)
    package_changelog_loaded = pyqtSignal(str, list)
    package_verification_finished = pyqtSignal(str, list)
    system_updates_loaded = pyqtSignal(dict)
    repo_list_loaded = pyqtSignal(list)


# =============================================================================
# Worker: System Package Discovery
# =============================================================================

class PackageQueryWorker(QRunnable):
    def __init__(self, category: str = "all", search_query: str = ""):
        super().__init__()
        self.signals = BackendSignals()
        self.category = category.lower()
        self.search_query = search_query.strip().lower()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        try:
            self.signals.status_update.emit("Scanning AppStream catalog & RPM database...")
            appstream = AppStreamCatalog.get_instance()
            desktop_apps, cli_apps, settings_apps = parse_installed_desktop_applications()

            if HAS_NATIVE_RPM and not is_running_in_flatpak():
                packages = self._query_native_librpm(desktop_apps, cli_apps, settings_apps, appstream)
            else:
                packages = self._query_cli_subprocess(desktop_apps, cli_apps, settings_apps, appstream)

            if self._is_cancelled.is_set():
                return

            packages.sort(key=lambda p: p.name.lower())
            self.signals.packages_loaded.emit(packages)
            self.signals.status_update.emit(f"Loaded {len(packages):,} packages successfully.")

        except Exception as ex:
            self.signals.error_occurred.emit("", f"Failed to query database: {str(ex)}")

    def _query_native_librpm(
        self,
        desktop_apps: Set[str],
        cli_apps: Set[str],
        settings_apps: Set[str],
        appstream: AppStreamCatalog
    ) -> List[PackageInfo]:
        packages: List[PackageInfo] = []
        if not HAS_NATIVE_RPM or is_running_in_flatpak():
            return self._query_cli_subprocess(desktop_apps, cli_apps, settings_apps, appstream)

        with RPM_GLOBAL_LOCK:
            ts = None
            match_iterator = None
            try:
                ts = rpm.TransactionSet()
                if hasattr(rpm, "_RPMVSF_NOSIGNATURES"):
                    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
                match_iterator = ts.dbMatch()
                for header in match_iterator:
                    if self._is_cancelled.is_set():
                        break

                    name = _decode_rpm_str(header[rpm.RPMTAG_NAME])
                    if not name:
                        continue

                    ver = _decode_rpm_str(header[rpm.RPMTAG_VERSION])
                    rel = _decode_rpm_str(header[rpm.RPMTAG_RELEASE])
                    arch = _decode_rpm_str(header[rpm.RPMTAG_ARCH])
                    group = _decode_rpm_str(header[rpm.RPMTAG_GROUP]) or "General"
                    summary = _decode_rpm_str(header[rpm.RPMTAG_SUMMARY])
                    description = _decode_rpm_str(header[rpm.RPMTAG_DESCRIPTION])
                    license_str = _decode_rpm_str(header[rpm.RPMTAG_LICENSE])
                    url = _decode_rpm_str(header[rpm.RPMTAG_URL])
                    packager = _decode_rpm_str(header[rpm.RPMTAG_PACKAGER])
                    vendor = _decode_rpm_str(header[rpm.RPMTAG_VENDOR])

                    b_time_raw = header[rpm.RPMTAG_BUILDTIME]
                    build_time = datetime.fromtimestamp(b_time_raw).strftime('%Y-%m-%d %H:%M') if b_time_raw else ""

                    i_time_raw = header[rpm.RPMTAG_INSTALLTIME]
                    install_time = datetime.fromtimestamp(i_time_raw).strftime('%Y-%m-%d %H:%M') if i_time_raw else ""

                    size_bytes = int(header[rpm.RPMTAG_SIZE] or 0)
                    anatomy = PackagePhysicalAnatomy.from_rpm_header(header)

                    name_clean = name.lower()
                    appstream_desktop = name_clean in appstream.desktop_packages
                    appstream_console = name_clean in appstream.console_packages

                    repo = "Fedora Project"
                    packager_lower = packager.lower()
                    vendor_lower = vendor.lower()
                    if "copr" in packager_lower or "copr" in vendor_lower:
                        repo = "COPR Repository"
                    elif "rpmfusion" in packager_lower or "rpmfusion" in vendor_lower:
                        repo = "RPM Fusion"
                    elif vendor:
                        repo = vendor

                    decision = IntelligentPackageClassifier.classify(
                        name=name,
                        summary=summary,
                        description=description,
                        anatomy=anatomy,
                        appstream_desktop=appstream_desktop,
                        appstream_console=appstream_console,
                        desktop_apps_discovered=desktop_apps,
                        cli_apps_discovered=cli_apps,
                        settings_apps_discovered=settings_apps,
                        vendor=vendor,
                        packager=packager
                    )

                    flags = decision.flags

                    packages.append(
                        PackageInfo(
                            name=name,
                            version=ver,
                            release=rel,
                            arch=arch,
                            summary=summary,
                            description=description,
                            license=license_str,
                            url=url,
                            packager=packager,
                            vendor=vendor,
                            build_time=build_time,
                            install_time=install_time,
                            group=group,
                            size_bytes=size_bytes,
                            state=PackageState.INSTALLED,
                            repository=repo,
                            is_orphan=False,
                            is_user_installed=False,
                            primary_category=decision.primary_category,
                            classification_confidence=decision.confidence,
                            classification_rationale=decision.rationale,
                            secondary_tags=decision.secondary_tags,
                            is_desktop_app=flags["is_desktop_app"],
                            is_cli_tool=flags["is_cli_tool"],
                            is_system_settings=flags["is_system_settings"],
                            is_graphics_driver=flags["is_graphics_driver"],
                            is_audio_sound=flags["is_audio_sound"],
                            is_media_plugin=flags["is_media_plugin"],
                            is_desktop_addon=flags["is_desktop_addon"],
                            is_gui_toolkit=flags["is_gui_toolkit"],
                            is_fedora_core=flags["is_fedora_core"],
                            is_c_lib=flags["is_c_lib"],
                            is_python_pkg=flags["is_python_pkg"],
                            is_rust_pkg=flags["is_rust_pkg"],
                            is_jvm_pkg=flags["is_jvm_pkg"],
                            is_nodejs_pkg=flags["is_nodejs_pkg"],
                            is_kernel_module=flags["is_kernel_module"],
                            is_systemd_service=flags["is_systemd_service"],
                            is_security_pkg=flags["is_security_pkg"],
                            is_firmware=flags["is_firmware"],
                            is_font=flags["is_font"],
                            is_locale=flags["is_locale"],
                            is_devel=flags["is_devel"],
                            is_theme=flags["is_theme"],
                            is_library=flags["is_library"]
                        )
                    )
            except Exception:
                pass
            finally:
                del match_iterator
                del ts

        if not packages and not self._is_cancelled.is_set():
            return self._query_cli_subprocess(desktop_apps, cli_apps, settings_apps, appstream)

        return packages

    def _query_cli_subprocess(
        self,
        desktop_apps: Set[str],
        cli_apps: Set[str],
        settings_apps: Set[str],
        appstream: AppStreamCatalog
    ) -> List[PackageInfo]:
        query_format = (
            "%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}|%{GROUP}|%{SIZE}|%{LICENSE}|"
            "%{URL}|%{PACKAGER}|%{VENDOR}|%{INSTALLTIME:date}|%{SUMMARY}|"
            "[%{DIRNAMES};]|[%{PROVIDENAME};]\n"
        )
        cmd = get_host_command_prefix() + ["rpm", "-qa", "--queryformat", query_format]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            env=get_clean_env(),
            timeout=30
        )

        if proc.returncode != 0:
            raise RuntimeError(f"RPM query failed: {proc.stderr}")

        packages: List[PackageInfo] = []
        for line in proc.stdout.splitlines():
            if self._is_cancelled.is_set():
                return []
            if not line.strip():
                continue

            parts = line.split("|")
            if len(parts) < 12:
                continue

            name, ver, rel, arch, group, size_str, license_str, url, packager, vendor, inst_time, summary = parts[:12]
            try:
                size_bytes = int(size_str)
            except ValueError:
                size_bytes = 0

            repo = "Fedora Project"
            packager_lower = packager.lower()
            vendor_lower = vendor.lower()
            if "copr" in packager_lower or "copr" in vendor_lower:
                repo = "COPR Repository"
            elif "rpmfusion" in packager_lower or "rpmfusion" in vendor_lower:
                repo = "RPM Fusion"
            elif vendor:
                repo = vendor

            name_clean = name.lower()
            appstream_desktop = name_clean in appstream.desktop_packages
            appstream_console = name_clean in appstream.console_packages

            raw_dirs = parts[12].split(";") if len(parts) > 12 else []
            raw_provs = parts[13].split(";") if len(parts) > 13 else []

            anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, raw_provs)

            if (name_clean in desktop_apps or appstream_desktop) and name_clean not in settings_apps:
                anatomy.has_desktop_file = True

            decision = IntelligentPackageClassifier.classify(
                name=name,
                summary=summary,
                description="",
                anatomy=anatomy,
                appstream_desktop=appstream_desktop,
                appstream_console=appstream_console,
                desktop_apps_discovered=desktop_apps,
                cli_apps_discovered=cli_apps,
                settings_apps_discovered=settings_apps,
                vendor=vendor,
                packager=packager
            )

            flags = decision.flags

            packages.append(
                PackageInfo(
                    name=name,
                    version=ver,
                    release=rel,
                    arch=arch,
                    summary=summary,
                    license=license_str,
                    url=url,
                    packager=packager,
                    vendor=vendor,
                    install_time=inst_time,
                    group=group or "General",
                    size_bytes=size_bytes,
                    state=PackageState.INSTALLED,
                    repository=repo,
                    is_orphan=False,
                    is_user_installed=False,
                    primary_category=decision.primary_category,
                    classification_confidence=decision.confidence,
                    classification_rationale=decision.rationale,
                    secondary_tags=decision.secondary_tags,
                    is_desktop_app=flags["is_desktop_app"],
                    is_cli_tool=flags["is_cli_tool"],
                    is_system_settings=flags["is_system_settings"],
                    is_graphics_driver=flags["is_graphics_driver"],
                    is_audio_sound=flags["is_audio_sound"],
                    is_media_plugin=flags["is_media_plugin"],
                    is_desktop_addon=flags["is_desktop_addon"],
                    is_gui_toolkit=flags["is_gui_toolkit"],
                    is_fedora_core=flags["is_fedora_core"],
                    is_c_lib=flags["is_c_lib"],
                    is_python_pkg=flags["is_python_pkg"],
                    is_rust_pkg=flags["is_rust_pkg"],
                    is_jvm_pkg=flags["is_jvm_pkg"],
                    is_nodejs_pkg=flags["is_nodejs_pkg"],
                    is_kernel_module=flags["is_kernel_module"],
                    is_systemd_service=flags["is_systemd_service"],
                    is_security_pkg=flags["is_security_pkg"],
                    is_firmware=flags["is_firmware"],
                    is_font=flags["is_font"],
                    is_locale=flags["is_locale"],
                    is_devel=flags["is_devel"],
                    is_theme=flags["is_theme"],
                    is_library=flags["is_library"]
                )
            )

        return packages


# =============================================================================
# Worker: User-Installed Query (Native libdnf5 PackageQuery with Fallback)
# =============================================================================

class UserInstalledQueryWorker(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = BackendSignals()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        base = create_libdnf5_base(load_repos=False)
        if base is not None:
            try:
                pq = libdnf5.rpm.PackageQuery(base)
                pq.filter_installed()
                if hasattr(pq, "filter_userinstalled"):
                    pq.filter_userinstalled()
                    user_pkgs = {pkg.get_name() for pkg in pq}
                    if not self._is_cancelled.is_set():
                        self.signals.userinstalled_loaded.emit(user_pkgs)
                        return
            except Exception:
                pass

        dnf_bin = get_dnf_binary_path()
        if not dnf_bin and not get_host_command_prefix():
            return

        try:
            cmd = get_host_command_prefix() + [dnf_bin, "repoquery", "--userinstalled", "-q", "--queryformat", "%{name}"]
            res = subprocess.run(cmd, capture_output=True, text=True, env=get_clean_env(), timeout=35)
            if res.returncode == 0 and not self._is_cancelled.is_set():
                user_pkgs = {line.strip() for line in res.stdout.splitlines() if line.strip()}
                self.signals.userinstalled_loaded.emit(user_pkgs)
        except Exception:
            pass


# =============================================================================
# Worker: Leaf / Orphan Packages Query
# =============================================================================

class OrphanQueryWorker(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = BackendSignals()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        base = create_libdnf5_base(load_repos=False)
        if base is not None:
            try:
                pq = libdnf5.rpm.PackageQuery(base)
                pq.filter_installed()
                if hasattr(pq, "filter_leaves"):
                    pq.filter_leaves()
                    orphans = {pkg.get_name() for pkg in pq}
                    if not self._is_cancelled.is_set():
                        self.signals.orphans_loaded.emit(orphans)
                        return
            except Exception:
                pass

        dnf_bin = get_dnf_binary_path()
        if not dnf_bin and not get_host_command_prefix():
            return

        try:
            cmd = get_host_command_prefix() + [dnf_bin, "repoquery", "--unneeded", "-q", "--queryformat", "%{name}"]
            res = subprocess.run(cmd, capture_output=True, text=True, env=get_clean_env(), timeout=35)
            if res.returncode == 0 and not self._is_cancelled.is_set():
                orphans = {line.strip() for line in res.stdout.splitlines() if line.strip()}
                self.signals.orphans_loaded.emit(orphans)
        except Exception:
            pass


# =============================================================================
# Worker: Direct Dependency Tree Hierarchy
# =============================================================================

class DependencyTreeWorker(QRunnable):
    def __init__(self, root_package: str, max_depth: int = 1, target_index: Optional[Any] = None):
        super().__init__()
        self.signals = BackendSignals()
        self.root_package = root_package
        self.max_depth = max_depth
        self.target_index = target_index
        self._is_cancelled = threading.Event()
        self.cache = SQLiteCapabilityCache.get_instance()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        ts = create_rpm_transaction_set()
        try:
            raw_reqs, parsed_reqs = self._fetch_package_requires(self.root_package, ts)
            if not parsed_reqs or self._is_cancelled.is_set():
                self.signals.dependencies_resolved.emit(self.root_package, [], self.target_index)
                return

            caps_to_query: List[str] = []
            for _, cap_name, _ in parsed_reqs:
                cached = self.cache.get(cap_name)
                if cached is None:
                    caps_to_query.append(cap_name)

            if caps_to_query and not self._is_cancelled.is_set():
                self._resolve_capabilities_batch(caps_to_query, ts)

            resolved_nodes: List[DependencyNode] = []
            seen_clean_names: Set[str] = {self.root_package}

            for raw_req, cap_name, constraint in parsed_reqs:
                if self._is_cancelled.is_set():
                    return

                cached = self.cache.get(cap_name)
                is_sat, provider = cached if cached else (True, cap_name)

                if provider in seen_clean_names:
                    continue
                seen_clean_names.add(provider)

                resolved_nodes.append(
                    DependencyNode(
                        raw_requirement=raw_req,
                        resolved_package_name=provider,
                        version_constraint=constraint,
                        is_satisfied=is_sat,
                        is_cycle=False
                    )
                )

            if not self._is_cancelled.is_set():
                self.signals.dependencies_resolved.emit(self.root_package, resolved_nodes, self.target_index)

        except Exception as ex:
            self.signals.error_occurred.emit(self.root_package, f"Dependency error: {str(ex)}")
        finally:
            if ts is not None:
                del ts

    def _fetch_package_requires(
        self, pkg_name: str, ts: Optional[object]
    ) -> Tuple[List[str], List[Tuple[str, str, str]]]:
        raw_reqs: List[str] = []
        parsed_reqs: List[Tuple[str, str, str]] = []

        if ts is not None:
            match = ts.dbMatch("name", pkg_name)
            for hdr in match:
                requires = hdr[rpm.RPMTAG_REQUIRENAME] or []
                flags = hdr[rpm.RPMTAG_REQUIREFLAGS] or []
                versions = hdr[rpm.RPMTAG_REQUIREVERSION] or []

                for i, req in enumerate(requires):
                    req_str = _decode_rpm_str(req)
                    raw_reqs.append(req_str)

                    if req_str.startswith(("rpmlib(", "config(", "/", "rtld(")):
                        continue

                    constraint = ""
                    if i < len(flags) and i < len(versions) and versions[i]:
                        flag = flags[i]
                        op = ""
                        if (flag & rpm.RPMSENSE_LESS) and (flag & rpm.RPMSENSE_EQUAL):
                            op = "<="
                        elif (flag & rpm.RPMSENSE_GREATER) and (flag & rpm.RPMSENSE_EQUAL):
                            op = ">="
                        elif flag & rpm.RPMSENSE_LESS:
                            op = "<"
                        elif flag & rpm.RPMSENSE_GREATER:
                            op = ">"
                        elif flag & rpm.RPMSENSE_EQUAL:
                            op = "="

                        ver_str = _decode_rpm_str(versions[i])
                        if op and ver_str:
                            constraint = f"{op} {ver_str}"

                    parsed_reqs.append((req_str, req_str, constraint))
                break
        else:
            cmd = get_host_command_prefix() + ["rpm", "-qR", pkg_name]
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=5)
            if proc.returncode == 0:
                raw_reqs = proc.stdout.splitlines()

            for req in raw_reqs:
                req = req.strip()
                if not req or req.startswith(("rpmlib(", "config(", "/", "rtld(")):
                    continue

                tokens = re.split(r'([<>=]+)', req, maxsplit=1)
                cap_name = tokens[0].strip()
                constraint = (tokens[1] + tokens[2]) if len(tokens) == 3 else ""
                parsed_reqs.append((req, cap_name, constraint))

        return raw_reqs, parsed_reqs

    def _resolve_capabilities_batch(self, capabilities: List[str], ts: Optional[object]):
        batch_results: List[Tuple[str, bool, str]] = []
        if ts is not None:
            for cap in capabilities:
                match_name = ts.dbMatch("name", cap)
                if match_name.count() > 0:
                    batch_results.append((cap, True, cap))
                    continue

                matches = ts.dbMatch("providename", cap)
                provider = None
                for hdr in matches:
                    provider = _decode_rpm_str(hdr[rpm.RPMTAG_NAME])
                    break
                if provider:
                    batch_results.append((cap, True, provider))
                else:
                    clean_name = re.sub(r'\.so(\.[0-9]+)*(\([^\)]*\))?$', '', cap)
                    batch_results.append((cap, True, clean_name))
        else:
            batch_cmd = get_host_command_prefix() + ["rpm", "-q", "--whatprovides", "--queryformat", "%{NAME}\n"] + capabilities
            batch_proc = subprocess.run(batch_cmd, capture_output=True, text=True, env=get_clean_env(), timeout=6)
            providers = batch_proc.stdout.splitlines()

            for i, cap in enumerate(capabilities):
                if i < len(providers) and "no package provides" not in providers[i]:
                    batch_results.append((cap, True, providers[i].strip()))
                else:
                    clean_name = re.sub(r'\.so(\.[0-9]+)*(\([^\)]*\))?$', '', cap)
                    batch_results.append((cap, True, clean_name))

        self.cache.set_batch(batch_results)


# =============================================================================
# Worker: Reverse Dependencies
# =============================================================================

class ReverseDependencyWorker(QRunnable):
    def __init__(self, target_package: str):
        super().__init__()
        self.signals = BackendSignals()
        self.target_package = target_package
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        try:
            self.signals.status_update.emit(f"Finding packages that depend on '{self.target_package}'...")

            ts = create_rpm_transaction_set()
            reverse_nodes: List[DependencyNode] = []
            seen: Set[str] = set()

            if ts is not None:
                try:
                    matches = ts.dbMatch("requirename", self.target_package)
                    for hdr in matches:
                        if self._is_cancelled.is_set():
                            return

                        pkg_name = _decode_rpm_str(hdr[rpm.RPMTAG_NAME])
                        if pkg_name and pkg_name != self.target_package and pkg_name not in seen:
                            seen.add(pkg_name)
                            reverse_nodes.append(
                                DependencyNode(
                                    raw_requirement=self.target_package,
                                    resolved_package_name=pkg_name,
                                    is_satisfied=True,
                                    is_reverse=True
                                )
                            )
                finally:
                    del ts
            else:
                cmd = get_host_command_prefix() + ["rpm", "-q", "--whatrequires", self.target_package]
                res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=12)

                if res.returncode == 0 and not self._is_cancelled.is_set():
                    for line in res.stdout.splitlines():
                        clean_line = line.strip()
                        if not clean_line or "no package requires" in clean_line.lower():
                            continue

                        pkg_base_name = re.sub(r'-[0-9].*$', '', clean_line)
                        if pkg_base_name not in seen:
                            seen.add(pkg_base_name)
                            reverse_nodes.append(
                                DependencyNode(
                                    raw_requirement=self.target_package,
                                    resolved_package_name=pkg_base_name or clean_line,
                                    is_satisfied=True,
                                    is_reverse=True
                                )
                            )

            if not self._is_cancelled.is_set():
                self.signals.reverse_dependencies_resolved.emit(self.target_package, reverse_nodes)
                self.signals.status_update.emit(f"Found {len(reverse_nodes)} dependents for '{self.target_package}'.")

        except Exception as ex:
            self.signals.error_occurred.emit(self.target_package, f"Reverse dependency error: {str(ex)}")


# =============================================================================
# Worker: Package File Hierarchy Inspector
# =============================================================================

class PackageFilesWorker(QRunnable):
    def __init__(self, package_name: str):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        try:
            files: List[PackageFileInfo] = []
            ts = create_rpm_transaction_set()

            if ts is not None:
                try:
                    matches = ts.dbMatch("name", self.package_name)
                    for hdr in matches:
                        if self._is_cancelled.is_set():
                            return

                        filenames = hdr[rpm.RPMTAG_FILENAMES] or []
                        filesizes = hdr[rpm.RPMTAG_FILESIZES] or []
                        filemodes = hdr[rpm.RPMTAG_FILEMODES] or []

                        for i, raw_path in enumerate(filenames):
                            path = _decode_rpm_str(raw_path)
                            size = int(filesizes[i]) if i < len(filesizes) else 0
                            mode_int = int(filemodes[i]) if i < len(filemodes) else 0
                            mode_octal = oct(mode_int)

                            is_dir = bool(mode_int & 0o040000) or (size == 0 and not os.path.splitext(path)[1])
                            is_config = path.startswith("/etc/")
                            is_executable = bool(mode_int & 0o111) or ("/bin/" in path or "/sbin/" in path)

                            files.append(
                                PackageFileInfo(
                                    path=path,
                                    size_bytes=size,
                                    mode=mode_octal,
                                    is_dir=is_dir,
                                    is_config=is_config,
                                    is_executable=is_executable
                                )
                            )
                        break
                finally:
                    del ts
            else:
                cmd = get_host_command_prefix() + ["rpm", "-ql", "--dump", self.package_name]
                res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=10)

                if res.returncode == 0 and not self._is_cancelled.is_set():
                    for line in res.stdout.splitlines():
                        parts = line.split()
                        if len(parts) >= 6:
                            path = parts[0]
                            size = int(parts[1])
                            is_dir = (size == 0 and not os.path.splitext(path)[1])
                            is_config = path.startswith("/etc/")
                            is_executable = "/bin/" in path or "/sbin/" in path

                            files.append(
                                PackageFileInfo(
                                    path=path,
                                    size_bytes=size,
                                    mode=parts[4],
                                    is_dir=is_dir,
                                    is_config=is_config,
                                    is_executable=is_executable
                                )
                            )

            if not self._is_cancelled.is_set():
                self.signals.package_files_loaded.emit(self.package_name, files)

        except Exception as ex:
            self.signals.error_occurred.emit(self.package_name, f"File query error: {str(ex)}")


# =============================================================================
# Worker: DNF Transaction History
# =============================================================================

class DnfHistoryWorker(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = BackendSignals()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        if HAS_LIBDNF5 and not is_running_in_flatpak():
            try:
                base = create_libdnf5_base(load_repos=False)
                if base is not None and hasattr(base, "get_transaction_history"):
                    history_mgr = base.get_transaction_history()
                    if hasattr(history_mgr, "list_all_transactions"):
                        tx_list = history_mgr.list_all_transactions()
                        history_list: List[HistoryEntry] = []
                        for tx in tx_list:
                            if self._is_cancelled.is_set():
                                return

                            tx_id = tx.get_id() if hasattr(tx, "get_id") else 0
                            cmd = tx.get_command_line() if hasattr(tx, "get_command_line") else "dnf5 transaction"

                            dt_str = ""
                            if hasattr(tx, "get_dt_start") and tx.get_dt_start():
                                dt_str = datetime.fromtimestamp(tx.get_dt_start()).strftime("%Y-%m-%d %H:%M")

                            action = "Transaction"
                            history_list.append(
                                HistoryEntry(
                                    id=tx_id,
                                    command_line=cmd,
                                    date_time=dt_str,
                                    action=action,
                                    altered_count=1,
                                    return_code=0
                                )
                            )

                        if history_list and not self._is_cancelled.is_set():
                            history_list.sort(key=lambda x: x.id, reverse=True)
                            self.signals.history_loaded.emit(history_list)
                            return
            except Exception:
                pass

        dnf_bin = get_dnf_binary_path()
        try:
            cmd = get_host_command_prefix() + [dnf_bin, "history", "list"]
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=15)

            history_list: List[HistoryEntry] = []
            if res.returncode == 0 and not self._is_cancelled.is_set():
                lines = res.stdout.splitlines()
                for line in lines:
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 4 and parts[0].isdigit():
                        hid = int(parts[0])
                        cmd_line = parts[1]
                        dt = parts[2]
                        action = parts[3]
                        altered = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1
                        history_list.append(
                            HistoryEntry(
                                id=hid,
                                command_line=cmd_line,
                                date_time=dt,
                                action=action,
                                altered_count=altered,
                                return_code=0
                            )
                        )

            if not self._is_cancelled.is_set():
                self.signals.history_loaded.emit(history_list)

        except Exception as ex:
            self.signals.error_occurred.emit("", f"History error: {str(ex)}")


# =============================================================================
# Worker: Transaction Simulation
# =============================================================================

class TransactionDryRunWorker(QRunnable):
    def __init__(self, to_install: List[str], to_remove: List[str]):
        super().__init__()
        self.signals = BackendSignals()
        self.to_install = to_install
        self.to_remove = to_remove
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        if HAS_LIBDNF5 and not is_running_in_flatpak():
            try:
                base = create_libdnf5_base(load_repos=True)
                if base is not None:
                    goal = libdnf5.base.Goal(base)
                    for pkg_name in self.to_install:
                        goal.add_install(pkg_name)
                    for pkg_name in self.to_remove:
                        goal.add_remove(pkg_name)

                    transaction = goal.resolve()
                    trans_pkgs = transaction.get_transaction_packages()

                    to_install_res: List[str] = []
                    to_remove_res: List[str] = []
                    to_upgrade_res: List[str] = []
                    critical_pkgs: List[str] = []

                    output_lines: List[str] = [
                        "=== Native libdnf5 Transaction Simulation ===",
                        f"Target Installs: {len(self.to_install)} | Target Removals: {len(self.to_remove)}",
                        ""
                    ]

                    for t_pkg in trans_pkgs:
                        pkg_obj = t_pkg.get_package()
                        p_name = pkg_obj.get_name()
                        action = t_pkg.get_action()

                        action_name = "ALTER"
                        action_str = str(action).lower()

                        if hasattr(libdnf5.transaction, "TransactionItemAction_INSTALL") and action == libdnf5.transaction.TransactionItemAction_INSTALL:
                            to_install_res.append(p_name)
                            action_name = "INSTALL"
                        elif hasattr(libdnf5.transaction, "TransactionItemAction_REMOVE") and action == libdnf5.transaction.TransactionItemAction_REMOVE:
                            to_remove_res.append(p_name)
                            action_name = "REMOVE"
                            if p_name in FEDORA_SYSTEM_ROOT_PILLARS or p_name.lower() in FEDORA_SYSTEM_ROOT_PILLARS:
                                critical_pkgs.append(p_name)
                        elif hasattr(libdnf5.transaction, "TransactionItemAction_UPGRADE") and action == libdnf5.transaction.TransactionItemAction_UPGRADE:
                            to_upgrade_res.append(p_name)
                            action_name = "UPGRADE"
                        elif "install" in action_str:
                            to_install_res.append(p_name)
                            action_name = "INSTALL"
                        elif "remove" in action_str:
                            to_remove_res.append(p_name)
                            action_name = "REMOVE"
                            if p_name in FEDORA_SYSTEM_ROOT_PILLARS or p_name.lower() in FEDORA_SYSTEM_ROOT_PILLARS:
                                critical_pkgs.append(p_name)
                        else:
                            to_upgrade_res.append(p_name)

                        output_lines.append(f"  [{action_name}] {p_name}-{pkg_obj.get_version()}-{pkg_obj.get_release()}.{pkg_obj.get_arch()}")

                    raw_out = "\n".join(output_lines)
                    result = DryRunSimulationResult(
                        to_install=to_install_res,
                        to_remove=to_remove_res,
                        to_upgrade=to_upgrade_res,
                        has_critical_system_removal=bool(critical_pkgs),
                        critical_packages=critical_pkgs,
                        raw_output=raw_out
                    )

                    if not self._is_cancelled.is_set():
                        self.signals.dry_run_finished.emit(result)
                        return
            except Exception:
                pass

        dnf_bin = get_dnf_binary_path()
        try:
            output = ""
            if self.to_remove:
                cmd_rm = get_host_command_prefix() + [dnf_bin, "--assumeno", "remove"] + self.to_remove
                res_rm = subprocess.run(cmd_rm, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=25)
                output += res_rm.stdout + res_rm.stderr
            if self.to_install:
                cmd_in = get_host_command_prefix() + [dnf_bin, "--assumeno", "install"] + self.to_install
                res_in = subprocess.run(cmd_in, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=25)
                output += "\n" + res_in.stdout + res_in.stderr

            result = DryRunSimulationResult(raw_output=output)

            for pillar in FEDORA_SYSTEM_ROOT_PILLARS:
                if re.search(rf"\bRemoving:\s+.*\b{re.escape(pillar)}\b", output, re.IGNORECASE):
                    result.has_critical_system_removal = True
                    result.critical_packages.append(pillar)

            if not self._is_cancelled.is_set():
                self.signals.dry_run_finished.emit(result)

        except Exception as ex:
            self.signals.error_occurred.emit("", f"Dry-run simulation failed: {str(ex)}")


# =============================================================================
# Worker: Package Changelog Extractor (Sub-millisecond native librpm)
# =============================================================================

class PackageChangelogWorker(QRunnable):
    def __init__(self, package_name: str):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name
        self._cve_pattern = re.compile(r'\b(CVE-\d{4}-\d{4,7})\b', re.IGNORECASE)

    @pyqtSlot()
    def run(self):
        entries: List[PackageChangelogEntry] = []
        ts = create_rpm_transaction_set()
        if ts is not None:
            try:
                matches = ts.dbMatch("name", self.package_name)
                for hdr in matches:
                    names = hdr[rpm.RPMTAG_CHANGELOGNAME] or []
                    times = hdr[rpm.RPMTAG_CHANGELOGTIME] or []
                    texts = hdr[rpm.RPMTAG_CHANGELOGTEXT] or []

                    for i in range(len(names)):
                        author = _decode_rpm_str(names[i])
                        t_stamp = int(times[i]) if i < len(times) else 0
                        txt = _decode_rpm_str(texts[i]) if i < len(texts) else ""
                        dt_str = datetime.fromtimestamp(t_stamp).strftime("%Y-%m-%d") if t_stamp else ""
                        cves = list(set(self._cve_pattern.findall(txt)))

                        entries.append(
                            PackageChangelogEntry(
                                author=author,
                                timestamp=t_stamp,
                                date_str=dt_str,
                                text=txt,
                                cves=cves
                            )
                        )
                    break
            except Exception:
                pass
            finally:
                del ts

        if not entries:
            try:
                cmd = get_host_command_prefix() + ["rpm", "-q", "--changelog", self.package_name]
                res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=8)
                if res.returncode == 0:
                    current_header = ""
                    current_body: List[str] = []
                    for line in res.stdout.splitlines():
                        if line.startswith("* "):
                            if current_header and current_body:
                                body_str = "\n".join(current_body).strip()
                                cves = list(set(self._cve_pattern.findall(body_str)))
                                entries.append(PackageChangelogEntry(author=current_header, timestamp=0, date_str="", text=body_str, cves=cves))
                            current_header = line[2:].strip()
                            current_body = []
                        else:
                            current_body.append(line)
                    if current_header and current_body:
                        body_str = "\n".join(current_body).strip()
                        cves = list(set(self._cve_pattern.findall(body_str)))
                        entries.append(PackageChangelogEntry(author=current_header, timestamp=0, date_str="", text=body_str, cves=cves))
            except Exception:
                pass

        self.signals.package_changelog_loaded.emit(self.package_name, entries)


# =============================================================================
# Worker: File Integrity & Tamper Auditor (rpm -V)
# =============================================================================

class PackageVerifyWorker(QRunnable):
    def __init__(self, package_name: str):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name

    @pyqtSlot()
    def run(self):
        results: List[FileVerificationResult] = []
        try:
            cmd = get_host_command_prefix() + ["rpm", "-V", self.package_name]
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=18)
            
            for line in res.stdout.splitlines():
                line = line.rstrip()
                if not line:
                    continue

                if "missing" in line:
                    parts = line.split(None, 1)
                    path = parts[1] if len(parts) > 1 else ""
                    results.append(
                        FileVerificationResult(
                            path=path,
                            status_flags="missing",
                            is_config=False,
                            is_missing=True,
                            size_differs=False,
                            mode_differs=False,
                            digest_differs=False,
                            mtime_differs=False,
                            raw_line=line
                        )
                    )
                    continue

                parts = line.split()
                if len(parts) >= 2:
                    flags = parts[0]
                    is_config = ("c" in parts[1:]) if len(parts) > 2 else False
                    path = parts[-1]

                    results.append(
                        FileVerificationResult(
                            path=path,
                            status_flags=flags,
                            is_config=is_config,
                            is_missing=False,
                            size_differs=("S" in flags),
                            mode_differs=("M" in flags),
                            digest_differs=("5" in flags),
                            mtime_differs=("T" in flags),
                            raw_line=line
                        )
                    )
        except Exception:
            pass

        self.signals.package_verification_finished.emit(self.package_name, results)


# =============================================================================
# Worker: Available System Updates & Security Advisories
# =============================================================================

class SystemUpdatesCheckWorker(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = BackendSignals()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        dnf_bin = get_dnf_binary_path()
        updates_map: Dict[str, AvailableUpdateInfo] = {}

        try:
            is_dnf5 = "dnf5" in dnf_bin
            if is_dnf5:
                cmd = get_host_command_prefix() + [dnf_bin, "check-upgrade", "--json"]
            else:
                cmd = get_host_command_prefix() + [dnf_bin, "check-update", "-q"]

            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=45)
            
            # Exit code 100 indicates updates are available
            if res.returncode in (0, 100) and not self._is_cancelled.is_set():
                if is_dnf5 and res.stdout.strip().startswith("{"):
                    data = json.loads(res.stdout)
                    for section_items in data.values():
                        if isinstance(section_items, list):
                            for item in section_items:
                                p_name = item.get("name", "")
                                if p_name:
                                    updates_map[p_name] = AvailableUpdateInfo(
                                        name=p_name,
                                        new_version=item.get("version", ""),
                                        new_release=item.get("release", ""),
                                        arch=item.get("arch", ""),
                                        repository=item.get("repo", "Updates"),
                                    )
                else:
                    for line in res.stdout.splitlines():
                        parts = line.split()
                        if len(parts) >= 3 and "." in parts[0]:
                            p_name, p_arch = parts[0].rsplit(".", 1)
                            full_ver = parts[1]
                            repo = parts[2]
                            v, r = full_ver.split("-", 1) if "-" in full_ver else (full_ver, "")
                            updates_map[p_name] = AvailableUpdateInfo(
                                name=p_name,
                                new_version=v,
                                new_release=r,
                                arch=p_arch,
                                repository=repo,
                            )
        except Exception:
            pass

        if not self._is_cancelled.is_set():
            self.signals.system_updates_loaded.emit(updates_map)


# =============================================================================
# Helper: YUM / DNF Software Repository Manager (/etc/yum.repos.d)
# =============================================================================

class RepoManagerHelper:
    @staticmethod
    def get_system_repositories() -> List[RepoInfo]:
        repos: List[RepoInfo] = []
        repo_dirs = ["/etc/yum.repos.d"]
        if is_running_in_flatpak():
            repo_dirs.append("/run/host/etc/yum.repos.d")

        for d in repo_dirs:
            if not os.path.isdir(d):
                continue
            for fname in os.listdir(d):
                if not fname.endswith(".repo"):
                    continue
                fpath = os.path.join(d, fname)
                config = configparser.ConfigParser(interpolation=None)
                try:
                    config.read(fpath, encoding="utf-8")
                    for section in config.sections():
                        repo_id = section.strip()
                        name = config.get(section, "name", fallback=repo_id)
                        enabled_val = config.get(section, "enabled", fallback="0").strip().lower()
                        enabled = enabled_val in ("1", "true", "yes")
                        baseurl = config.get(section, "baseurl", fallback="")

                        is_copr = "copr" in repo_id.lower() or "copr" in fname.lower()
                        is_rpmfusion = "rpmfusion" in repo_id.lower() or "rpmfusion" in fname.lower()
                        is_core = not is_copr and not is_rpmfusion

                        repos.append(
                            RepoInfo(
                                id=repo_id,
                                name=name,
                                enabled=enabled,
                                repo_file=fname,
                                is_copr=is_copr,
                                is_rpmfusion=is_rpmfusion,
                                is_core=is_core,
                                baseurl=baseurl
                            )
                        )
                except Exception:
                    continue

        repos.sort(key=lambda r: (not r.is_core, not r.is_rpmfusion, r.id))
        return repos

    @staticmethod
    def build_toggle_repo_args(repo_id: str, enable: bool) -> List[str]:
        dnf_bin = get_dnf_binary_path()
        val_str = "1" if enable else "0"
        if "dnf5" in dnf_bin:
            return ["config-manager", "setopt", f"{repo_id}.enabled={val_str}"]
        else:
            flag = "--set-enabled" if enable else "--set-disabled"
            return ["config-manager", flag, repo_id]

    @staticmethod
    def build_enable_copr_args(copr_repo_spec: str) -> List[str]:
        return ["copr", "enable", "-y", copr_repo_spec]


# =============================================================================
# Privileged Polkit Transaction Runner (Sequential Multi-Stage Execution)
# =============================================================================

class PolkitTransactionRunner(QObject):
    log_received = pyqtSignal(str)
    progress_percent = pyqtSignal(int)
    transaction_finished = pyqtSignal(bool, int)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.process: Optional[QProcess] = None
        self._ansi_cleaner = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        self._line_buffer = ""
        self._queue_stages: List[List[str]] = []

    def execute_transaction(self, to_install: List[str], to_remove: List[str]):
        """Runs package additions and removals cleanly without command argument collision."""
        dnf_bin = get_dnf_binary_path()
        
        if to_install and to_remove:
            self._queue_stages = [
                [dnf_bin, "-y", "install", "--"] + to_install,
                [dnf_bin, "-y", "remove", "--"] + to_remove
            ]
            self._run_next_stage()
        elif to_install:
            self._start_process([dnf_bin, "-y", "install", "--"] + to_install)
        elif to_remove:
            self._start_process([dnf_bin, "-y", "remove", "--"] + to_remove)

    def execute_custom_command(self, custom_dnf_args: List[str]):
        """Executes targeted DNF actions under administrative elevation."""
        dnf_bin = get_dnf_binary_path()
        args: List[str] = [dnf_bin] + custom_dnf_args
        self._start_process(args)

    def _run_next_stage(self):
        if self._queue_stages:
            stage_args = self._queue_stages.pop(0)
            self._start_process(stage_args)

    def _start_process(self, dnf_args: List[str]):
        if self.process and self.process.state() == QProcess.ProcessState.Running:
            self.log_received.emit("Error: Another transaction is currently active.\n")
            return

        self.process = QProcess(self)

        process_env = QProcessEnvironment.systemEnvironment()
        for var in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            process_env.remove(var)
        self.process.setProcessEnvironment(process_env)

        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.finished.connect(self._on_finished)

        prefix = get_host_command_prefix()
        is_root = (os.geteuid() == 0)

        if is_root and not prefix:
            program = dnf_args[0]
            full_args = dnf_args[1:]
            self.log_received.emit("⚡ Running with direct root privileges (bypassing Polkit elevation)...\n")
            self.log_received.emit(f"Executing: {program} {' '.join(full_args)}\n\n")
        else:
            program = prefix[0] if prefix else "pkexec"
            full_args: List[str] = prefix[1:] + ["pkexec"] if prefix else []
            full_args.extend(dnf_args)
            self.log_received.emit("🔒 Requesting administrative authorization...\n")
            self.log_received.emit(f"Executing: {program} {' '.join(full_args)}\n\n")

        self.process.start(program, full_args)

    def cancel_transaction(self):
        """Sends SIGINT/SIGTERM gracefully to avoid corrupting RPM locks."""
        self._queue_stages.clear()
        if self.process and self.process.state() == QProcess.ProcessState.Running:
            self.log_received.emit("\n⚠️ Sending SIGINT to transaction (preserving RPM lock)...\n")
            self.process.terminate()

    def _on_stdout(self):
        if not self.process:
            return
        raw_data = self.process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        clean_text = self._ansi_cleaner.sub('', raw_data)
        self._process_stream_chunks(clean_text)

    def _on_stderr(self):
        if not self.process:
            return
        raw_data = self.process.readAllStandardError().data().decode("utf-8", errors="replace")
        clean_text = self._ansi_cleaner.sub('', raw_data)
        self.log_received.emit(f"[ERR] {clean_text}")

    def _process_stream_chunks(self, text: str):
        self._line_buffer += text
        if '\n' in self._line_buffer or '\r' in self._line_buffer:
            self.log_received.emit(self._line_buffer)
            self._parse_progress(self._line_buffer)
            self._line_buffer = ""

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus):
        if self._line_buffer:
            self.log_received.emit(self._line_buffer)
            self._line_buffer = ""

        success = (exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit)
        
        if success and self._queue_stages:
            self._run_next_stage()
            return

        self.transaction_finished.emit(success, exit_code)

    def _parse_progress(self, text: str):
        match = re.search(r'\[\s*(\d+)%\s*\]', text)
        if match:
            percent = int(match.group(1))
            self.progress_percent.emit(percent)
