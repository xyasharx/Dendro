# dendro/core/backend.py
from __future__ import annotations

import glob
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Dict, Final, List, Optional, Set, Tuple

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QRunnable, pyqtSignal, pyqtSlot

try:
    import rpm  # type: ignore[import-untyped]
    HAS_NATIVE_RPM: Final[bool] = True
except ImportError:
    HAS_NATIVE_RPM = False


# =============================================================================
# محیط و ابزارهای سیستم
# =============================================================================

def is_running_in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def get_host_command_prefix() -> List[str]:
    if is_running_in_flatpak() and shutil.which("flatpak-spawn"):
        return ["flatpak-spawn", "--host"]
    return []


def get_clean_env() -> Dict[str, str]:
    env = os.environ.copy()
    for var in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(var, None)
    return env


def get_dnf_binary_path() -> str:
    prefix = get_host_command_prefix()
    if prefix:
        return "/usr/bin/dnf5" if os.path.exists("/run/host/usr/bin/dnf5") else "/usr/bin/dnf"
    return shutil.which("dnf5") or shutil.which("dnf") or "/usr/bin/dnf"


# =============================================================================
# تعاریف ثابت هسته فدورا و ابزارهای خط فرمان
# =============================================================================

FEDORA_SYSTEM_ROOT_PILLARS: Final[Set[str]] = {
    "kernel", "kernel-core", "kernel-modules", "gnome-shell", "plasma-desktop",
    "systemd", "systemd-udev", "pipewire", "wireplumber", "NetworkManager",
    "firewalld", "gdm", "sddm", "mesa-dri-drivers", "mesa-vulkan-drivers",
    "grub2-common", "grub2-efi-x64", "dracut", "polkit", "dnf5", "dnf",
    "flatpak", "udisks2", "upower", "bluez", "cups", "mutter", "kwin",
    "xorg-x11-server-Xorg", "selinux-policy", "btrfs-progs", "chrony",
    "coreutils", "bash", "sudo", "shadow-utils", "util-linux", "glibc"
}

KNOWN_CLI_USER_TOOLS: Final[Set[str]] = {
    "neovim", "vim", "htop", "btop", "tmux", "zsh", "fish", "git",
    "curl", "wget", "ripgrep", "fd-find", "fzf", "tree", "fastfetch",
    "neofetch", "nmap", "ffmpeg", "rsync", "jq", "micro", "bat", "eza", "lazygit"
}


# =============================================================================
# ساختارهای داده‌ای اصلی (Data Models)
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
    is_satisfied: bool = False
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

    # پرچم‌های دسته‌بندی هوشمند
    is_orphan: bool = False
    is_user_app: bool = False
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

    # متادیتای مخزن
    repository: str = "System RPM DB"

    # وابستگی‌ها و فایل‌ها
    dependencies_loaded: bool = False
    dependencies: List[DependencyNode] = field(default_factory=list)
    reverse_dependencies: List[DependencyNode] = field(default_factory=list)
    files: List[PackageFileInfo] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    changelog: List[str] = field(default_factory=list)

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
# موتور کش محلی پایگاه داده SQLite
# =============================================================================

