# dendro/core/flatpak_uninstall.py
"""
Uninstall and update Flatpak apps that belong to the current user's installation.

Scope is deliberately narrow. Only applications in the per-user installation
(``flatpak --user``) can be changed here. Installing new apps, changing runtimes
directly, and touching the system-wide installation are not handled. System-wide
changes go through Flatpak's own privileged helper and Polkit, which would need a
separate design and approval.

Safety rules:
- Only the fixed binary ``/usr/bin/flatpak`` runs, with an argument list and no
  shell. Every element of the command is validated before it is used.
- The ref comes from a listing of installed apps. Its application ID, arch, and
  branch must match strict patterns, so no option or path can be injected.
- Each command names exactly one app ref. Nothing else in the installation is
  touched, and no runtime cleanup is requested.
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
UPDATE_TIMEOUT_SECONDS = 900  # updates download content, so they can take longer
MAX_STDERR_EXCERPT_CHARS = 300
USER_INSTALLATION = "user"
OPERATIONS: tuple[str, ...] = ("uninstall", "update")

# Flatpak application IDs are reverse-DNS names with at least three elements.
_APP_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+){2,}")
_ARCH_PATTERN = re.compile(r"[a-z0-9_]{1,32}")
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")


class FlatpakUninstallError(Exception):
    """A user-presentable failure while changing a Flatpak app."""

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
    """Plain-language description shown in the uninstall confirmation."""
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


def user_app_ref(entry: FlatpakEntry) -> str:
    """Validate an entry for a per-user action and return its ref (``app/<id>/<arch>/<branch>``)."""
    if not is_user_app(entry):
        raise ValueError(
            "Only apps in the per-user Flatpak installation can be changed here."
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


def build_uninstall_command(entry: FlatpakEntry, *, delete_data: bool) -> list[str]:
    """Return the exact argv that uninstalls one per-user app (no shell)."""
    ref = user_app_ref(entry)
    argv = [FLATPAK_BINARY, "--user", "uninstall", "--noninteractive", "-y"]
    if delete_data:
        argv.append("--delete-data")
    argv.append(ref)
    return argv


def build_update_command(entry: FlatpakEntry) -> list[str]:
    """Return the exact argv that updates one per-user app (no shell, no runtime cleanup)."""
    ref = user_app_ref(entry)
    return [FLATPAK_BINARY, "--user", "update", "--noninteractive", "-y", ref]


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _flatpak_binary_present() -> bool:
    return os.path.isfile(FLATPAK_BINARY) and os.access(FLATPAK_BINARY, os.X_OK)


def _sanitize_excerpt(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    cleaned = "".join(ch if ch.isprintable() or ch == " " else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_STDERR_EXCERPT_CHARS]


def run_flatpak_user_action(
    entry: FlatpakEntry,
    operation: str,
    *,
    delete_data: bool = False,
    runner: Runner = subprocess.run,
    in_sandbox: Callable[[], bool] = is_running_in_flatpak,
    binary_present: Callable[[], bool] = _flatpak_binary_present,
    timeout: float | None = None,
) -> None:
    """Run one uninstall or update of a per-user app. Blocking; worker threads only."""
    if operation not in OPERATIONS:
        raise ValueError(f"unknown Flatpak operation: {operation!r}")
    if in_sandbox():
        raise FlatpakUninstallError(
            flatpak_read_only_unavailable(f"Flatpak {operation}")
        )
    if not binary_present():
        raise FlatpakUninstallError(
            f"Flatpak is not installed at {FLATPAK_BINARY}. Nothing was changed."
        )
    if operation == "uninstall":
        command = build_uninstall_command(entry, delete_data=delete_data)
        limit = UNINSTALL_TIMEOUT_SECONDS if timeout is None else timeout
    else:
        command = build_update_command(entry)
        limit = UPDATE_TIMEOUT_SECONDS if timeout is None else timeout

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
            f"Flatpak did not finish the {operation} of {entry.name} within "
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
            f"Flatpak could not {operation} {entry.name} "
            f"(status {result.returncode}).{suffix}"
        )


def run_flatpak_uninstall(entry: FlatpakEntry, *, delete_data: bool, **kwargs) -> None:
    """Uninstall one per-user app. Blocking; call from a worker thread only."""
    run_flatpak_user_action(entry, "uninstall", delete_data=delete_data, **kwargs)


def run_flatpak_update(entry: FlatpakEntry, **kwargs) -> None:
    """Update one per-user app. Blocking; call from a worker thread only."""
    run_flatpak_user_action(entry, "update", **kwargs)


class FlatpakUserActionSignals(QObject):
    """Results carry the request serial so the UI can drop superseded results."""

    succeeded = pyqtSignal(int, str)  # request serial, display name
    failed = pyqtSignal(int, str)  # request serial, user-presentable detail


class FlatpakUserActionWorker(QRunnable):
    """Runs one uninstall or update off the UI thread and reports exactly one result."""

    def __init__(
        self,
        request_serial: int,
        entry: FlatpakEntry,
        *,
        operation: str = "uninstall",
        delete_data: bool = False,
        action: Callable[[], None] | None = None,
    ):
        super().__init__()
        if operation not in OPERATIONS:
            raise ValueError(f"unknown Flatpak operation: {operation!r}")
        self.request_serial = request_serial
        self.entry = entry
        self.operation = operation
        self.signals = FlatpakUserActionSignals()
        self._action: Callable[[], None] = action or (
            lambda: run_flatpak_user_action(entry, operation, delete_data=delete_data)
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
                f"Unexpected error while trying to {self.operation}: "
                f"{type(error).__name__}.",
            )
            return
        self.signals.succeeded.emit(self.request_serial, self.entry.name)


__all__ = (
    "OPERATIONS",
    "FlatpakUninstallError",
    "FlatpakUserActionWorker",
    "build_uninstall_command",
    "build_update_command",
    "describe_uninstall",
    "is_user_app",
    "run_flatpak_uninstall",
    "run_flatpak_update",
    "run_flatpak_user_action",
    "user_app_ref",
)
