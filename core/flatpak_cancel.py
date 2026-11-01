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

import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable

from core.flatpak_uninstall import FlatpakUninstallError

_log = logging.getLogger(__name__)

CANCEL_KILL_AFTER_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.25
_READ_CHUNK_BYTES = 4096
_LINE_SPLIT = re.compile(rb"[\r\n]")

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

    def __init__(
        self,
        *,
        kill_after: float = CANCEL_KILL_AFTER_SECONDS,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        self._kill_after = kill_after
        # Called on a reader thread, once per line of stdout, while the command runs.
        self._on_line = on_line
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._cancel_requested_at: float | None = None

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested_at is not None

    def set_on_line(self, handler: Callable[[str], None] | None) -> None:
        """Set the line callback. Call it before the command starts, not while it runs."""
        self._on_line = handler

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
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        readers = [
            threading.Thread(
                target=_pump_stdout,
                args=(process.stdout, stdout_chunks, self._on_line),
                daemon=True,
            ),
            threading.Thread(
                target=_drain, args=(process.stderr, stderr_chunks), daemon=True
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            self._wait(process, command, timeout, deadline)
        finally:
            with self._lock:
                self._process = None
                cancelled = self._cancel_requested_at is not None
            for reader in readers:
                reader.join(timeout=5)
        if cancelled and process.returncode != 0:
            raise FlatpakCancelled()
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            b"".join(stdout_chunks),
            b"".join(stderr_chunks),
        )

    def _wait(
        self,
        process: subprocess.Popen[bytes],
        command: list[str],
        timeout: float | None,
        deadline: float | None,
    ) -> None:
        while True:
            try:
                process.wait(timeout=_POLL_INTERVAL_SECONDS)
                return
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            with self._lock:
                requested_at = self._cancel_requested_at
            if requested_at is not None and now - requested_at >= self._kill_after:
                process.kill()
            if deadline is not None and now >= deadline:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(command, timeout)


def _pump_stdout(
    stream,
    chunks: list[bytes],
    on_line: Callable[[str], None] | None,
) -> None:
    """Keep every byte of stdout, and hand each complete line to ``on_line`` as it arrives.

    Flatpak ends progress updates with a newline, but a carriage return is accepted too,
    so both count as line ends. A failing callback is logged and does not stop the run.
    """
    pending = b""
    fd = stream.fileno()
    while True:
        data = os.read(fd, _READ_CHUNK_BYTES)
        if not data:
            break
        chunks.append(data)
        if on_line is None:
            continue
        pending += data
        *complete, pending = _LINE_SPLIT.split(pending)
        for raw in complete:
            _emit(on_line, raw)
    if on_line is not None and pending:
        _emit(on_line, pending)


def _emit(on_line: Callable[[str], None], raw: bytes) -> None:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return
    try:
        on_line(text)
    except Exception:  # noqa: BLE001 - a display callback must never break the command
        _log.exception("Flatpak output callback failed")


def _drain(stream, chunks: list[bytes]) -> None:
    fd = stream.fileno()
    while True:
        data = os.read(fd, _READ_CHUNK_BYTES)
        if not data:
            break
        chunks.append(data)


__all__ = (
    "CANCELLED_DETAIL",
    "CancellableRunner",
    "FlatpakCancelled",
)
