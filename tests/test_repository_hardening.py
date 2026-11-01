"""Regression tests for repository-file hardening found during the 2.15 review."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from core import repository_addition
from core.repository_addition import (
    NETWORK_TOTAL_TIMEOUT_SECONDS,
    RepositoryConfigDownloadError,
    _download_repository_configuration,
)
from core.repository_deletion import describe_helper_exit


def _load_helper() -> ModuleType:
    path = Path(__file__).parents[1] / "tools" / "dendro-repo-helper.py"
    spec = importlib.util.spec_from_file_location("dendro_repo_hardening_helper", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# pkexec exit-status explanations
# ---------------------------------------------------------------------------


def test_dismissed_authorization_is_explained_instead_of_a_raw_status() -> None:
    message = describe_helper_exit(126)

    assert "cancelled" in message
    assert "126" not in message


def test_unauthorized_pkexec_exit_is_explained_instead_of_a_raw_status() -> None:
    message = describe_helper_exit(127)

    assert "not granted" in message
    assert "127" not in message


def test_other_helper_failures_keep_their_numeric_status() -> None:
    assert describe_helper_exit(2) == "Repository helper exited with status 2."


# ---------------------------------------------------------------------------
# Repository-file deletion of a symbolic link
# ---------------------------------------------------------------------------


def test_helper_removes_a_root_owned_symlink_without_touching_its_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = _load_helper()
    target = tmp_path / "real.repo"
    target.write_text("[real]\nname=Real\n", encoding="utf-8")
    link = tmp_path / "linked.repo"
    link.symlink_to(target.name)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_stat = helper.os.stat

    def root_owned_lstat(*args, **kwargs):
        status = real_stat(*args, **kwargs)
        return SimpleNamespace(st_mode=status.st_mode, st_uid=0)

    helper_os = SimpleNamespace(
        O_DIRECTORY=helper.os.O_DIRECTORY,
        O_NOFOLLOW=getattr(helper.os, "O_NOFOLLOW", 0),
        O_RDONLY=helper.os.O_RDONLY,
        close=helper.os.close,
        geteuid=lambda: 0,
        stat=root_owned_lstat,
        unlink=helper.os.unlink,
    )
    monkeypatch.setattr(helper, "_ALLOWED_DIRECTORIES", (str(tmp_path),))
    monkeypatch.setattr(helper, "_open_trusted_directory", lambda _d: directory_fd)
    monkeypatch.setattr(helper, "os", helper_os)

    # The helper owns and closes the descriptor it receives.
    assert helper.delete_repository_file(str(link)) == 0

    assert not os.path.lexists(link)
    assert target.read_text(encoding="utf-8") == "[real]\nname=Real\n"


def test_helper_still_refuses_a_group_writable_regular_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    helper = _load_helper()
    target = tmp_path / "loose.repo"
    target.write_text("[loose]\nname=Loose\n", encoding="utf-8")
    target.chmod(0o664)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_stat = helper.os.stat

    def root_owned_stat(*args, **kwargs):
        status = real_stat(*args, **kwargs)
        return SimpleNamespace(st_mode=status.st_mode, st_uid=0)

    helper_os = SimpleNamespace(
        O_DIRECTORY=helper.os.O_DIRECTORY,
        O_NOFOLLOW=getattr(helper.os, "O_NOFOLLOW", 0),
        O_RDONLY=helper.os.O_RDONLY,
        close=helper.os.close,
        geteuid=lambda: 0,
        stat=root_owned_stat,
        unlink=helper.os.unlink,
    )
    monkeypatch.setattr(helper, "_ALLOWED_DIRECTORIES", (str(tmp_path),))
    monkeypatch.setattr(helper, "_open_trusted_directory", lambda _d: directory_fd)
    monkeypatch.setattr(helper, "os", helper_os)

    # The helper owns and closes the descriptor it receives.
    assert helper.delete_repository_file(str(target)) == 1

    assert target.exists()
    assert "writable by a non-root group or user" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Whole-download deadline for untrusted repository configuration
# ---------------------------------------------------------------------------


class _TricklingResponse:
    """Yields one byte per read, forever, like a slow-drip server."""

    def __init__(self) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def geturl(self) -> str:
        return "https://repo.example.org/dendro.repo"

    def read(self, _size: int) -> bytes:
        return b"x"


class _TricklingOpener:
    def open(self, _request, timeout: int):
        assert timeout == repository_addition.NETWORK_TIMEOUT_SECONDS
        return _TricklingResponse()


def test_slow_drip_download_is_stopped_by_the_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1000.0}

    def fake_monotonic() -> float:
        # Each read advances time by ten seconds, so the deadline is reached
        # long before the 256 KiB size limit.
        clock["now"] += 10.0
        return clock["now"]

    monkeypatch.setattr(repository_addition, "_monotonic", fake_monotonic)
    monkeypatch.setattr(
        repository_addition, "build_opener", lambda *_handlers: _TricklingOpener()
    )

    import threading

    with pytest.raises(RepositoryConfigDownloadError) as excinfo:
        _download_repository_configuration(
            "https://repo.example.org/dendro.repo", threading.Event()
        )

    assert excinfo.value.title == "Repository download failed"
    assert f"within {NETWORK_TOTAL_TIMEOUT_SECONDS} seconds" in excinfo.value.detail
