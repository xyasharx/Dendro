# dendro/core/flatpak_inventory.py
"""
Read-only inventory of Flatpak apps and runtimes installed on an RPM host.

Flatpak packages are managed by Flatpak, not by DNF. This module therefore
never mixes them into the RPM package model, categories, or dependency trees.

Safety rules:
- Only the fixed binary ``/usr/bin/flatpak`` is executed, with an argument list
  and no shell.
- The child process gets the sanitized environment from ``get_clean_env``.
- Output is bounded in size and parsed strictly from stdout only; stderr is
  shown as a truncated, sanitized excerpt and never parsed.
- Inside a Flatpak sandbox the feature reports itself unavailable instead of
  bridging to host commands (no ``flatpak-spawn --host``).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot

from core.backend import (
    flatpak_read_only_unavailable,
    get_clean_env,
    is_running_in_flatpak,
)

FLATPAK_BINARY = "/usr/bin/flatpak"
FLATPAK_TIMEOUT_SECONDS = 60
MAX_FLATPAK_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_EXCERPT_CHARS = 300

# Flatpak column title -> JSON key emitted by `flatpak list --json`
# (titles are lowercased with spaces replaced by underscores).
_COLUMN_KEYS: Dict[str, str] = {
    "Name": "name",
    "Application ID": "application_id",
    "Version": "version",
    "Branch": "branch",
    "Arch": "arch",
    "Origin": "origin",
    "Installation": "installation",
    "Ref": "ref",
    "Installed size": "installed_size",
    "Runtime": "runtime",
}
_REQUIRED_KEYS = tuple(key for key in _COLUMN_KEYS.values() if key != "runtime")
_KINDS = ("app", "runtime")


class FlatpakInventoryError(Exception):
    """A user-presentable failure while reading Flatpak installations."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class FlatpakEntry:
    kind: str  # "app" or "runtime"
    name: str
    application_id: str
    version: str
    branch: str
    arch: str
    origin: str
    installation: str
    ref: str
    installed_size_bytes: Optional[int]
    runtime: str


@dataclass(frozen=True)
class FlatpakInventory:
    entries: Tuple[FlatpakEntry, ...]


def build_flatpak_list_command() -> List[str]:
    """Return the exact argv used to list installations (no shell, fixed binary)."""
    columns = ",".join(
        (
            "name",
            "application",
            "version",
            "branch",
            "arch",
            "origin",
            "installation",
            "ref",
            "size",
            "runtime",
        )
    )
    return [FLATPAK_BINARY, "list", "--json", "--bytes", f"--columns={columns}"]


