# dendro/core/flatpak_runtime_removal.py
"""
Remove a Flatpak runtime that an uninstalled app leaves unused.

Flatpak's own ``--unused`` removes every unused runtime on the computer, which
could include runtimes the user did not mean to touch, and it cannot be previewed.
So Dendro decides itself which runtime may go, and then removes only that exact ref.

A runtime is offered only when all of these hold:
- the app's runtime (from the listing) is installed in the same installation as the
  app, and that runtime is in the per-user or system-wide installation;
- no other installed app or runtime, in any installation, lists it as its runtime.
  Any doubt keeps the runtime.

Removal runs with ``--no-related`` so that Flatpak does not also remove the
runtime's extensions (for example GPU drivers) that other software may use.

Authorization matches the other Flatpak actions. A per-user runtime runs as the
user. A system-wide runtime goes through Flatpak's helper and Polkit, under the
shared host operation gate, and never through ``sudo`` or ``pkexec``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable

from core.backend import (
    flatpak_read_only_unavailable,
    get_clean_env,
    is_running_in_flatpak,
)
from core.flatpak_inventory import FLATPAK_BINARY, FlatpakEntry
from core.flatpak_system_update import (
    _APP_ID_PATTERN,
    _ARCH_PATTERN,
    _BRANCH_PATTERN,
    GATE_OWNER,
    SYSTEM_INSTALLATIONS,
    SYSTEM_MIN_VERSION,
    _flatpak_binary_present,
    _probe_version,
    _sanitize_excerpt,
    format_version,
)
from core.flatpak_uninstall import USER_INSTALLATION, FlatpakUninstallError
from core.operation_gate import HostOperationGate

RUNTIME_REMOVAL_TIMEOUT_SECONDS = 600  # includes a possible administrator prompt

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def runtime_key(entry: FlatpakEntry) -> str:
    """The ``id/arch/branch`` form that an app's listing uses to name its runtime."""
    return f"{entry.application_id}/{entry.arch}/{entry.branch}"


def is_removable_runtime(entry: FlatpakEntry) -> bool:
    """True for runtimes in the per-user or system-wide installation only."""
    return entry.kind == "runtime" and (
        entry.installation == USER_INSTALLATION
        or entry.installation in SYSTEM_INSTALLATIONS
    )


def runtime_for_app(
    app: FlatpakEntry, entries: Iterable[FlatpakEntry]
) -> FlatpakEntry | None:
    """The one runtime, in the app's own installation, that the app names."""
    if app.kind != "app" or not app.runtime:
        return None
    matches = [
        entry
        for entry in entries
        if entry.kind == "runtime"
        and entry.installation == app.installation
        and runtime_key(entry) == app.runtime
    ]
    return matches[0] if len(matches) == 1 else None


def runtime_left_unused(
    app: FlatpakEntry, entries: Iterable[FlatpakEntry]
) -> FlatpakEntry | None:
    """Return the app's runtime if removing ``app`` would leave it unused, else None.

    ``entries`` is the full inventory. Any other entry that names the runtime keeps
    it, whichever installation that entry is in.
    """
    entries = tuple(entries)
    runtime = runtime_for_app(app, entries)
    if runtime is None or not is_removable_runtime(runtime):
        return None
    key = runtime_key(runtime)
    for other in entries:
        if other.installation == app.installation and other.ref == app.ref:
            continue  # the app being removed does not count
        if other.runtime == key:
            return None
    return runtime


def runtime_removal_ref(runtime: FlatpakEntry) -> str:
    """Validate a runtime entry and return its ref (``runtime/<id>/<arch>/<branch>``)."""
    if runtime.kind != "runtime":
        raise ValueError("Only runtimes can be removed here.")
    if not is_removable_runtime(runtime):
        raise ValueError(
            "Only runtimes in the per-user or system-wide Flatpak installation "
            "can be removed here."
        )
    if not _APP_ID_PATTERN.fullmatch(runtime.application_id):
        raise ValueError("Flatpak runtime ID is not a valid reverse-DNS name.")
    if not _ARCH_PATTERN.fullmatch(runtime.arch):
        raise ValueError("Flatpak architecture is not a valid identifier.")
    if not _BRANCH_PATTERN.fullmatch(runtime.branch):
        raise ValueError("Flatpak branch is not a valid identifier.")
    expected = f"runtime/{runtime.application_id}/{runtime.arch}/{runtime.branch}"
    if runtime.ref != expected:
        raise ValueError(
            "Flatpak ref does not match its runtime, arch, and branch."
        )
    return expected


def is_system_runtime(runtime: FlatpakEntry) -> bool:
    return runtime.installation in SYSTEM_INSTALLATIONS


