# dendro/core/flatpak_versions.py
"""
Read one installed app's version, so an update can report what really changed.

Flatpak exits successfully when an update finds nothing new, so a zero exit code
alone cannot tell the user whether anything changed. This module compares the
version before and after the update and writes a message that says only what is
known. When a version cannot be read, the message makes no claim about the result.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from core.backend import get_clean_env
from core.flatpak_inventory import FLATPAK_BINARY, FlatpakEntry

VERSION_READ_TIMEOUT_SECONDS = 30
USER_SCOPE = "--user"
SYSTEM_SCOPE = "--system"
_SCOPES = (USER_SCOPE, SYSTEM_SCOPE)
_VERSION_COLUMNS = "application,arch,branch,version"

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def read_installed_version(
    entry: FlatpakEntry,
    *,
    scope_flag: str,
    runner: Runner = subprocess.run,
) -> str | None:
    """Return the installed version of ``entry`` in one scope, or ``None`` if unknown.

    Never raises: a failed read only means the message cannot state a version.
    """
    if scope_flag not in _SCOPES:
        raise ValueError(f"unknown Flatpak scope: {scope_flag!r}")
    try:
        result = runner(
            [
                FLATPAK_BINARY,
                scope_flag,
                "list",
                "--app",
                f"--columns={_VERSION_COLUMNS}",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=VERSION_READ_TIMEOUT_SECONDS,
            env=get_clean_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    text = (result.stdout or b"").decode("utf-8", errors="replace")
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        application, arch, branch, version = fields
        if (application, arch, branch) == (
            entry.application_id,
            entry.arch,
            entry.branch,
        ):
            return version or None
    return None


def update_summary(name: str, before: str | None, after: str | None) -> str:
    """Plain-language result of an update, stating only what the versions show."""
    if before is None or after is None:
        return f"Flatpak finished the update of {name}."
    if before == after:
        return (
            f"Flatpak finished, but {name} is still at version {after}, "
            "so no newer version was installed."
        )
    return f"Updated {name} from version {before} to {after}."


__all__ = (
    "SYSTEM_SCOPE",
    "USER_SCOPE",
    "read_installed_version",
    "update_summary",
)
