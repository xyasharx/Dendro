# dendro/core/backend.py
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Final, List, Optional, Set, Tuple

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QRunnable, pyqtSignal, pyqtSlot

try:
    import rpm  # type: ignore[import-untyped]
    HAS_NATIVE_RPM: Final[bool] = True
except ImportError:
    HAS_NATIVE_RPM = False


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


FEDORA_SYSTEM_ROOT_PILLARS: Final[Set[str]] = {
    "kernel", "kernel-core", "kernel-modules", "gnome-shell", "plasma-desktop",
    "systemd", "systemd-udev", "pipewire", "wireplumber", "NetworkManager",
    "firewalld", "gdm", "sddm", "mesa-dri-drivers", "mesa-vulkan-drivers",
    "grub2-common", "grub2-efi-x64", "dracut", "polkit", "dnf5", "dnf",
    "flatpak", "udisks2", "upower", "bluez", "cups", "mutter", "kwin",
    "xorg-x11-server-Xorg", "selinux-policy", "btrfs-progs", "chrony",
    "coreutils", "bash", "sudo", "shadow-utils", "util-linux"
}

KNOWN_CLI_USER_TOOLS: Final[Set[str]] = {
    "neovim", "vim", "htop", "btop", "tmux", "zsh", "fish", "git",
    "curl", "wget", "ripgrep", "fd-find", "fzf", "tree", "fastfetch",
    "neofetch", "nmap", "ffmpeg", "rsync", "jq", "micro"
}


def classify_package(
    name: str,
    summary: str,
    group: str,
    installed_desktop_pkgs: Set[str]
) -> Dict[str, bool]:
    name_lower = name.lower()
    sum_lower = summary.lower()

    # ۱. فونت‌ها (Fonts)
    is_font = (
        any(name_lower.startswith(pfx) for pfx in ("font-", "google-noto-", "dejavu-", "fonts-", "gnu-free-", "urw-base35-")) or
        any(name_lower.endswith(sfx) for sfx in ("-fonts", "-font", "-fonts-all")) or
        "font" in name_lower or "font" in sum_lower
    )

    # ۲. فریمورها و میکروکد سخت‌افزار (Firmware)
    is_firmware = (
        any(kw in name_lower for kw in ("firmware", "microcode", "ucode")) or
        any(kw in sum_lower for kw in ("firmware", "microcode", "hardware support"))
    )

    # ۳. بسته‌های زبانی و ترجمه‌ها (Locales & Langpacks)
    is_locale = (
        name_lower.startswith(("glibc-langpack-", "langpacks-", "ibus-")) or
        name_lower.endswith(("-langpack", "-langpacks", "-i18n", "-l10n", "-doc-locale")) or
        "language pack" in sum_lower or "translation" in sum_lower or "locale" in sum_lower
    )

    # ۴. هدرهای توسعه و SDKها (Development)
    is_devel = (
        name_lower.endswith(("-devel", "-static", "-debuginfo", "-debugsource")) or
        "development files" in sum_lower or "header files" in sum_lower or "development libraries" in sum_lower
    )

    # ۵. تم‌ها و آیکون‌ها (Themes & Icons)
    is_theme = (
        any(kw in name_lower for kw in ("-theme", "-icon-theme", "-backgrounds", "-wallpapers", "sound-theme-")) or
        "icon theme" in sum_lower or "desktop theme" in sum_lower or "wallpapers" in sum_lower
    )

    # ۶. کتابخانه‌های اشتراکی C/C++ و سیستم (C/C++ Shared Libraries)
    is_c_lib = False
    if not is_font and not is_firmware and not is_locale and not is_devel and not is_theme:
        lib_suffixes = ("-libs", "-common", "-data", "-help", "-filesystem")
        if any(name_lower.endswith(sfx) for sfx in lib_suffixes):
            is_c_lib = True
        elif name_lower.startswith("lib") and name_lower not in (
            "libreoffice", "libtree", "libvirt", "libguestfs-tools", "libcamera-tools"
        ):
            is_c_lib = True
        elif "shared library" in sum_lower or "libraries for" in sum_lower:
            is_c_lib = True

    is_general_lib = is_c_lib or is_font or is_firmware or is_locale or is_devel or is_theme

    # ۷. برنامه‌های کاربردی کاربر (User Apps)
    has_desktop = (name in installed_desktop_pkgs or name_lower in installed_desktop_pkgs)
    is_cli_tool = name_lower in KNOWN_CLI_USER_TOOLS
    is_user_app = (has_desktop or is_cli_tool) and not is_general_lib

    # ۸. ستون‌های اصلی فدورا (Fedora Core Pillars)
    is_fedora_core = (name in FEDORA_SYSTEM_ROOT_PILLARS or name_lower in FEDORA_SYSTEM_ROOT_PILLARS)
    if not is_fedora_core and not is_general_lib and not is_user_app:
        if any(name_lower.startswith(pfx) for pfx in ("systemd-", "kernel-", "gnome-", "plasma-", "pipewire-")):
            is_fedora_core = True

    return {
        "is_user_app": is_user_app,
        "is_fedora_core": is_fedora_core,
        "is_c_lib": is_c_lib,
        "is_firmware": is_firmware,
        "is_font": is_font,
        "is_locale": is_locale,
        "is_devel": is_devel,
        "is_theme": is_theme,
        "is_library": is_general_lib
    }


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
    sub_dependencies: List[DependencyNode] = field(default_factory=list)


