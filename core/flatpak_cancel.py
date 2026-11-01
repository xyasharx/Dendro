# dendro/core/flatpak_cancel.py
"""
Run one Flatpak command that the user can cancel while it is running.

A system-wide Flatpak change can wait for a Polkit prompt or a long download. The
user must be able to stop that wait. ``CancellableRunner`` has the same call shape
as ``subprocess.run`` (so tests and the existing code keep working), and it adds
``cancel()``, which can be called from the UI thread while the worker waits.

What cancelling does and does not do:
- It stops the local ``flatpak`` process: SIGTERM first, then SIGKILL if the process
  ignores it for ``kill_after`` seconds.
- It cannot undo anything Flatpak's privileged helper has already started. So a
  cancelled run raises ``FlatpakCancelled`` with a message that says the change may
  be partly applied or may still finish, and that the list must be refreshed.
- If the command finished before the cancel took effect, its real result is used.
  A successful run is reported as a success, never as a cancel.
"""

from __future__ import annotations

import subprocess
import threading
import time

from core.flatpak_uninstall import FlatpakUninstallError

CANCEL_KILL_AFTER_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.25

CANCELLED_DETAIL = (
    "Cancelled. Flatpak was stopped before it reported a result, so the change may be "
    "partly applied or may still finish. Refresh the list to see the current state."
)


class FlatpakCancelled(FlatpakUninstallError):
    """The user cancelled a Flatpak command before it reported a result."""

    def __init__(self) -> None:
        super().__init__(CANCELLED_DETAIL)


class CancellableRunner:
    """A ``subprocess.run`` replacement that the UI thread can cancel.

    Use one instance per operation. Only ``__call__`` runs on a worker thread, and
    only ``cancel`` is called from the UI thread.
    """

    def __init__(self, *, kill_after: float = CANCEL_KILL_AFTER_SECONDS) -> None:
        self._kill_after = kill_after
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._cancel_requested_at: float | None = None

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested_at is not None

    def cancel(self) -> None:
        """Ask the running command to stop. Safe to call at any time, any number of times."""
        with self._lock:
            if self._cancel_requested_at is None:
                self._cancel_requested_at = time.monotonic()
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def __call__(
        self,
        command: list[str],
        *,
        stdin=None,
        capture_output: bool = True,  # always captured; accepted for signature parity
        check: bool = False,  # the caller reads returncode itself
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        with self._lock:
            if self._cancel_requested_at is not None:
                raise FlatpakCancelled()
        process = subprocess.Popen(
            command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        with self._lock:
            self._process = process
            # A cancel that arrived while the process was starting must still stop it.
            already_cancelled = self._cancel_requested_at is not None
        if already_cancelled:
            process.terminate()
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            stdout, stderr = self._wait(process, command, timeout, deadline)
        finally:
            with self._lock:
                self._process = None
                cancelled = self._cancel_requested_at is not None
        if cancelled and process.returncode != 0:
            raise FlatpakCancelled()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _wait(
        self,
        process: subprocess.Popen[bytes],
        command: list[str],
        timeout: float | None,
        deadline: float | None,
    ) -> tuple[bytes, bytes]:
        while True:
            try:
                return process.communicate(timeout=_POLL_INTERVAL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            with self._lock:
                requested_at = self._cancel_requested_at
            if requested_at is not None and now - requested_at >= self._kill_after:
                process.kill()
            if deadline is not None and now >= deadline:
                process.kill()
                process.communicate()
                raise subprocess.TimeoutExpired(command, timeout)


__all__ = (
    "CANCELLED_DETAIL",
    "CancellableRunner",
    "FlatpakCancelled",
)
