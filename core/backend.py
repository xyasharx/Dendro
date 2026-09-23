# dendro/core/backend.py
"""
High-performance native backend engine for Dendro.
Features native librpm and libdnf5 bindings, AppStream catalog truth analysis,
Fedora 42+ unified /usr/bin & /usr/sbin execution footprint extraction,
/usr/libexec internal helper detection, natural language semantic intent profiling,
two-tier L1/L2 capability caching, and container/root execution bypass.
"""
from __future__ import annotations

import glob
import gzip
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
    Creates an isolated read-only rpm.TransactionSet for the current thread.
    Signature checking is bypassed to ensure low-latency lookups.
    """
    if not HAS_NATIVE_RPM or is_running_in_flatpak():
        return None
    try:
        ts = rpm.TransactionSet()
        if hasattr(rpm, "_RPMVSF_NOSIGNATURES"):
            ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
        return ts
    except Exception:
        return None


def create_libdnf5_base(load_repos: bool = False) -> Optional[object]:
    """
    Instantiates an isolated, thread-safe libdnf5.base.Base instance.
    Correctly invokes base.load_config() and handles unprivileged environments.
    """
    if not HAS_LIBDNF5 or is_running_in_flatpak():
        return None
    try:
        base = libdnf5.base.Base()

        if hasattr(base, "load_config"):
            base.load_config()
        elif hasattr(base, "load_config_from_file"):
            base.load_config_from_file("/etc/dnf/dnf.conf")

        # Set user-level cache directories if running unprivileged (prevents /var/cache write errors)
        if os.geteuid() != 0:
            user_cache_dir = os.path.expanduser("~/.cache/dendro/dnf5")
            os.makedirs(user_cache_dir, exist_ok=True)
            config = base.get_config()
            if hasattr(config, "cachedir"):
                config.cachedir.set(user_cache_dir)

        base.setup()

        if load_repos:
            repo_sack = base.get_repo_sack()
            repo_sack.create_repos_from_system_configuration()
            repo_sack.update_and_load_enabled_repos(False)

        return base
    except Exception:
        return None


# =============================================================================
# Fedora System Pillars & Core Definitions
# =============================================================================

FEDORA_SYSTEM_ROOT_PILLARS: Final[Set[str]] = {
    "kernel", "kernel-core", "kernel-modules", "grub2-common", "grub2-efi-x64", "dracut",
    "systemd", "systemd-udev", "systemd-libs", "glibc", "glibc-common", "coreutils",
    "bash", "sudo", "shadow-utils", "util-linux", "polkit", "pam", "chrony",
    "btrfs-progs", "e2fsprogs", "lvm2", "cryptsetup", "dosfstools", "mdadm",
    "NetworkManager", "firewalld", "selinux-policy", "audit", "iptables",
    "pipewire", "wireplumber", "mesa-dri-drivers", "mesa-vulkan-drivers",
    "xorg-x11-server-Xorg", "xorg-x11-server-Xwayland", "gdm", "sddm",
    "gnome-shell", "mutter", "plasma-desktop", "kwin", "kwin-wayland",
    "dnf5", "dnf", "rpm", "flatpak"
}

KNOWN_CLI_USER_TOOLS: Final[Set[str]] = {
    "neovim", "vim", "htop", "btop", "tmux", "zsh", "fish", "git",
    "curl", "wget", "ripgrep", "fd-find", "fzf", "tree", "fastfetch",
    "neofetch", "nmap", "ffmpeg", "rsync", "jq", "micro", "bat", "eza",
    "lazygit", "bwrap", "tar", "gzip", "bzip2", "xz", "zip", "unzip",
    "sed", "gawk", "grep", "findutils", "diffutils", "which", "iproute",
    "traceroute", "net-tools", "iperf3", "strace", "gdb", "valgrind"
}


# =============================================================================
# Core Data Models (Preserving Complete Public Interface)
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

    # Categorization flags (Preserved for full UI compatibility)
    is_orphan: bool = False
    is_desktop_app: bool = False
    is_cli_tool: bool = False
    is_fedora_core: bool = False
    is_c_lib: bool = False
    is_python_pkg: bool = False
    is_rust_pkg: bool = False
    is_jvm_pkg: bool = False
    is_nodejs_pkg: bool = False
    is_kernel_module: bool = False
    is_systemd_service: bool = False
    is_security_pkg: bool = False
    is_firmware: bool = False
    is_font: bool = False
    is_locale: bool = False
    is_devel: bool = False
    is_theme: bool = False
    is_library: bool = False

    # Repository & packaging metadata
    repository: str = "Fedora Project"

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
# Tier 1: Fedora AppStream Software Catalog Parser
# =============================================================================

class AppStreamCatalog:
    """
    Parses and caches the official Fedora AppStream software catalog.
    Extracts explicit application types directly mapped to RPM package names (<pkgname>).
    """
    _instance: Optional[AppStreamCatalog] = None
    _lock = threading.Lock()

    def __init__(self):
        self.desktop_packages: Set[str] = set()
        self.console_packages: Set[str] = set()
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
                        comp_type = elem.get("type", "")
                        pkgname_elem = elem.find("pkgname")

                        if pkgname_elem is not None and pkgname_elem.text:
                            pkg_name = pkgname_elem.text.strip().lower()

                            if comp_type in ("desktop", "desktop-application"):
                                self.desktop_packages.add(pkg_name)
                            elif comp_type in ("console", "console-application"):
                                self.console_packages.add(pkg_name)

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
# Tier 2: Physical Anatomy Extractor (Fedora 42+ Unified Execution Footprint & ABI)
# =============================================================================

@dataclass(slots=True)
class PackagePhysicalAnatomy:
    """
    Physical evidence extracted from RPM headers.
    Fully adapted to Fedora 42+ where /usr/sbin is symlinked to /usr/bin.
    Distinguishes internal daemons via /usr/libexec, systemd unit files, and man8.
    """
    has_binaries: bool = False           # Unified /usr/bin, /bin, /usr/sbin, /sbin
    has_user_bin: bool = False           # Alias for backwards compatibility
    has_admin_sbin: bool = False         # Legacy flag for packages specifically mentioning sbin
    has_libexec: bool = False            # /usr/libexec (Internal daemons, helpers, D-Bus backends)
    has_desktop_file: bool = False       # /usr/share/applications/ (Desktop entry present)
    has_systemd_system: bool = False     # /usr/lib/systemd/system/ (System service unit)
    has_systemd_user: bool = False       # /usr/lib/systemd/user/ (User session service)
    has_c_headers: bool = False          # /usr/include/ (Development C/C++ headers)
    has_fonts_dir: bool = False          # /usr/share/fonts/ (Typography)
    has_firmware_dir: bool = False       # /usr/lib/firmware/ (Hardware microcode blobs)
    has_kernel_modules_dir: bool = False # /usr/lib/modules/ (Linux kernel drivers)
    has_locales_dir: bool = False        # /usr/share/locale/
    has_shared_libs_dir: bool = False    # /usr/lib64, /usr/lib
    has_man1: bool = False               # /usr/share/man/man1/ (User command documentation)
    has_man8: bool = False               # /usr/share/man/man8/ (System administration & daemon docs)
    has_man3: bool = False               # /usr/share/man/man3/ (Library call documentation)
    has_python_runtime: bool = False     # /usr/lib/python3.*, site-packages

    # ABI / Capability evidence
    exported_sonames: List[str] = field(default_factory=list) # e.g. libc.so.6, libssl.so.3
    provides_pkgconfig: bool = False     # pkgconfig(...)
    provides_font: bool = False          # font(...)
    provides_kmod: bool = False          # kmod(...)
    provides_appstream: bool = False     # appdata(...) / metainfo(...)

    @classmethod
    def from_manifest_data(cls, dirnames: List[str], provides: List[str]) -> PackagePhysicalAnatomy:
        """
        Shared anatomy analyzer: Evaluates physical filesystem footprints and ABI contracts.
        Used identically by both native librpm (COPR) and CLI queries (AppImage).
        """
        anatomy = cls()
        for d in dirnames:
            d_clean = d.strip().rstrip("/")
            if not d_clean:
                continue

            if d_clean in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
                anatomy.has_binaries = True
                anatomy.has_user_bin = True
                if d_clean in ("/usr/sbin", "/sbin"):
                    anatomy.has_admin_sbin = True
            elif d_clean.startswith("/usr/libexec"):
                anatomy.has_libexec = True
            elif d_clean.startswith("/usr/share/applications"):
                anatomy.has_desktop_file = True
            elif "/systemd/system" in d_clean:
                anatomy.has_systemd_system = True
            elif "/systemd/user" in d_clean:
                anatomy.has_systemd_user = True
            elif d_clean.startswith("/usr/include"):
                anatomy.has_c_headers = True
            elif "/fonts" in d_clean:
                anatomy.has_fonts_dir = True
            elif "/firmware" in d_clean:
                anatomy.has_firmware_dir = True
            elif "/modules" in d_clean and not d_clean.endswith("/node_modules"):
                anatomy.has_kernel_modules_dir = True
            elif "/locale" in d_clean or "/zoneinfo" in d_clean:
                anatomy.has_locales_dir = True
            elif d_clean in ("/usr/lib64", "/usr/lib"):
                anatomy.has_shared_libs_dir = True
            elif "/man/man1" in d_clean:
                anatomy.has_man1 = True
            elif "/man/man8" in d_clean:
                anatomy.has_man8 = True
            elif "/man/man3" in d_clean:
                anatomy.has_man3 = True
            elif "/python3" in d_clean or "/site-packages" in d_clean:
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
# Desktop Entry Metadata Parser
# =============================================================================

def parse_installed_desktop_applications() -> Tuple[Set[str], Set[str]]:
    gui_apps: Set[str] = set()
    cli_apps: Set[str] = set()

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
                                elif key == "Exec" and not exec_bin:
                                    token = val.split()[0].strip('"\'')
                                    exec_bin = os.path.basename(token).lower()

                        if is_app and not no_display:
                            desktop_base = os.path.splitext(file)[0].lower()
                            target_set = cli_apps if terminal else gui_apps
                            target_set.add(desktop_base)

                            if exec_bin:
                                target_set.add(exec_bin)
                                clean_exec = re.sub(r'-[0-9].*$', '', exec_bin)
                                if clean_exec:
                                    target_set.add(clean_exec)

                            parts = desktop_base.split(".")
                            if len(parts) > 1:
                                last_token = parts[-1]
                                if len(last_token) > 2 and last_token not in ("desktop", "bin", "app"):
                                    target_set.add(last_token)

                except Exception:
                    continue

    return gui_apps, cli_apps


# =============================================================================
# Intelligent Semantic Intent Profiler (Natural Language Domain Analysis)
# =============================================================================

class SemanticIntentAnalyzer:
    LEXICON_DESKTOP_GUI: Final[Dict[str, float]] = {
        "graphical": 2.5, "gui": 2.5, "desktop": 2.0, "viewer": 1.8,
        "editor": 1.5, "player": 1.8, "browser": 2.0, "client": 1.2,
        "canvas": 1.5, "window": 1.2, "interface": 1.0, "frontend": 1.5,
        "calculator": 2.0, "drawing": 2.0, "media player": 2.5, "ide": 1.8,
    }

    LEXICON_CLI_TOOL: Final[Dict[str, float]] = {
        "command-line": 3.0, "command line": 3.0, "cli": 3.0, "terminal": 2.5,
        "console": 2.0, "utility": 1.5, "tool": 1.2, "debugger": 2.2,
        "benchmark": 2.0, "interactive process": 2.5, "analyzer": 1.5,
        "shell": 1.8, "generator": 1.2, "parser": 1.2, "linter": 2.2,
        "formatter": 2.0, "downloader": 1.5, "uploader": 1.5, "grep": 2.5,
    }

    LEXICON_DAEMON_SERVICE: Final[Dict[str, float]] = {
        "daemon": 3.0, "service": 2.2, "background process": 2.8, "server": 2.0,
        "monitoring": 1.5, "listener": 2.0, "supervisor": 2.0, "agent": 1.5,
        "proxy": 1.8, "broker": 2.0, "scheduler": 1.8, "relay": 1.8,
    }

    LEXICON_LIBRARY: Final[Dict[str, float]] = {
        "shared library": 3.0, "library": 2.0, "c library": 2.8, "c++ library": 2.8,
        "bindings": 2.5, "api": 1.8, "wrapper": 1.8, "sdk": 1.5,
        "framework": 1.5, "header files": 2.5, "development files": 2.5,
        "algorithms": 1.5, "toolkit": 1.2, "subroutines": 2.0,
    }

    LEXICON_SECURITY: Final[Dict[str, float]] = {
        "cryptographic": 2.5, "encryption": 2.5, "security": 2.0, "authentication": 2.5,
        "authorization": 2.5, "firewall": 2.5, "selinux": 3.0, "policy": 1.5,
        "certificate": 2.0, "keyring": 2.2, "pam": 2.5, "tls": 2.0, "ssl": 2.0,
    }

    @classmethod
    def score_text(cls, text: str) -> Dict[str, float]:
        if not text:
            return {"gui": 0.0, "cli": 0.0, "daemon": 0.0, "lib": 0.0, "sec": 0.0}

        text_lower = text.lower()
        def match_score(lexicon: Dict[str, float]) -> float:
            return sum(weight for term, weight in lexicon.items() if term in text_lower)

        return {
            "gui": match_score(cls.LEXICON_DESKTOP_GUI),
            "cli": match_score(cls.LEXICON_CLI_TOOL),
            "daemon": match_score(cls.LEXICON_DAEMON_SERVICE),
            "lib": match_score(cls.LEXICON_LIBRARY),
            "sec": match_score(cls.LEXICON_SECURITY),
        }


# =============================================================================
# Multi-Factor Scored Decision Engine (Intelligent Package Classifier)
# =============================================================================

@dataclass(slots=True)
class ClassificationDecision:
    primary_category: str
    confidence: float
    rationale: List[str]
    secondary_tags: List[str]
    flags: Dict[str, bool]


class IntelligentPackageClassifier:
    CORE_PILLAR_PACKAGES: Final[Set[str]] = {
        "kernel", "kernel-core", "kernel-modules", "grub2-common", "grub2-efi-x64", "dracut",
        "systemd", "systemd-udev", "systemd-libs", "glibc", "glibc-common", "coreutils",
        "bash", "sudo", "shadow-utils", "util-linux", "polkit", "pam", "chrony",
        "btrfs-progs", "e2fsprogs", "lvm2", "cryptsetup", "dosfstools", "mdadm",
        "NetworkManager", "firewalld", "selinux-policy", "audit", "iptables",
        "pipewire", "wireplumber", "mesa-dri-drivers", "mesa-vulkan-drivers",
        "xorg-x11-server-Xorg", "xorg-x11-server-Xwayland", "gdm", "sddm",
        "gnome-shell", "mutter", "plasma-desktop", "kwin", "kwin-wayland",
        "dnf5", "dnf", "rpm", "flatpak"
    }

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
        vendor: str = "",
        packager: str = "",
    ) -> ClassificationDecision:
        name_lower = name.lower()
        full_text = f"{summary} {description}"
        semantic_scores = SemanticIntentAnalyzer.score_text(full_text)

        scores: Dict[str, float] = {}
        reasons: Dict[str, List[str]] = {}

        def add_score(cat: str, delta: float, reason: str):
            scores[cat] = scores.get(cat, 0.0) + delta
            reasons.setdefault(cat, []).append(reason)

        # ---------------------------------------------------------------------
        # 1. Fedora System Core Pillars
        # ---------------------------------------------------------------------
        if name in cls.CORE_PILLAR_PACKAGES or name_lower in cls.CORE_PILLAR_PACKAGES:
            add_score("fedora_core", 15.0, "Identified as foundational Fedora root pillar")
        elif any(name_lower.startswith(pfx) for pfx in ("systemd-", "glibc-", "pipewire-", "mesa-", "grub2-")):
            add_score("fedora_core", 10.0, "Belongs to essential system service/driver family")

        # ---------------------------------------------------------------------
        # 2. Desktop Applications (GUI)
        # ---------------------------------------------------------------------
        if appstream_desktop:
            add_score("desktop_app", 10.0, "Verified in official Fedora AppStream desktop catalog")
        if anatomy.has_desktop_file:
            add_score("desktop_app", 8.0, "Ships desktop launcher in /usr/share/applications")
        if name in desktop_apps_discovered or name_lower in desktop_apps_discovered:
            add_score("desktop_app", 6.0, "Matches registered graphical application desktop entry")
        if semantic_scores["gui"] > 0:
            add_score("desktop_app", semantic_scores["gui"] * 1.5, f"Natural language GUI semantic affinity (+{semantic_scores['gui']:.1f})")

        if not anatomy.has_binaries and not appstream_desktop:
            add_score("desktop_app", -6.0, "Lacks executable binary")

        # ---------------------------------------------------------------------
        # 3. Command-Line Tools & Console Applications
        # ---------------------------------------------------------------------
        if appstream_console:
            add_score("cli_tool", 10.0, "Verified in official Fedora AppStream console catalog")
        if anatomy.has_binaries and not anatomy.has_libexec:
            add_score("cli_tool", 7.0, "Delivers executable command into unified /usr/bin")
        elif anatomy.has_binaries and anatomy.has_libexec:
            add_score("cli_tool", 3.0, "Delivers command binary with internal libexec helper")
        if anatomy.has_man1:
            add_score("cli_tool", 5.0, "Provides Section 1 (User Commands) manual documentation")
        if name in cli_apps_discovered or name_lower in cli_apps_discovered or name_lower in KNOWN_CLI_USER_TOOLS:
            add_score("cli_tool", 5.0, "Matches known CLI user tool entry")
        if semantic_scores["cli"] > 0:
            add_score("cli_tool", semantic_scores["cli"] * 1.5, f"Natural language CLI semantic affinity (+{semantic_scores['cli']:.1f})")

        # Penalties: Avoid misclassifying GUI apps, pure daemons, or libexec helpers as CLI tools
        if anatomy.has_desktop_file or appstream_desktop:
            add_score("cli_tool", -10.0, "Suppressed due to presence of graphical desktop application launcher")
        if anatomy.has_systemd_system:
            add_score("cli_tool", -6.0, "Suppressed: Primary role is background systemd service")
        if anatomy.has_man8 and not anatomy.has_man1:
            add_score("cli_tool", -5.0, "Suppressed: Provides admin/daemon man8 without user man1")
        if anatomy.has_libexec and not anatomy.has_man1 and not appstream_console:
            add_score("cli_tool", -4.0, "Suppressed: Delivers private binaries to /usr/libexec without user manual")

        # ---------------------------------------------------------------------
        # 4. Hardware Firmware & Microcode
        # ---------------------------------------------------------------------
        if anatomy.has_firmware_dir:
            add_score("firmware", 12.0, "Delivers hardware binary microcode into /usr/lib/firmware")
        if any(kw in name_lower for kw in ("microcode", "ucode", "linux-firmware")):
            add_score("firmware", 6.0, "Hardware firmware package identifier")
        if anatomy.has_desktop_file or anatomy.has_binaries:
            add_score("firmware", -12.0, "Contains user executable/desktop GUI (Tool, not raw firmware)")

        # ---------------------------------------------------------------------
        # 5. Linux Kernel Modules & Drivers
        # ---------------------------------------------------------------------
        if anatomy.has_kernel_modules_dir or anatomy.provides_kmod:
            add_score("kernel_module", 12.0, "Delivers compiled kernel drivers into /usr/lib/modules or provides kmod()")
        if name_lower.startswith(("kernel-", "kmod-", "akmod-", "dkms-")):
            add_score("kernel_module", 7.0, "Matches standard Fedora kernel driver naming convention")

        # ---------------------------------------------------------------------
        # 6. Shared C/C++ Dynamic Libraries
        # ---------------------------------------------------------------------
        if anatomy.exported_sonames:
            add_score("c_lib", 8.0, f"Exports {len(anatomy.exported_sonames)} dynamic ELF SONAME ABI contracts")
        if anatomy.has_man3:
            add_score("c_lib", 3.0, "Provides Section 3 (Library Calls) manual documentation")
        if name_lower.startswith("lib") and not anatomy.has_binaries and not anatomy.has_libexec:
            add_score("c_lib", 3.0, "Traditional library prefix with no command binaries")
        if anatomy.has_binaries:
            add_score("c_lib", -7.0, "Contains command binaries")

        # ---------------------------------------------------------------------
        # 7. Systemd Daemons & Background Services (Modern Fedora 42+ criteria)
        # ---------------------------------------------------------------------
        if anatomy.has_systemd_system or anatomy.has_systemd_user:
            add_score("systemd_service", 10.0, "Installs native systemd service/socket/timer unit")
        if anatomy.has_libexec and not anatomy.has_man1 and not appstream_console:
            add_score("systemd_service", 7.0, "Delivers internal daemon/helper binaries into /usr/libexec")
        if anatomy.has_man8 and not anatomy.has_man1:
            add_score("systemd_service", 6.0, "Provides Section 8 (System Administration / Daemons) manual documentation")
        if anatomy.has_admin_sbin and not anatomy.has_man1:
            add_score("systemd_service", 3.0, "Legacy administrative binary specification")
        if semantic_scores["daemon"] > 0:
            add_score("systemd_service", semantic_scores["daemon"] * 2.0, f"Natural language service/daemon affinity (+{semantic_scores['daemon']:.1f})")

        # ---------------------------------------------------------------------
        # 8. Fonts & Devel SDKs
        # ---------------------------------------------------------------------
        if anatomy.has_fonts_dir or anatomy.provides_font or name_lower.endswith(("-fonts", "-font")):
            add_score("font", 12.0, "Delivers typography assets into /usr/share/fonts or provides font()")

        if anatomy.has_c_headers or anatomy.provides_pkgconfig or name_lower.endswith(("-devel", "-static")):
            add_score("devel", 10.0, "Delivers C/C++ header interfaces (/usr/include) or pkgconfig file")

        # ---------------------------------------------------------------------
        # Resolution & Confidence Scoring
        # ---------------------------------------------------------------------
        valid_candidates = {cat: score for cat, score in scores.items() if score > 0}
        if not valid_candidates:
            if anatomy.has_binaries and not anatomy.has_libexec:
                primary = "cli_tool"
            elif anatomy.has_libexec or anatomy.has_systemd_system:
                primary = "systemd_service"
            else:
                primary = "c_lib"
            reasons[primary] = ["Fallback classification based on binary presence"]
            valid_candidates[primary] = 1.0

        primary_category = max(valid_candidates.items(), key=lambda item: item[1])[0]
        top_score = valid_candidates[primary_category]
        confidence = min(0.99, max(0.60, top_score / (top_score + 3.0)))

        # Multi-Tagging Context
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
            "is_fedora_core": (primary_category == "fedora_core"),
            "is_c_lib": (primary_category == "c_lib"),
            "is_systemd_service": (primary_category == "systemd_service"),
            "is_firmware": (primary_category == "firmware"),
            "is_kernel_module": (primary_category == "kernel_module"),
            "is_font": (primary_category == "font"),
            "is_devel": (primary_category == "devel"),
            "is_python_pkg": is_python,
            "is_rust_pkg": is_rust,
            "is_jvm_pkg": is_jvm,
            "is_nodejs_pkg": is_node,
            "is_security_pkg": (semantic_scores["sec"] >= 2.0 or "selinux" in name_lower),
            "is_locale": (primary_category == "font" or name_lower.startswith("glibc-langpack-")),
            "is_theme": any(kw in name_lower for kw in ("-theme", "-icon-theme", "-backgrounds")),
            "is_library": primary_category in ("c_lib", "devel", "font", "firmware"),
        }

        return ClassificationDecision(
            primary_category=primary_category,
            confidence=round(confidence, 2),
            rationale=reasons.get(primary_category, ["Classified by intelligent multi-factor scoring"]),
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
    dependencies_resolved = pyqtSignal(str, list, object)   # root_pkg, nodes, target_index
    reverse_dependencies_resolved = pyqtSignal(str, list)
    package_files_loaded = pyqtSignal(str, list)
    package_details_loaded = pyqtSignal(object)
    history_loaded = pyqtSignal(list)
    dry_run_finished = pyqtSignal(object)
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)


# =============================================================================
# Worker: System Package Discovery (Native librpm with CLI Fallback)
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
            desktop_apps, cli_apps = parse_installed_desktop_applications()

            if HAS_NATIVE_RPM and not is_running_in_flatpak():
                packages = self._query_native_librpm(desktop_apps, cli_apps, appstream)
            else:
                packages = self._query_cli_subprocess(desktop_apps, cli_apps, appstream)

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
        appstream: AppStreamCatalog
    ) -> List[PackageInfo]:
        packages: List[PackageInfo] = []
        ts = create_rpm_transaction_set()
        if ts is None:
            return self._query_cli_subprocess(desktop_apps, cli_apps, appstream)

        try:
            match_iterator = ts.dbMatch()
            for header in match_iterator:
                if self._is_cancelled.is_set():
                    return []

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

                # 1. Physical Anatomy Extraction (Unified /usr/bin footprint & /usr/libexec)
                anatomy = PackagePhysicalAnatomy.from_rpm_header(header)

                # 2. AppStream Ground Truth
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

                # 3. Intelligent Multi-Factor Decision Engine
                decision = IntelligentPackageClassifier.classify(
                    name=name,
                    summary=summary,
                    description=description,
                    anatomy=anatomy,
                    appstream_desktop=appstream_desktop,
                    appstream_console=appstream_console,
                    desktop_apps_discovered=desktop_apps,
                    cli_apps_discovered=cli_apps,
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
                        primary_category=decision.primary_category,
                        classification_confidence=decision.confidence,
                        classification_rationale=decision.rationale,
                        secondary_tags=decision.secondary_tags,
                        is_desktop_app=flags["is_desktop_app"],
                        is_cli_tool=flags["is_cli_tool"],
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
        finally:
            del ts

        return packages

    def _query_cli_subprocess(
        self,
        desktop_apps: Set[str],
        cli_apps: Set[str],
        appstream: AppStreamCatalog
    ) -> List[PackageInfo]:
        # Include DIRNAMES and PROVIDENAME arrays in the query output
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

            # Extract directory footprints and exported SONAMEs from CLI output
            raw_dirs = parts[12].split(";") if len(parts) > 12 else []
            raw_provs = parts[13].split(";") if len(parts) > 13 else []

            # 100% Identical physical anatomy to native librpm
            anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, raw_provs)

            # Supplement with desktop launcher cache if available
            if name_clean in desktop_apps or appstream_desktop:
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
                    primary_category=decision.primary_category,
                    classification_confidence=decision.confidence,
                    classification_rationale=decision.rationale,
                    secondary_tags=decision.secondary_tags,
                    is_desktop_app=flags["is_desktop_app"],
                    is_cli_tool=flags["is_cli_tool"],
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
# Worker: Leaf / Orphan Packages Query (Native libdnf5 filter_leaves)
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
# Worker: Direct Dependency Tree Hierarchy (librpm Provider & Cap Resolution)
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
            match = ts.dbMatch("name", pkg_name)  # type: ignore[attr-defined]
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
                match_name = ts.dbMatch("name", cap)  # type: ignore[attr-defined]
                if match_name.count() > 0:
                    batch_results.append((cap, True, cap))
                    continue

                matches = ts.dbMatch("providename", cap)  # type: ignore[attr-defined]
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
# Worker: Reverse Dependencies (Native librpm Index Lookups)
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
                    matches = ts.dbMatch("requirename", self.target_package)  # type: ignore[attr-defined]
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
# Worker: Package File Hierarchy Inspector (Native librpm Tags)
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
                    matches = ts.dbMatch("name", self.package_name)  # type: ignore[attr-defined]
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
# Worker: DNF Transaction History (Native libdnf5 with CLI Fallback)
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
# Worker: Transaction Simulation (Native libdnf5 Goal & System Pillar Guard)
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
            args = get_host_command_prefix() + [dnf_bin, "--assumeno"]
            if self.to_install:
                args.extend(["install"] + self.to_install)
            if self.to_remove:
                args.extend(["remove"] + self.to_remove)

            res = subprocess.run(args, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=25)

            output = res.stdout + res.stderr
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
# Privileged Polkit Transaction Runner (Root Bypass & System Elevation)
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

    def execute_transaction(self, to_install: List[str], to_remove: List[str]):
        """Runs package additions and removals under system administrative elevation."""
        dnf_bin = get_dnf_binary_path()
        args: List[str] = [dnf_bin, "-y"]
        if to_install:
            args.extend(["install", "--"] + to_install)
        if to_remove:
            args.extend(["remove", "--"] + to_remove)

        self._start_process(args)

    def execute_custom_command(self, custom_dnf_args: List[str]):
        """Executes targeted DNF actions (e.g. dnf history undo) under elevation."""
        dnf_bin = get_dnf_binary_path()
        args: List[str] = [dnf_bin] + custom_dnf_args
        self._start_process(args)

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
            # Running directly as root (e.g. inside Docker/Podman test container)
            program = dnf_args[0]
            full_args = dnf_args[1:]
            self.log_received.emit("⚡ Running with direct root privileges (bypassing Polkit elevation)...\n")
            self.log_received.emit(f"Executing: {program} {' '.join(full_args)}\n\n")
        else:
            # Standard unprivileged user requiring Polkit elevation
            program = prefix[0] if prefix else "pkexec"
            full_args: List[str] = prefix[1:] + ["pkexec"] if prefix else []
            full_args.extend(dnf_args)
            self.log_received.emit("🔒 Requesting administrative authorization...\n")
            self.log_received.emit(f"Executing: {program} {' '.join(full_args)}\n\n")

        self.process.start(program, full_args)

    def cancel_transaction(self):
        """Sends SIGINT/SIGTERM gracefully to avoid corrupting RPM locks."""
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
        self.transaction_finished.emit(success, exit_code)

    def _parse_progress(self, text: str):
        match = re.search(r'\[\s*(\d+)%\s*\]', text)
        if match:
            percent = int(match.group(1))
            self.progress_percent.emit(percent)