@dataclass(slots=True)
class PackageInfo:
    name: str
    version: str = ""
    release: str = ""
    arch: str = ""
    summary: str = ""
    group: str = "System"
    size_bytes: int = 0
    state: PackageState = PackageState.AVAILABLE
    is_orphan: bool = False
    is_user_app: bool = False
    is_fedora_core: bool = False
    is_c_lib: bool = False
    is_firmware: bool = False
    is_font: bool = False
    is_locale: bool = False
    is_devel: bool = False
    is_theme: bool = False
    is_library: bool = False
    dependencies_loaded: bool = False
    dependencies: List[DependencyNode] = field(default_factory=list)

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


class BackendSignals(QObject):
    packages_loaded = pyqtSignal(list)
    orphans_loaded = pyqtSignal(set)
    userinstalled_loaded = pyqtSignal(set)
    dependencies_resolved = pyqtSignal(str, list)
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)


_CACHE_LOCK: Final[threading.Lock] = threading.Lock()
_CAPABILITY_CACHE: Dict[str, Tuple[bool, str]] = {}
_SUBTREE_CACHE: Dict[str, List[DependencyNode]] = {}


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
                return []

            name = header[rpm.RPMTAG_NAME]
            if not name:
                continue

            name = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
            ver = header[rpm.RPMTAG_VERSION] or ""
            ver = ver.decode("utf-8", errors="replace") if isinstance(ver, bytes) else str(ver)
            rel = header[rpm.RPMTAG_RELEASE] or ""
            rel = rel.decode("utf-8", errors="replace") if isinstance(rel, bytes) else str(rel)
            arch = header[rpm.RPMTAG_ARCH] or ""
            arch = arch.decode("utf-8", errors="replace") if isinstance(arch, bytes) else str(arch)
            group = header[rpm.RPMTAG_GROUP] or "General"
            group = group.decode("utf-8", errors="replace") if isinstance(group, bytes) else str(group)
            summary = header[rpm.RPMTAG_SUMMARY] or ""
            summary = summary.decode("utf-8", errors="replace") if isinstance(summary, bytes) else str(summary)
            size_bytes = int(header[rpm.RPMTAG_SIZE] or 0)

            if self.search_query:
                if (self.search_query not in name.lower()) and (self.search_query not in summary.lower()):
                    continue

            flags = classify_package(name, summary, group, desktop_apps)

            packages.append(
                PackageInfo(
                    name=name,
                    version=ver,
                    release=rel,
                    arch=arch,
                    summary=summary,
                    group=group,
                    size_bytes=size_bytes,
                    state=PackageState.INSTALLED,
                    is_orphan=False,
                    is_user_app=flags["is_user_app"],
                    is_fedora_core=flags["is_fedora_core"],
                    is_c_lib=flags["is_c_lib"],
                    is_firmware=flags["is_firmware"],
                    is_font=flags["is_font"],
                    is_locale=flags["is_locale"],
                    is_devel=flags["is_devel"],
                    is_theme=flags["is_theme"],
                    is_library=flags["is_library"]
                )
            )

        return packages

    def _query_cli_subprocess(self, desktop_apps: Set[str]) -> List[PackageInfo]:
        query_format = "%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}|%{GROUP}|%{SIZE}|%{SUMMARY}\n"
        cmd = get_host_command_prefix() + ["rpm", "-qa", "--queryformat", query_format]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            env=get_clean_env(),
            timeout=25
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
            if len(parts) < 7:
                continue

            name, ver, rel, arch, group, size_str, summary = parts[:7]
            try:
                size_bytes = int(size_str)
            except ValueError:
                size_bytes = 0

            if self.search_query:
                if (self.search_query not in name.lower()) and (self.search_query not in summary.lower()):
                    continue

            flags = classify_package(name, summary, group, desktop_apps)

            packages.append(
                PackageInfo(
                    name=name,
                    version=ver,
                    release=rel,
                    arch=arch,
                    summary=summary,
                    group=group or "General",
                    size_bytes=size_bytes,
                    state=PackageState.INSTALLED,
                    is_orphan=False,
                    is_user_app=flags["is_user_app"],
                    is_fedora_core=flags["is_fedora_core"],
                    is_c_lib=flags["is_c_lib"],
                    is_firmware=flags["is_firmware"],
                    is_font=flags["is_font"],
                    is_locale=flags["is_locale"],
                    is_devel=flags["is_devel"],
                    is_theme=flags["is_theme"],
                    is_library=flags["is_library"]
                )
            )

        return packages


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