class SQLiteCapabilityCache:
    """کش محلی سریع بر بستر SQLite برای رفع آنی نام بسته‌های ارائه‌دهنده قابلیت‌ها"""
    _instance: Optional[SQLiteCapabilityCache] = None
    _lock = threading.Lock()

    def __init__(self):
        cache_dir = os.path.expanduser("~/.cache/dendro")
        os.makedirs(cache_dir, exist_ok=True)
        self.db_path = os.path.join(cache_dir, "capabilities_v2.db")
        self._init_db()

    @classmethod
    def get_instance(cls) -> SQLiteCapabilityCache:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS capabilities (
                    cap_name TEXT PRIMARY KEY,
                    is_satisfied INTEGER,
                    provider_name TEXT
                )
            """)
            conn.commit()

    def get(self, cap_name: str) -> Optional[Tuple[bool, str]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT is_satisfied, provider_name FROM capabilities WHERE cap_name = ?", (cap_name,))
                row = cur.fetchone()
                if row:
                    return bool(row[0]), str(row[1])
        except Exception:
            pass
        return None

    def set_batch(self, items: List[Tuple[str, bool, str]]):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO capabilities (cap_name, is_satisfied, provider_name) VALUES (?, ?, ?)",
                    [(name, int(sat), prov) for name, sat, prov in items]
                )
                conn.commit()
        except Exception:
            pass


# =============================================================================
# موتور طبقه‌بندی هوشمند بسته‌ها (Smart Classifier)
# =============================================================================

def classify_package(
    name: str,
    summary: str,
    group: str,
    installed_desktop_pkgs: Set[str],
    vendor: str = "",
    packager: str = ""
) -> Dict[str, bool]:
    name_lower = name.lower()
    sum_lower = summary.lower()

    # ۱. فریم‌ورک‌ها و زبان‌های برنامه‌نویسی
    is_python_pkg = name_lower.startswith(("python3-", "python-", "pytest-")) or "python" in sum_lower
    is_rust_pkg = name_lower.startswith(("rust-", "cargo-")) or "rust crate" in sum_lower
    is_jvm_pkg = name_lower.startswith(("java-", "openjdk-", "maven-", "scala-")) or "java" in sum_lower
    is_nodejs_pkg = name_lower.startswith(("nodejs-", "npm-", "yarn-")) or "node.js" in sum_lower

    # ۲. هسته و درایورها
    is_kernel_module = (
        name_lower.startswith(("kernel-", "kmod-", "akmod-", "dkms-", "nvidia-")) or
        name_lower in ("kernel", "kernel-core", "kernel-modules", "kernel-devel", "akmods", "dkms") or
        "kernel module" in sum_lower
    )

    # ۳. فریم‌ورک‌ها و فرم‌ورها
    is_firmware = (
        any(kw in name_lower for kw in ("firmware", "microcode", "ucode")) or
        any(kw in sum_lower for kw in ("firmware", "microcode", "hardware support"))
    )

    # ۴. فونت‌ها
    is_font = (
        any(name_lower.startswith(pfx) for pfx in ("font-", "google-noto-", "dejavu-", "fonts-", "gnu-free-", "urw-base35-")) or
        any(name_lower.endswith(sfx) for sfx in ("-fonts", "-font", "-fonts-all")) or
        "font" in name_lower or "font" in sum_lower
    )

    # ۵. زبان‌ها و لوکال‌ها
    is_locale = (
        name_lower.startswith(("glibc-langpack-", "langpacks-", "ibus-")) or
        name_lower.endswith(("-langpack", "-langpacks", "-i18n", "-l10n", "-doc-locale")) or
        "language pack" in sum_lower or "translation" in sum_lower or "locale" in sum_lower
    )

    # ۶. پکیج‌های توسعه و هدرها
    is_devel = (
        name_lower.endswith(("-devel", "-static", "-debuginfo", "-debugsource")) or
        "development files" in sum_lower or "header files" in sum_lower or "development libraries" in sum_lower
    )

    # ۷. تم و آیکون
    is_theme = (
        any(kw in name_lower for kw in ("-theme", "-icon-theme", "-backgrounds", "-wallpapers", "sound-theme-")) or
        "icon theme" in sum_lower or "desktop theme" in sum_lower or "wallpapers" in sum_lower
    )

    # ۸. سرویس‌های Systemd
    is_systemd_service = (
        any(kw in name_lower for kw in ("-daemon", "server", "service", "systemd-")) or
        any(kw in sum_lower for kw in ("daemon", "service", "systemd unit", "server process"))
    )

    # ۹. امنیت و SELinux
    is_security_pkg = (
        any(kw in name_lower for kw in ("selinux", "crypto", "auth", "pam-", "polkit", "shadow-utils", "gnupg", "openssl", "audit")) or
        "selinux" in sum_lower or "cryptographic" in sum_lower or "authentication" in sum_lower
    )

    # ۱۰. کتابخانه‌های C/C++
    is_c_lib = False
    if not any([is_font, is_firmware, is_locale, is_devel, is_theme, is_python_pkg, is_rust_pkg, is_jvm_pkg, is_nodejs_pkg]):
        lib_suffixes = ("-libs", "-common", "-data", "-help", "-filesystem")
        if any(name_lower.endswith(sfx) for sfx in lib_suffixes):
            is_c_lib = True
        elif name_lower.startswith("lib") and name_lower not in (
            "libreoffice", "libtree", "libvirt", "libguestfs-tools", "libcamera-tools"
        ):
            is_c_lib = True
        elif "shared library" in sum_lower or "libraries for" in sum_lower:
            is_c_lib = True

    is_general_lib = (
        is_c_lib or is_font or is_firmware or is_locale or is_devel or
        is_theme or is_python_pkg or is_rust_pkg or is_jvm_pkg or is_nodejs_pkg
    )

    has_desktop = (name in installed_desktop_pkgs or name_lower in installed_desktop_pkgs)
    is_cli_tool = name_lower in KNOWN_CLI_USER_TOOLS
    is_user_app = (has_desktop or is_cli_tool) and not is_general_lib

    is_fedora_core = (name in FEDORA_SYSTEM_ROOT_PILLARS or name_lower in FEDORA_SYSTEM_ROOT_PILLARS)
    if not is_fedora_core and not is_general_lib and not is_user_app:
        if any(name_lower.startswith(pfx) for pfx in ("systemd-", "kernel-", "gnome-", "plasma-", "pipewire-")):
            is_fedora_core = True

    return {
        "is_user_app": is_user_app,
        "is_cli_tool": is_cli_tool,
        "is_fedora_core": is_fedora_core,
        "is_c_lib": is_c_lib,
        "is_python_pkg": is_python_pkg,
        "is_rust_pkg": is_rust_pkg,
        "is_jvm_pkg": is_jvm_pkg,
        "is_nodejs_pkg": is_nodejs_pkg,
        "is_kernel_module": is_kernel_module,
        "is_systemd_service": is_systemd_service,
        "is_security_pkg": is_security_pkg,
        "is_firmware": is_firmware,
        "is_font": is_font,
        "is_locale": is_locale,
        "is_devel": is_devel,
        "is_theme": is_theme,
        "is_library": is_general_lib
    }


# =============================================================================
# سیگنال‌های بک‌اند
# =============================================================================

class BackendSignals(QObject):
    packages_loaded = pyqtSignal(list)
    orphans_loaded = pyqtSignal(set)
    userinstalled_loaded = pyqtSignal(set)
    dependencies_resolved = pyqtSignal(str, list)
    reverse_dependencies_resolved = pyqtSignal(str, list)
    package_files_loaded = pyqtSignal(str, list)
    package_details_loaded = pyqtSignal(object)
    history_loaded = pyqtSignal(list)
    dry_run_finished = pyqtSignal(object)
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)


# =============================================================================
# ورکر استخراج پکیج‌های سیستم
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

    def _get_installed_desktop_apps(self) -> Set[str]:
        app_names: Set[str] = set()
        search_dirs = [
            "/usr/share/applications/*.desktop",
            "/usr/local/share/applications/*.desktop",
            os.path.expanduser("~/.local/share/applications/*.desktop"),
            "/var/lib/flatpak/exports/share/applications/*.desktop",
        ]
        if is_running_in_flatpak():
            search_dirs.extend([
                "/run/host/usr/share/applications/*.desktop",
                "/run/host/usr/local/share/applications/*.desktop"
            ])

        for pattern in search_dirs:
            for df in glob.glob(pattern):
                base = os.path.splitext(os.path.basename(df))[0].lower()
                for chunk in base.split("."):
                    if chunk and len(chunk) > 2:
                        app_names.add(chunk)
                app_names.add(base)
        return app_names

    @pyqtSlot()
    def run(self):
        try:
            self.signals.status_update.emit("Reading system RPM database...")
            desktop_apps = self._get_installed_desktop_apps()

            if HAS_NATIVE_RPM and not is_running_in_flatpak():
                packages = self._query_native_librpm(desktop_apps)
            else:
                packages = self._query_cli_subprocess(desktop_apps)

            if self._is_cancelled.is_set():
                return

            packages.sort(key=lambda p: p.name.lower())
            self.signals.packages_loaded.emit(packages)
            self.signals.status_update.emit(f"Loaded {len(packages)} packages.")

        except Exception as ex:
            self.signals.error_occurred.emit("", f"Failed to query database: {str(ex)}")

    def _query_native_librpm(self, desktop_apps: Set[str]) -> List[PackageInfo]:
        packages: List[PackageInfo] = []
        ts = rpm.TransactionSet()
        match_iterator = ts.dbMatch()

        for header in match_iterator:
            if self._is_cancelled.is_set():
                del ts
                return []

            def dec(val):
                if val is None:
                    return ""
                return val.decode("utf-8", errors="replace") if isinstance(val, bytes) else str(val)

            name = dec(header[rpm.RPMTAG_NAME])
            if not name:
                continue

            ver = dec(header[rpm.RPMTAG_VERSION])
            rel = dec(header[rpm.RPMTAG_RELEASE])
            arch = dec(header[rpm.RPMTAG_ARCH])
            group = dec(header[rpm.RPMTAG_GROUP]) or "General"
            summary = dec(header[rpm.RPMTAG_SUMMARY])
            description = dec(header[rpm.RPMTAG_DESCRIPTION])
            license_str = dec(header[rpm.RPMTAG_LICENSE])
            url = dec(header[rpm.RPMTAG_URL])
            packager = dec(header[rpm.RPMTAG_PACKAGER])
            vendor = dec(header[rpm.RPMTAG_VENDOR])

            b_time_raw = header[rpm.RPMTAG_BUILDTIME]
            build_time = datetime.fromtimestamp(b_time_raw).strftime('%Y-%m-%d %H:%M') if b_time_raw else ""

            i_time_raw = header[rpm.RPMTAG_INSTALLTIME]
            install_time = datetime.fromtimestamp(i_time_raw).strftime('%Y-%m-%d %H:%M') if i_time_raw else ""

            size_bytes = int(header[rpm.RPMTAG_SIZE] or 0)

            repo = "Fedora Project"
            if "copr" in packager.lower() or "copr" in vendor.lower():
                repo = "COPR Repository"
            elif "rpmfusion" in packager.lower() or "rpmfusion" in vendor.lower():
                repo = "RPM Fusion"
            elif vendor:
                repo = vendor

            flags = classify_package(name, summary, group, desktop_apps, vendor, packager)

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
                    is_user_app=flags["is_user_app"],
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

        del ts
        return packages

    def _query_cli_subprocess(self, desktop_apps: Set[str]) -> List[PackageInfo]:
        query_format = "%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}|%{GROUP}|%{SIZE}|%{LICENSE}|%{URL}|%{PACKAGER}|%{VENDOR}|%{INSTALLTIME:date}|%{SUMMARY}\n"
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
            if "copr" in packager.lower() or "copr" in vendor.lower():
                repo = "COPR Repository"
            elif "rpmfusion" in packager.lower() or "rpmfusion" in vendor.lower():
                repo = "RPM Fusion"
            elif vendor:
                repo = vendor

            flags = classify_package(name, summary, group, desktop_apps, vendor, packager)

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
                    is_user_app=flags["is_user_app"],
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
# ورکر پکیج‌های نصب‌شده توسط کاربر (User-Installed)
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
# ورکر پکیج‌های بی‌استفاده (Orphans)
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
            cmd = get_host_command_prefix() + [dnf_bin, "repoquery", "--unneeded", "-q", "--queryformat", "%{name}"]
            res = subprocess.run(cmd, capture_output=True, text=True, env=get_clean_env(), timeout=35)
            if res.returncode == 0 and not self._is_cancelled.is_set():
                orphans = {line.strip() for line in res.stdout.splitlines() if line.strip()}
                self.signals.orphans_loaded.emit(orphans)
        except Exception:
            pass


# =============================================================================
# ورکر درخت مستقیم وابستگی‌ها (Forward Dependency Tree)
# =============================================================================

class DependencyTreeWorker(QRunnable):
    def __init__(self, root_package: str, max_depth: int = 3):
        super().__init__()
        self.signals = BackendSignals()
        self.root_package = root_package
        self.max_depth = max_depth
        self._is_cancelled = threading.Event()
        self.cache = SQLiteCapabilityCache.get_instance()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        ts = None
        if HAS_NATIVE_RPM and not is_running_in_flatpak():
            try:
                ts = rpm.TransactionSet()
            except Exception:
                ts = None

        try:
            self.signals.status_update.emit(f"Resolving dependency graph for '{self.root_package}'...")
            visited_path: Set[str] = {self.root_package}
            deps = self._resolve_recursive(self.root_package, depth=1, visited=visited_path, ts=ts)

            if not self._is_cancelled.is_set():
                self.signals.dependencies_resolved.emit(self.root_package, deps)
                self.signals.status_update.emit(
                    f"Resolved {len(deps)} dependencies for '{self.root_package}'."
                )
        except Exception as ex:
            self.signals.error_occurred.emit(self.root_package, f"Dependency error: {str(ex)}")
        finally:
            if ts is not None:
                del ts

    def _resolve_recursive(self, pkg_name: str, depth: int, visited: Set[str], ts: Optional[object]) -> List[DependencyNode]:
        if depth > self.max_depth or self._is_cancelled.is_set():
            return []

        resolved_nodes: List[DependencyNode] = []

        try:
            raw_reqs, parsed_reqs = self._fetch_package_requires(pkg_name, ts)
            if not parsed_reqs:
                return []

            caps_to_query: List[str] = []
            for _, cap_name, _ in parsed_reqs:
                cached = self.cache.get(cap_name)
                if cached is None:
                    caps_to_query.append(cap_name)

            if caps_to_query:
                self._resolve_capabilities_batch(caps_to_query, ts)

            seen_clean_names: Set[str] = set()

            for raw_req, cap_name, constraint in parsed_reqs:
                if self._is_cancelled.is_set():
                    return []

                cached = self.cache.get(cap_name)
                is_satisfied, provider_name = cached if cached else (False, cap_name)

                if provider_name in seen_clean_names:
                    continue
                seen_clean_names.add(provider_name)

                is_cycle = provider_name in visited

                node = DependencyNode(
                    raw_requirement=raw_req,
                    resolved_package_name=provider_name,
                    version_constraint=constraint,
                    is_satisfied=is_satisfied,
                    is_cycle=is_cycle
                )

                if not is_cycle and is_satisfied and depth < self.max_depth:
                    next_visited = set(visited)
                    next_visited.add(provider_name)
                    sub_deps = self._resolve_recursive(provider_name, depth=depth + 1, visited=next_visited, ts=ts)
                    node.sub_dependencies = sub_deps

                resolved_nodes.append(node)

        except Exception:
            pass

        return resolved_nodes

    def _fetch_package_requires(self, pkg_name: str, ts: Optional[object]) -> Tuple[List[str], List[Tuple[str, str, str]]]:
        raw_reqs: List[str] = []
        parsed_reqs: List[Tuple[str, str, str]] = []

        if ts is not None:
            match = ts.dbMatch("name", pkg_name)  # type: ignore[attr-defined]
            for hdr in match:
                requires = hdr[rpm.RPMTAG_REQUIRENAME] or []
                for req in requires:
                    req_str = req.decode("utf-8", errors="replace") if isinstance(req, bytes) else str(req)
                    raw_reqs.append(req_str)
                break
        else:
            cmd = get_host_command_prefix() + ["rpm", "-qR", pkg_name]
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=6)
            if proc.returncode == 0:
                raw_reqs = proc.stdout.splitlines()

        for req in raw_reqs:
            req = req.strip()
            if not req or req.startswith(("rpmlib(", "config(", "/")):
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
                matches = ts.dbMatch("provides", cap)  # type: ignore[attr-defined]
                provider = None
                for hdr in matches:
                    name = hdr[rpm.RPMTAG_NAME]
                    provider = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
                    break
                if provider:
                    batch_results.append((cap, True, provider))
                else:
                    batch_results.append((cap, False, cap))
        else:
            batch_cmd = get_host_command_prefix() + ["rpm", "-q", "--whatprovides", "--queryformat", "%{NAME}\n"] + capabilities
            batch_proc = subprocess.run(batch_cmd, capture_output=True, text=True, env=get_clean_env(), timeout=8)
            providers = batch_proc.stdout.splitlines()

            for i, cap in enumerate(capabilities):
                if i < len(providers) and "no package provides" not in providers[i]:
                    batch_results.append((cap, True, providers[i].strip()))
                else:
                    batch_results.append((cap, False, cap))

        self.cache.set_batch(batch_results)


# =============================================================================
# ورکر درخت معکوس وابستگی‌ها (Reverse Dependency Explorer / "What Requires This?")
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

            cmd = get_host_command_prefix() + ["rpm", "-q", "--whatrequires", self.target_package]
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=12)

            reverse_nodes: List[DependencyNode] = []
            if res.returncode == 0 and not self._is_cancelled.is_set():
                lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
                for line in lines:
                    if "no package requires" in line.lower():
                        continue

                    pkg_base_name = re.sub(r'-[0-9].*$', '', line)
                    reverse_nodes.append(
                        DependencyNode(
                            raw_requirement=self.target_package,
                            resolved_package_name=pkg_base_name or line,
                            is_satisfied=True,
                            is_reverse=True
                        )
                    )

            self.signals.reverse_dependencies_resolved.emit(self.target_package, reverse_nodes)
            self.signals.status_update.emit(f"Found {len(reverse_nodes)} dependents for '{self.target_package}'.")
        except Exception as ex:
            self.signals.error_occurred.emit(self.target_package, f"Reverse dependency error: {str(ex)}")


# =============================================================================
# ورکر استخراج فایل‌های بسته (Package File Inspector)
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
            cmd = get_host_command_prefix() + ["rpm", "-ql", "--dump", self.package_name]
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=10)

            files: List[PackageFileInfo] = []
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

            self.signals.package_files_loaded.emit(self.package_name, files)
        except Exception as ex:
            self.signals.error_occurred.emit(self.package_name, f"File query error: {str(ex)}")


# =============================================================================
# ورکر دریافت تاریخچه تراکنش‌ها (DNF History Explorer)
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

            self.signals.history_loaded.emit(history_list)
        except Exception as ex:
            self.signals.error_occurred.emit("", f"History error: {str(ex)}")


# =============================================================================
# ورکر شبیه‌ساز تراکنش (Dry-Run Simulation)
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

            self.signals.dry_run_finished.emit(result)
        except Exception as ex:
            self.signals.error_occurred.emit("", f"Dry-run simulation failed: {str(ex)}")


# =============================================================================
# مجری تراکنش‌های Polkit
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
        dnf_bin = get_dnf_binary_path()
        args: List[str] = [dnf_bin, "-y"]
        if to_install:
            args.extend(["install", "--"] + to_install)
        if to_remove:
            args.extend(["remove", "--"] + to_remove)

        self._start_process(args)

    def execute_custom_command(self, custom_dnf_args: List[str]):
        """اجرای مستقیم دستورات خاص مانند dnf history undo یا dnf autoremove"""
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
        program = prefix[0] if prefix else "pkexec"
        full_args: List[str] = prefix[1:] + ["pkexec"] if prefix else []
        full_args.extend(dnf_args)

        self.log_received.emit("🔒 Requesting administrative authorization...\n")
        self.log_received.emit(f"Executing: {program} {' '.join(full_args)}\n\n")

        self.process.start(program, full_args)

    def cancel_transaction(self):
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
