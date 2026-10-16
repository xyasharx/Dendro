"""Validation tests for the only Dendro-specific privileged helper boundary."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from core.dnf5daemon import Dnf5DaemonValidationError
from core.operation_gate import HostOperationGate
from core.repository_deletion import RepositoryDeletionRunner, validate_repository_file_path


def _load_helper() -> ModuleType:
    path = Path(__file__).parents[1] / "tools" / "dendro-repo-helper.py"
    spec = importlib.util.spec_from_file_location("dendro_repo_helper_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "candidate",
    [
        "/etc/yum.repos.d/third-party.repo",
        "/etc/dnf/repos.d/unicodé-🌳.repo",
    ],
)
def test_client_validation_accepts_only_direct_repo_files(candidate: str) -> None:
    assert validate_repository_file_path(candidate) == candidate


@pytest.mark.parametrize(
    "candidate",
    [
        "relative.repo",
        "/etc/yum.repos.d/nested/third.repo",
        "/etc/yum.repos.d/../dnf/repos.d/third.repo",
        "/etc/yum.repos.d/not-a-repository.conf",
        "/etc/yum.repos.d/\x00bad.repo",
        "/usr/lib/dnf/repos.d/third.repo",
    ],
)
def test_client_validation_rejects_path_traversal_and_outside_directories(candidate: str) -> None:
    with pytest.raises(Dnf5DaemonValidationError):
        validate_repository_file_path(candidate)


def test_helper_target_validation_requires_normalized_direct_path() -> None:
    helper = _load_helper()
    assert helper._validate_target("/etc/dnf/repos.d/unicodé-🌳.repo") == (
        "/etc/dnf/repos.d", "unicodé-🌳.repo",
    )
    for candidate in (
        "/etc/dnf/repos.d/../repos.d/third.repo",
        "/etc/dnf/repos.d/nested/third.repo",
        "/etc/dnf/repos.d/third.conf",
        "/etc/dnf/repos.d/\x00third.repo",
    ):
        with pytest.raises(ValueError):
            helper._validate_target(candidate)


def test_helper_rejects_non_root_before_it_touches_the_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    helper = _load_helper()
    target = tmp_path / "third.repo"
    target.write_text("[third]\nname=Third\n", encoding="utf-8")
    monkeypatch.setattr(helper.os, "geteuid", lambda: 1000)

    result = helper.delete_repository_file(str(target))

    assert result == 1
    assert target.exists()
    assert "must be run as root" in capsys.readouterr().err


def test_helper_deletes_only_directory_entry_after_trusted_directory_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    """Exercise unlinkat-style deletion without weakening production ownership checks."""
    helper = _load_helper()
    target = tmp_path / "third.repo"
    target.write_text("[third]\nname=Third\n", encoding="utf-8")
    target.chmod(0o600)
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
    monkeypatch.setattr(helper, "_open_trusted_directory", lambda _directory: directory_fd)
    monkeypatch.setattr(helper, "os", helper_os)

    assert helper.delete_repository_file(str(target)) == 0
    assert not target.exists()
    assert f"Deleted repository configuration: {tmp_path}/third.repo" in capsys.readouterr().out


def test_helper_refuses_directory_without_root_owned_trust_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise the ownership rejection deterministically, including root-built RPMs."""
    helper = _load_helper()
    real_fstat = helper.os.fstat

    def non_root_owned_fstat(descriptor: int):
        status = real_fstat(descriptor)
        return SimpleNamespace(st_mode=status.st_mode, st_uid=1000)

    monkeypatch.setattr(helper.os, "fstat", non_root_owned_fstat)
    with pytest.raises(PermissionError, match="not owned by root"):
        helper._open_trusted_directory(str(tmp_path))


def test_runner_rejects_flatpak_before_starting_any_privileged_process(tmp_path: Path) -> None:
    helper = tmp_path / "dendro-repo-helper"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    runner = RepositoryDeletionRunner(helper_path=str(helper), flatpak_detector=lambda: True)

    with pytest.raises(Dnf5DaemonValidationError, match="cannot launch a host privileged helper"):
        runner.delete("/etc/yum.repos.d/third.repo")
    assert runner.is_running is False


def test_runner_requires_the_fixed_installed_helper(tmp_path: Path) -> None:
    runner = RepositoryDeletionRunner(helper_path=str(tmp_path / "missing-helper"), flatpak_detector=lambda: False)
    with pytest.raises(Dnf5DaemonValidationError, match="not installed or executable"):
        runner.delete("/etc/yum.repos.d/third.repo")


def test_runner_observes_and_releases_the_shared_host_operation_gate(tmp_path: Path) -> None:
    helper = tmp_path / "dendro-repo-helper"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    gate = HostOperationGate()
    assert gate.claim("dnf5-daemon", "DNF5 daemon package operation") is None
    runner = RepositoryDeletionRunner(
        helper_path=str(helper),
        flatpak_detector=lambda: False,
        host_operation_gate=gate,
    )

    with pytest.raises(Dnf5DaemonValidationError, match="DNF5 daemon package operation"):
        runner.delete("/etc/yum.repos.d/third.repo")
    assert runner.is_running is False
    gate.release("dnf5-daemon")

    runner._claim_host_operation()
    assert gate.active is not None
    assert gate.active.owner == "repository-file-deletion"
    runner._finish(False, "simulated terminal failure")
    assert gate.active is None