class DependencyTreeWorker(QRunnable):
    def __init__(self, root_package: str, max_depth: int = 3):
        super().__init__()
        self.signals = BackendSignals()
        self.root_package = root_package
        self.max_depth = max_depth
        self._is_cancelled = threading.Event()
        self._ts: Optional[object] = rpm.TransactionSet() if HAS_NATIVE_RPM and not is_running_in_flatpak() else None

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        try:
            self.signals.status_update.emit(f"Resolving dependency graph for '{self.root_package}'...")
            visited_path: Set[str] = {self.root_package}
            deps = self._resolve_recursive(self.root_package, depth=1, visited=visited_path)

            if not self._is_cancelled.is_set():
                self.signals.dependencies_resolved.emit(self.root_package, deps)
                self.signals.status_update.emit(
                    f"Resolved {len(deps)} dependencies for '{self.root_package}'."
                )
        except Exception as ex:
            self.signals.error_occurred.emit(self.root_package, f"Dependency error: {str(ex)}")

    def _resolve_recursive(self, pkg_name: str, depth: int, visited: Set[str]) -> List[DependencyNode]:
        if depth > self.max_depth or self._is_cancelled.is_set():
            return []

        resolved_nodes: List[DependencyNode] = []

        try:
            raw_reqs, parsed_reqs = self._fetch_package_requires(pkg_name)
            if not parsed_reqs:
                return []

            caps_to_query: List[str] = []
            for _, cap_name, _ in parsed_reqs:
                with _CACHE_LOCK:
                    if cap_name not in _CAPABILITY_CACHE:
                        caps_to_query.append(cap_name)

            if caps_to_query:
                self._resolve_capabilities_batch(caps_to_query)

            seen_clean_names: Set[str] = set()

            for raw_req, cap_name, constraint in parsed_reqs:
                if self._is_cancelled.is_set():
                    return []

                with _CACHE_LOCK:
                    is_satisfied, provider_name = _CAPABILITY_CACHE.get(cap_name, (False, cap_name))

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
                    with _CACHE_LOCK:
                        cached_sub = _SUBTREE_CACHE.get(provider_name)

                    if cached_sub is not None:
                        node.sub_dependencies = cached_sub
                    else:
                        next_visited = set(visited)
                        next_visited.add(provider_name)
                        sub_deps = self._resolve_recursive(provider_name, depth=depth + 1, visited=next_visited)
                        with _CACHE_LOCK:
                            _SUBTREE_CACHE[provider_name] = sub_deps
                        node.sub_dependencies = sub_deps

                resolved_nodes.append(node)

        except Exception:
            pass

        return resolved_nodes

    def _fetch_package_requires(self, pkg_name: str) -> Tuple[List[str], List[Tuple[str, str, str]]]:
        raw_reqs: List[str] = []
        parsed_reqs: List[Tuple[str, str, str]] = []

        if self._ts is not None:
            match = self._ts.dbMatch("name", pkg_name)
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

    def _resolve_capabilities_batch(self, capabilities: List[str]):
        if self._ts is not None:
            for cap in capabilities:
                matches = self._ts.dbMatch("provides", cap)
                provider = None
                for hdr in matches:
                    name = hdr[rpm.RPMTAG_NAME]
                    provider = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
                    break
                with _CACHE_LOCK:
                    if provider:
                        _CAPABILITY_CACHE[cap] = (True, provider)
                    else:
                        _CAPABILITY_CACHE[cap] = (False, cap)
        else:
            batch_cmd = get_host_command_prefix() + ["rpm", "-q", "--whatprovides", "--queryformat", "%{NAME}\n"] + capabilities
            batch_proc = subprocess.run(batch_cmd, capture_output=True, text=True, env=get_clean_env(), timeout=8)
            providers = batch_proc.stdout.splitlines()

            with _CACHE_LOCK:
                for i, cap in enumerate(capabilities):
                    if i < len(providers) and "no package provides" not in providers[i]:
                        _CAPABILITY_CACHE[cap] = (True, providers[i].strip())
                    else:
                        _CAPABILITY_CACHE[cap] = (False, cap)


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
        if self.process and self.process.state() == QProcess.ProcessState.Running:
            self.log_received.emit("Error: Another transaction is currently active.\n")
            return

        if not to_install and not to_remove:
            self.log_received.emit("No operations queued.\n")
            return

        self.process = QProcess(self)
        
        process_env = QProcessEnvironment.systemEnvironment()
        for var in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            process_env.remove(var)
        self.process.setProcessEnvironment(process_env)

        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.finished.connect(self._on_finished)

        dnf_bin = get_dnf_binary_path()
        prefix = get_host_command_prefix()
        program = prefix[0] if prefix else "pkexec"
        args: List[str] = prefix[1:] + ["pkexec"] if prefix else []
        args.extend([dnf_bin, "-y"])

        if to_install:
            args.extend(["install", "--"] + to_install)
        if to_remove:
            args.extend(["remove", "--"] + to_remove)

        self.log_received.emit("🔒 Requesting administrative authorization for package transaction...\n")
        self.log_received.emit(f"Executing: {program} {' '.join(args)}\n\n")

        self.process.start(program, args)

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
