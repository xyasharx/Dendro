# dendro/core/backend.py
from __future__ import annotations

import configparser
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
    "systemd", "systemd-udev", "systemd-libs", "pipewire", "wireplumber", "NetworkManager",
    "firewalld", "gdm", "sddm", "mesa-dri-drivers", "mesa-vulkan-drivers",
    "grub2-common", "grub2-efi-x64", "dracut", "polkit", "dnf5", "dnf",
    "flatpak", "udisks2", "upower", "bluez", "cups", "mutter", "kwin",
    "xorg-x11-server-Xorg", "selinux-policy", "btrfs-progs", "chrony",
    "coreutils", "bash", "sudo", "shadow-utils", "util-linux", "glibc"
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

    # پرچم‌های دسته‌بندی تفکیک‌شده و دقیق
    is_orphan: bool = False
    is_desktop_app: bool = False      # فقط برنامه‌های گرافیکی دسکتاپ
    is_cli_tool: bool = False         # فقط ابزارهای مستقل ترمینال
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
    repository: str = "Fedora Project"

    # وابستگی‌ها و فایل‌ها
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
# موتور کش دومرحله‌ای فوق سریع (L1 RAM + L2 SQLite)
# =============================================================================

class SQLiteCapabilityCache:
    """کش هیبریدی به شدت بهینه‌سازی شده با حافظه موقت رم و پایگاه داده پایدار"""
    _instance: Optional[SQLiteCapabilityCache] = None
    _lock = threading.Lock()

    def __init__(self):
        cache_dir = os.path.expanduser("~/.cache/dendro")
        os.makedirs(cache_dir, exist_ok=True)
        self.db_path = os.path.join(cache_dir, "capabilities_v3.db")
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
        # بررسی سریع L1 در RAM
        if cap_name in self._memory_cache:
            return self._memory_cache[cap_name]

        # بررسی L2 در SQLite
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
# موتور دقیق شناسایی فایل‌های Desktop
# =============================================================================

def parse_installed_desktop_applications() -> Tuple[Set[str], Set[str]]:
    """
    اسکن عمیق و استاندارد فایل‌های دسکتاپ.
    خروجی: (مجموعه نام برنامه‌های گرافیکی دسکتاپ, مجموعه برنامه‌های خط فرمان دارای دسکتاپ)
    """
    desktop_apps: Set[str] = set()
    cli_desktop_apps: Set[str] = set()

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

    parser = configparser.ConfigParser(interpolation=None)

    for directory in search_dirs:
        if not os.path.isdir(directory):
            continue
        for root, _, files in os.walk(directory):
            for file in files:
                if not file.endswith(".desktop"):
                    continue

                full_path = os.path.join(root, file)
                try:
                    parser.read(full_path, encoding="utf-8")
                    if not parser.has_section("Desktop Entry"):
                        continue

                    # فیلتر فایل‌های مخفی یا سیستمی
                    if parser.has_option("Desktop Entry", "NoDisplay"):
                        if parser.get("Desktop Entry", "NoDisplay").lower() == "true":
                            continue

                    if parser.has_option("Desktop Entry", "Type"):
                        if parser.get("Desktop Entry", "Type") != "Application":
                            continue

                    is_terminal = False
                    if parser.has_option("Desktop Entry", "Terminal"):
                        is_terminal = parser.get("Desktop Entry", "Terminal").lower() == "true"

                    # استخراج نام اجرایی اصلی از فیلد Exec
                    exec_cmd = ""
                    if parser.has_option("Desktop Entry", "Exec"):
                        exec_raw = parser.get("Desktop Entry", "Exec")
                        exec_cmd = exec_raw.split()[0].strip('"\'')
                        exec_cmd = os.path.basename(exec_cmd).lower()

                    # استخراج نام بسته از اسم فایل (مثلا org.mozilla.firefox -> firefox)
                    file_base = os.path.splitext(file)[0].lower()
                    last_token = file_base.split(".")[-1]

                    target_set = cli_desktop_apps if is_terminal else desktop_apps

                    if exec_cmd and len(exec_cmd) > 1:
                        target_set.add(exec_cmd)
                    if last_token and len(last_token) > 2:
                        target_set.add(last_token)
                    target_set.add(file_base)

                except Exception:
                    continue

    return desktop_apps, cli_desktop_apps


# =============================================================================
# موتور طبقه‌بندی هوشمند و ایزوله بسته‌ها (Smart Classifier Engine)
# =============================================================================

