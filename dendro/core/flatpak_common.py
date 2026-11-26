# dendro/core/flatpak_common.py
"""Shared pure-stdlib helpers and constants for ``core.flatpak_*`` modules.

Centralizes the fixed Flatpak binary path, identifier validation, stderr excerpt
sanitization, and standardized subprocess error translation without pulling Qt or
RPM backend dependencies.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from typing import Final

FLATPAK_BINARY: Final[str] = "/usr/bin/flatpak"
MAX_STDERR_EXCERPT_CHARS: Final[int] = 300

APP_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+){2,}"
)
ARCH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9_]{1,32}")
BRANCH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]{1,64}")

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


class FlatpakUninstallError(Exception):
    """A user-presentable failure while changing a Flatpak app or runtime."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def flatpak_binary_present() -> bool:
    """Return True when ``/usr/bin/flatpak`` exists and is executable."""
    return os.path.isfile(FLATPAK_BINARY) and os.access(FLATPAK_BINARY, os.X_OK)


def sanitize_stderr_excerpt(
    raw: bytes, *, max_chars: int = MAX_STDERR_EXCERPT_CHARS
) -> str:
    """Normalize and bound untrusted stderr output for status messages."""
    text = raw.decode("utf-8", errors="replace")
    cleaned = "".join(ch if ch.isprintable() or ch == " " else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:max_chars]


def validate_flatpak_ref(
    kind: str,
    application_id: str,
    arch: str,
    branch: str,
    actual_ref: str,
) -> str:
    """Validate Flatpak ref components strictly and return ``<kind>/<id>/<arch>/<branch>``."""
    noun = "application" if kind == "app" else "runtime"
    if not APP_ID_PATTERN.fullmatch(application_id):
        raise ValueError(f"Flatpak {noun} ID is not a valid reverse-DNS name.")
    if not ARCH_PATTERN.fullmatch(arch):
        raise ValueError("Flatpak architecture is not a valid identifier.")
    if not BRANCH_PATTERN.fullmatch(branch):
        raise ValueError("Flatpak branch is not a valid identifier.")
    expected_ref = f"{kind}/{application_id}/{arch}/{branch}"
    if actual_ref != expected_ref:
        raise ValueError(f"Flatpak ref does not match its {noun}, arch, and branch.")
    return expected_ref


def run_flatpak_action_subprocess(
    command: list[str],
    *,
    timeout: float,
    env: dict[str, str],
    action_noun: str,
    target_name: str,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[bytes]:
    """Execute one Flatpak action command with uniform timeout/OSError/stderr handling."""
    try:
        result = runner(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise FlatpakUninstallError(
            f"Flatpak did not finish {action_noun} {target_name} within "
            f"{int(timeout)} seconds. Refresh the list to see its current state."
        ) from error
    except OSError as error:
        raise FlatpakUninstallError(
            f"Could not start Flatpak: {type(error).__name__}. Nothing was changed."
        ) from error

    if result.returncode != 0:
        excerpt = sanitize_stderr_excerpt(result.stderr or b"")
        suffix = f" {excerpt}" if excerpt else ""
        verb = action_noun.removeprefix("the ").removesuffix(" of").strip()
        if verb == "removing":
            verb = "remove"
        raise FlatpakUninstallError(
            f"Flatpak could not {verb} {target_name} "
            f"(status {result.returncode}).{suffix}"
        )
    return result


__all__ = (
    "APP_ID_PATTERN",
    "ARCH_PATTERN",
    "BRANCH_PATTERN",
    "FLATPAK_BINARY",
    "MAX_STDERR_EXCERPT_CHARS",
    "FlatpakUninstallError",
    "Runner",
    "flatpak_binary_present",
    "run_flatpak_action_subprocess",
    "sanitize_stderr_excerpt",
    "validate_flatpak_ref",
)
