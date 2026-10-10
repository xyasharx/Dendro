# dendro/core/backend.py
"""
High-performance native backend engine for Dendro.
Features native librpm and libdnf5 bindings, deterministic two-phase ontological taxonomy,
FHS filesystem structural inspection, FreeDesktop AppStream 1.0+ and XDG specification parity,
file integrity verification (rpm -V), native RPM changelogs with CVE linking,
standalone local .rpm inspection, live updates checking, software repository management,
and multi-stage Polkit transactions.
Completely free of unicode font emoji glyphs to prevent Fontconfig crashes.
"""
from __future__ import annotations

import bz2
import configparser
import glob
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, Final, List, Optional, Set, Tuple, Union

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QRunnable, pyqtSignal, pyqtSlot

# Thread-safety reentrant lock for native C librpm calls
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
    # Prioritise standard /usr/bin paths declared in org.dendro.policy
    for candidate in ("/usr/bin/dnf5", "/usr/bin/dnf"):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("dnf5") or shutil.which("dnf") or "/usr/bin/dnf"
    return os.path.realpath(found) if os.path.exists(found) else found


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
# Dynamic System Protection & Root Pillars
# =============================================================================

DEFAULT_SYSTEM_ROOT_PILLARS: Final[Set[str]] = {
    "kernel", "kernel-core", "kernel-modules", "grub2-common", "grub2-efi-x64", "dracut",
    "systemd", "systemd-udev", "systemd-libs", "glibc", "glibc-common", "coreutils",
    "bash", "sudo", "shadow-utils", "util-linux", "polkit", "pam", "chrony",
    "btrfs-progs", "e2fsprogs", "lvm2", "cryptsetup", "dosfstools", "mdadm",
    "networkmanager", "firewalld", "selinux-policy", "audit", "iptables",
    "dnf5", "dnf", "rpm", "flatpak", "shared-mime-info", "desktop-file-utils", "glib2"
}

# Base OS packages strictly assigned to Pillar 4: fedora_core
DEFAULT_FEDORA_CORE_PACKAGES: Final[Set[str]] = {
    "filesystem", "setup", "glibc", "glibc-common", "glibc-minimal-langpack", "glibc-all-langpacks",
    "coreutils", "coreutils-common", "bash", "sh", "util-linux", "util-linux-core",
    "systemd", "systemd-udev", "systemd-libs",
    "rpm", "rpm-libs", "dnf", "dnf5", "libdnf5", "dnf5-plugins",
    "grub2-common", "grub2-efi-x64", "grub2-pc", "dracut",
    "btrfs-progs", "e2fsprogs", "lvm2", "cryptsetup", "dosfstools", "mdadm"
}

def get_system_protected_packages() -> Set[str]:
    """Dynamically loads protected packages from /etc/dnf/protected.d/*.conf and kernel."""
    protected: Set[str] = set()
    protected_dirs = ["/etc/dnf/protected.d"]
    if is_running_in_flatpak():
        protected_dirs.append("/run/host/etc/dnf/protected.d")

    for p_dir in protected_dirs:
        if not os.path.isdir(p_dir):
            continue
        try:
            for fname in os.listdir(p_dir):
                if fname.endswith(".conf"):
                    fpath = os.path.join(p_dir, fname)
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            pkg = line.strip()
                            if pkg and not pkg.startswith("#"):
                                protected.add(pkg.lower())
        except Exception:
            continue

    try:
        uname_r = os.uname().release
        protected.add(f"kernel-{uname_r}")
        protected.add("kernel")
        protected.add("kernel-core")
    except Exception:
        pass

    if not protected:
        protected.update(DEFAULT_SYSTEM_ROOT_PILLARS)
    else:
        protected.update({"systemd", "glibc", "dnf", "dnf5", "rpm", "shared-mime-info", "glib2"})

    return protected

FEDORA_SYSTEM_ROOT_PILLARS: Final[Set[str]] = DEFAULT_SYSTEM_ROOT_PILLARS


# =============================================================================
# Core Data Models
# =============================================================================

class PackageState(Enum):
    INSTALLED = auto()
    AVAILABLE = auto()
    MISSING = auto()
    QUEUED_INSTALL = auto()
    QUEUED_REMOVE = auto()
    QUEUED_UPGRADE = auto()


class PackageArchetype(Enum):
    """The deterministic physical delivery form factor of a package."""
    CORE_SYSTEM = auto()       # Protected base OS, kernel, glibc, dnf5
    HARDWARE_DRIVER = auto()   # Kernel modules, firmware, DRI acceleration
    DESKTOP_APP = auto()       # Interactive user-facing GUI application
    SYSTEM_DAEMON = auto()     # Systemd services, background daemons
    DESKTOP_ADDON = auto()     # Window managers, shell extensions, KIO workers
    DEVELOPMENT_SDK = auto()   # C/C++ headers, static libs, pkg-config
    MEDIA_CODEC = auto()       # GStreamer decoders, media player plugins
    GUI_TOOLKIT = auto()       # Qt, GTK, Tkinter, WxWidgets widget frameworks
    ECOSYSTEM_RUNTIME = auto() # Python, Rust, Java/JVM, Node.js packages
    STATIC_ASSET = auto()      # Fonts, icons, themes, localization
    CLI_UTILITY = auto()       # Interactive terminal commands in $PATH
    SHARED_LIBRARY = auto()    # Dynamic ELF shared objects (.so) fallback


# Law 4: O(1) Taxonomy Overrides Dictionary for upstream packaging quirks
PACKAGE_TAXONOMY_OVERRIDES: Final[Dict[str, Tuple[PackageArchetype, str, str, str, str]]] = {
    # Format: package_name_lower: (archetype, parent_pillar, primary_category, sub_category, rationale)
    # Hardware & Audio Architecture
    "pipewire": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "audio_sound", "audio_sound", "Core Linux sound server and routing infrastructure"),
    "wireplumber": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "audio_sound", "audio_sound", "Session and policy manager for PipeWire"),
    "pulseaudio": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "audio_sound", "audio_sound", "Sound server for POSIX systems"),
    "pulseaudio-daemon": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "audio_sound", "audio_sound", "PulseAudio sound daemon"),
    "alsa-lib": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "audio_sound", "audio_sound", "Advanced Linux Sound Architecture library"),
    "alsa-ucm": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "audio_sound", "audio_sound", "ALSA Use Case Manager configuration"),
    "alsa-topology": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "audio_sound", "audio_sound", "ALSA topology configuration"),
    "alsa-plugins": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "audio_sound", "audio_sound", "ALSA sound system plugins"),
    "jack-audio-connection-kit": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "audio_sound", "audio_sound", "Low-latency professional audio server"),

    # Hardware Graphics & DRI Stack
    "mesa-dri-drivers": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "graphics_drivers", "graphics_drivers", "Mesa Direct Rendering Infrastructure drivers"),
    "mesa-vulkan-drivers": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "graphics_drivers", "graphics_drivers", "Mesa Vulkan graphics acceleration drivers"),
    "libdrm": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "graphics_drivers", "graphics_drivers", "Direct Rendering Manager userspace library"),

    # Hardware Kernel Modules & Drivers
    "kernel": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "kernel_modules", "kernel_modules", "Linux kernel package"),
    "kernel-core": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "kernel_modules", "kernel_modules", "Core Linux kernel binary and basic modules"),
    "kernel-modules": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "kernel_modules", "kernel_modules", "Kernel modules for core Linux functionality"),
    "kernel-modules-core": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "kernel_modules", "kernel_modules", "Standard kernel modules"),
    "kernel-modules-extra": (PackageArchetype.HARDWARE_DRIVER, "pillar_hardware", "kernel_modules", "kernel_modules", "Extra kernel drivers and hardware modules"),

    # Security & Access Control
    "selinux-policy": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "SELinux policy configuration"),
    "selinux-policy-targeted": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "SELinux targeted policy"),
    "libselinux": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "SELinux core security library"),
    "polkit": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "Authorization manager and privilege control framework"),
    "polkit-libs": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "Polkit authorization libraries"),
    "pam": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "Pluggable Authentication Modules architecture"),
    "pam-libs": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "Pluggable Authentication Modules shared libraries"),
    "shadow-utils": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "System password and shadow account utilities"),
    "sudo": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "Privileged command execution utility"),
    "audit": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "Linux audit framework daemon and utilities"),
    "audit-libs": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "Linux audit subsystem libraries"),
    "firewalld": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "Dynamically managed firewall daemon"),
    "iptables": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "IPv4 packet filtering administration"),
    "nftables": (PackageArchetype.CORE_SYSTEM, "pillar_system", "security_pkgs", "security_pkgs", "Netfilter packet classification framework"),

    # Desktop Environment Shells, Window Managers & Compositors
    "plasma-desktop": (PackageArchetype.DESKTOP_ADDON, "pillar_system", "desktop_addons", "desktop_addons", "KDE Plasma desktop shell environment"),
    "plasma-workspace": (PackageArchetype.DESKTOP_ADDON, "pillar_system", "desktop_addons", "desktop_addons", "KDE Plasma workspace session and tools"),
    "gnome-shell": (PackageArchetype.DESKTOP_ADDON, "pillar_system", "desktop_addons", "desktop_addons", "GNOME desktop environment shell and compositor"),
    "kamera": (PackageArchetype.DESKTOP_APP, "pillar_apps", "system_settings", "system_settings", "KDE digital camera configuration in System Settings"),
    "mutter": (PackageArchetype.DESKTOP_ADDON, "pillar_system", "desktop_addons", "desktop_addons", "Wayland display compositor and window manager for GNOME"),
    "kwin": (PackageArchetype.DESKTOP_ADDON, "pillar_system", "desktop_addons", "desktop_addons", "KDE Plasma window manager and Wayland compositor"),
    "cinnamon": (PackageArchetype.DESKTOP_ADDON, "pillar_system", "desktop_addons", "desktop_addons", "Cinnamon desktop environment shell"),
    "mate-desktop": (PackageArchetype.DESKTOP_ADDON, "pillar_system", "desktop_addons", "desktop_addons", "MATE desktop environment core library and interface"),
    "xfdesktop": (PackageArchetype.DESKTOP_ADDON, "pillar_system", "desktop_addons", "desktop_addons", "Xfce desktop environment manager"),

    # System Daemons & Services
    "networkmanager": (PackageArchetype.SYSTEM_DAEMON, "pillar_system", "systemd_services", "systemd_services", "Network management system daemon"),
    "chrony": (PackageArchetype.SYSTEM_DAEMON, "pillar_system", "systemd_services", "systemd_services", "NTP network time synchronization daemon"),
    "localsearch": (PackageArchetype.SYSTEM_DAEMON, "pillar_system", "systemd_services", "systemd_services", "GNOME desktop file indexing daemon"),
    "tinysparql": (PackageArchetype.SHARED_LIBRARY, "pillar_libs", "c_libs", "c_libs", "Low-footprint RDF database engine for desktop search"),

    # Core Base Infrastructure
    "systemd": (PackageArchetype.CORE_SYSTEM, "pillar_system", "fedora_core", "fedora_core", "System and Service Manager"),
    "systemd-udev": (PackageArchetype.CORE_SYSTEM, "pillar_system", "fedora_core", "fedora_core", "Rule-based device node manager"),
    "systemd-libs": (PackageArchetype.CORE_SYSTEM, "pillar_system", "fedora_core", "fedora_core", "systemd shared libraries"),
    "glibc": (PackageArchetype.CORE_SYSTEM, "pillar_system", "fedora_core", "fedora_core", "The GNU C Library core system runtime"),
    "coreutils": (PackageArchetype.CORE_SYSTEM, "pillar_system", "fedora_core", "fedora_core", "Core GNU command-line utilities"),
    "bash": (PackageArchetype.CORE_SYSTEM, "pillar_system", "fedora_core", "fedora_core", "GNU Bourne-Again Shell and core interpreter"),
    "rpm": (PackageArchetype.CORE_SYSTEM, "pillar_system", "fedora_core", "fedora_core", "RPM Package Manager core executable"),
    "dnf5": (PackageArchetype.CORE_SYSTEM, "pillar_system", "fedora_core", "fedora_core", "Next-generation package manager"),
    "shared-mime-info": (PackageArchetype.CORE_SYSTEM, "pillar_system", "fedora_core", "fedora_core", "Core FreeDesktop MIME-info database"),

    # Desktop Applications with Packaging Quirks
    "dendro": (PackageArchetype.DESKTOP_APP, "pillar_apps", "user_apps", "desktop_utilities", "Native graphical package manager and dependency hierarchy explorer"),

    # CLI Utilities with Special Packaging Quirks
    "7zip": (PackageArchetype.CLI_UTILITY, "pillar_cli", "cli_tools", "cli_data_archiving", "High-ratio file archiver CLI utility"),
    "p7zip": (PackageArchetype.CLI_UTILITY, "pillar_cli", "cli_tools", "cli_data_archiving", "POSIX port of 7-Zip archiver"),
    "flatpak": (PackageArchetype.CLI_UTILITY, "pillar_cli", "cli_tools", "cli_general", "Application sandboxing and deployment tool"),
    "desktop-file-utils": (PackageArchetype.CLI_UTILITY, "pillar_cli", "cli_tools", "cli_search_files", "Utilities for working with desktop entries"),

    # GUI Toolkits
    "python3-tkinter": (PackageArchetype.GUI_TOOLKIT, "pillar_libs", "gui_toolkits", "gui_toolkits", "Python interface to Tcl/Tk GUI toolkit"),
}


