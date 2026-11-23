# dendro/core/process_utils.py
"""Pure-stdlib runtime sandbox, clean-environment, and subprocess-error helpers.

Kept intentionally free of Qt, D-Bus, ``librpm``, and ``sqlite3`` imports so
leaf modules (such as Flatpak workers and test harnesses) never pull heavy
native or bus dependencies into pure-Python processes.
"""

from __future__ import annotations

import os
import subprocess
from typing import Final

FLATPAK_INFO_PATH: Final[str] = "/.flatpak-info"
_UNSAFE_CHILD_ENV_VARS: Final[tuple[str, ...]] = (
    "LD_AUDIT",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PYTHONPATH",
    "PYTHONHOME",
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
)


def is_running_in_flatpak() -> bool:
    """Detect whether Dendro runs inside a Flatpak sandbox."""
    return os.path.exists(FLATPAK_INFO_PATH)


def flatpak_read_only_unavailable(feature: str) -> str:
    """Explain why a host-RPM or local-Flatpak action is unavailable in Flatpak."""
    return (
        f"{feature} is unavailable in Flatpak. Dendro does not inspect the host RPM database "
        "or run host commands from the sandbox."
    )


def get_clean_env() -> dict[str, str]:
    """Return an environment safe for host RPM, DNF, and Flatpak subprocesses."""
    env = os.environ.copy()
    for variable in _UNSAFE_CHILD_ENV_VARS:
        env.pop(variable, None)
    return env


def describe_subprocess_failure(exc: BaseException, command: str = "") -> str:
    """Return a user-facing reason for a failed host command."""
    tool = command or "the command"
    if isinstance(exc, FileNotFoundError):
        return f"{tool} is not installed on this system"
    if isinstance(exc, PermissionError):
        return f"permission denied while starting {tool}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"{tool} did not finish in time"
    detail = str(exc).strip()
    return detail or type(exc).__name__


__all__ = (
    "FLATPAK_INFO_PATH",
    "describe_subprocess_failure",
    "flatpak_read_only_unavailable",
    "get_clean_env",
    "is_running_in_flatpak",
)
