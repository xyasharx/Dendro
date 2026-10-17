"""Focused tests for direct HTTPS repository-configuration addition."""
from __future__ import annotations

import io
import importlib.util
import os
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.request import Request

import pytest

from core.dnf5daemon import Dnf5DaemonValidationError
from core.operation_gate import HostOperationGate
from core.repository_addition import (
    MAX_REPOSITORY_CONFIGURATION_BYTES,
    _HTTPSOnlyRedirectHandler,
    RepositoryAdditionRunner,
    RepositoryConfigDownloadError,
    repository_filename_for_url,
    validate_downloaded_repository_configuration,
    validate_repository_configuration_url,
)


def _load_helper() -> ModuleType:
    path = Path(__file__).parents[1] / "tools" / "dendro-repo-helper.py"
    spec = importlib.util.spec_from_file_location("dendro_repo_addition_helper_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_configuration() -> bytes:
    return (
        b"[community-tools]\n"
        b"name=Community Tools\n"
        b"baseurl=https://packages.example.test/fedora/$releasever/$basearch/\n"
        b"enabled=1\n"
        b"gpgcheck=1\n"
        b"gpgkey=https://packages.example.test/keys/RPM-GPG-KEY-community\n"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://download.example.test/projects/community.repo",
        "https://download.copr.fedorainfracloud.org/results/example/tools/fedora.repo?arch=x86_64",
        "https://repo.example.test:8443/configuration.repo",
    ],
)
def test_direct_https_repository_urls_are_accepted(url: str) -> None:
    assert validate_repository_configuration_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        " https://repo.example.test/config.repo",
        "http://repo.example.test/config.repo",
        "file:///etc/yum.repos.d/example.repo",
        "https://user:password@repo.example.test/config.repo",
        "https://repo.example.test/config.repo#fragment",
        "https://repo.example.test/path with space.repo",
        "https://repo.example.test/first\nsecond",
        "https://repo.example.test/" + ("a" * 4096),
    ],
)
def test_repository_url_validation_rejects_unbounded_or_insecure_addresses(url: str) -> None:
    with pytest.raises(RepositoryConfigDownloadError):
        validate_repository_configuration_url(url)


def test_redirect_handler_rejects_an_https_to_http_downgrade() -> None:
    handler = _HTTPSOnlyRedirectHandler(threading.Event())

    with pytest.raises(RepositoryConfigDownloadError):
        handler.redirect_request(
            Request("https://repo.example.test/config.repo"),
            None,
            302,
            "Found",
            None,
            "http://repo.example.test/config.repo",
        )


def test_destination_filename_is_stable_safe_and_independent_of_remote_path() -> None:
    url = "https://Repo.Example.TEST/../../not-trusted.repo?name=🌳"
    filename = repository_filename_for_url(url)

    assert filename.startswith("dendro-repo.example.test-")
    assert filename.endswith(".repo")
    assert "/" not in filename
    assert "🌳" not in filename
    assert filename == repository_filename_for_url(url)
    assert filename != repository_filename_for_url("https://repo.example.test/other.repo")


def test_downloaded_configuration_is_normalized_before_authorization() -> None:
    normalized = validate_downloaded_repository_configuration(_valid_configuration().replace(b"\n", b"\r\n"))

    assert normalized == _valid_configuration()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\xff\xfe\x00",
        b"[community]\x00\n",
        b"x" * (MAX_REPOSITORY_CONFIGURATION_BYTES + 1),
    ],
)
def test_downloaded_configuration_rejects_non_text_empty_and_oversized_payloads(payload: bytes) -> None:
    with pytest.raises(RepositoryConfigDownloadError):
        validate_downloaded_repository_configuration(payload)


def test_helper_accepts_a_signed_https_repository_configuration() -> None:
    helper = _load_helper()

    assert helper._validated_repository_configuration(_valid_configuration()) == _valid_configuration()


def test_helper_accepts_the_current_direct_copr_repository_file_shape() -> None:
    helper = _load_helper()
    copr_configuration = (
        b"[copr:copr.fedorainfracloud.org:xyasharx:dendro]\n"
        b"name=Copr repo for dendro owned by xyasharx\n"
        b"baseurl=https://download.copr.fedorainfracloud.org/results/xyasharx/dendro/fedora-$releasever-$basearch/\n"
        b"type=rpm-md\n"
        b"skip_if_unavailable=True\n"
        b"gpgcheck=1\n"
        b"gpgkey=https://download.copr.fedorainfracloud.org/results/xyasharx/dendro/pubkey.gpg\n"
        b"repo_gpgcheck=0\n"
        b"enabled=1\n"
        b"enabled_metadata=1\n"
    )

    assert helper._validated_repository_configuration(copr_configuration) == copr_configuration


@pytest.mark.parametrize(
    "filename",
    [
        "../dendro-repo.example.test-0123456789abcdef.repo",
        "dendro-repo.example.test-0123456789abcdef.conf",
        "dendro-emoji-🌳.repo",
        "dendro-.repo",
    ],
)
def test_helper_accepts_only_dendro_generated_destination_filenames(filename: str) -> None:
    helper = _load_helper()

    with pytest.raises(ValueError):
        helper._validate_add_filename(filename)