@dataclass(slots=True)
class DependencyNode:
    raw_requirement: str
    resolved_package_name: str
    version_constraint: str = ""
    is_satisfied: bool = True
    is_cycle: bool = False
    is_reverse: bool = False
    installed_version: str = ""
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
class PackageScriptlets:
    prein: str = ""
    postin: str = ""
    preun: str = ""
    postun: str = ""

    @property
    def has_any(self) -> bool:
        return bool(self.prein or self.postin or self.preun or self.postun)


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
class LocalRpmInspectionResult:
    file_path: str
    name: str
    version: str
    release: str
    arch: str
    summary: str
    description: str
    license: str
    url: str
    size_bytes: int
    requires: List[str] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
    is_already_installed: bool = False
    installed_version: str = ""


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

    # Two-Tier Hierarchical Taxonomy
    parent_pillar: str = "pillar_system"
    sub_category: str = "general"
    primary_category: str = "General"
    classification_confidence: float = 1.0
    classification_rationale: List[str] = field(default_factory=list)
    secondary_tags: List[str] = field(default_factory=list)

    # Core & Application Flags
    is_orphan: bool = False
    is_user_installed: bool = False
    is_desktop_app: bool = False
    is_cli_tool: bool = False
    is_system_settings: bool = False
    is_fedora_core: bool = False
    is_protected: bool = False

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
    has_dependencies: bool = True
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
# Standalone Local .rpm Inspection (Direct In-Process Analysis)
# =============================================================================

def inspect_local_rpm_file(file_path: str) -> Optional[LocalRpmInspectionResult]:
    """
    Parses a local .rpm package file directly via native librpm without root privileges.
    Checks whether the package is already installed and compares versions.
    """
    if not os.path.isfile(file_path):
        return None

    if HAS_NATIVE_RPM and not is_running_in_flatpak():
        with RPM_GLOBAL_LOCK:
            try:
                ts = rpm.TransactionSet()
                if hasattr(rpm, "_RPMVSF_NOSIGNATURES"):
                    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

                with open(file_path, "rb") as fd:
                    hdr = ts.hdrFromFdno(fd.fileno())
                    if hdr is None:
                        return None

                    name = _decode_rpm_str(hdr[rpm.RPMTAG_NAME])
                    ver = _decode_rpm_str(hdr[rpm.RPMTAG_VERSION])
                    rel = _decode_rpm_str(hdr[rpm.RPMTAG_RELEASE])
                    arch = _decode_rpm_str(hdr[rpm.RPMTAG_ARCH])
                    summary = _decode_rpm_str(hdr[rpm.RPMTAG_SUMMARY])
                    desc = _decode_rpm_str(hdr[rpm.RPMTAG_DESCRIPTION])
                    lic = _decode_rpm_str(hdr[rpm.RPMTAG_LICENSE])
                    url = _decode_rpm_str(hdr[rpm.RPMTAG_URL])
                    size = int(hdr[rpm.RPMTAG_SIZE] or 0)

                    raw_reqs = [_decode_rpm_str(r) for r in (hdr[rpm.RPMTAG_REQUIRENAME] or [])]
                    raw_provs = [_decode_rpm_str(p) for p in (hdr[rpm.RPMTAG_PROVIDENAME] or [])]

                    # Check if already installed
                    is_installed = False
                    installed_ver = ""
                    matches = ts.dbMatch("name", name)
                    for inst_hdr in matches:
                        is_installed = True
                        inst_v = _decode_rpm_str(inst_hdr[rpm.RPMTAG_VERSION])
                        inst_r = _decode_rpm_str(inst_hdr[rpm.RPMTAG_RELEASE])
                        installed_ver = f"{inst_v}-{inst_r}"
                        break
                    del matches

                    return LocalRpmInspectionResult(
                        file_path=os.path.abspath(file_path),
                        name=name,
                        version=ver,
                        release=rel,
                        arch=arch,
                        summary=summary,
                        description=desc,
                        license=lic,
                        url=url,
                        size_bytes=size,
                        requires=raw_reqs,
                        provides=raw_provs,
                        is_already_installed=is_installed,
                        installed_version=installed_ver
                    )
            except Exception:
                pass

    # Subprocess fallback via host rpm -q
    try:
        qf = "%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}|%{SUMMARY}|%{LICENSE}|%{URL}|%{SIZE}\n"
        cmd = get_host_command_prefix() + ["rpm", "-qp", "--queryformat", qf, file_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=8)
        if proc.returncode == 0 and proc.stdout.strip():
            parts = proc.stdout.strip().split("|")
            if len(parts) >= 8:
                name, ver, rel, arch, summary, lic, url, size_str = parts[:8]
                size = int(size_str) if size_str.isdigit() else 0

                # Check if installed
                chk_cmd = get_host_command_prefix() + ["rpm", "-q", "--queryformat", "%{VERSION}-%{RELEASE}\n", name]
                chk_proc = subprocess.run(chk_cmd, capture_output=True, text=True, env=get_clean_env(), timeout=4)
                is_inst = (chk_proc.returncode == 0 and "not installed" not in chk_proc.stdout)
                inst_ver = chk_proc.stdout.strip() if is_inst else ""

                return LocalRpmInspectionResult(
                    file_path=os.path.abspath(file_path),
                    name=name,
                    version=ver,
                    release=rel,
                    arch=arch,
                    summary=summary,
                    description="",
                    license=lic,
                    url=url,
                    size_bytes=size,
                    is_already_installed=is_inst,
                    installed_version=inst_ver
                )
    except Exception:
        pass

    return None


def find_package_owning_file(file_path: str) -> Optional[str]:
    """
    Resolves which installed RPM package owns the specified file path (rpm -qf).
    Uses the native librpm 'basenames' B-Tree index for sub-millisecond lookups.
    """
    clean_path = file_path.strip().removeprefix("file:").strip()
    if not clean_path.startswith("/"):
        return None

    clean_path = os.path.normpath(clean_path)
    base_name = os.path.basename(clean_path)
    dir_name = os.path.dirname(clean_path).rstrip("/") + "/"

    ts = create_rpm_transaction_set()
    if ts is not None:
        with RPM_GLOBAL_LOCK:
            matches = None
            try:
                matches = ts.dbMatch("basenames", base_name)
                for hdr in matches:
                    dirs = [_decode_rpm_str(d) for d in (hdr[rpm.RPMTAG_DIRNAMES] or [])]
                    bases = [_decode_rpm_str(b) for b in (hdr[rpm.RPMTAG_BASENAMES] or [])]
                    d_indices = hdr[rpm.RPMTAG_DIRINDEXES] or []

                    if len(d_indices) == len(bases):
                        for b, d_idx in zip(bases, d_indices):
                            if b == base_name and 0 <= d_idx < len(dirs):
                                full_file = dirs[d_idx].rstrip("/") + "/" + b
                                if full_file == clean_path:
                                    pkg_name = _decode_rpm_str(hdr[rpm.RPMTAG_NAME])
                                    return pkg_name
                    else:
                        files = [_decode_rpm_str(f) for f in (hdr[rpm.RPMTAG_FILENAMES] or [])]
                        if clean_path in files:
                            pkg_name = _decode_rpm_str(hdr[rpm.RPMTAG_NAME])
                            return pkg_name
            except Exception:
                pass
            finally:
                del matches
                del ts

    # Subprocess fallback (rpm -qf)
    try:
        cmd = get_host_command_prefix() + ["rpm", "-qf", "--queryformat", "%{NAME}\n", clean_path]
        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=4)
        if res.returncode == 0 and res.stdout.strip():
            lines = res.stdout.strip().splitlines()
            if lines and not lines[0].startswith("error:"):
                return lines[0].strip()
    except Exception:
        pass

    return None


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
# Layer 0A: Fedora Distribution Comps Catalog (comps.xml)
# =============================================================================

