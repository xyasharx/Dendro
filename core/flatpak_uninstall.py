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

import subprocess
from collections.abc import Callable

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from core.flatpak_common import (
    APP_ID_PATTERN,
    ARCH_PATTERN,
    BRANCH_PATTERN,
    FLATPAK_BINARY,
    FlatpakUninstallError,
    flatpak_binary_present,
    run_flatpak_action_subprocess,
    sanitize_stderr_excerpt,
    validate_flatpak_ref,
)
from core.flatpak_inventory import FlatpakEntry
from core.flatpak_progress import FlatpakProgress
from core.flatpak_versions import USER_SCOPE, read_installed_version, update_summary
from core.process_utils import (
    flatpak_read_only_unavailable,
    get_clean_env,
    is_running_in_flatpak,
)
from core.worker_lifecycle import ShutdownSafeRunnable

UNINSTALL_TIMEOUT_SECONDS = 300
UPDATE_TIMEOUT_SECONDS = 900  # updates download content, so they can take longer
USER_INSTALLATION = "user"
OPERATIONS: tuple[str, ...] = ("uninstall", "update")

_APP_ID_PATTERN = APP_ID_PATTERN
_ARCH_PATTERN = ARCH_PATTERN
_BRANCH_PATTERN = BRANCH_PATTERN


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
    return validate_flatpak_ref(
        "app", entry.application_id, entry.arch, entry.branch, entry.ref
    )


def build_uninstall_command(entry: FlatpakEntry, *, delete_data: bool) -> list[str]:
    """Return the exact argv that uninstalls one per-user app (no shell)."""
    ref = user_app_ref(entry)
    argv = [FLATPAK_BINARY, "--user", "uninstall", "--noninteractive", "-y"]
    if delete_data:
        argv.append("--delete-data")
    argv.append(ref)
    return argv


def build_update_command(entry: FlatpakEntry) -> list[str]:
    """Return the exact argv that updates one per-user app (no shell, no runtime cleanup).

    ``--noninteractive`` is deliberately absent: it makes Flatpak print no progress
    lines, so the progress bar would have no percentage. Stdin is closed by the caller,
    the ref is one exact ref, and ``-y`` answers any prompt, so Flatpak cannot wait.
    """
    ref = user_app_ref(entry)
    return [FLATPAK_BINARY, "--user", "update", "-y", ref]


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


_flatpak_binary_present = flatpak_binary_present
_sanitize_excerpt = sanitize_stderr_excerpt


def run_flatpak_user_action(
    entry: FlatpakEntry,
    operation: str,
    *,
    delete_data: bool = False,
    runner: Runner = subprocess.run,
    in_sandbox: Callable[[], bool] = is_running_in_flatpak,
    binary_present: Callable[[], bool] = _flatpak_binary_present,
    timeout: float | None = None,
) -> str:
    """Run one uninstall or update of a per-user app. Blocking; worker threads only.

    Returns the finished message for the status line.
    """
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
        before = None
    else:
        command = build_update_command(entry)
        limit = UPDATE_TIMEOUT_SECONDS if timeout is None else timeout
        before = read_installed_version(entry, scope_flag=USER_SCOPE, runner=runner)

    run_flatpak_action_subprocess(
        command,
        timeout=limit,
        env=get_clean_env(),
        action_noun=f"the {operation} of",
        target_name=entry.name,
        runner=runner,
    )
    if operation == "uninstall":
        return f"Uninstalled {entry.name}."
    after = read_installed_version(entry, scope_flag=USER_SCOPE, runner=runner)
    return update_summary(entry.name, before, after)


def run_flatpak_uninstall(entry: FlatpakEntry, *, delete_data: bool, **kwargs) -> str:
    """Uninstall one per-user app. Blocking; call from a worker thread only."""
    return run_flatpak_user_action(
        entry, "uninstall", delete_data=delete_data, **kwargs
    )


class FlatpakUserActionSignals(QObject):
    """Results carry the request serial so the UI can drop superseded results."""

    succeeded = pyqtSignal(
        int, str
    )  # request serial, finished message for the status line
    failed = pyqtSignal(int, str)  # request serial, user-presentable detail
    progress = pyqtSignal(int, object)  # request serial, FlatpakProgress snapshot


class FlatpakUserActionWorker(ShutdownSafeRunnable):
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
        self._action: Callable[[], str | None] = action or (
            lambda: run_flatpak_user_action(entry, operation, delete_data=delete_data)
        )

    def report_progress(self, snapshot: FlatpakProgress) -> None:
        """Called on the worker's reader thread. The UI receives it as a queued signal."""
        self.signals.progress.emit(self.request_serial, snapshot)

    @pyqtSlot()
    def run(self) -> None:
        try:
            message = self._action()
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
        if not message:
            past = "Uninstalled" if self.operation == "uninstall" else "Updated"
            message = f"{past} {self.entry.name}."
        self.signals.succeeded.emit(self.request_serial, message)


__all__ = (
    "OPERATIONS",
    "FlatpakUninstallError",
    "FlatpakUserActionWorker",
    "build_uninstall_command",
    "build_update_command",
    "describe_uninstall",
    "is_user_app",
    "run_flatpak_uninstall",
    "run_flatpak_user_action",
    "user_app_ref",
)