def build_runtime_removal_command(runtime: FlatpakEntry) -> list[str]:
    """Return the exact argv that removes one unused runtime (no shell, no ``--unused``).

    ``--noninteractive`` is omitted for the system-wide case so Polkit can show its
    prompt. ``-y`` only answers Flatpak's own CLI questions.
    """
    ref = runtime_removal_ref(runtime)
    if is_system_runtime(runtime):
        return [FLATPAK_BINARY, "--system", "uninstall", "-y", "--no-related", ref]
    return [
        FLATPAK_BINARY,
        "--user",
        "uninstall",
        "--noninteractive",
        "-y",
        "--no-related",
        ref,
    ]


def describe_runtime_removal(runtime: FlatpakEntry) -> str:
    """Plain-language text for the confirmation, one line per fact."""
    if is_system_runtime(runtime):
        where = (
            "from the system-wide installation, so it is removed for everyone on this "
            "computer. You may be asked for an administrator password."
        )
    else:
        where = "from your user installation."
    return (
        f"The runtime {runtime.application_id} {runtime.branch} ({runtime.arch}) is "
        f"not used by any other installed app. It will be removed {where}"
    )


def run_flatpak_runtime_removal(
    runtime: FlatpakEntry,
    *,
    gate: HostOperationGate | None,
    runner: Runner = subprocess.run,
    in_sandbox: Callable[[], bool] = is_running_in_flatpak,
    binary_present: Callable[[], bool] = _flatpak_binary_present,
    timeout: float | None = None,
) -> str:
    """Remove one unused runtime. Blocking; call from a worker thread only.

    Raises ``FlatpakUninstallError`` with a user-presentable detail on failure.
    Returns the finished message for the status line.
    """
    if in_sandbox():
        raise FlatpakUninstallError(
            flatpak_read_only_unavailable("Flatpak runtime removal")
        )
    if not binary_present():
        raise FlatpakUninstallError(
            f"Flatpak is not installed at {FLATPAK_BINARY}. Nothing was changed."
        )
    try:
        command = build_runtime_removal_command(runtime)
    except ValueError as error:
        raise FlatpakUninstallError(str(error)) from error

    limit = RUNTIME_REMOVAL_TIMEOUT_SECONDS if timeout is None else timeout
    name = f"{runtime.application_id} {runtime.branch}"
    if not is_system_runtime(runtime):
        _execute(command, name, runner=runner, limit=limit)
        return f"Removed the unused runtime {name}."

    if gate is None:
        raise FlatpakUninstallError(
            "Dendro could not confirm that no other system operation is running. "
            "Nothing was changed."
        )
    conflict = gate.claim(
        GATE_OWNER, f"System-wide Flatpak runtime removal of {name}"
    )
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
        _execute(command, name, runner=runner, limit=limit)
    finally:
        gate.release(GATE_OWNER)
    return f"Removed the unused runtime {name} for everyone."


def _execute(
    command: list[str], name: str, *, runner: Runner, limit: float
) -> None:
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
            f"Flatpak did not finish removing {name} within {int(limit)} seconds. "
            "Refresh the list to see its current state."
        ) from error
    except OSError as error:
        raise FlatpakUninstallError(
            f"Could not start Flatpak: {type(error).__name__}. Nothing was changed."
        ) from error
    if result.returncode != 0:
        excerpt = _sanitize_excerpt(result.stderr or b"")
        suffix = f" {excerpt}" if excerpt else ""
        raise FlatpakUninstallError(
            f"Flatpak could not remove {name} (status {result.returncode}).{suffix}"
        )


def uninstall_app_and_runtime(
    remove_app: Callable[[], str],
    runtime: FlatpakEntry | None,
    *,
    gate: HostOperationGate | None,
    runner: Runner = subprocess.run,
) -> str:
    """Remove the app first, then the runtime only if the app removal succeeded.

    If the app removal fails, its ``FlatpakUninstallError`` propagates and the
    runtime is never touched. If the runtime removal fails after the app is gone,
    the message says the app was removed and the runtime was kept.
    """
    message = remove_app()
    if runtime is None:
        return message
    try:
        extra = run_flatpak_runtime_removal(runtime, gate=gate, runner=runner)
    except FlatpakUninstallError as error:
        return f"{message} The runtime was kept: {error.detail}"
    return f"{message} {extra}"


__all__ = (
    "build_runtime_removal_command",
    "describe_runtime_removal",
    "is_removable_runtime",
    "run_flatpak_runtime_removal",
    "runtime_for_app",
    "runtime_key",
    "runtime_left_unused",
    "runtime_removal_ref",
    "uninstall_app_and_runtime",
)