class FedoraCompsCatalog:
    """
    Parses and indexes distribution package groups directly from local DNF5 / DNF
    repository caches (/var/cache/libdnf5/**/repodata/*comps*.xml*).
    Used as an auxiliary signal for CLI utilities and security tools.
    """
    _instance: Optional[FedoraCompsCatalog] = None
    _lock = threading.Lock()

    COMPS_GROUP_MAP: Final[Dict[str, str]] = {
        "core": "fedora_core",
        "base": "fedora_core",
        "standard": "fedora_core",
        "development-tools": "devel",
        "development-libs": "c_libs",
        "c-development": "devel",
        "system-tools": "cli_tools",
        "security-lab": "security_pkgs",
    }

    def __init__(self):
        self.package_to_comps: Dict[str, str] = {}
        self._loaded = False
        self._load_comps()

    @classmethod
    def get_instance(cls) -> FedoraCompsCatalog:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _load_comps(self):
        if self._loaded:
            return

        # Shallow repodata globbing (repodata is always at /var/cache/libdnf5/<repo>/repodata/)
        cache_patterns = [
            "/var/cache/libdnf5/*/repodata/*comps*.xml*",
            "/var/cache/dnf/*/repodata/*comps*.xml*",
            os.path.expanduser("~/.cache/libdnf5/*/repodata/*comps*.xml*"),
        ]

        comps_files: List[str] = []
        for pattern in cache_patterns:
            comps_files.extend(glob.glob(pattern, recursive=False))

        for file_path in comps_files:
            try:
                open_fn = open
                if file_path.endswith(".gz"):
                    open_fn = gzip.open
                elif file_path.endswith(".bz2"):
                    open_fn = bz2.open

                with open_fn(file_path, "rb") as f:
                    context = ET.iterparse(f, events=("end",))
                    current_group_id = ""
                    for _, elem in context:
                        if elem.tag == "id" and elem.text:
                            current_group_id = elem.text.strip().lower()
                        elif elem.tag == "packagereq" and elem.text and current_group_id:
                            pkg_name = elem.text.strip().lower()
                            if current_group_id in self.COMPS_GROUP_MAP and pkg_name not in self.package_to_comps:
                                self.package_to_comps[pkg_name] = self.COMPS_GROUP_MAP[current_group_id]
                        elif elem.tag == "group":
                            current_group_id = ""
                            elem.clear()
            except Exception:
                continue

        self._loaded = True

    def get_domain_for_package(self, pkg_name: str) -> Optional[str]:
        return self.package_to_comps.get(pkg_name.lower())


# =============================================================================
# Layer 0B: FreeDesktop AppStream Specification Catalog (1.0+)
# =============================================================================

class AppStreamCatalog:
    """
    Parses and indexes AppStream MetaInfo and Catalog metadata adhering strictly
    to the FreeDesktop AppStream Component Specification (1.0+).
    Never defaults untyped components to 'desktop' to prevent headless package pollution.
    """
    _instance: Optional[AppStreamCatalog] = None
    _lock = threading.Lock()

    def __init__(self):
        self.desktop_packages: Set[str] = set()
        self.console_packages: Set[str] = set()
        self.inputmethod_packages: Set[str] = set()
        self.addon_packages: Set[str] = set()
        self.codec_packages: Set[str] = set()
        self.font_packages: Set[str] = set()
        self.firmware_packages: Set[str] = set()
        self.driver_packages: Set[str] = set()
        self.service_packages: Set[str] = set()
        self.localization_packages: Set[str] = set()
        self.icon_theme_packages: Set[str] = set()
        self.runtime_packages: Set[str] = set()
        self.pkg_xdg_categories: Dict[str, Set[str]] = {}
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
            "/usr/share/metainfo",
        ]
        if is_running_in_flatpak():
            catalog_dirs.extend([
                "/run/host/usr/share/swcatalog/xml",
                "/run/host/var/lib/swcatalog/xml",
                "/run/host/usr/share/metainfo",
            ])

        for cat_dir in catalog_dirs:
            if not os.path.isdir(cat_dir):
                continue
            try:
                for file_name in os.listdir(cat_dir):
                    if file_name.endswith(".xml") or file_name.endswith(".xml.gz"):
                        full_path = os.path.join(cat_dir, file_name)
                        self._parse_appstream_file(full_path)
            except Exception:
                continue

        self._loaded = True

    def _parse_appstream_file(self, file_path: str):
        try:
            open_fn = gzip.open if file_path.endswith(".gz") else open
            with open_fn(file_path, "rb") as f:
                # Fast iterparse with root cleanup to prevent memory bloat on large XML catalogs
                context = ET.iterparse(f, events=("start", "end"))
                _, root = next(context)
                for event, elem in context:
                    if event == "end" and elem.tag in ("component", "application"):
                        comp_type = (elem.get("type") or "").strip().lower()
                        pkg_elem = elem.find("pkgname")

                        if pkg_elem is None:
                            bundle = elem.find("bundle")
                            if bundle is not None and bundle.get("type") == "package":
                                pkg_elem = bundle

                        if pkg_elem is not None and pkg_elem.text:
                            pkg_name = pkg_elem.text.strip().lower()

                            if comp_type in ("desktop", "desktop-application"):
                                self.desktop_packages.add(pkg_name)
                            elif comp_type in ("console", "console-application"):
                                self.console_packages.add(pkg_name)
                            elif comp_type in ("inputmethod", "input-method"):
                                self.inputmethod_packages.add(pkg_name)
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
                            elif comp_type == "runtime":
                                self.runtime_packages.add(pkg_name)

                            cats_elem = elem.find("categories")
                            if cats_elem is not None:
                                cats = {c.text.strip().lower() for c in cats_elem.findall("category") if c.text}
                                if pkg_name in self.pkg_xdg_categories:
                                    self.pkg_xdg_categories[pkg_name].update(cats)
                                else:
                                    self.pkg_xdg_categories[pkg_name] = cats

                        # Clear both element and root references to free RAM and eliminate parse latency
                        elem.clear()
                        root.clear()
        except Exception:
            pass


# =============================================================================
# Layer 1: Structural Physical & Capability Anatomy
# =============================================================================

@dataclass(slots=True)
class PackagePhysicalAnatomy:
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
    has_media_plugins_dir: bool = False
    has_gui_toolkit_dir: bool = False
    has_kio_dir: bool = False
    has_shell_or_session_dir: bool = False
    has_jvm_dir: bool = False
    has_jar_files: bool = False
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
    provides_wm: bool = False
    provides_gstreamer: bool = False
    provides_python_dist: bool = False
    provides_rust_crate: bool = False
    provides_jvm_artifact: bool = False
    provides_nodejs_pkg: bool = False
    provided_desktop_ids: Set[str] = field(default_factory=set)

    @property
    def has_plugins_dir(self) -> bool:
        return self.has_media_plugins_dir or self.has_gui_toolkit_dir

    @classmethod
    def from_manifest_data(
        cls,
        dirnames: List[str],
        provides: List[str],
        basenames: Optional[List[str]] = None
    ) -> PackagePhysicalAnatomy:
        anatomy = cls()
        if basenames is None:
            basenames = []

        for d in dirnames:
            d_clean = d.strip().rstrip("/")
            if not d_clean:
                continue

            if d_clean in ("/usr/bin", "/bin"):
                anatomy.has_binaries = True
                anatomy.has_user_bin = True

            elif d_clean in ("/usr/sbin", "/sbin"):
                anatomy.has_binaries = True
                anatomy.has_admin_sbin = True

            elif d_clean == "/usr/libexec" or d_clean.startswith("/usr/libexec/"):
                anatomy.has_libexec = True

            elif d_clean in ("/usr/share/applications", "/usr/local/share/applications") or d_clean.startswith(
                ("/usr/share/applications/", "/usr/local/share/applications/")
            ):
                anatomy.has_desktop_file = True

            elif d_clean.startswith(("/usr/lib/systemd/system", "/lib/systemd/system")):
                anatomy.has_systemd_system = True
            elif d_clean.startswith("/usr/lib/systemd/user"):
                anatomy.has_systemd_user = True

            elif d_clean == "/usr/include" or d_clean.startswith("/usr/include/"):
                anatomy.has_c_headers = True

            elif d_clean == "/usr/share/fonts" or d_clean.startswith("/usr/share/fonts/"):
                anatomy.has_fonts_dir = True

            elif any(d_clean == p or d_clean.startswith(p + "/") for p in ("/usr/share/themes", "/usr/share/backgrounds")):
                anatomy.has_themes_dir = True

            elif d_clean.startswith("/usr/share/icons/"):
                # Standard GUI apps install icons into /usr/share/icons/hicolor/*/apps/.
                # Only dedicated icon themes outside 'hicolor' qualify as theme packages.
                parts = d_clean.split("/")
                if len(parts) >= 5 and parts[4] != "hicolor":
                    anatomy.has_themes_dir = True

            elif any(d_clean == p or d_clean.startswith(p + "/") for p in ("/usr/share/doc", "/usr/share/help")):
                anatomy.has_docs_dir = True

            elif d_clean == "/usr/lib/firmware" or d_clean.startswith("/usr/lib/firmware/"):
                anatomy.has_firmware_dir = True

            elif d_clean.startswith("/usr/lib/modules/"):
                anatomy.has_kernel_modules_dir = True

            elif d_clean in ("/usr/lib64/dri", "/usr/lib/dri") or d_clean.startswith(("/usr/lib64/dri/", "/usr/lib/dri/")):
                anatomy.has_dri_dir = True
            elif d_clean in ("/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d"):
                anatomy.has_dri_dir = True

            # Separate media plugins from GUI toolkit plugin architectures
            elif any(p in d_clean for p in ("/vlc/plugins", "/gstreamer-1.0", "/xine/plugins", "/ladspa", "/lv2")):
                anatomy.has_media_plugins_dir = True
            elif any(p in d_clean for p in ("/qt5/plugins", "/qt6/plugins", "/gtk-3.0", "/gtk-4.0")):
                anatomy.has_gui_toolkit_dir = True
            elif "/kio" in d_clean:
                anatomy.has_kio_dir = True

            # Desktop shells, Wayland/X11 sessions and desktop manager layouts
            elif any(p in d_clean for p in (
                "/usr/share/plasma/shells", "/usr/share/gnome-shell",
                "/usr/share/cinnamon", "/usr/share/wayland-sessions", "/usr/share/xsessions"
            )):
                anatomy.has_shell_or_session_dir = True

            elif d_clean in ("/usr/share/java", "/usr/lib/jvm") or d_clean.startswith(("/usr/share/java/", "/usr/lib/jvm/")):
                anatomy.has_jvm_dir = True

            elif d_clean == "/usr/share/locale" or d_clean.startswith("/usr/share/locale/"):
                anatomy.has_locales_dir = True

            elif d_clean in ("/usr/lib64", "/usr/lib"):
                anatomy.has_shared_libs_dir = True

            elif d_clean.endswith("/man/man1") or "/man/man1/" in d_clean:
                anatomy.has_man1 = True
            elif d_clean.endswith("/man/man8") or "/man/man8/" in d_clean:
                anatomy.has_man8 = True
            elif d_clean.endswith("/man/man3") or "/man/man3/" in d_clean:
                anatomy.has_man3 = True

            elif "/python3" in d_clean and "site-packages" in d_clean:
                anatomy.has_python_runtime = True

        for b in basenames:
            if b.endswith(".jar"):
                anatomy.has_jar_files = True
                break

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
            elif "windowmanager" in prov_str.lower():
                anatomy.provides_wm = True
            elif prov_str.startswith("gstreamer1("):
                anatomy.provides_gstreamer = True
            elif prov_str.startswith("python3dist("):
                anatomy.provides_python_dist = True
            elif prov_str.startswith("crate("):
                anatomy.provides_rust_crate = True
            elif prov_str.startswith(("mvn(", "osgi(")):
                anatomy.provides_jvm_artifact = True
            elif prov_str.startswith("npm("):
                anatomy.provides_nodejs_pkg = True
            elif prov_str.startswith("application(") and prov_str.endswith(")"):
                desktop_id = prov_str[12:-1].strip()
                if desktop_id:
                    anatomy.provided_desktop_ids.add(desktop_id)
                    anatomy.has_desktop_file = True

        return anatomy

    @classmethod
    def from_rpm_header(cls, header: Any) -> PackagePhysicalAnatomy:
        if not HAS_NATIVE_RPM or header is None:
            return cls()

        try:
            raw_dirs = [_decode_rpm_str(d) for d in (header[rpm.RPMTAG_DIRNAMES] or [])]
            raw_provs = [_decode_rpm_str(p) for p in (header[rpm.RPMTAG_PROVIDENAME] or [])]
            raw_basenames = [_decode_rpm_str(b) for b in (header[rpm.RPMTAG_BASENAMES] or [])]
            anatomy = cls.from_manifest_data(raw_dirs, raw_provs, raw_basenames)
            raw_dirindexes = header[rpm.RPMTAG_DIRINDEXES] or []

            has_real_desktop_ext = False
            if raw_dirindexes and len(raw_dirindexes) == len(raw_basenames):
                for b_name, d_idx in zip(raw_basenames, raw_dirindexes):
                    if b_name.endswith(".desktop") and 0 <= d_idx < len(raw_dirs):
                        d_path = raw_dirs[d_idx].rstrip("/")
                        if d_path in ("/usr/share/applications", "/usr/local/share/applications") or d_path.startswith(
                            ("/usr/share/applications/", "/usr/local/share/applications/")
                        ):
                            has_real_desktop_ext = True
                            anatomy.provided_desktop_ids.add(b_name)
            else:
                has_real_desktop_ext = any(b.endswith(".desktop") for b in raw_basenames)

            if not has_real_desktop_ext and not anatomy.provided_desktop_ids:
                anatomy.has_desktop_file = False

            return anatomy
        except Exception:
            return cls()


