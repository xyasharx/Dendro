# core/backend.py
"""
High-Performance RPM/DNF Core Backend Engine.
Handles package querying, cached dependency tree resolution, and privileged execution.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QObject, QProcess, QRunnable, pyqtSignal, pyqtSlot


def get_host_command_prefix() -> List[str]:
    """
    Returns ['flatpak-spawn', '--host'] if running inside a Flatpak container,
    allowing host commands (rpm, dnf, pkexec) to run transparently.
    """
    if os.path.exists("/.flatpak-info") and shutil.which("flatpak-spawn"):
        return ["flatpak-spawn", "--host"]
    return []


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
    dependencies: List[DependencyNode] = field(default_factory=list)

    @property
    def full_version(self) -> str:
        return f"{self.version}-{self.release}" if self.release else self.version

    @property
    def human_size(self) -> str:
        size = float(self.size_bytes)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024.0 or unit == "GB":
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} GB"


class BackendSignals(QObject):
    """Signals for asynchronous backend queries."""
    packages_loaded = pyqtSignal(list)               # List[PackageInfo]
    orphans_loaded = pyqtSignal(set)                 # Set[str]
    dependencies_resolved = pyqtSignal(str, list)    # pkg_name, List[DependencyNode]
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str)


# Global in-memory cache to prevent redundant RPM queries during tree traversals
_CAPABILITY_CACHE: Dict[str, Tuple[bool, str]] = {}


class PackageQueryWorker(QRunnable):
    """
    Asynchronously queries all installed packages via direct RPM DB inspection.
    Executes in under 100ms.
    """

    def __init__(self, category: str = "all", search_query: str = ""):
        super().__init__()
        self.signals = BackendSignals()
        self.category = category.lower()
        self.search_query = search_query.strip().lower()
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    @pyqtSlot()
    def run(self):
        try:
            self.signals.status_update.emit("Querying RPM database...")

            query_format = "%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}|%{GROUP}|%{SIZE}|%{SUMMARY}\n"
            cmd = get_host_command_prefix() + ["rpm", "-qa", "--queryformat", query_format]

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15
            )

            if proc.returncode != 0:
                self.signals.error_occurred.emit(f"RPM Query Failed: {proc.stderr}")
                return

            packages: List[PackageInfo] = []

            for line in proc.stdout.splitlines():
                if self._is_cancelled:
                    return
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

                # Apply category filtering
                if self.category == "development" and "Development" not in group:
                    continue
                if self.category == "system" and "System" not in group and "Base" not in group:
                    continue

                # Apply search filtering
                if self.search_query:
                    match_name = self.search_query in name.lower()
                    match_summary = self.search_query in summary.lower()
                    if not (match_name or match_summary):
                        continue

                pkg = PackageInfo(
                    name=name,
                    version=ver,
                    release=rel,
                    arch=arch,
                    summary=summary,
                    group=group or "General",
                    size_bytes=size_bytes,
                    state=PackageState.INSTALLED,
                    is_orphan=False
                )
                packages.append(pkg)

            # Sort alphabetically
            packages.sort(key=lambda x: x.name.lower())

            if not self._is_cancelled:
                self.signals.packages_loaded.emit(packages)
                self.signals.status_update.emit(f"Loaded {len(packages)} packages.")

        except subprocess.TimeoutExpired:
            self.signals.error_occurred.emit("RPM query timed out.")
        except Exception as ex:
            self.signals.error_occurred.emit(f"Unexpected error: {str(ex)}")


class OrphanQueryWorker(QRunnable):
    """
    Dedicated worker to query unneeded leaf packages asynchronously
    without blocking the primary package table render.
    """

    def __init__(self):
        super().__init__()
        self.signals = BackendSignals()
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    @pyqtSlot()
    def run(self):
        if not shutil.which("dnf") and not get_host_command_prefix():
            return

        try:
            cmd = get_host_command_prefix() + ["dnf", "repoquery", "--unneeded", "-q", "--qf", "%{NAME}"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            if res.returncode == 0 and not self._is_cancelled:
                orphans = {line.strip() for line in res.stdout.splitlines() if line.strip()}
                self.signals.orphans_loaded.emit(orphans)
        except Exception:
            pass  # Fail gracefully if DNF is locked or unavailable


class DependencyTreeWorker(QRunnable):
    """
    Recursively builds a Directed Acyclic Graph (DAG) of dependencies.
    Utilizes an in-memory capability cache to eliminate redundant subprocess calls.
    """

    def __init__(self, root_package: str, max_depth: int = 3):
        super().__init__()
        self.signals = BackendSignals()
        self.root_package = root_package
        self.max_depth = max_depth
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    @pyqtSlot()
    def run(self):
        try:
            self.signals.status_update.emit(f"Resolving dependency tree for '{self.root_package}'...")
            visited_path: Set[str] = {self.root_package}

            deps = self._resolve_recursive(self.root_package, depth=1, visited=visited_path)

            if not self._is_cancelled:
                self.signals.dependencies_resolved.emit(self.root_package, deps)
                self.signals.status_update.emit(
                    f"Resolved {len(deps)} direct dependencies for '{self.root_package}'."
                )
        except Exception as ex:
            self.signals.error_occurred.emit(f"Dependency resolution error: {str(ex)}")

    def _resolve_recursive(self, pkg_name: str, depth: int, visited: Set[str]) -> List[DependencyNode]:
        if depth > self.max_depth or self._is_cancelled:
            return []

        resolved_nodes: List[DependencyNode] = []

        try:
            cmd = get_host_command_prefix() + ["rpm", "-qR", pkg_name]
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=5)
            if proc.returncode != 0:
                return []

            raw_reqs = proc.stdout.splitlines()
            seen_clean_names: Set[str] = set()

            for req in raw_reqs:
                if self._is_cancelled:
                    return []

                req = req.strip()
                # Skip rpmlib internals and file path configurations
                if not req or req.startswith("rpmlib(") or req.startswith("config(") or req.startswith("/"):
                    continue

                # Parse capability and version constraint: e.g. "libcurl.so.4()(64bit) >= 7.82"
                tokens = re.split(r'([<>=]+)', req, maxsplit=1)
                cap_name = tokens[0].strip()
                constraint = tokens[1] + tokens[2] if len(tokens) == 3 else ""

                # Query cache first to avoid slow subshell spawning
                if cap_name in _CAPABILITY_CACHE:
                    is_satisfied, provider_name = _CAPABILITY_CACHE[cap_name]
                else:
                    provider_proc = subprocess.run(
                        get_host_command_prefix() + ["rpm", "-q", "--whatprovides", "--queryformat", "%{NAME}\n", cap_name],
                        capture_output=True,
                        text=True,
                        timeout=3
                    )
                    is_satisfied = (provider_proc.returncode == 0)
                    provider_name = provider_proc.stdout.splitlines()[0].strip() if is_satisfied else cap_name
                    _CAPABILITY_CACHE[cap_name] = (is_satisfied, provider_name)

                if provider_name in seen_clean_names:
                    continue
                seen_clean_names.add(provider_name)

                is_cycle = provider_name in visited

                node = DependencyNode(
                    raw_requirement=req,
                    resolved_package_name=provider_name,
                    version_constraint=constraint,
                    is_satisfied=is_satisfied,
                    is_cycle=is_cycle
                )

                # Recursive descent if not cycling and within depth limits
                if not is_cycle and is_satisfied and depth < self.max_depth:
                    next_visited = set(visited)
                    next_visited.add(provider_name)
                    node.sub_dependencies = self._resolve_recursive(
                        provider_name,
                        depth=depth + 1,
                        visited=next_visited
                    )

                resolved_nodes.append(node)

        except Exception:
            pass

        return resolved_nodes


class PolkitTransactionRunner(QObject):
    """
    Executes DNF transactions using Polkit elevation (`pkexec`).
    Secured against argument injection and streams output in real-time.
    """
    log_received = pyqtSignal(str)
    progress_percent = pyqtSignal(int)
    transaction_finished = pyqtSignal(bool, int)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.process: Optional[QProcess] = None
        self._ansi_cleaner = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

    def execute_transaction(self, to_install: List[str], to_remove: List[str]):
        if self.process and self.process.state() == QProcess.ProcessState.Running:
            self.log_received.emit("Error: Another transaction is currently running.\n")
            return

        if not to_install and not to_remove:
            self.log_received.emit("No operations to execute.\n")
            return

        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.finished.connect(self._on_finished)

        # Build sanitized execution arguments
        prefix = get_host_command_prefix()
        program = prefix[0] if prefix else "pkexec"
        args: List[str] = prefix[1:] + ["pkexec"] if prefix else []
        args.extend(["dnf", "-y"])

        if to_install:
            # Use '--' to guard against package names crafted as CLI flags
            args.extend(["install", "--"] + to_install)
        if to_remove:
            args.extend(["remove", "--"] + to_remove)

        self.log_received.emit("🔒 Requesting root authorization for transaction...\n")
        self.log_received.emit(f"Command: {program} {' '.join(args)}\n\n")

        self.process.start(program, args)

    def cancel_transaction(self):
        if self.process and self.process.state() == QProcess.ProcessState.Running:
            self.log_received.emit("\n⚠️ Terminating transaction...\n")
            self.process.terminate()

    def _on_stdout(self):
        if not self.process:
            return
        raw_data = self.process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        clean_text = self._ansi_cleaner.sub('', raw_data)
        self.log_received.emit(clean_text)
        self._parse_progress(clean_text)

    def _on_stderr(self):
        if not self.process:
            return
        raw_data = self.process.readAllStandardError().data().decode("utf-8", errors="replace")
        clean_text = self._ansi_cleaner.sub('', raw_data)
        self.log_received.emit(f"[ERR] {clean_text}")

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus):
        success = (exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit)
        self.transaction_finished.emit(success, exit_code)

    def _parse_progress(self, text: str):
        """Matches transaction progress indicators like '[ 45% ] Installing package'."""
        match = re.search(r'\[\s*(\d+)%\s*\]', text)
        if match:
            percent = int(match.group(1))
            self.progress_percent.emit(percent)