def classify_package(
    name: str,
    summary: str,
    group: str,
    desktop_apps: Set[str],
    cli_desktop_apps: Set[str],
    vendor: str = "",
    packager: str = ""
) -> Dict[str, bool]:
    name_lower = name.lower()
    sum_lower = summary.lower()

    # ۱. فریم‌ورک‌ها و پکیج‌های توسعه زبان‌ها
    is_python_pkg = name_lower.startswith(("python3-", "python-", "pytest-")) or "python 3" in sum_lower or "python module" in sum_lower
    is_rust_pkg = name_lower.startswith(("rust-", "cargo-", "rust-lib")) or "rust crate" in sum_lower
    is_jvm_pkg = name_lower.startswith(("java-", "openjdk-", "maven-", "scala-", "apache-commons-")) or "java runtime" in sum_lower or "java class" in sum_lower
    is_nodejs_pkg = name_lower.startswith(("nodejs-", "npm-", "yarn-")) or "node.js package" in sum_lower

    # ۲. هسته و درایورها
    is_kernel_module = (
        name_lower.startswith(("kernel-", "kmod-", "akmod-", "dkms-", "nvidia-kmod")) or
        name_lower in ("kernel", "kernel-core", "kernel-modules", "kernel-devel", "akmods", "dkms") or
        "kernel module" in sum_lower or "linux kernel" in sum_lower
    )

    # ۳. فریم‌ورها و میکروکدها
    is_firmware = (
        any(kw in name_lower for kw in ("firmware", "microcode", "ucode", "alsa-firmware", "iwl", "linux-firmware")) or
        any(kw in sum_lower for kw in ("firmware", "microcode", "hardware support"))
    )

    # ۴. فونت‌ها
    is_font = (
        any(name_lower.startswith(pfx) for pfx in ("font-", "google-noto-", "dejavu-", "fonts-", "gnu-free-", "urw-base35-", "liberation-")) or
        any(name_lower.endswith(sfx) for sfx in ("-fonts", "-font", "-fonts-all")) or
        "font " in sum_lower or sum_lower.endswith(" fonts") or sum_lower.endswith(" font")
    )

    # ۵. زبان‌ها و لوکال‌ها
    is_locale = (
        name_lower.startswith(("glibc-langpack-", "langpacks-", "ibus-", "man-pages-")) or
        name_lower.endswith(("-langpack", "-langpacks", "-i18n", "-l10n", "-doc-locale")) or
        "language pack" in sum_lower or "translation" in sum_lower or "locale data" in sum_lower
    )

    # ۶. پکیج‌های توسعه و کتابخانه‌های هدر
    is_devel = (
        name_lower.endswith(("-devel", "-static", "-debuginfo", "-debugsource")) or
        "development files" in sum_lower or "header files" in sum_lower or "development libraries" in sum_lower
    )

    # ۷. تم و آیکون
    is_theme = (
        any(kw in name_lower for kw in ("-theme", "-icon-theme", "-backgrounds", "-wallpapers", "sound-theme-", "cursor-theme")) or
        "icon theme" in sum_lower or "desktop theme" in sum_lower or "wallpapers" in sum_lower or "sound theme" in sum_lower
    )

    # ۸. سرویس‌های Systemd و پس‌زمینه‌ای
    is_systemd_service = (
        any(kw in name_lower for kw in ("-daemon", "server", "systemd-", "dbus-daemon")) or
        any(kw in sum_lower for kw in ("daemon", "service unit", "systemd service", "background daemon"))
    )

    # ۹. امنیت، احراز هویت و SELinux
    is_security_pkg = (
        any(kw in name_lower for kw in ("selinux", "crypto", "auth", "pam-", "polkit", "shadow-utils", "gnupg", "openssl", "audit", "firewalld", "iptables")) or
        "selinux" in sum_lower or "cryptographic" in sum_lower or "authentication" in sum_lower
    )

    # ۱۰. ستون‌های اصلی و حیاتی فدورا
    is_fedora_core = (name in FEDORA_SYSTEM_ROOT_PILLARS or name_lower in FEDORA_SYSTEM_ROOT_PILLARS)
    if not is_fedora_core:
        if any(name_lower.startswith(pfx) for pfx in ("systemd-", "gnome-shell", "plasma-desktop", "pipewire-", "glibc-")):
            is_fedora_core = True

    # ۱۱. کتابخانه‌های C/C++ و فایلی
    is_c_lib = False
    if not any([is_font, is_firmware, is_locale, is_devel, is_theme, is_python_pkg, is_rust_pkg, is_jvm_pkg, is_nodejs_pkg, is_fedora_core]):
        lib_suffixes = ("-libs", "-common", "-data", "-help", "-filesystem", "-compat")
        if any(name_lower.endswith(sfx) for sfx in lib_suffixes):
            is_c_lib = True
        elif name_lower.startswith("lib") and name_lower not in (
            "libreoffice", "libtree", "libvirt", "libguestfs-tools", "libcamera-tools", "librewolf"
        ):
            is_c_lib = True
        elif "shared library" in sum_lower or "libraries for" in sum_lower or "c library" in sum_lower:
            is_c_lib = True

    is_general_lib = (
        is_c_lib or is_font or is_firmware or is_locale or is_devel or
        is_theme or is_python_pkg or is_rust_pkg or is_jvm_pkg or is_nodejs_pkg
    )

    # ۱۲. تفکیک دقیق برنامه‌های کاربر (کامپیوتر رومیزی و CLI)
    has_desktop_file = (name in desktop_apps or name_lower in desktop_apps)
    is_cli_exclusive = (name in cli_desktop_apps or name_lower in cli_desktop_apps or name_lower in KNOWN_CLI_USER_TOOLS)

    # برنامه‌های دسکتاپ فقط و فقط در صورتی true می‌شوند که فایل گرافیکی معتبر داشته و کتابخانه/هسته نباشند
    is_desktop_app = has_desktop_file and not is_general_lib and not is_cli_exclusive and not is_fedora_core

    # ابزارهای CLI فقط برای ابزارهای خط فرمان مستقل
    is_cli_tool = is_cli_exclusive and not is_desktop_app and not is_general_lib

    return {
        "is_desktop_app": is_desktop_app,
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

    @pyqtSlot()
    def run(self):
        try:
            self.signals.status_update.emit("Scanning system applications & RPM database...")
            desktop_apps, cli_apps = parse_installed_desktop_applications()

            if HAS_NATIVE_RPM and not is_running_in_flatpak():
                packages = self._query_native_librpm(desktop_apps, cli_apps)
            else:
                packages = self._query_cli_subprocess(desktop_apps, cli_apps)

            if self._is_cancelled.is_set():
                return

            packages.sort(key=lambda p: p.name.lower())
            self.signals.packages_loaded.emit(packages)
            self.signals.status_update.emit(f"Loaded {len(packages):,} packages successfully.")

        except Exception as ex:
            self.signals.error_occurred.emit("", f"Failed to query database: {str(ex)}")

    def _query_native_librpm(self, desktop_apps: Set[str], cli_apps: Set[str]) -> List[PackageInfo]:
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

            flags = classify_package(name, summary, group, desktop_apps, cli_apps, vendor, packager)

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

        del ts
        return packages

    def _query_cli_subprocess(self, desktop_apps: Set[str], cli_apps: Set[str]) -> List[PackageInfo]:
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

            flags = classify_package(name, summary, group, desktop_apps, cli_apps, vendor, packager)

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
# ورکر پکیج‌های نصب‌شده توسط کاربر
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
# ورکر آنی درخت مستقیم وابستگی‌ها (Direct Direct Dependency Resolver)
# =============================================================================

class DependencyTreeWorker(QRunnable):
    """حل‌کننده درجا و آنی وابستگی‌های مستقیم برای سرعت میلی‌ثانیه‌ای"""
    def __init__(self, root_package: str, max_depth: int = 1):
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
            raw_reqs, parsed_reqs = self._fetch_package_requires(self.root_package, ts)
            if not parsed_reqs or self._is_cancelled.is_set():
                self.signals.dependencies_resolved.emit(self.root_package, [])
                return

            # ۱. جمع‌آوری مواردی که در کش رم یا دیسک موجود نیستند
            caps_to_query: List[str] = []
            for _, cap_name, _ in parsed_reqs:
                cached = self.cache.get(cap_name)
                if cached is None:
                    caps_to_query.append(cap_name)

            # ۲. ریزالو موازی و دسته‌ای موارد جدید
            if caps_to_query and not self._is_cancelled.is_set():
                self._resolve_capabilities_batch(caps_to_query, ts)

            # ۳. ساخت سریع گره‌های درخت
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
                self.signals.dependencies_resolved.emit(self.root_package, resolved_nodes)

        except Exception as ex:
            self.signals.error_occurred.emit(self.root_package, f"Dependency error: {str(ex)}")
        finally:
            if ts is not None:
                del ts

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
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=get_clean_env(), timeout=5)
            if proc.returncode == 0:
                raw_reqs = proc.stdout.splitlines()

        for req in raw_reqs:
            req = req.strip()
            # فیلتر هوشمند پیش‌نیازهای ساختگی، مجازی و فایل‌های سیستمی
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
                # اول بررسی اینکه آیا قابلیت خودش نام یک پکیج است
                match_name = ts.dbMatch("name", cap)  # type: ignore[attr-defined]
                if match_name.count() > 0:
                    batch_results.append((cap, True, cap))
                    continue

                # بررسی provides
                matches = ts.dbMatch("provides", cap)  # type: ignore[attr-defined]
                provider = None
                for hdr in matches:
                    name = hdr[rpm.RPMTAG_NAME]
                    provider = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
                    break
                if provider:
                    batch_results.append((cap, True, provider))
                else:
                    # تمیزکاری نام‌های کتابخانه مانند libssl.so.3 -> openssl
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
# ورکر درخت معکوس وابستگی‌ها (Reverse Dependency Explorer)
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
