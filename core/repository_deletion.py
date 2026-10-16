"""Narrow frontend launcher for Dendro's repository-file deletion helper.

Package transactions and repository enable/disable use Fedora's DNF5 daemon.
Deletion is intentionally the one exception because that daemon has no API for
removing a repository configuration file.  This class can launch only the
fixed, packaged helper with one validated ``delete <file>`` verb; it never
accepts an executable or arbitrary argument list from the UI.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import Callable

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

from core.dnf5daemon import Dnf5DaemonValidationError
from core.operation_gate import HostOperationGate


DEFAULT_HELPER_PATH = "/usr/libexec/dendro-repo-helper"
_ALLOWED_REPOSITORY_DIRECTORIES = (
    PurePosixPath("/etc/yum.repos.d"),
    PurePosixPath("/etc/dnf/repos.d"),
)


def validate_repository_file_path(value: str) -> str:
    """Validate the client-side portion of the helper's narrow contract.

    The privileged helper repeats and strengthens these checks using directory
    file descriptors.  Revalidating here gives the user an immediate, clear
    failure without treating UI data as trustworthy authority.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        raise Dnf5DaemonValidationError("Invalid repository file", "A repository file path must be non-empty text.")
    path = PurePosixPath(value)
    if not path.is_absolute() or path.name in {"", ".", ".."} or path.suffix.casefold() != ".repo":
        raise Dnf5DaemonValidationError(
            "Invalid repository file",
            "Only an absolute .repo file directly inside a supported repository directory can be removed.",
        )
    if path.parent not in _ALLOWED_REPOSITORY_DIRECTORIES:
        raise Dnf5DaemonValidationError(
            "Invalid repository file",
            "Dendro can delete only files directly in /etc/yum.repos.d or /etc/dnf/repos.d.",
        )
    return str(path)


class RepositoryDeletionRunner(QObject):
    """Event-driven process lifecycle for the fixed privileged helper."""

    log_received = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(
        self,
        *,
        helper_path: str = DEFAULT_HELPER_PATH,
        flatpak_detector: Callable[[], bool] | None = None,
        host_operation_gate: HostOperationGate | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._helper_path = helper_path
        self._flatpak_detector = flatpak_detector or (lambda: os.path.exists("/.flatpak-info"))
        self._host_operation_gate = host_operation_gate
        self._host_operation_owner = "repository-file-deletion"
        self._process: QProcess | None = None
        self._finished = False
        self._gate_claimed = False

    @property
    def is_running(self) -> bool:
        # QProcess may briefly still report NotRunning after start() was called.
        # Ownership, rather than that transitional process state, defines the
        # operation lifecycle and prevents a duplicate helper launch.
        return self._process is not None and not self._finished

    def delete(self, repository_file: str) -> None:
        path = validate_repository_file_path(repository_file)
        if self.is_running:
            raise Dnf5DaemonValidationError(
                "Repository deletion already active",
                "Wait for the active repository-file deletion to finish before starting another one.",
            )
        if self._flatpak_detector():
            raise Dnf5DaemonValidationError(
                "Repository deletion is unavailable in Flatpak",
                "Flatpak Dendro can use the narrowly permitted DNF5 system service, but it cannot launch a host privileged helper.",
            )
        if not os.path.isfile(self._helper_path) or not os.access(self._helper_path, os.X_OK):
            raise Dnf5DaemonValidationError(
                "Repository deletion helper is unavailable",
                f"The RPM-host helper is not installed or executable: {self._helper_path}",
            )

        self._claim_host_operation()
        self._finished = False
        try:
            process = QProcess(self)
            process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            environment = QProcessEnvironment.systemEnvironment()
            for variable in (
                "LD_LIBRARY_PATH",
                "PYTHONPATH",
                "PYTHONHOME",
                "QT_PLUGIN_PATH",
                "QT_QPA_PLATFORM_PLUGIN_PATH",
            ):
                environment.remove(variable)
            process.setProcessEnvironment(environment)
            process.readyReadStandardOutput.connect(self._read_output)
            process.errorOccurred.connect(self._on_process_error)
            process.finished.connect(self._on_process_finished)
            self._process = process

            if os.geteuid() == 0:
                self.log_received.emit("Deleting repository configuration with the fixed Dendro helper…\n")
                process.start(self._helper_path, ["delete", path])
            else:
                self.log_received.emit("Requesting authorization for the fixed Dendro repository helper…\n")
                process.start("pkexec", [self._helper_path, "delete", path])
        except Exception as exc:
            # QProcess construction and signal wiring are local UI work. Turn
            # an unexpected failure into the same terminal lifecycle result as
            # a child-process error, and always release the shared operation
            # reservation.
            self._finish(False, f"Could not start repository helper: {type(exc).__name__}: {exc}")

    def _claim_host_operation(self) -> None:
        if self._host_operation_gate is None:
            return
        conflict = self._host_operation_gate.claim(
            self._host_operation_owner,
            "repository-file deletion",
        )
        if conflict is not None:
            raise Dnf5DaemonValidationError(
                "Another host package operation is active",
                f"Cannot delete a repository file while {conflict.label} is active.",
            )
        self._gate_claimed = True

    def _release_host_operation(self) -> None:
        if self._host_operation_gate is None or not self._gate_claimed:
            return
        self._host_operation_gate.release(self._host_operation_owner)
        self._gate_claimed = False

    def cancel(self) -> None:
        """Request graceful termination; no forced kill is used for this helper."""
        if not self.is_running or self._process is None:
            return
        self.log_received.emit("Requesting cancellation of repository-file deletion…\n")
        self._process.terminate()

    def _read_output(self) -> None:
        process = self._process
        if process is None:
            return
        data = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if data:
            self.log_received.emit(data)

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        process = self._process
        detail = process.errorString() if process is not None else str(error)
        self._finish(False, f"Repository helper process error: {detail}")

    def _on_process_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        process = self._process
        if process is not None:
            self._read_output()
        if exit_status != QProcess.ExitStatus.NormalExit:
            self._finish(False, "Repository helper terminated unexpectedly.")
            return
        if exit_code != 0:
            self._finish(False, f"Repository helper exited with status {exit_code}.")
            return
        self._finish(True, "")

    def _finish(self, success: bool, detail: str) -> None:
        if self._finished:
            return
        self._finished = True
        process = self._process
        self._process = None
        if process is not None:
            process.deleteLater()
        self._release_host_operation()
        self.finished.emit(success, detail)
