# dendro/core/process_utils.py
"""Pure-stdlib runtime sandbox, clean-environment, and subprocess-error helpers.

Kept intentionally free of Qt, D-Bus, ``librpm``, and ``sqlite3`` imports so
leaf modules (such as Flatpak workers and test harnesses) never pull heavy
native or bus dependencies into pure-Python processes.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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
_APPRUN_PRESERVED_VARS: Final[tuple[str, ...]] = (
    "PATH",
    "LD_LIBRARY_PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
)
_APPIMAGE_RUNTIME_VARS: Final[tuple[str, ...]] = (
    "APPDIR",
    "APPIMAGE",
    "ARGV0",
    "OWD",
    "DENDRO_APPRUN_ENV_SAVED",
    "DENDRO_APPDIR",
)
_QT_LOADER_PATH_VARS: Final[frozenset[str]] = frozenset(
    {
        "LD_LIBRARY_PATH",
        "LD_LIBRARY_PATH_ORIG",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
    }
)
_BUNDLE_SCRUBBED_PATH_VARS: Final[tuple[str, ...]] = (
    "PATH",
    "LD_LIBRARY_PATH",
    "LD_LIBRARY_PATH_ORIG",
    "PYTHONPATH",
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "GI_TYPELIB_PATH",
    "GIO_MODULE_DIR",
    "GSETTINGS_SCHEMA_DIR",
    "XDG_DATA_DIRS",
)
_ORIG_ENV_PREFIX: Final[str] = "DENDRO_ORIG_"
_BUNDLED_PYQT6_QT6_FRAGMENT: Final[str] = f"PyQt6{os.sep}Qt6"
_HOST_ENV_LOCK: Final[threading.RLock] = threading.RLock()


def is_running_in_flatpak() -> bool:
    """Detect whether Dendro runs inside a Flatpak sandbox."""
    return os.path.exists(FLATPAK_INFO_PATH)


def flatpak_read_only_unavailable(feature: str) -> str:
    """Explain why a host-RPM or local-Flatpak action is unavailable in Flatpak."""
    return (
        f"{feature} is unavailable in Flatpak. Dendro does not inspect the host RPM database "
        "or run host commands from the sandbox."
    )


def _is_path_within(candidate: str, root: str) -> bool:
    """Return whether ``candidate`` resolves inside ``root``."""
    if not candidate or not root:
        return False
    for normalizer in (os.path.normpath, os.path.realpath):
        root_path = normalizer(root)
        if not root_path or root_path == os.sep:
            continue
        candidate_path = normalizer(candidate)
        try:
            if os.path.commonpath((candidate_path, root_path)) == root_path:
                return True
        except ValueError:
            continue
    return False


def _is_bundled_qt_entry(entry: str) -> bool:
    """Return whether ``entry`` points into a wheel/bundle ``PyQt6/Qt6`` tree."""
    if not entry:
        return False
    normalized = os.path.normpath(entry)
    return _BUNDLED_PYQT6_QT6_FRAGMENT in normalized


def _scrub_path_var(
    env: dict[str, str],
    var: str,
    appdir: str,
    *,
    strip_bundled_qt: bool,
) -> None:
    """Remove bundle paths from ``env[var]`` without rewriting clean values."""
    raw = env.get(var)
    if raw is None:
        return
    entries = raw.split(os.pathsep)
    kept: list[str] = []
    removed = False
    for entry in entries:
        if not entry:
            continue
        if _is_path_within(entry, appdir) or (
            strip_bundled_qt and _is_bundled_qt_entry(entry)
        ):
            removed = True
            continue
        kept.append(entry)

    if not removed:
        return
    if kept:
        env[var] = os.pathsep.join(kept)
    elif var == "PATH":
        env[var] = os.defpath
    else:
        env.pop(var, None)


def has_bundled_desktop_env(source_env: Mapping[str, str] | None = None) -> bool:
    """Return whether AppImage or bundled Qt/Python overrides are present."""
    env = os.environ if source_env is None else source_env
    if env.get("DENDRO_APPRUN_ENV_SAVED") == "1":
        return True
    if any(key in env for key in ("APPDIR", "APPIMAGE", "DENDRO_APPDIR")):
        return True
    for var in _QT_LOADER_PATH_VARS:
        raw = env.get(var, "")
        if any(_is_bundled_qt_entry(entry) for entry in raw.split(os.pathsep) if entry):
            return True
    return False


def get_host_desktop_env(
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment restored for host desktop URL and app handoffs."""
    env = dict(os.environ if source_env is None else source_env)
    appdir = env.get("DENDRO_APPDIR") or env.get("APPDIR") or ""
    saved_by_apprun = env.get("DENDRO_APPRUN_ENV_SAVED") == "1"

    if saved_by_apprun:
        for var in _APPRUN_PRESERVED_VARS:
            orig_key = f"{_ORIG_ENV_PREFIX}{var}"
            if orig_key in env:
                env[var] = env.pop(orig_key)
            else:
                env.pop(var, None)
    elif appdir or "APPIMAGE" in env:
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)

    for marker in _APPIMAGE_RUNTIME_VARS:
        env.pop(marker, None)
    for key in list(env):
        if key.startswith(_ORIG_ENV_PREFIX):
            env.pop(key, None)

    pythonhome = env.get("PYTHONHOME", "")
    if pythonhome and _is_path_within(pythonhome, appdir):
        env.pop("PYTHONHOME", None)

    for var in _BUNDLE_SCRUBBED_PATH_VARS:
        _scrub_path_var(
            env,
            var,
            appdir,
            strip_bundled_qt=var in _QT_LOADER_PATH_VARS,
        )

    return env


def host_desktop_env_delta(
    source_env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Return ``(unset_keys, set_items)`` transforming ``source_env`` to host env."""
    current = os.environ if source_env is None else source_env
    target = get_host_desktop_env(current)
    unset_keys = tuple(sorted(k for k in current if k not in target))
    changed = sorted((k, v) for k, v in target.items() if current.get(k) != v)
    return unset_keys, tuple(changed)


@contextmanager
def restored_host_desktop_env() -> Iterator[None]:
    """Temporarily restore the pre-AppImage host environment around desktop handoffs."""
    with _HOST_ENV_LOCK:
        if not has_bundled_desktop_env():
            yield
            return

        previous_env = os.environ.copy()
        clean_env = get_host_desktop_env(previous_env)
        for key in list(os.environ):
            if key not in clean_env:
                del os.environ[key]
        for key, value in clean_env.items():
            if os.environ.get(key) != value:
                os.environ[key] = value
        try:
            yield
        finally:
            for key in list(os.environ):
                if key not in previous_env:
                    del os.environ[key]
            for key, value in previous_env.items():
                if os.environ.get(key) != value:
                    os.environ[key] = value


def get_clean_env() -> dict[str, str]:
    """Return an environment safe for host RPM, DNF, and Flatpak subprocesses."""
    env = get_host_desktop_env() if has_bundled_desktop_env() else os.environ.copy()
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
    "get_host_desktop_env",
    "has_bundled_desktop_env",
    "host_desktop_env_delta",
    "is_running_in_flatpak",
    "restored_host_desktop_env",
)