# =============================================================================
# Layer 2: XDG Desktop Entry Specification Parser
# =============================================================================

class DesktopEntryMetadata:
    __slots__ = (
        "desktop_id", "name", "exec_cmd", "categories", 
        "is_nodisplay", "is_terminal", "is_wm", "is_im", "is_settings", "is_auxiliary"
    )

    def __init__(self, file_path: str):
        self.desktop_id = os.path.basename(file_path)
        self.name = ""
        self.exec_cmd = ""
        self.categories: Set[str] = set()
        self.is_nodisplay = False
        self.is_terminal = False
        self.is_wm = False
        self.is_im = False
        self.is_settings = False
        self.is_auxiliary = False
        self._parse(file_path)

    def _parse(self, file_path: str):
        clean_p = file_path.replace("\\", "/")
        # Non-application desktop entries MUST NEVER be treated as interactive applications
        if "/autostart/" in clean_p or "/kservices" in clean_p or "/akonadi/" in clean_p:
            self.is_auxiliary = True

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                in_entry = False
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("["):
                        in_entry = (line == "[Desktop Entry]")
                        continue
                    if in_entry and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip()
                        if k == "Type" and v != "Application":
                            self.is_nodisplay = True
                        elif k in ("NoDisplay", "Hidden") and v.lower() == "true":
                            self.is_nodisplay = True
                        elif k == "Terminal" and v.lower() == "true":
                            self.is_terminal = True
                        elif k == "Exec" and not self.exec_cmd:
                            self.exec_cmd = os.path.basename(v.split()[0].strip('"\'')).lower()
                        elif k == "Categories":
                            self.categories = {c.strip().lower() for c in v.split(";") if c.strip()}
                        elif k in ("X-GNOME-Provides", "X-GNOME-Autostart-Phase"):
                            val_l = v.lower()
                            if "windowmanager" in val_l:
                                self.is_wm = True
                            if "inputmethod" in val_l:
                                self.is_im = True

            if any(c in self.categories for c in ("windowmanager", "windowmaker")):
                self.is_wm = True
            if any(c in self.categories for c in ("inputmethod", "x-inputmethod")):
                self.is_im = True
            if any(c in self.categories for c in (
                "settings", "desktopsettings", "hardwaresettings",
                "packagemanager", "preferences", "x-kde-settings",
                "x-gnome-settings-panel", "x-unity-settings-panel"
            )):
                self.is_settings = True
            if any(c in self.categories for c in ("screensaver", "trayicon", "applet", "statusicon", "shell")):
                self.is_auxiliary = True
        except Exception:
            self.is_nodisplay = True


class SystemDesktopIndex:
    _instance: Optional[SystemDesktopIndex] = None
    _lock = threading.Lock()

    def __init__(self):
        self.desktop_map: Dict[str, DesktopEntryMetadata] = {}
        self._load_all_desktop_files()

    @classmethod
    def get_instance(cls) -> SystemDesktopIndex:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _load_all_desktop_files(self):
        search_dirs = ["/usr/share/applications", "/usr/local/share/applications"]
        if is_running_in_flatpak():
            search_dirs.extend(["/run/host/usr/share/applications", "/run/host/usr/local/share/applications"])

        for s_dir in search_dirs:
            if not os.path.isdir(s_dir):
                continue
            for root, _, files in os.walk(s_dir):
                for file in files:
                    if file.endswith(".desktop"):
                        full_path = os.path.join(root, file)
                        entry = DesktopEntryMetadata(full_path)
                        self.desktop_map[file] = entry
                        self.desktop_map[file.lower()] = entry


# =============================================================================
# Layer 3: Deterministic Two-Phase Ontological Classification Engine
# =============================================================================

@dataclass(slots=True)
class ClassificationDecision:
    primary_category: str
    sub_category: str
    parent_pillar: str
    confidence: float
    rationale: List[str]
    secondary_tags: List[str]
    flags: Dict[str, bool]


