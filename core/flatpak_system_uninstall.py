# dendro/core/flatpak_system_uninstall.py
"""
Uninstall one system-wide Flatpak app through Flatpak's own privileged helper.

This is Phase B of the system-wide design. It follows the same authorization path
as the system update in ``flatpak_system_update``: the ``flatpak`` command runs as
the logged-in user, Flatpak's helper asks Polkit, and Dendro never runs as root,
never handles a password, and never passes ``--noninteractive``.

Scope: one app in the system-wide installation per request. Runtimes and named
installations are not handled here.

Effect on other users: the app is removed for everyone on the computer. Data
deletion (``--delete-data``) applies only to the calling user's own data.
Confirm this on a real machine before relying on it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot

from core.backend import (
    flatpak_read_only_unavailable,
    get_clean_env,
    is_running_in_flatpak,
)
from core.flatpak_inventory import FLATPAK_BINARY, FlatpakEntry
from core.flatpak_system_update import (
    GATE_OWNER,
    SYSTEM_MIN_VERSION,
    _flatpak_binary_present,
    _probe_version,
    _sanitize_excerpt,
    format_version,
    system_app_ref,
)
from core.flatpak_uninstall import FlatpakUninstallError
from core.operation_gate import HostOperationGate

SYSTEM_UNINSTALL_TIMEOUT_SECONDS = 600  # includes a possible administrator prompt
SYSTEM_DATA_DIRECTORY_NOTE = "Only your own data is deleted; other users' data is kept."

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def build_system_uninstall_command(
    entry: FlatpakEntry, *, delete_data: bool
) -> list[str]:
    """Return the exact argv that uninstalls one system-wide app.

    ``--noninteractive`` is deliberately absent so that Polkit can prompt through
    the desktop agent. ``-y`` only answers Flatpak's own CLI questions.
    """
    ref = system_app_ref(entry)
    argv = [FLATPAK_BINARY, "--system", "uninstall", "-y"]
    if delete_data:
        argv.append("--delete-data")
    argv.append(ref)
    return argv


def describe_system_uninstall(entry: FlatpakEntry, *, delete_data: bool) -> str:
    """Plain-language description shown in the confirmation for a system-wide app."""
    lines = [
        f"{entry.name} ({entry.application_id}) will be removed from the system-wide "
        "Flatpak installation. It is removed for everyone on this computer.",
        "You may be asked for an administrator password.",
        "Other apps and shared runtimes are not removed.",
    ]
    if delete_data:
        lines.append(
            f"Your data for this app will also be deleted. {SYSTEM_DATA_DIRECTORY_NOTE} "
            "This cannot be undone."
        )
    else:
        lines.append("Your data for this app will be kept.")
    return "\n".join(lines)


def run_flatpak_system_uninstall(
    entry: FlatpakEntry,
    *,
    delete_data: bool,
    gate: HostOperationGate | None,
    runner: Runner = subprocess.run,
    in_sandbox: Callable[[], bool] = is_running_in_flatpak,
    binary_present: Callable[[], bool] = _flatpak_binary_present,
    timeout: float | None = None,
) -> str:
    """Uninstall one system-wide app. Blocking; call from a worker thread only.

    Returns the finished message for the status line.
    """
    if in_sandbox():
        raise FlatpakUninstallError(
            flatpak_read_only_unavailable("System-wide Flatpak uninstall")
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
        command = build_system_uninstall_command(entry, delete_data=delete_data)
    except ValueError as error:
        raise FlatpakUninstallError(str(error)) from error

    conflict = gate.claim(GATE_OWNER, f"System-wide Flatpak uninstall of {entry.name}")
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
        limit = SYSTEM_UNINSTALL_TIMEOUT_SECONDS if timeout is None else timeout
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
                f"Flatpak did not finish the uninstall of {entry.name} within "
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
                f"Flatpak could not uninstall {entry.name} (status {result.returncode})."
                f"{suffix}"
            )
    finally:
        gate.release(GATE_OWNER)

    if delete_data:
        return f"Uninstalled {entry.name} for everyone, and deleted your data for it."
    return f"Uninstalled {entry.name} for everyone. Your data was kept."


class FlatpakSystemUninstallSignals(QObject):
    """Results carry the request serial so the UI can drop superseded results."""

    succeeded = pyqtSignal(int, str)  # serial, finished message for the status line
    failed = pyqtSignal(int, str)  # serial, user-presentable detail


class FlatpakSystemUninstallWorker(QRunnable):
    """Runs one system-wide uninstall off the UI thread and reports exactly one result."""

    def __init__(
        self,
        request_serial: int,
        entry: FlatpakEntry,
        *,
        delete_data: bool,
        gate: HostOperationGate | None,
        action: Callable[[], str] | None = None,
    ):
        super().__init__()
        self.request_serial = request_serial
        self.entry = entry
        self.signals = FlatpakSystemUninstallSignals()
        self._action: Callable[[], str] = action or (
            lambda: run_flatpak_system_uninstall(
                entry, delete_data=delete_data, gate=gate
            )
        )

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
                f"Unexpected error while trying to uninstall: {type(error).__name__}.",
            )
            return
        self.signals.succeeded.emit(
            self.request_serial, message or f"Uninstalled {self.entry.name}."
        )


__all__ = (
    "FlatpakSystemUninstallWorker",
    "build_system_uninstall_command",
    "describe_system_uninstall",
    "run_flatpak_system_uninstall",
)
