# dendro/core/flatpak_system_update.py
"""
Update one system-wide Flatpak app through Flatpak's own privileged helper.

Scope is deliberately narrow: one app in the system-wide installation per request.
Runtimes, installs, uninstalls, and named system installations are not handled here.

How authorization works: the ``flatpak`` command runs as the logged-in user. It
forwards the change to ``org.freedesktop.Flatpak.SystemHelper``, which asks Polkit
to authorize it. Dendro never runs as root, never handles a password, and never
passes ``--noninteractive``, because that flag would stop Polkit from showing the
desktop agent's prompt. Where Polkit refuses, Flatpak's own message is shown.

Safety rules:
- Only the fixed binary ``/usr/bin/flatpak`` runs, with an argument list and no shell.
- The ref comes from a listing of installed apps and must match strict patterns, so
  an ID that starts with ``-`` can never reach Flatpak as an option.
- The installed Flatpak must be at least ``SYSTEM_MIN_VERSION``. Older releases
  lack fixes for the system helper, so they are refused.
- Only one host-affecting operation runs at a time. The request claims the shared
  ``HostOperationGate`` and releases it on every path. Without a gate the request
  is refused, so Dendro never runs an unguarded system change.
- Stdin is closed (``DEVNULL``), and the child gets ``get_clean_env()``.
- Inside a Flatpak sandbox the action is refused rather than bridged to the host.
- The result message states only what the app's version before and after shows.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot

from core.backend import (
    flatpak_read_only_unavailable,
    get_clean_env,
    is_running_in_flatpak,
)
from core.flatpak_inventory import FLATPAK_BINARY, FlatpakEntry
from core.flatpak_uninstall import FlatpakUninstallError
from core.flatpak_versions import SYSTEM_SCOPE, read_installed_version, update_summary
from core.operation_gate import HostOperationGate

# Flatpak names the system-wide installation "system" on Fedora (confirmed on a real
# machine); "default" is the internal id of the same installation in other builds.
SYSTEM_INSTALLATIONS: frozenset[str] = frozenset({"system", "default"})
SYSTEM_MIN_VERSION: tuple[int, int, int] = (1, 16, 4)
SYSTEM_RECOMMENDED_VERSION: tuple[int, int, int] = (1, 18, 4)
SYSTEM_UPDATE_TIMEOUT_SECONDS = 900  # downloads and authentication can take a while
VERSION_PROBE_TIMEOUT_SECONDS = 10
MAX_STDERR_EXCERPT_CHARS = 300
GATE_OWNER = "flatpak-system"

# Same strict identifier rules as the per-user actions.
_APP_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+){2,}")
_ARCH_PATTERN = re.compile(r"[a-z0-9_]{1,32}")
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
_VERSION_PATTERN = re.compile(r"^[ \t]*Flatpak\s+(\d+)\.(\d+)(?:\.(\d+))?", re.MULTILINE)

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]
Version = tuple[int, int, int]


@dataclass(frozen=True)
class SystemUpdateResult:
    """What one successful system update reports back to the dialog."""

    flatpak_version: Version
    summary: str


def is_system_app(entry: FlatpakEntry) -> bool:
    """True only for apps in the system-wide installation, the only scope handled here."""
    return entry.kind == "app" and entry.installation in SYSTEM_INSTALLATIONS


def system_app_ref(entry: FlatpakEntry) -> str:
    """Validate a system app entry and return its ref (``app/<id>/<arch>/<branch>``)."""
    if not is_system_app(entry):
        raise ValueError(
            "Only apps in the system-wide Flatpak installation can be updated here."
        )
    if not _APP_ID_PATTERN.fullmatch(entry.application_id):
        raise ValueError("Flatpak application ID is not a valid reverse-DNS name.")
    if not _ARCH_PATTERN.fullmatch(entry.arch):
        raise ValueError("Flatpak architecture is not a valid identifier.")
    if not _BRANCH_PATTERN.fullmatch(entry.branch):
        raise ValueError("Flatpak branch is not a valid identifier.")
    expected_ref = f"app/{entry.application_id}/{entry.arch}/{entry.branch}"
    if entry.ref != expected_ref:
        raise ValueError(
            "Flatpak ref does not match its application, arch, and branch."
        )
    return expected_ref


def build_system_update_command(entry: FlatpakEntry) -> list[str]:
    """Return the exact argv that updates one system-wide app.

    ``--noninteractive`` is deliberately absent: it would stop Polkit from asking
    the desktop agent for authorization. ``-y`` only answers Flatpak's own CLI
    questions, which matters because stdin is closed.
    """
    ref = system_app_ref(entry)
    return [FLATPAK_BINARY, "--system", "update", "-y", ref]


def parse_flatpak_version(text: str) -> Version | None:
    """Parse the output of ``flatpak --version`` (for example ``Flatpak 1.16.6``)."""
    match = _VERSION_PATTERN.search(text)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch or 0))


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def _flatpak_binary_present() -> bool:
    return os.path.isfile(FLATPAK_BINARY) and os.access(FLATPAK_BINARY, os.X_OK)


def _sanitize_excerpt(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    cleaned = "".join(ch if ch.isprintable() or ch == " " else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_STDERR_EXCERPT_CHARS]


def _probe_version(runner: Runner) -> Version:
    """Read the installed Flatpak version, or refuse with a clear message."""
    try:
        result = runner(
            [FLATPAK_BINARY, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=VERSION_PROBE_TIMEOUT_SECONDS,
            env=get_clean_env(),
        )
    except subprocess.TimeoutExpired as error:
        raise FlatpakUninstallError(
            "Flatpak did not report its version in time. Nothing was changed."
        ) from error
    except OSError as error:
        raise FlatpakUninstallError(
            f"Could not start Flatpak: {type(error).__name__}. Nothing was changed."
        ) from error
    if result.returncode != 0:
        raise FlatpakUninstallError(
            "Could not read the Flatpak version. Nothing was changed."
        )
    version = parse_flatpak_version(result.stdout.decode("utf-8", errors="replace"))
    if version is None:
        raise FlatpakUninstallError(
            "Could not read the Flatpak version. Nothing was changed."
        )
    return version


def run_flatpak_system_update(
    entry: FlatpakEntry,
    *,
    gate: HostOperationGate | None,
    runner: Runner = subprocess.run,
    in_sandbox: Callable[[], bool] = is_running_in_flatpak,
    binary_present: Callable[[], bool] = _flatpak_binary_present,
    timeout: float | None = None,
) -> SystemUpdateResult:
    """Update one system-wide app. Blocking; call from a worker thread only."""
    if in_sandbox():
        raise FlatpakUninstallError(
            flatpak_read_only_unavailable("System-wide Flatpak update")
        )
    if not binary_present():
        raise FlatpakUninstallError(
            f"Flatpak is not installed at {FLATPAK_BINARY}. Nothing was changed."
        )
    if gate is None:
        raise FlatpakUninstallError(
            "Dendro could not confirm that no other system operation is running. "
            "Nothing was changed."
        )
    try:
        command = build_system_update_command(entry)
    except ValueError as error:
        raise FlatpakUninstallError(str(error)) from error

    conflict = gate.claim(GATE_OWNER, f"System-wide Flatpak update of {entry.name}")
    if conflict is not None:
        raise FlatpakUninstallError(
            f"Another system operation is running ({conflict.label}). "
            "Try again when it finishes. Nothing was changed."
        )
    try:
        version = _probe_version(runner)
        if version < SYSTEM_MIN_VERSION:
            raise FlatpakUninstallError(
                f"Flatpak {format_version(version)} is older than "
                f"{format_version(SYSTEM_MIN_VERSION)}, the minimum for system-wide "
                "actions. Update Flatpak first. Nothing was changed."
            )
        before = read_installed_version(entry, scope_flag=SYSTEM_SCOPE, runner=runner)
        limit = SYSTEM_UPDATE_TIMEOUT_SECONDS if timeout is None else timeout
        try:
            result = runner(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=limit,
                env=get_clean_env(),
            )
        except subprocess.TimeoutExpired as error:
            raise FlatpakUninstallError(
                f"Flatpak did not finish the update of {entry.name} within "
                f"{int(limit)} seconds. Refresh the list to see its current state."
            ) from error
        except OSError as error:
            raise FlatpakUninstallError(
                f"Could not start Flatpak: {type(error).__name__}. Nothing was changed."
            ) from error
        if result.returncode != 0:
            excerpt = _sanitize_excerpt(result.stderr or b"")
            suffix = f" {excerpt}" if excerpt else ""
            raise FlatpakUninstallError(
                f"Flatpak could not update {entry.name} (status {result.returncode})."
                f"{suffix}"
            )
        after = read_installed_version(entry, scope_flag=SYSTEM_SCOPE, runner=runner)
        return SystemUpdateResult(
            flatpak_version=version,
            summary=update_summary(entry.name, before, after),
        )
    finally:
        gate.release(GATE_OWNER)


class FlatpakSystemUpdateSignals(QObject):
    """Results carry the request serial so the UI can drop superseded results."""

    succeeded = pyqtSignal(int, str)  # serial, finished message for the status line
    failed = pyqtSignal(int, str)  # serial, user-presentable detail


class FlatpakSystemUpdateWorker(QRunnable):
    """Runs one system-wide update off the UI thread and reports exactly one result."""

    def __init__(
        self,
        request_serial: int,
        entry: FlatpakEntry,
        *,
        gate: HostOperationGate | None,
        action: Callable[[], SystemUpdateResult] | None = None,
    ):
        super().__init__()
        self.request_serial = request_serial
        self.entry = entry
        self.signals = FlatpakSystemUpdateSignals()
        self._action: Callable[[], SystemUpdateResult] = action or (
            lambda: run_flatpak_system_update(entry, gate=gate)
        )

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self._action()
        except FlatpakUninstallError as error:
            self.signals.failed.emit(self.request_serial, error.detail)
            return
        except Exception as error:
            self.signals.failed.emit(
                self.request_serial,
                f"Unexpected error while trying to update: {type(error).__name__}.",
            )
            return
        message = result.summary
        if result.flatpak_version < SYSTEM_RECOMMENDED_VERSION:
            message += (
                f" Flatpak {format_version(result.flatpak_version)} is older than the "
                f"recommended {format_version(SYSTEM_RECOMMENDED_VERSION)}. "
                "Consider updating Flatpak."
            )
        self.signals.succeeded.emit(self.request_serial, message)


__all__ = (
    "GATE_OWNER",
    "SYSTEM_MIN_VERSION",
    "SYSTEM_RECOMMENDED_VERSION",
    "FlatpakSystemUpdateWorker",
    "SystemUpdateResult",
    "build_system_update_command",
    "is_system_app",
    "parse_flatpak_version",
    "run_flatpak_system_update",
    "system_app_ref",
)