class ProductionTaxonomyEngine:
    """
    Two-Phase Deterministic Classification Pipeline:
    Phase 1: Deterministic Physical Archetype (FHS Footprint & explicit contracts)
    Phase 2: FreeDesktop XDG Domain & POSIX Subcategory Mapping
    Zero floating-point scoring, zero coupled negative guards, 100% deterministic.
    """

    CORE_PILLARS = FEDORA_SYSTEM_ROOT_PILLARS

    @classmethod
    def classify(
        cls,
        name: str,
        summary: str = "",
        description: str = "",
        anatomy: Optional[PackagePhysicalAnatomy] = None,
        desktop_entry_files: Optional[List[str]] = None,
        provides: Optional[List[str]] = None,
        appstream: Optional[AppStreamCatalog] = None,
        comps: Optional[FedoraCompsCatalog] = None,
        vendor: str = "",
        packager: str = "",
    ) -> ClassificationDecision:
        if anatomy is None:
            anatomy = PackagePhysicalAnatomy()
        if desktop_entry_files is None:
            desktop_entry_files = list(anatomy.provided_desktop_ids)
        if provides is None:
            provides = []
        if appstream is None:
            appstream = AppStreamCatalog.get_instance()
        if comps is None:
            comps = FedoraCompsCatalog.get_instance()

        name_lower = name.lower()
        desktop_index = SystemDesktopIndex.get_instance()

        # Law 4: O(1) Direct Taxonomy Override Resolution
        if name_lower in PACKAGE_TAXONOMY_OVERRIDES:
            ovr_arch, ovr_pillar, ovr_primary, ovr_sub, ovr_rationale = PACKAGE_TAXONOMY_OVERRIDES[name_lower]
            flags = {
                "is_desktop_app": (ovr_arch == PackageArchetype.DESKTOP_APP and ovr_primary != "system_settings"),
                "is_cli_tool": (ovr_arch == PackageArchetype.CLI_UTILITY),
                "is_system_settings": (ovr_primary == "system_settings" or ovr_sub == "system_settings"),
                "is_graphics_driver": (ovr_primary == "graphics_drivers"),
                "is_audio_sound": (ovr_primary == "audio_sound"),
                "is_media_plugin": (ovr_arch == PackageArchetype.MEDIA_CODEC or ovr_primary == "media_plugins"),
                "is_desktop_addon": (ovr_arch == PackageArchetype.DESKTOP_ADDON or ovr_primary == "desktop_addons"),
                "is_gui_toolkit": (ovr_arch == PackageArchetype.GUI_TOOLKIT or ovr_primary == "gui_toolkits"),
                "is_fedora_core": (ovr_primary == "fedora_core"),
                "is_c_lib": (ovr_arch == PackageArchetype.SHARED_LIBRARY and ovr_primary == "c_libs"),
                "is_systemd_service": (ovr_arch == PackageArchetype.SYSTEM_DAEMON or ovr_primary == "systemd_services"),
                "is_firmware": (ovr_primary == "firmware"),
                "is_kernel_module": (ovr_primary == "kernel_modules"),
                "is_font": (ovr_primary == "fonts"),
                "is_devel": (ovr_arch == PackageArchetype.DEVELOPMENT_SDK or ovr_primary == "devel"),
                "is_locale": (ovr_primary == "locales"),
                "is_theme": (ovr_primary == "themes"),
                "is_python_pkg": (ovr_primary == "python_pkgs"),
                "is_rust_pkg": (ovr_primary == "rust_pkgs"),
                "is_jvm_pkg": (ovr_primary == "jvm_pkgs"),
                "is_nodejs_pkg": (ovr_primary == "nodejs_pkgs"),
                "is_security_pkg": (ovr_primary == "security_pkgs"),
                "is_library": ovr_arch in (
                    PackageArchetype.SHARED_LIBRARY,
                    PackageArchetype.DEVELOPMENT_SDK,
                    PackageArchetype.STATIC_ASSET,
                    PackageArchetype.GUI_TOOLKIT,
                    PackageArchetype.MEDIA_CODEC,
                ),
            }
            return ClassificationDecision(
                primary_category=ovr_primary,
                sub_category=ovr_sub,
                parent_pillar=ovr_pillar,
                confidence=1.0,
                rationale=[ovr_rationale],
                secondary_tags=[],
                flags=flags,
            )

        # ---------------------------------------------------------------------
        # 1. Parse Valid Interactive GUI Launchers
        #    STRICT RULE: Only desktop entries in /usr/share/applications qualify!
        #    Autostart files, agent files, and background services ARE EXCLUDED.
        # ---------------------------------------------------------------------
        xdg_categories: Set[str] = set(appstream.pkg_xdg_categories.get(name_lower, set()))
        has_gui_launcher = False
        is_settings_panel = False
        is_terminal_launcher = False
        is_wm = False

        for d_id in desktop_entry_files:
            meta = desktop_index.desktop_map.get(d_id) or desktop_index.desktop_map.get(d_id.lower())
            if not meta:
                # Do NOT default to True! If it's not in /usr/share/applications, it's not a GUI app launcher.
                continue

            xdg_categories.update(meta.categories)

            if meta.is_wm:
                is_wm = True
            if meta.is_settings:
                is_settings_panel = True
            if meta.is_terminal:
                is_terminal_launcher = True

            # Visible application launchers are always valid GUI launchers
            if not meta.is_nodisplay and not meta.is_auxiliary and not meta.is_im and not meta.is_wm:
                has_gui_launcher = True

        if name_lower in appstream.desktop_packages:
            has_gui_launcher = True
        if name_lower in appstream.console_packages:
            is_terminal_launcher = True

        # ---------------------------------------------------------------------
        # PHASE 1: DETERMINISTIC ARCHETYPE SELECTION (Strict Decision Ladder)
        # ---------------------------------------------------------------------
        archetype: PackageArchetype
        parent_pillar: str
        primary_category: str
        rationale: List[str] = []

        # 1. Core Fedora Base OS Infrastructure (Strictly Isolated from Drivers/Services)
        if (
            name_lower in DEFAULT_FEDORA_CORE_PACKAGES
            or any(p.startswith(("system-release", "fedora-release", "generic-release")) for p in provides)
            or any(p in ("filesystem", "setup") for p in provides)
        ):
            archetype = PackageArchetype.CORE_SYSTEM
            parent_pillar = "pillar_system"
            primary_category = "fedora_core"
            rationale.append("Core Fedora base OS infrastructure component")

        # 2. Hardware Kernel Modules & Microcode Firmware
        elif anatomy.has_kernel_modules_dir or anatomy.provides_kmod or name_lower.startswith(("kmod-", "akmod-")):
            archetype = PackageArchetype.HARDWARE_DRIVER
            parent_pillar = "pillar_hardware"
            primary_category = "kernel_modules"
            rationale.append("Ships compiled kernel modules in /usr/lib/modules")

        elif anatomy.has_firmware_dir or name_lower in appstream.firmware_packages or name_lower.startswith("linux-firmware") or name_lower.endswith("-firmware"):
            archetype = PackageArchetype.HARDWARE_DRIVER
            parent_pillar = "pillar_hardware"
            primary_category = "firmware"
            rationale.append("Ships hardware firmware microcode in /usr/lib/firmware")

        # 3. Hardware GPU Acceleration Stack (Only if not a desktop application)
        elif not has_gui_launcher and (
            anatomy.has_dri_dir
            or name_lower.startswith(("mesa-dri-", "mesa-vulkan-", "nvidia-", "libdrm"))
            or name_lower in appstream.driver_packages
        ):
            archetype = PackageArchetype.HARDWARE_DRIVER
            parent_pillar = "pillar_hardware"
            primary_category = "graphics_drivers"
            rationale.append("Low-level 3D acceleration or GPU driver library")

        # 4. Low-Level Sound Architecture (Daemons/servers only, NOT user media players)
        elif not has_gui_launcher and (
            name_lower in ("pipewire", "wireplumber", "pulseaudio", "alsa-lib", "alsa-ucm", "alsa-topology")
            or name_lower.startswith("alsa-")
        ):
            archetype = PackageArchetype.HARDWARE_DRIVER
            parent_pillar = "pillar_hardware"
            primary_category = "audio_sound"
            rationale.append("Core Linux sound server and routing infrastructure")

        # 5. Window Managers & True Display Compositors
        elif is_wm or anatomy.provides_wm or any("windowmanager" in p.lower() for p in provides):
            archetype = PackageArchetype.DESKTOP_ADDON
            parent_pillar = "pillar_system"
            primary_category = "desktop_addons"
            rationale.append("Window manager or display compositor")

        # 6. User-Facing Desktop Applications (GUI) - Evaluated Before Desktop Addons
        elif has_gui_launcher and not is_terminal_launcher:
            archetype = PackageArchetype.DESKTOP_APP
            parent_pillar = "pillar_apps"
            primary_category = "system_settings" if is_settings_panel else "user_apps"
            rationale.append("Interactive graphical application with verified desktop launcher")

        # 7. Systemd Daemons & Background Services
        elif anatomy.has_systemd_system or anatomy.has_systemd_user or name_lower in appstream.service_packages:
            archetype = PackageArchetype.SYSTEM_DAEMON
            parent_pillar = "pillar_system"
            primary_category = "systemd_services"
            rationale.append("Managed background service (systemd unit present)")

        # 8. Security & System Policies
        elif (
            "selinux" in name_lower
            or name_lower.startswith(("pam", "shadow-utils", "audit", "firewalld", "polkit"))
            or comps.get_domain_for_package(name_lower) == "security_pkgs"
        ):
            archetype = PackageArchetype.CORE_SYSTEM
            parent_pillar = "pillar_system"
            primary_category = "security_pkgs"
            rationale.append("Security authentication, PAM module, or access control policy")

        # 9. Desktop Environment Addons & Shell Extensions
        elif (
            anatomy.has_kio_dir
            or name_lower in appstream.addon_packages
            or any(kw in name_lower for kw in ("gnome-shell-extension-", "kwin-script-", "plymouth-plugin-"))
        ):
            archetype = PackageArchetype.DESKTOP_ADDON
            parent_pillar = "pillar_system"
            primary_category = "desktop_addons"
            rationale.append("Desktop environment shell extension, addon, or KIO worker")

        # 10. Development Headers, SDKs & Reference Manuals
        elif (
            name_lower.endswith(("-devel", "-static", "-dev"))
            or anatomy.has_c_headers
            or (anatomy.provides_pkgconfig and not anatomy.has_user_bin)
            or (
                (anatomy.has_docs_dir or anatomy.has_man1 or anatomy.has_man3)
                and not anatomy.exported_sonames
                and not anatomy.has_binaries
                and name_lower.endswith(("-doc", "-docs", "-manual", "-man", "man-pages"))
            )
        ):
            archetype = PackageArchetype.DEVELOPMENT_SDK
            parent_pillar = "pillar_libs"
            primary_category = "devel"
            rationale.append("Development headers, pkgconfig interface, or system reference documentation")

        # 11. Media Codecs & Decoders
        elif (
            name_lower in appstream.codec_packages
            or anatomy.has_media_plugins_dir
            or anatomy.provides_gstreamer
            or any(kw in name_lower for kw in ("gstreamer1-", "ffmpeg-libs", "libavcodec", "kimageformats", "openh264"))
        ):
            archetype = PackageArchetype.MEDIA_CODEC
            parent_pillar = "pillar_libs"
            primary_category = "media_plugins"
            rationale.append("Media decoder, format plugin, or codec library")

        # 12. GUI Toolkits & Widget Frameworks
        elif (
            anatomy.has_gui_toolkit_dir
            or any(kw in name_lower for kw in ("qt6-", "qt5-", "gtk3", "gtk4", "wxwidgets", "tkinter", "libadwaita"))
        ):
            archetype = PackageArchetype.GUI_TOOLKIT
            parent_pillar = "pillar_libs"
            primary_category = "gui_toolkits"
            rationale.append("GUI widget toolkit and windowing system framework")

        # 13. Static Desktop Assets (Fonts, Themes, Locales)
        elif anatomy.has_fonts_dir or anatomy.provides_font or name_lower.endswith(("-fonts", "-font")):
            archetype = PackageArchetype.STATIC_ASSET
            parent_pillar = "pillar_libs"
            primary_category = "fonts"
            rationale.append("Typography and font assets in /usr/share/fonts")

        elif anatomy.has_themes_dir or name_lower in appstream.icon_theme_packages or any(kw in name_lower for kw in ("-theme", "-icon-theme")):
            archetype = PackageArchetype.STATIC_ASSET
            parent_pillar = "pillar_libs"
            primary_category = "themes"
            rationale.append("Visual desktop theme, icon theme, or cursor assets")

        elif anatomy.has_locales_dir or name_lower.startswith(("glibc-langpack-", "langpacks-")):
            archetype = PackageArchetype.STATIC_ASSET
            parent_pillar = "pillar_libs"
            primary_category = "locales"
            rationale.append("Localization, message catalogs, and translation assets")

        # 14. Language Ecosystem Modules (Libraries only; CLI commands like ansible/meson route to Step 15)
        elif (
            (anatomy.has_python_runtime and not (anatomy.has_user_bin and not name_lower.startswith(("python3-", "python-"))))
            or (anatomy.provides_python_dist and not (anatomy.has_user_bin and not name_lower.startswith(("python3-", "python-"))))
            or name_lower.startswith(("python3-", "python-"))
        ):
            archetype = PackageArchetype.ECOSYSTEM_RUNTIME
            parent_pillar = "pillar_libs"
            primary_category = "python_pkgs"
            rationale.append("Python language package or runtime library")

        elif anatomy.provides_rust_crate or name_lower.startswith(("rust-", "cargo-")):
            archetype = PackageArchetype.ECOSYSTEM_RUNTIME
            parent_pillar = "pillar_libs"
            primary_category = "rust_pkgs"
            rationale.append("Rust language crate or toolchain library")

        elif (
            anatomy.has_jvm_dir
            or anatomy.has_jar_files
            or anatomy.provides_jvm_artifact
            or name_lower.startswith(("java-", "openjdk-"))
        ):
            archetype = PackageArchetype.ECOSYSTEM_RUNTIME
            parent_pillar = "pillar_libs"
            primary_category = "jvm_pkgs"
            rationale.append("Java / JVM bytecode archive or runtime platform")

        elif anatomy.provides_nodejs_pkg or name_lower.startswith(("nodejs-", "npm-")):
            archetype = PackageArchetype.ECOSYSTEM_RUNTIME
            parent_pillar = "pillar_libs"
            primary_category = "nodejs_pkgs"
            rationale.append("Node.js runtime or npm ecosystem package")

        # 15. Command-Line Utilities (Binary in PATH without GUI desktop launcher)
        elif anatomy.has_binaries or is_terminal_launcher:
            archetype = PackageArchetype.CLI_UTILITY
            parent_pillar = "pillar_cli"
            primary_category = "cli_tools"
            rationale.append("Interactive command-line executable installed in user PATH")

        # 16. Shared ELF Object Libraries (Fallback)
        elif anatomy.exported_sonames:
            archetype = PackageArchetype.SHARED_LIBRARY
            parent_pillar = "pillar_libs"
            primary_category = "c_libs"
            rationale.append("Exports dynamic C/C++ ELF shared object libraries")

        else:
            archetype = PackageArchetype.SHARED_LIBRARY
            parent_pillar = "pillar_libs"
            primary_category = "c_libs"
            rationale.append("Classified by FHS structural footprint inspection")

        # =====================================================================
        # PHASE 2: FUNCTIONAL SUBCATEGORY MAPPING
        #          Only evaluated within the boundary of the resolved Archetype!
        # =====================================================================
        sub_category = primary_category

        # Subcategories for Desktop Applications (FreeDesktop XDG Menu Spec)
        if archetype == PackageArchetype.DESKTOP_APP:
            has_net_comms = any(c in xdg_categories for c in (
                "webbrowser", "email", "chat", "ircclient", "feed",
                "news", "filetransfer", "p2p", "remoteaccess", "telephony",
                "videoconference", "instantmessaging"
            ))
            is_generic_net_tool = ("network" in xdg_categories) and not has_net_comms and any(
                c in xdg_categories for c in ("utility", "system", "filetools", "settings")
            )

            if is_settings_panel:
                sub_category = "system_settings"
            elif is_generic_net_tool:
                sub_category = "desktop_utilities"
            elif has_net_comms or "network" in xdg_categories:
                sub_category = "desktop_internet"
            elif any(c in xdg_categories for c in (
                "audiovideo", "audio", "video", "player", "recorder", "music",
                "audiovideoediting", "discburning", "mixer", "sequencer", "midi", "tuner", "tv"
            )):
                sub_category = "desktop_multimedia"
            elif any(c in xdg_categories for c in (
                "graphics", "2dgraphics", "rastergraphics", "vectorgraphics",
                "3dgraphics", "photography", "viewer", "scanning", "ocr"
            )):
                sub_category = "desktop_graphics"
            elif any(c in xdg_categories for c in (
                "office", "wordprocessor", "spreadsheet", "presentation", "publishing",
                "finance", "calendar", "contactmanagement", "database", "dictionary",
                "chart", "flowchart", "projectmanagement"
            )):
                sub_category = "desktop_office"
            elif any(c in xdg_categories for c in (
                "development", "ide", "debugger", "building", "texteditor",
                "revisioncontrol", "translation", "guidesigner", "profiling", "webdevelopment"
            )):
                sub_category = "desktop_development"
            elif any(c in xdg_categories for c in (
                "game", "simulation", "emulator", "arcade", "boardgame", "actiongame",
                "adventuregame", "blocksgame", "cardgame", "kidsgame", "logicgame",
                "roleplaying", "shooter", "sportsgame", "strategygame"
            )):
                sub_category = "desktop_games"
            else:
                sub_category = "desktop_utilities"

        # Subcategories for Command-Line Utilities (POSIX Execution Roles)
        elif archetype == PackageArchetype.CLI_UTILITY:
            if any(p == "editor" for p in provides) or any(k in name_lower for k in ("vim", "nano", "less", "micro", "emacs", "neovim", "helix", "kakoune")):
                sub_category = "cli_editors"
            elif any(p == "shell" for p in provides) or any(k in name_lower for k in ("bash", "zsh", "fish", "tmux", "screen", "zellij")):
                sub_category = "cli_shells"
            elif any(k in name_lower for k in ("tar", "gzip", "7z", "7zip", "p7zip", "zip", "unzip", "bzip2", "xz", "zstd", "lz4", "cpio", "jq", "yq", "sed", "gawk", "awk")):
                sub_category = "cli_data_archiving"
            elif any(k in name_lower for k in ("curl", "wget", "nmap", "rsync", "iproute", "ssh", "openssh", "traceroute", "net-tools", "tcpdump", "socat", "iperf", "bind-utils", "whois", "dig")):
                sub_category = "cli_networking"
            elif any(k in name_lower for k in ("htop", "btop", "atop", "iotop", "iftop", "strace", "glances", "gdb", "valgrind", "ncdu", "duf", "procps", "perf", "lsof", "sysstat")):
                sub_category = "cli_monitoring"
            elif any(k in name_lower for k in ("findutils", "grep", "ripgrep", "fd-find", "fzf", "tree", "eza", "bat", "diffutils", "patch")):
                sub_category = "cli_search_files"
            else:
                sub_category = "cli_general"

        # Secondary Tags for multi-role transparency in search
        secondary_tags: List[str] = []
        if anatomy.has_python_runtime or name_lower.startswith("python3-"):
            secondary_tags.append("Python")
        if name_lower.startswith(("rust-", "cargo-")):
            secondary_tags.append("Rust")
        if name_lower.startswith(("java-", "openjdk-")):
            secondary_tags.append("Java/JVM")
        if name_lower.startswith("nodejs-"):
            secondary_tags.append("Node.js")
        if archetype != PackageArchetype.CLI_UTILITY and anatomy.has_binaries and not anatomy.has_libexec:
            secondary_tags.append("CLI Tool")
        if archetype != PackageArchetype.SHARED_LIBRARY and anatomy.exported_sonames:
            secondary_tags.append("Library")
        if archetype != PackageArchetype.DESKTOP_APP and has_gui_launcher:
            secondary_tags.append("Desktop App")

        flags = {
            "is_desktop_app": (archetype == PackageArchetype.DESKTOP_APP and not is_settings_panel),
            "is_cli_tool": (archetype == PackageArchetype.CLI_UTILITY),
            "is_system_settings": (primary_category == "system_settings" or sub_category == "system_settings"),
            "is_graphics_driver": (primary_category == "graphics_drivers"),
            "is_audio_sound": (primary_category == "audio_sound"),
            "is_media_plugin": (archetype == PackageArchetype.MEDIA_CODEC),
            "is_desktop_addon": (archetype == PackageArchetype.DESKTOP_ADDON),
            "is_gui_toolkit": (archetype == PackageArchetype.GUI_TOOLKIT),
            "is_fedora_core": (primary_category == "fedora_core"),
            "is_c_lib": (archetype == PackageArchetype.SHARED_LIBRARY),
            "is_systemd_service": (archetype == PackageArchetype.SYSTEM_DAEMON),
            "is_firmware": (primary_category == "firmware"),
            "is_kernel_module": (primary_category == "kernel_modules"),
            "is_font": (primary_category == "fonts"),
            "is_devel": (archetype == PackageArchetype.DEVELOPMENT_SDK),
            "is_locale": (primary_category == "locales"),
            "is_theme": (primary_category == "themes"),
            "is_python_pkg": "Python" in secondary_tags or primary_category == "python_pkgs",
            "is_rust_pkg": "Rust" in secondary_tags or primary_category == "rust_pkgs",
            "is_jvm_pkg": "Java/JVM" in secondary_tags or primary_category == "jvm_pkgs",
            "is_nodejs_pkg": "Node.js" in secondary_tags or primary_category == "nodejs_pkgs",
            "is_security_pkg": (primary_category == "security_pkgs"),
            "is_library": archetype in (
                PackageArchetype.SHARED_LIBRARY,
                PackageArchetype.DEVELOPMENT_SDK,
                PackageArchetype.STATIC_ASSET,
                PackageArchetype.GUI_TOOLKIT,
                PackageArchetype.MEDIA_CODEC
            ) or "Library" in secondary_tags,
        }

        return ClassificationDecision(
            primary_category=primary_category,
            sub_category=sub_category,
            parent_pillar=parent_pillar,
            confidence=1.0,
            rationale=rationale,
            secondary_tags=secondary_tags,
            flags=flags,
        )