def _require_str(row: Dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise FlatpakInventoryError(
            f"Flatpak output has a non-text '{key}' field; the format is not supported."
        )
    return value


def _parse_size(raw: str) -> Optional[int]:
    if raw == "":
        return None
    if not raw.isascii() or not raw.isdigit():
        raise FlatpakInventoryError(
            f"Flatpak output has an invalid installed size: {raw[:40]!r}."
        )
    return int(raw)


def _parse_row(row: Any, index: int) -> FlatpakEntry:
    if not isinstance(row, dict):
        raise FlatpakInventoryError(f"Flatpak output row {index} is not an object.")
    missing = [key for key in _REQUIRED_KEYS if key not in row]
    if missing:
        raise FlatpakInventoryError(
            f"Flatpak output row {index} is missing fields: {', '.join(missing)}."
        )

    values = {
        key: _require_str(row, key) for key in _REQUIRED_KEYS if key != "installed_size"
    }
    runtime = row.get("runtime", "")
    if not isinstance(runtime, str):
        raise FlatpakInventoryError(
            f"Flatpak output row {index} has a non-text runtime field."
        )
    application_id = values["application_id"]
    ref = values["ref"]

    # A ref has the form <kind>/<application-id>/<arch>/<branch>.
    ref_parts = ref.split("/")
    if len(ref_parts) != 4:
        raise FlatpakInventoryError(f"Flatpak output row {index} has a malformed ref.")
    kind, ref_app_id, ref_arch, ref_branch = ref_parts
    if kind not in _KINDS:
        raise FlatpakInventoryError(
            f"Flatpak output row {index} has an unknown kind in its ref."
        )
    if (ref_app_id, ref_arch, ref_branch) != (
        application_id,
        values["arch"],
        values["branch"],
    ):
        raise FlatpakInventoryError(
            f"Flatpak output row {index} has columns that disagree with its ref."
        )

    installation = values["installation"]
    if not installation:
        raise FlatpakInventoryError(f"Flatpak output row {index} has no installation.")

    return FlatpakEntry(
        kind=kind,
        name=values["name"] or application_id,
        application_id=application_id,
        version=values["version"],
        branch=values["branch"],
        arch=values["arch"],
        origin=values["origin"],
        installation=installation,
        ref=ref,
        installed_size_bytes=_parse_size(_require_str(row, "installed_size")),
        runtime=runtime,
    )


def parse_flatpak_list_json(text: str) -> Tuple[FlatpakEntry, ...]:
    """Parse `flatpak list --json` output strictly.

    Flatpak prints nothing (not ``[]``) when there are no rows, so empty or
    whitespace-only text means an empty inventory.
    """
    if not text.strip():
        return ()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise FlatpakInventoryError(
            f"Flatpak output was not valid JSON (line {error.lineno})."
        ) from error
    if not isinstance(payload, list):
        raise FlatpakInventoryError("Flatpak output was not a JSON array.")

    entries = tuple(_parse_row(row, index) for index, row in enumerate(payload))
    seen = set()
    for entry in entries:
        key = (entry.installation, entry.ref)
        if key in seen:
            raise FlatpakInventoryError(
                f"Flatpak output lists {entry.ref} twice in one installation."
            )
        seen.add(key)
    return entries


def _sanitize_excerpt(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    cleaned = "".join(ch if ch.isprintable() or ch == " " else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_STDERR_EXCERPT_CHARS]


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _flatpak_binary_present() -> bool:
    return os.path.isfile(FLATPAK_BINARY) and os.access(FLATPAK_BINARY, os.X_OK)


def load_flatpak_inventory(
    *,
    runner: Runner = subprocess.run,
    in_sandbox: Callable[[], bool] = is_running_in_flatpak,
    binary_present: Callable[[], bool] = _flatpak_binary_present,
    timeout: float = FLATPAK_TIMEOUT_SECONDS,
) -> FlatpakInventory:
    """Read Flatpak installations. Blocking; call from a worker thread only."""
    if in_sandbox():
        raise FlatpakInventoryError(flatpak_read_only_unavailable("Flatpak inventory"))
    if not binary_present():
        raise FlatpakInventoryError(
            f"Flatpak is not installed at {FLATPAK_BINARY}. Install Flatpak to see Flatpak apps and runtimes."
        )

    command = build_flatpak_list_command()
    try:
        result = runner(
            command,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=get_clean_env(),
        )
    except subprocess.TimeoutExpired as error:
        raise FlatpakInventoryError(
            f"Flatpak did not finish within {int(timeout)} seconds."
        ) from error
    except OSError as error:
        raise FlatpakInventoryError(
            f"Could not start Flatpak: {type(error).__name__}."
        ) from error

    if result.returncode != 0:
        excerpt = _sanitize_excerpt(result.stderr or b"")
        suffix = f" {excerpt}" if excerpt else ""
        raise FlatpakInventoryError(
            f"Flatpak exited with status {result.returncode}.{suffix}"
        )

    stdout = result.stdout or b""
    if len(stdout) > MAX_FLATPAK_OUTPUT_BYTES:
        raise FlatpakInventoryError("Flatpak output was too large to read safely.")
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FlatpakInventoryError("Flatpak output was not valid UTF-8.") from error

    return FlatpakInventory(entries=parse_flatpak_list_json(text))


class FlatpakInventorySignals(QObject):
    """Results carry the request serial so the UI can drop superseded refreshes."""

    loaded = pyqtSignal(int, object)  # request serial, FlatpakInventory
    failed = pyqtSignal(int, str)  # request serial, user-presentable detail


class FlatpakInventoryWorker(QRunnable):
    """Runs the blocking Flatpak query off the UI thread and reports one result."""

    def __init__(
        self,
        request_serial: int,
        loader: Optional[Callable[[], FlatpakInventory]] = None,
    ):
        super().__init__()
        self.request_serial = request_serial
        self.signals = FlatpakInventorySignals()
        self._loader: Callable[[], FlatpakInventory] = loader or load_flatpak_inventory

    @pyqtSlot()
    def run(self) -> None:
        try:
            inventory = self._loader()
        except FlatpakInventoryError as error:
            self.signals.failed.emit(self.request_serial, error.detail)
            return
        except Exception as error:  # noqa: BLE001 - never leave the dialog loading forever
            self.signals.failed.emit(
                self.request_serial,
                f"Unexpected error while reading Flatpak: {type(error).__name__}.",
            )
            return
        self.signals.loaded.emit(self.request_serial, inventory)


__all__: Sequence[str] = (
    "FLATPAK_BINARY",
    "FlatpakEntry",
    "FlatpakInventory",
    "FlatpakInventoryError",
    "FlatpakInventoryWorker",
    "build_flatpak_list_command",
    "load_flatpak_inventory",
    "parse_flatpak_list_json",
)
