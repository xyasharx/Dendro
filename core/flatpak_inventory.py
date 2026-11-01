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

Output format: ``flatpak list --app|--runtime --columns=...`` writes one
tab-separated row per installed ref when stdout is not a terminal, in the
column order requested, with no header row. The ``ref`` column holds the
partial ref (``id/arch/branch``), so the kind comes from which listing ran.
``--json`` and ``--bytes`` are not used because some Flatpak versions reject them.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Optional

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

# Requested column order. Parsing depends on it, so it lives in one place.
_COLUMNS: tuple[str, ...] = (
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
_KINDS: tuple[str, ...] = ("app", "runtime")
# Flatpak formats sizes with decimal units; the separator before the unit is not
# stable across builds and locales, so it is matched loosely.
_SIZE_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)[^0-9A-Za-z]{0,2}(B|kB|MB|GB|TB)$")
_UNIT_FACTORS = {"B": 1, "kB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}


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
    installed_size_text: str  # as printed by Flatpak, shown to the user
    installed_size_bytes: Optional[int]  # used only for sorting; None if unknown
    runtime: str


@dataclass(frozen=True)
class FlatpakInventory:
    entries: tuple[FlatpakEntry, ...]


def build_flatpak_list_command(kind: str) -> list[str]:
    """Return the exact argv listing one kind (no shell, fixed binary)."""
    if kind not in _KINDS:
        raise ValueError(f"unknown Flatpak kind: {kind!r}")
    return [FLATPAK_BINARY, "list", f"--{kind}", f"--columns={','.join(_COLUMNS)}"]


def _parse_size_bytes(text: str) -> Optional[int]:
    match = _SIZE_PATTERN.match(text)
    if match is None:
        return None
    number, unit = match.groups()
    return round(float(number) * _UNIT_FACTORS[unit])


def _excerpt_line(line: str) -> str:
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in line)
    return cleaned[:80]


def _parse_line(line: str, kind: str, index: int) -> FlatpakEntry:
    fields = line.split("\t")
    # Some Flatpak builds omit the trailing, empty "runtime" cell (apps and
    # runtimes without a runtime column). Only that trailing cell may be missing;
    # the ref cross-check below still rejects any other misalignment.
    if len(fields) == len(_COLUMNS) - 1 and _COLUMNS[-1] == "runtime":
        fields.append("")
    if len(fields) != len(_COLUMNS):
        raise FlatpakInventoryError(
            f"Flatpak output line {index} has {len(fields)} fields; expected "
            f"{len(_COLUMNS)} (line starts: {_excerpt_line(line)!r})."
        )
    row = dict(zip(_COLUMNS, fields, strict=True))

    partial_ref = row["ref"]
    ref_parts = partial_ref.split("/")
    if len(ref_parts) != 3 or not all(ref_parts):
        raise FlatpakInventoryError(f"Flatpak output line {index} has a malformed ref.")
    ref_app_id, ref_arch, ref_branch = ref_parts
    if (ref_app_id, ref_arch, ref_branch) != (
        row["application"],
        row["arch"],
        row["branch"],
    ):
        raise FlatpakInventoryError(
            f"Flatpak output line {index} has columns that disagree with its ref."
        )
    if not row["installation"]:
        raise FlatpakInventoryError(f"Flatpak output line {index} has no installation.")

    size_text = row["size"]
    return FlatpakEntry(
        kind=kind,
        name=row["name"] or row["application"],
        application_id=row["application"],
        version=row["version"],
        branch=row["branch"],
        arch=row["arch"],
        origin=row["origin"],
        installation=row["installation"],
        ref=f"{kind}/{partial_ref}",
        installed_size_text=size_text,
        installed_size_bytes=_parse_size_bytes(size_text),
        runtime=row["runtime"],
    )


def parse_flatpak_list_output(text: str, kind: str) -> tuple[FlatpakEntry, ...]:
    """Parse ``flatpak list --<kind> --columns=...`` output strictly.

    Flatpak prints nothing when there are no rows, so empty or whitespace-only
    text means an empty inventory.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown Flatpak kind: {kind!r}")
    if not text.strip():
        return ()
    lines = [line for line in text.split("\n") if line.strip()]
    entries = tuple(
        _parse_line(line, kind, index) for index, line in enumerate(lines, start=1)
    )
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

    entries: list[FlatpakEntry] = []
    for kind in _KINDS:
        text = _run_flatpak_list(kind, runner=runner, timeout=timeout)
        entries.extend(parse_flatpak_list_output(text, kind))

    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.installation, entry.ref)
        if key in seen:
            raise FlatpakInventoryError(
                f"Flatpak output lists {entry.ref} twice in one installation."
            )
        seen.add(key)
    return FlatpakInventory(entries=tuple(entries))


def _run_flatpak_list(kind: str, *, runner: Runner, timeout: float) -> str:
    command = build_flatpak_list_command(kind)
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
        return stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FlatpakInventoryError("Flatpak output was not valid UTF-8.") from error


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
    "parse_flatpak_list_output",
)