# Backward-compatible aliases
ProductionTaxonomyEngine.classify = ProductionTaxonomyEngine.classify
FreeDesktopTaxonomyEngine = ProductionTaxonomyEngine
IntelligentPackageClassifier = ProductionTaxonomyEngine


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
    package_scriptlets_loaded = pyqtSignal(str, object)
    remote_search_finished = pyqtSignal(str, list)
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
        import time
        t_start = time.time()
        try:
            self.signals.status_update.emit("Scanning AppStream catalog, comps groups & RPM database...")
            
            t0 = time.time()
            appstream = AppStreamCatalog.get_instance()
            t_appstream = time.time() - t0
            
            t0 = time.time()
            comps = FedoraCompsCatalog.get_instance()
            t_comps = time.time() - t0

            t0 = time.time()
            if HAS_NATIVE_RPM and not is_running_in_flatpak():
                packages = self._query_native_librpm(appstream, comps)
            else:
                packages = self._query_cli_subprocess(appstream, comps)
            t_rpm = time.time() - t0

            if self._is_cancelled.is_set():
                return

            packages.sort(key=lambda p: p.name.lower())
            
            if os.environ.get("DENDRO_DEBUG"):
                print(f"[Dendro:debug] Startup timings -> AppStream: {t_appstream:.2f}s | Comps: {t_comps:.2f}s | RPM DB: {t_rpm:.2f}s | Total: {time.time() - t_start:.2f}s ({len(packages)} pkgs)", file=sys.stderr)

            self.signals.packages_loaded.emit(packages)
            self.signals.status_update.emit(f"Loaded {len(packages):,} packages successfully.")

        except Exception as ex:
            self.signals.error_occurred.emit("", f"Failed to query database: {str(ex)}")

    def _query_native_librpm(self, appstream: AppStreamCatalog, comps: FedoraCompsCatalog) -> List[PackageInfo]:
        packages: List[PackageInfo] = []
        if not HAS_NATIVE_RPM or is_running_in_flatpak():
            return self._query_cli_subprocess(appstream, comps)

        protected_pkgs = get_system_protected_packages()
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
                    build_time = ""
                    if b_time_raw and b_time_raw > 0:
                        try:
                            build_time = datetime.fromtimestamp(b_time_raw).strftime('%Y-%m-%d %H:%M')
                        except (OSError, ValueError, OverflowError):
                            build_time = ""

                    i_time_raw = header[rpm.RPMTAG_INSTALLTIME]
                    install_time = ""
                    if i_time_raw and i_time_raw > 0:
                        try:
                            install_time = datetime.fromtimestamp(i_time_raw).strftime('%Y-%m-%d %H:%M')
                        except (OSError, ValueError, OverflowError):
                            install_time = ""

                    size_bytes = int(header[rpm.RPMTAG_SIZE] or 0)
                    anatomy = PackagePhysicalAnatomy.from_rpm_header(header)

                    raw_basenames = [_decode_rpm_str(b) for b in (header[rpm.RPMTAG_BASENAMES] or [])]
                    owned_desktop_files = [b for b in raw_basenames if b.endswith(".desktop")]
                    for did in anatomy.provided_desktop_ids:
                        if did not in owned_desktop_files:
                            owned_desktop_files.append(did)

                    raw_provs = [_decode_rpm_str(p) for p in (header[rpm.RPMTAG_PROVIDENAME] or [])]

                    # Check if package has real requirements (ignoring rpmlib/config/rtld internals)
                    raw_reqs = [_decode_rpm_str(r) for r in (header[rpm.RPMTAG_REQUIRENAME] or [])]
                    has_real_deps = any(
                        not r.startswith(("rpmlib(", "config(", "rtld("))
                        for r in raw_reqs
                    )

                    repo = "Fedora Project"
                    packager_lower = packager.lower()
                    vendor_lower = vendor.lower()
                    if "copr" in packager_lower or "copr" in vendor_lower:
                        repo = "COPR Repository"
                    elif "rpmfusion" in packager_lower or "rpmfusion" in vendor_lower:
                        repo = "RPM Fusion"
                    elif vendor:
                        repo = vendor

                    decision = ProductionTaxonomyEngine.classify(
                        name=name,
                        summary=summary,
                        description=description,
                        anatomy=anatomy,
                        desktop_entry_files=owned_desktop_files,
                        provides=raw_provs,
                        appstream=appstream,
                        comps=comps,
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
                            is_protected=(name.lower() in protected_pkgs or name in FEDORA_SYSTEM_ROOT_PILLARS),
                            has_dependencies=has_real_deps,
                            dependencies_loaded=(not has_real_deps),
                            parent_pillar=decision.parent_pillar,
                            sub_category=decision.sub_category,
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
            return self._query_cli_subprocess(appstream, comps)

        return packages

    def _query_cli_subprocess(self, appstream: AppStreamCatalog, comps: FedoraCompsCatalog) -> List[PackageInfo]:
        protected_pkgs = get_system_protected_packages()
        query_format = (
            "%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}|%{GROUP}|%{SIZE}|%{LICENSE}|"
            "%{URL}|%{PACKAGER}|%{VENDOR}|%{INSTALLTIME:date}|%{SUMMARY}|"
            "[%{DIRNAMES};]|[%{PROVIDENAME};]|[%{BASENAMES};]\n"
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

            raw_dirs = parts[12].split(";") if len(parts) > 12 else []
            raw_provs = parts[13].split(";") if len(parts) > 13 else []
            raw_base = parts[14].split(";") if len(parts) > 14 else []

            anatomy = PackagePhysicalAnatomy.from_manifest_data(raw_dirs, raw_provs)

            owned_desktop_files = [b for b in raw_base if b.endswith(".desktop")]
            for did in anatomy.provided_desktop_ids:
                if did not in owned_desktop_files:
                    owned_desktop_files.append(did)

            if not owned_desktop_files and not anatomy.provided_desktop_ids:
                anatomy.has_desktop_file = False

            decision = ProductionTaxonomyEngine.classify(
                name=name,
                summary=summary,
                description="",
                anatomy=anatomy,
                desktop_entry_files=owned_desktop_files,
                provides=raw_provs,
                appstream=appstream,
                comps=comps,
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
                    is_protected=(name.lower() in protected_pkgs or name in FEDORA_SYSTEM_ROOT_PILLARS),
                    parent_pillar=decision.parent_pillar,
                    sub_category=decision.sub_category,
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
# Worker: User-Installed Query (Subprocess-Safe Engine)
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
# Worker: Leaf / Orphan Packages Query (Subprocess-Safe Engine)
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
        dnf_bin = get_dnf_binary_path()
        if not dnf_bin and not get_host_command_prefix():
            return

        try:
            # Query local installed leaf dependencies without network refresh overhead
            cmd = get_host_command_prefix() + [dnf_bin, "repoquery", "--unneeded", "--installed", "-q", "--queryformat", "%{name}"]
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

            # Query installed version for resolved providers
            provider_versions: Dict[str, str] = {}
            if ts is not None:
                with RPM_GLOBAL_LOCK:
                    for _, cap_name, _ in parsed_reqs:
                        cached = self.cache.get(cap_name)
                        p_name = cached[1] if cached else cap_name
                        if p_name and p_name not in provider_versions:
                            match = None
                            try:
                                match = ts.dbMatch("name", p_name)
                                for hdr in match:
                                    v = _decode_rpm_str(hdr[rpm.RPMTAG_VERSION])
                                    r = _decode_rpm_str(hdr[rpm.RPMTAG_RELEASE])
                                    provider_versions[p_name] = f"{v}-{r}" if r else v
                                    break
                            finally:
                                del match

            for raw_req, cap_name, constraint in parsed_reqs:
                if self._is_cancelled.is_set():
                    return

                cached = self.cache.get(cap_name)
                is_sat, provider = cached if cached else (True, cap_name)

                if provider in seen_clean_names:
                    continue
                seen_clean_names.add(provider)

                inst_ver = provider_versions.get(provider, "")

                resolved_nodes.append(
                    DependencyNode(
                        raw_requirement=raw_req,
                        resolved_package_name=provider,
                        version_constraint=constraint,
                        is_satisfied=is_sat,
                        is_cycle=False,
                        installed_version=inst_ver
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
            with RPM_GLOBAL_LOCK:
                match = None
                try:
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
                finally:
                    del match
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
            with RPM_GLOBAL_LOCK:
                for cap in capabilities:
                    match_name = None
                    try:
                        match_name = ts.dbMatch("name", cap)
                        if match_name.count() > 0:
                            batch_results.append((cap, True, cap))
                            continue
                    finally:
                        del match_name

                    matches = None
                    provider = None
                    try:
                        matches = ts.dbMatch("providename", cap)
                        for hdr in matches:
                            provider = _decode_rpm_str(hdr[rpm.RPMTAG_NAME])
                            break
                    finally:
                        del matches

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
                with RPM_GLOBAL_LOCK:
                    capabilities: List[str] = [self.target_package]
                    target_match = None
                    try:
                        target_match = ts.dbMatch("name", self.target_package)
                        for hdr in target_match:
                            raw_provs = hdr[rpm.RPMTAG_PROVIDENAME] or []
                            for p in raw_provs:
                                p_str = _decode_rpm_str(p)
                                if p_str and not p_str.startswith(("rpmlib(", "config(")):
                                    capabilities.append(p_str)
                            break
                    finally:
                        del target_match

                    for cap in set(capabilities):
                        if self._is_cancelled.is_set():
                            del ts
                            return

                        matches = None
                        try:
                            matches = ts.dbMatch("requirename", cap)
                            for hdr in matches:
                                if self._is_cancelled.is_set():
                                    del matches
                                    del ts
                                    return

                                pkg_name = _decode_rpm_str(hdr[rpm.RPMTAG_NAME])
                                if pkg_name and pkg_name != self.target_package and pkg_name not in seen:
                                    seen.add(pkg_name)
                                    reverse_nodes.append(
                                        DependencyNode(
                                            raw_requirement=cap,
                                            resolved_package_name=pkg_name,
                                            is_satisfied=True,
                                            is_reverse=True
                                        )
                                    )
                        finally:
                            del matches
                    del ts
            else:
                caps_cmd = get_host_command_prefix() + ["rpm", "-q", "--provides", self.target_package]
                caps_proc = subprocess.run(caps_cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=8)
                caps_to_check = [self.target_package]
                if caps_proc.returncode == 0:
                    for line in caps_proc.stdout.splitlines():
                        c_clean = line.split("=")[0].strip()
                        if c_clean and not c_clean.startswith(("rpmlib(", "config(")):
                            caps_to_check.append(c_clean)

                for cap in set(caps_to_check):
                    if self._is_cancelled.is_set():
                        return
                    cmd = get_host_command_prefix() + ["rpm", "-q", "--whatrequires", cap]
                    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=6)
                    if res.returncode == 0 and not self._is_cancelled.is_set():
                        for line in res.stdout.splitlines():
                            clean_line = line.strip()
                            if not clean_line or "no package requires" in clean_line.lower():
                                continue
                            pkg_base_name = re.sub(r'-[0-9].*$', '', clean_line)
                            if pkg_base_name not in seen and pkg_base_name != self.target_package:
                                seen.add(pkg_base_name)
                                reverse_nodes.append(
                                    DependencyNode(
                                        raw_requirement=cap,
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
                with RPM_GLOBAL_LOCK:
                    matches = None
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
                        del matches
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
# Worker: DNF Transaction History (Subprocess-Safe Engine)
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
# Worker: Transaction Simulation (Subprocess-Safe Engine)
# =============================================================================

class TransactionDryRunWorker(QRunnable):
    def __init__(
        self,
        to_install: List[str],
        to_remove: List[str],
        to_upgrade: Optional[List[str]] = None
    ):
        super().__init__()
        self.signals = BackendSignals()
        self.to_install = to_install
        self.to_remove = to_remove
        self.to_upgrade = to_upgrade or []
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        dnf_bin = get_dnf_binary_path()
        try:
            output = ""
            if self.to_upgrade:
                cmd_up = get_host_command_prefix() + [dnf_bin, "--assumeno", "upgrade"] + self.to_upgrade
                res_up = subprocess.run(cmd_up, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=25)
                output += res_up.stdout + res_up.stderr
            if self.to_remove:
                cmd_rm = get_host_command_prefix() + [dnf_bin, "--assumeno", "remove"] + self.to_remove
                res_rm = subprocess.run(cmd_rm, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=25)
                output += "\n" + res_rm.stdout + res_rm.stderr
            if self.to_install:
                cmd_in = get_host_command_prefix() + [dnf_bin, "--assumeno", "install"] + self.to_install
                res_in = subprocess.run(cmd_in, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=25)
                output += "\n" + res_in.stdout + res_in.stderr

            result = DryRunSimulationResult(raw_output=output)

            # Detect dangerous removal of protected system pillars across multiline tables
            protected_set = get_system_protected_packages()
            in_removing_section = False
            for line in output.splitlines():
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

            if not self._is_cancelled.is_set():
                self.signals.dry_run_finished.emit(result)

        except Exception as ex:
            self.signals.error_occurred.emit("", f"Dry-run simulation failed: {str(ex)}")


# =============================================================================
# Worker: Package Changelog Extractor
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
            with RPM_GLOBAL_LOCK:
                matches = None
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
                    del matches
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
# Worker: RPM Maintainer Scriptlets & Triggers (%pre, %post)
# =============================================================================

class PackageScriptletsWorker(QRunnable):
    def __init__(self, package_name: str):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name

    @pyqtSlot()
    def run(self):
        scripts = PackageScriptlets()
        ts = create_rpm_transaction_set()
        if ts is not None:
            with RPM_GLOBAL_LOCK:
                matches = None
                try:
                    matches = ts.dbMatch("name", self.package_name)
                    for hdr in matches:
                        scripts.prein = _decode_rpm_str(hdr[rpm.RPMTAG_PREIN] or "").strip()
                        scripts.postin = _decode_rpm_str(hdr[rpm.RPMTAG_POSTIN] or "").strip()
                        scripts.preun = _decode_rpm_str(hdr[rpm.RPMTAG_PREUN] or "").strip()
                        scripts.postun = _decode_rpm_str(hdr[rpm.RPMTAG_POSTUN] or "").strip()
                        break
                except Exception:
                    pass
                finally:
                    del matches
                    del ts

        if not scripts.has_any:
            try:
                cmd = get_host_command_prefix() + ["rpm", "-q", "--scripts", self.package_name]
                res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=6)
                if res.returncode == 0 and res.stdout.strip():
                    current_tag = None
                    buf: List[str] = []
                    for line in res.stdout.splitlines():
                        line_lower = line.strip().lower()
                        if line_lower.startswith("preinstall scriptlet"):
                            if current_tag:
                                setattr(scripts, current_tag, "\n".join(buf).strip())
                            current_tag, buf = "prein", []
                        elif line_lower.startswith("postinstall scriptlet"):
                            if current_tag:
                                setattr(scripts, current_tag, "\n".join(buf).strip())
                            current_tag, buf = "postin", []
                        elif line_lower.startswith("preuninstall scriptlet"):
                            if current_tag:
                                setattr(scripts, current_tag, "\n".join(buf).strip())
                            current_tag, buf = "preun", []
                        elif line_lower.startswith("postuninstall scriptlet"):
                            if current_tag:
                                setattr(scripts, current_tag, "\n".join(buf).strip())
                            current_tag, buf = "postun", []
                        else:
                            buf.append(line)
                    if current_tag and buf:
                        setattr(scripts, current_tag, "\n".join(buf).strip())
            except Exception:
                pass

        self.signals.package_scriptlets_loaded.emit(self.package_name, scripts)


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
    def __init__(self, force_refresh: bool = False):
        super().__init__()
        self.signals = BackendSignals()
        self.force_refresh = force_refresh
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        dnf_bin = get_dnf_binary_path()
        updates_map: Dict[str, AvailableUpdateInfo] = {}

        try:
            is_dnf5 = "dnf5" in dnf_bin
            cmd = get_host_command_prefix() + [dnf_bin, "check-upgrade" if is_dnf5 else "check-update"]

            # Force live sync with remote mirrors if requested by user
            if self.force_refresh:
                cmd.append("--refresh")

            if is_dnf5:
                cmd.append("--json")
            else:
                cmd.append("-q")

            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=45)

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
# Worker: On-Demand Remote Repository Package Search (DNF5)
# =============================================================================

class RemotePackageSearchWorker(QRunnable):
    """
    Asynchronously queries remote enabled DNF repositories for uninstalled software.
    Runs on-demand without memory bloat or startup latency.
    """

    def __init__(self, query: str, limit: int = 150):
        super().__init__()
        self.signals = BackendSignals()
        self.query = query.strip()
        self.limit = limit
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        dnf_bin = get_dnf_binary_path()
        if not dnf_bin or self._is_cancelled.is_set():
            self.signals.remote_search_finished.emit(self.query, [])
            return

        spec = self.query if ("*" in self.query or "?" in self.query) else f"*{self.query}*"
        # Query remote repositories using unit-separator (\x1f) delimiter
        qf = "%{name}\x1f%{version}\x1f%{release}\x1f%{arch}\x1f%{repo}\x1f%{size}\x1f%{summary}\n"
        cmd = get_host_command_prefix() + [dnf_bin, "repoquery", "--available", "-q", f"--queryformat={qf}", spec]

        try:
            self.signals.status_update.emit(f"Searching remote repositories for '{self.query}'...")
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=20)
            if self._is_cancelled.is_set():
                return

            results: List[PackageInfo] = []
            seen_names: Set[str] = set()

            if res.returncode == 0 and res.stdout.strip():
                appstream = AppStreamCatalog.get_instance()
                comps = FedoraCompsCatalog.get_instance()

                for line in res.stdout.splitlines():
                    if len(results) >= self.limit or self._is_cancelled.is_set():
                        break
                    parts = line.split("\x1f")
                    if len(parts) < 7:
                        continue

                    name, ver, rel, arch, repo, size_str, summary = parts[:7]
                    name_clean = name.strip()
                    if not name_clean or name_clean in seen_names:
                        continue
                    seen_names.add(name_clean)

                    try:
                        size_bytes = int(size_str.strip())
                    except ValueError:
                        size_bytes = 0

                    decision = ProductionTaxonomyEngine.classify(
                        name=name_clean,
                        summary=summary.strip(),
                        appstream=appstream,
                        comps=comps,
                    )
                    flags = decision.flags

                    results.append(
                        PackageInfo(
                            name=name_clean,
                            version=ver.strip(),
                            release=rel.strip(),
                            arch=arch.strip(),
                            summary=summary.strip(),
                            size_bytes=size_bytes,
                            state=PackageState.AVAILABLE,
                            repository=repo.strip() or "Repositories",
                            parent_pillar=decision.parent_pillar,
                            sub_category=decision.sub_category,
                            primary_category=decision.primary_category,
                            classification_confidence=1.0,
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
                            is_library=flags["is_library"],
                        )
                    )

            if not self._is_cancelled.is_set():
                self.signals.remote_search_finished.emit(self.query, results)

        except Exception as ex:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit("", f"Remote repository search failed: {str(ex)}")


# =============================================================================
# Helper: YUM / DNF Software Repository Manager (/etc/yum.repos.d)
# =============================================================================

class RepoManagerHelper:
    @staticmethod
    def get_system_repositories() -> List[RepoInfo]:
        repos: List[RepoInfo] = []
        # Support Fedora 45+ DNF5 relocated repository paths alongside classic paths
        repo_dirs = [
            "/etc/yum.repos.d",
            "/etc/dnf/repos.d",
            "/etc/distro.repos.d",
            "/usr/share/dnf5/repos.d",
            "/usr/share/dnf/repos.d",
        ]
        if is_running_in_flatpak():
            repo_dirs.extend([
                "/run/host/etc/yum.repos.d",
                "/run/host/etc/dnf/repos.d",
                "/run/host/usr/share/dnf5/repos.d",
            ])

        seen_repo_ids: Set[str] = set()

        for d in repo_dirs:
            if not os.path.isdir(d):
                continue
            try:
                for fname in sorted(os.listdir(d)):
                    if not fname.endswith(".repo"):
                        continue
                    fpath = os.path.join(d, fname)
                    config = configparser.ConfigParser(interpolation=None)
                    try:
                        config.read(fpath, encoding="utf-8")
                        for section in config.sections():
                            repo_id = section.strip()
                            if repo_id in seen_repo_ids:
                                continue
                            seen_repo_ids.add(repo_id)

                            name = config.get(section, "name", fallback=repo_id)
                            enabled_val = config.get(section, "enabled", fallback="0").strip().lower()
                            enabled = enabled_val in ("1", "true", "yes")
                            baseurl = config.get(section, "baseurl", fallback="")

                            id_lower = repo_id.lower()
                            fname_lower = fname.lower()
                            baseurl_lower = baseurl.lower()

                            is_copr = "copr" in id_lower or "copr" in fname_lower
                            is_rpmfusion = "rpmfusion" in id_lower or "rpmfusion" in fname_lower
                            
                            # Safely parse URL host to satisfy CodeQL py/incomplete-url-substring-sanitization
                            is_fedora_host = False
                            if baseurl:
                                try:
                                    parsed_host = urllib.parse.urlsplit(baseurl).hostname or ""
                                    parsed_host = parsed_host.lower()
                                    is_fedora_host = parsed_host == "fedoraproject.org" or parsed_host.endswith(".fedoraproject.org")
                                except Exception:
                                    is_fedora_host = False

                            # A repo is Fedora Project only if it originates from verified Fedora infrastructure
                            is_core = (
                                not is_copr
                                and not is_rpmfusion
                                and (
                                    "fedora" in id_lower
                                    or "fedora" in fname_lower
                                    or "rawhide" in id_lower
                                    or is_fedora_host
                                )
                            )

                            repos.append(
                                RepoInfo(
                                    id=repo_id,
                                    name=name,
                                    enabled=enabled,
                                    repo_file=fpath,
                                    is_copr=is_copr,
                                    is_rpmfusion=is_rpmfusion,
                                    is_core=is_core,
                                    baseurl=baseurl
                                )
                            )
                    except Exception:
                        continue
            except Exception:
                continue

        repos.sort(key=lambda r: (not r.is_core, not r.is_rpmfusion, not r.is_copr, r.id))
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
# Privileged Polkit Transaction Runner (Upgrades, Autoremove & Multi-Stage)
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

    def execute_transaction(
        self,
        to_install: List[str],
        to_remove: List[str],
        to_upgrade: Optional[List[str]] = None
    ):
        dnf_bin = get_dnf_binary_path()
        if to_upgrade is None:
            to_upgrade = []

        stages: List[List[str]] = []
        if to_upgrade:
            stages.append([dnf_bin, "-y", "upgrade", "--"] + to_upgrade)
        if to_install:
            stages.append([dnf_bin, "-y", "install", "--"] + to_install)
        if to_remove:
            stages.append([dnf_bin, "-y", "remove", "--"] + to_remove)

        if not stages:
            return

        self._queue_stages = stages
        self._run_next_stage()

    def execute_system_upgrade(self):
        """Executes full system upgrade (dnf5 -y upgrade) with Polkit authentication."""
        dnf_bin = get_dnf_binary_path()
        self._start_process([dnf_bin, "-y", "upgrade"])

    def execute_autoremove(self):
        """Executes unneeded leaf dependencies cleanup (dnf5 -y autoremove)."""
        dnf_bin = get_dnf_binary_path()
        self._start_process([dnf_bin, "-y", "autoremove"])

    def execute_install_local_rpm(self, rpm_path: str):
        """Installs a local .rpm package file with automatic repository dependency resolution."""
        dnf_bin = get_dnf_binary_path()
        self._start_process([dnf_bin, "-y", "install", rpm_path])

    def execute_clean_cache(self):
        """Cleans all downloaded package archives and expired metadata (dnf clean all)."""
        dnf_bin = get_dnf_binary_path()
        self._start_process([dnf_bin, "-y", "clean", "all"])

    def execute_custom_command(self, custom_dnf_args: List[str]):
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
            self.log_received.emit("[ROOT] Running with direct root privileges (bypassing Polkit elevation)...\n")
            self.log_received.emit(f"Executing: {program} {' '.join(full_args)}\n\n")
        else:
            program = prefix[0] if prefix else "pkexec"
            full_args: List[str] = prefix[1:] + ["pkexec"] if prefix else []
            full_args.extend(dnf_args)
            self.log_received.emit("[AUTH] Requesting administrative authorization...\n")
            self.log_received.emit(f"Executing: {program} {' '.join(full_args)}\n\n")

        self.process.start(program, full_args)

    def cancel_transaction(self):
        self._queue_stages.clear()
        if self.process and self.process.state() == QProcess.ProcessState.Running:
            self.log_received.emit("\n[CANCEL] Sending SIGINT to transaction (preserving RPM lock)...\n")
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
