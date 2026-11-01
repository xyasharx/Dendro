# dendro/core/flatpak_uninstall.py
"""
Uninstall Flatpak apps that belong to the current user's installation.

This is the first, deliberately narrow write action for Flatpak. It handles
only applications in the per-user installation (``flatpak --user``). It does not
install, update, remove runtimes, or touch the system-wide installation. Those
actions carry more risk and need their own confirmation and approval design.

Safety rules:
- Only the fixed binary ``/usr/bin/flatpak`` runs, with an argument list and no
  shell. Every element of the command is validated before it is used.
- The ref comes from a listing of the installed apps. Its application ID, arch,
  and branch must match strict patterns, so no option or path can be injected.
- Stdin is closed (``DEVNULL``), so Flatpak cannot wait on a question.
- The child process gets the sanitized environment from ``get_clean_env``.
- Inside a Flatpak sandbox the action is refused rather than bridged to the host.
- Data removal (``~/.var/app/<id>``) happens only when the caller asks for it.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot

from core.backend import (
    flatpak_read_only_unavailable,
    get_clean_env,
    is_running_in_flatpak,
)
from core.flatpak_inventory import FLATPAK_BINARY, FlatpakEntry

UNINSTALL_TIMEOUT_SECONDS = 300
MAX_STDERR_EXCERPT_CHARS = 300
USER_INSTALLATION = "user"

# Flatpak application IDs are reverse-DNS names with at least three elements.
_APP_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+){2,}")
_ARCH_PATTERN = re.compile(r"[a-z0-9_]{1,32}")
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")


class FlatpakUninstallError(Exception):
    """A user-presentable failure while uninstalling a Flatpak app."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def is_user_app(entry: FlatpakEntry) -> bool:
    """True only for apps in the per-user installation, the only scope handled here."""
    return entry.kind == "app" and entry.installation == USER_INSTALLATION


def data_directory_for(application_id: str) -> str:
    """Display path of the app's data directory (shown to the user, never used)."""
    return f"~/.var/app/{application_id}"


def describe_uninstall(entry: FlatpakEntry, *, delete_data: bool) -> str:
    """Plain-language description shown in the confirmation dialog."""
    lines = [
        f"{entry.name} ({entry.application_id}) will be removed from your user "
        "Flatpak installation.",
        "Other apps and shared runtimes are not removed.",
    ]
    if delete_data:
        lines.append(
            f"Its data in {data_directory_for(entry.application_id)} will also be "
            "deleted. This cannot be undone."
        )
    else:
        lines.append(
            f"Its data in {data_directory_for(entry.application_id)} will be kept."
        )
    return "\n".join(lines)


def build_uninstall_command(entry: FlatpakEntry, *, delete_data: bool) -> list[str]:
    """Return the exact argv that uninstalls one per-user app (no shell)."""
    if not is_user_app(entry):
        raise ValueError(
            "Only apps in the per-user Flatpak installation can be uninstalled here."
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

    argv = [FLATPAK_BINARY, "--user", "uninstall", "--noninteractive", "-y"]
    if delete_data:
        argv.append("--delete-data")
    argv.append(expected_ref)
    return argv


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _flatpak_binary_present() -> bool:
    return os.path.isfile(FLATPAK_BINARY) and os.access(FLATPAK_BINARY, os.X_OK)


def _sanitize_excerpt(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    cleaned = "".join(ch if ch.isprintable() or ch == " " else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_STDERR_EXCERPT_CHARS]


def run_flatpak_uninstall(
    entry: FlatpakEntry,
    *,
    delete_data: bool,
    runner: Runner = subprocess.run,
    in_sandbox: Callable[[], bool] = is_running_in_flatpak,
    binary_present: Callable[[], bool] = _flatpak_binary_present,
    timeout: float = UNINSTALL_TIMEOUT_SECONDS,
) -> None:
    """Uninstall one per-user app. Blocking; call from a worker thread only."""
    if in_sandbox():
        raise FlatpakUninstallError(flatpak_read_only_unavailable("Flatpak uninstall"))
    if not binary_present():
        raise FlatpakUninstallError(
            f"Flatpak is not installed at {FLATPAK_BINARY}. Nothing was removed."
        )
    command = build_uninstall_command(entry, delete_data=delete_data)

    try:
        result = runner(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=get_clean_env(),
        )
    except subprocess.TimeoutExpired as error:
        raise FlatpakUninstallError(
            f"Flatpak did not finish uninstalling {entry.name} within "
            f"{int(timeout)} seconds. Refresh the list to see its current state."
        ) from error
    except OSError as error:
        raise FlatpakUninstallError(
            f"Could not start Flatpak: {type(error).__name__}. Nothing was removed."
        ) from error

    if result.returncode != 0:
        excerpt = _sanitize_excerpt(result.stderr or b"")
        suffix = f" {excerpt}" if excerpt else ""
        raise FlatpakUninstallError(
            f"Flatpak could not uninstall {entry.name} "
            f"(status {result.returncode}).{suffix}"
        )


class FlatpakUninstallSignals(QObject):
    """Results carry the request serial so the UI can drop superseded results."""

    succeeded = pyqtSignal(int, str)  # request serial, display name
    failed = pyqtSignal(int, str)  # request serial, user-presentable detail


class FlatpakUninstallWorker(QRunnable):
    """Runs one uninstall off the UI thread and reports exactly one result."""

    def __init__(
        self,
        request_serial: int,
        entry: FlatpakEntry,
        *,
        delete_data: bool,
        action: Callable[[], None] | None = None,
    ):
        super().__init__()
        self.request_serial = request_serial
        self.entry = entry
        self.signals = FlatpakUninstallSignals()
        self._action: Callable[[], None] = action or (
            lambda: run_flatpak_uninstall(entry, delete_data=delete_data)
        )

    @pyqtSlot()
    def run(self) -> None:
        try:
            self._action()
        except FlatpakUninstallError as error:
            self.signals.failed.emit(self.request_serial, error.detail)
            return
        except Exception as error:
            self.signals.failed.emit(
                self.request_serial,
                f"Unexpected error while uninstalling: {type(error).__name__}.",
            )
            return
        self.signals.succeeded.emit(self.request_serial, self.entry.name)


__all__ = (
    "FlatpakUninstallError",
    "FlatpakUninstallWorker",
    "build_uninstall_command",
    "describe_uninstall",
    "is_user_app",
    "run_flatpak_uninstall",
)