@pytest.mark.parametrize(
    "payload",
    [
        b"[community]\nbaseurl=http://repo.example.test/path\ngpgcheck=1\n",
        b"[community]\nbaseurl=https://repo.example.test/path\ngpgcheck=0\n",
        b"[bad section]\nbaseurl=https://repo.example.test/path\ngpgcheck=1\n",
        b"[community]\nname=Only a name\ngpgcheck=1\n",
        b"[main]\nbaseurl=https://repo.example.test/path\ngpgcheck=1\n",
        b"[community]\nbaseurl=https://repo.example.test/path\ngpgcheck=1\n[COMMUNITY]\nbaseurl=https://repo.example.test/path\ngpgcheck=1\n",
        b"[community]\nbaseurl=https://repo.example.test/path\ngpgcheck=1\nproxy_password=not-accepted\n",
        b"[community]\nbaseurl=https://repo.example.test/path\ngpgcheck=1\n[DEFAULT]\nbaseurl=https://evil.example/\n",
        b"[DEFAULT]\n[community]\nbaseurl=https://repo.example.test/path\ngpgcheck=1\n",
    ],
)
def test_helper_rejects_unsafe_or_incomplete_repository_configuration(payload: bytes) -> None:
    helper = _load_helper()

    with pytest.raises(ValueError):
        helper._validated_repository_configuration(payload)


def test_helper_creates_one_new_file_without_overwriting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    helper = _load_helper()
    destination = repository_filename_for_url("https://repo.example.test/community.repo")
    real_open = os.open
    real_fstat = helper.os.fstat

    def root_owned_fstat(descriptor: int):
        status = real_fstat(descriptor)
        return SimpleNamespace(st_mode=status.st_mode, st_uid=0, st_size=status.st_size)

    monkeypatch.setattr(helper, "_ADD_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(helper, "_KNOWN_REPOSITORY_DIRECTORIES", (str(tmp_path),))
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    monkeypatch.setattr(helper.os, "fchown", lambda _fd, _uid, _gid: None)
    monkeypatch.setattr(helper.os, "fstat", root_owned_fstat)
    monkeypatch.setattr(
        helper,
        "_open_trusted_directory",
        lambda _directory: real_open(tmp_path, os.O_RDONLY | os.O_DIRECTORY),
    )

    assert helper.add_repository_file(destination, io.BytesIO(_valid_configuration())) == 0
    created = tmp_path / destination
    assert created.read_bytes() == _valid_configuration()
    assert "Installed repository configuration" in capsys.readouterr().out

    alternate_destination = repository_filename_for_url("https://repo.example.test/other.repo")
    assert helper.add_repository_file(alternate_destination, io.BytesIO(_valid_configuration())) == 1

    replacement = _valid_configuration().replace(b"community-tools", b"replacement-tools")
    assert helper.add_repository_file(destination, io.BytesIO(replacement)) == 1
    assert created.read_bytes() == _valid_configuration()
    error_output = capsys.readouterr().err
    assert "refusing to shadow it" in error_output
    assert "refusing to overwrite" in error_output


def test_addition_runner_rejects_flatpak_before_starting_network_or_helper(tmp_path: Path) -> None:
    helper = tmp_path / "dendro-repo-helper"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    runner = RepositoryAdditionRunner(helper_path=str(helper), flatpak_detector=lambda: True)

    with pytest.raises(Dnf5DaemonValidationError, match="cannot launch a host privileged helper"):
        runner.add_from_url("https://repo.example.test/community.repo")
    assert runner.is_running is False


def test_addition_runner_requires_the_fixed_installed_helper_before_download(tmp_path: Path) -> None:
    runner = RepositoryAdditionRunner(helper_path=str(tmp_path / "missing-helper"), flatpak_detector=lambda: False)

    with pytest.raises(Dnf5DaemonValidationError, match="not installed or executable"):
        runner.add_from_url("https://repo.example.test/community.repo")
    assert runner.is_running is False


def test_addition_runner_waits_for_download_worker_terminal_result_after_cancellation() -> None:
    runner = RepositoryAdditionRunner()
    runner._finished = False
    runner._phase = "downloading"
    runner._cancellation = threading.Event()

    runner.cancel()

    assert runner.is_running is True
    assert runner._cancellation.is_set()
    runner._on_configuration_download_failed("Repository download cancelled", "simulated worker completion")
    assert runner.is_running is False


def test_addition_runner_releases_its_shared_gate_after_a_terminal_result() -> None:
    gate = HostOperationGate()
    runner = RepositoryAdditionRunner(host_operation_gate=gate)

    runner._claim_host_operation()
    assert gate.active is not None
    assert gate.active.owner == "repository-file-addition"
    runner._finished = False
    runner._finish(False, "simulated terminal failure")
    assert gate.active is None