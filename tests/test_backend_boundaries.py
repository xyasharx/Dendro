"""Boundary tests for host-RPM-only backend workers and fallback integrity."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.backend as backend


@pytest.mark.parametrize(
    ("worker_factory", "package_name"),
    [
        (lambda: backend.DependencyTreeWorker("example"), "example"),
        (lambda: backend.ReverseDependencyWorker("example"), "example"),
        (lambda: backend.PackageFilesWorker("example"), "example"),
        (lambda: backend.PackageChangelogWorker("example"), "example"),
        (lambda: backend.PackageScriptletsWorker("example"), "example"),
        (lambda: backend.PackageVerifyWorker("example"), "example"),
        (lambda: backend.DnfHistoryWorker(), ""),
        (lambda: backend.RemotePackageSearchWorker("example"), ""),
    ],
)
def test_host_rpm_workers_report_a_flatpak_capability_error(
    monkeypatch: pytest.MonkeyPatch,
    worker_factory,
    package_name: str,
) -> None:
    """No host-RPM worker may fall through to a sandbox subprocess or binding."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: True)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("a Flatpak guard attempted a subprocess"),
    )
    worker = worker_factory()
    errors: list[tuple[str, str]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )

    worker.run()

    assert len(errors) == 1
    assert errors[0][0] == package_name
    assert "unavailable in Flatpak" in errors[0][1]


def test_flatpak_guard_blocks_all_direct_host_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: True)
    local_rpm = tmp_path / "sample.rpm"
    local_rpm.write_bytes(b"not parsed because Flatpak is denied")

    assert backend.get_dnf_binary_path() == ""
    assert backend.find_package_owning_file("/usr/bin/bash") is None
    assert backend.inspect_local_rpm_file(str(local_rpm)) is None
    assert backend.create_rpm_transaction_set() is None
    assert backend.create_libdnf5_base() is None


def test_remote_search_reports_command_failure_instead_of_claiming_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="repository metadata is unavailable"
        ),
    )
    worker = backend.RemotePackageSearchWorker("café 🌳")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.remote_search_finished.connect(
        lambda query, values: completed.append((query, values))
    )

    worker.run()

    assert completed == []
    assert errors == [
        ("", "Remote repository search failed: repository metadata is unavailable")
    ]


def test_remote_search_reports_malformed_repoquery_data_instead_of_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="truncated repoquery record\n", stderr=""
        ),
    )
    worker = backend.RemotePackageSearchWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.remote_search_finished.connect(
        lambda query, values: completed.append((query, values))
    )

    worker.run()

    assert completed == []
    assert errors == [
        (
            "",
            "Remote repository search failed: RuntimeError: DNF repoquery returned a malformed package record.",
        )
    ]


def test_corrupt_capability_cache_record_is_not_used_as_a_dependency_fact(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(
        backend.os.path, "expanduser", lambda _path: str(tmp_path / "cache")
    )
    cache = backend.SQLiteCapabilityCache()
    connection = cache._get_connection()
    connection.execute(
        "INSERT INTO capabilities (cap_name, is_satisfied, provider_name) VALUES (?, ?, ?)",
        ("broken-capability", 9, "incorrect-provider"),
    )
    connection.commit()

    assert cache.get("broken-capability") is None
    assert not cache._disk_cache_enabled


def test_native_package_scan_discards_partial_data_when_binding_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A binding failure must use one complete fallback inventory, never a prefix."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(
            TransactionSet=lambda: (_ for _ in ()).throw(
                RuntimeError("broken RPM binding")
            )
        ),
        raising=False,
    )
    worker = backend.PackageQueryWorker()
    fallback = [
        backend.PackageInfo(
            name="complete-fallback", version="1", release="1", arch="noarch"
        )
    ]
    monkeypatch.setattr(
        worker, "_query_cli_subprocess", lambda _appstream, _comps: fallback
    )

    with caplog.at_level("WARNING", logger="core.backend"):
        result = worker._query_native_librpm(SimpleNamespace(), SimpleNamespace())

    assert result is fallback
    assert "discarding its partial result" in caplog.text


def test_native_package_scan_discards_a_record_with_missing_identity_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid native record must never become a partial successful inventory."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    transaction_set = SimpleNamespace(dbMatch=lambda: [{"name": ""}])
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(TransactionSet=lambda: transaction_set, RPMTAG_NAME="name"),
        raising=False,
    )
    worker = backend.PackageQueryWorker()
    fallback = [
        backend.PackageInfo(
            name="complete-fallback", version="1", release="1", arch="noarch"
        )
    ]
    monkeypatch.setattr(
        worker, "_query_cli_subprocess", lambda _appstream, _comps: fallback
    )

    result = worker._query_native_librpm(SimpleNamespace(), SimpleNamespace())

    assert result is fallback


def test_capability_cache_degrades_to_memory_when_its_directory_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_makedirs(*_args, **_kwargs) -> None:
        raise OSError("read-only home directory")

    monkeypatch.setattr(backend.os, "makedirs", fail_makedirs)
    with caplog.at_level("WARNING", logger="core.backend"):
        cache = backend.SQLiteCapabilityCache()
    cache.set_batch([("capability-🌳", True, "provider-café")])

    assert cache.get("capability-🌳") == (True, "provider-café")
    assert "using the in-memory cache only" in caplog.text
