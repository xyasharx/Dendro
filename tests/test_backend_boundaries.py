"""Boundary tests for host-RPM-only backend workers and fallback integrity."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import core.backend as backend


def _disable_dnf5_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unavailable(*_args, **_kwargs):
        raise backend.Dnf5DaemonUnavailable(
            "DNF5 daemon is unavailable",
            "DNF5 daemon disabled in CLI fallback test.",
        )

    monkeypatch.setattr(backend, "Dnf5DaemonClient", _unavailable)


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
    _disable_dnf5_daemon(monkeypatch)
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
    assert backend.find_package_owning_file_info("/usr/bin/bash") is None
    assert backend.inspect_local_rpm_file(str(local_rpm)) is None
    assert backend.create_rpm_transaction_set() is None


def test_clean_env_strips_every_loader_and_python_injection_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host RPM/DNF helpers must not inherit sandbox loader injections."""
    injected = {
        "LD_AUDIT": "/tmp/libaudit.so",
        "LD_LIBRARY_PATH": "/tmp/qt-stub-libs",
        "LD_PRELOAD": "/tmp/libpreload.so",
        "PYTHONPATH": "/tmp/inject",
        "PYTHONHOME": "/tmp/home",
        "QT_PLUGIN_PATH": "/tmp/plugins",
        "QT_QPA_PLATFORM_PLUGIN_PATH": "/tmp/plugins/platforms",
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")

    env = backend.get_clean_env()

    assert injected.keys().isdisjoint(env)
    # Everything else is preserved so host tooling keeps its own configuration.
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LANG"] == "C.UTF-8"


def test_file_owner_cli_fallback_retains_exact_rpm_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    record = "shared-runtime\x1f2\x1f4.0\x1f1.fc44\x1fi686\n"
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=record, stderr=""
        ),
    )

    owner = backend.find_package_owning_file_info("/usr/lib/libshared.so")

    assert owner is not None
    assert owner.name == "shared-runtime"
    assert owner.full_nevra == "shared-runtime-2:4.0-1.fc44.i686"


def test_remote_search_reports_command_failure_instead_of_claiming_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    _disable_dnf5_daemon(monkeypatch)
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


def test_remote_search_keeps_distinct_multilib_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    _disable_dnf5_daemon(monkeypatch)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    field_separator = backend.REMOTE_QUERY_FIELD_SEPARATOR
    record_separator = backend.REMOTE_QUERY_RECORD_SEPARATOR
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                f"shared-runtime{field_separator}4.0{field_separator}1.fc44"
                f"{field_separator}x86_64{field_separator}updates{field_separator}1"
                f"{field_separator}Runtime{field_separator}perl(Runtime)\n{record_separator}"
                f"shared-runtime{field_separator}4.0{field_separator}1.fc44"
                f"{field_separator}i686{field_separator}updates{field_separator}1"
                f"{field_separator}Runtime{field_separator}perl(Runtime)\n{record_separator}"
                f"shared-runtime{field_separator}4.1{field_separator}1.fc44"
                f"{field_separator}x86_64{field_separator}updates{field_separator}1"
                f"{field_separator}Runtime{field_separator}perl(Runtime)\n{record_separator}"
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(backend.AppStreamCatalog, "get_instance", lambda: object())
    monkeypatch.setattr(backend.FedoraCompsCatalog, "get_instance", lambda: object())
    monkeypatch.setattr(
        backend.ProductionTaxonomyEngine, "classify", lambda **_kwargs: object()
    )
    monkeypatch.setattr(backend, "classification_package_fields", lambda _decision: {})
    worker = backend.RemotePackageSearchWorker("shared-runtime")
    completed: list[tuple[str, list[backend.PackageInfo]]] = []
    worker.signals.remote_search_finished.connect(
        lambda query, values: completed.append((query, values))
    )

    worker.run()

    assert len(completed) == 1
    assert [package.name_arch for package in completed[0][1]] == [
        "shared-runtime.x86_64",
        "shared-runtime.i686",
    ]


def test_remote_search_reports_malformed_repoquery_data_instead_of_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    _disable_dnf5_daemon(monkeypatch)
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


def test_dnf5_remote_query_frames_multiline_provides_for_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    _disable_dnf5_daemon(monkeypatch)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    # Keep the unit contract independent of whether the CI host exposes dnf5
    # through a symlink or an alternatives-managed executable.
    monkeypatch.setattr(backend, "_is_dnf5_binary", lambda _binary: True)
    field_separator = backend.REMOTE_QUERY_FIELD_SEPARATOR
    record_separator = backend.REMOTE_QUERY_RECORD_SEPARATOR
    commands: list[list[str]] = []
    captured_classifications: list[dict[str, object]] = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"perl-Example{field_separator}1.0{field_separator}1.fc44"
                f"{field_separator}noarch{field_separator}updates{field_separator}42"
                f"{field_separator}Example Perl module{field_separator}perl(Example::Module)\n"
                f"perl(:MODULE_COMPAT_5.40.0)\n{record_separator}"
            ),
            stderr="",
        )

    def classify(**kwargs):
        captured_classifications.append(kwargs)
        return object()

    monkeypatch.setattr(backend.subprocess, "run", run)
    monkeypatch.setattr(backend.AppStreamCatalog, "get_instance", lambda: object())
    monkeypatch.setattr(backend.FedoraCompsCatalog, "get_instance", lambda: object())
    monkeypatch.setattr(backend.ProductionTaxonomyEngine, "classify", classify)
    monkeypatch.setattr(backend, "classification_package_fields", lambda _decision: {})
    worker = backend.RemotePackageSearchWorker("perl example")
    completed: list[tuple[str, list[backend.PackageInfo]]] = []
    worker.signals.remote_search_finished.connect(
        lambda query, values: completed.append((query, values))
    )

    worker.run()

    assert len(completed) == 1
    assert completed[0][1][0].provides == [
        "perl(Example::Module)",
        "perl(:MODULE_COMPAT_5.40.0)",
    ]
    assert captured_classifications[0]["provides"] == [
        "perl(Example::Module)",
        "perl(:MODULE_COMPAT_5.40.0)",
    ]
    anatomy = captured_classifications[0]["anatomy"]
    assert isinstance(anatomy, backend.PackagePhysicalAnatomy)
    assert anatomy.ecosystem_capability_ids == {"perl"}
    query_format = next(
        argument for argument in commands[0] if argument.startswith("--queryformat=")
    )
    assert "%{provides}" in query_format
    assert "%{installsize}" in query_format
    assert record_separator in query_format


def test_dnf4_remote_query_retains_scalar_only_compatibility_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    _disable_dnf5_daemon(monkeypatch)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf")
    # This test covers the compatibility branch, not the host's symlink layout.
    monkeypatch.setattr(backend, "_is_dnf5_binary", lambda _binary: False)
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout="example\x1f1\x1f1.fc44\x1fnoarch\x1fupdates\x1f42\x1fExample\n",
            stderr="",
        )

    monkeypatch.setattr(backend.subprocess, "run", run)
    monkeypatch.setattr(backend.AppStreamCatalog, "get_instance", lambda: object())
    monkeypatch.setattr(backend.FedoraCompsCatalog, "get_instance", lambda: object())
    monkeypatch.setattr(
        backend.ProductionTaxonomyEngine, "classify", lambda **_kwargs: object()
    )
    monkeypatch.setattr(backend, "classification_package_fields", lambda _decision: {})
    worker = backend.RemotePackageSearchWorker("example")
    completed: list[tuple[str, list[backend.PackageInfo]]] = []
    worker.signals.remote_search_finished.connect(
        lambda query, values: completed.append((query, values))
    )

    worker.run()

    assert len(completed) == 1
    assert completed[0][1][0].provides == []
    query_format = next(
        argument for argument in commands[0] if argument.startswith("--queryformat=")
    )
    assert "%{provides}" not in query_format
    assert backend.REMOTE_QUERY_RECORD_SEPARATOR not in query_format


def test_dnf5_remote_query_rejects_unsafe_capability_framing() -> None:
    field_separator = backend.REMOTE_QUERY_FIELD_SEPARATOR
    record_separator = backend.REMOTE_QUERY_RECORD_SEPARATOR
    output = (
        f"example{field_separator}1{field_separator}1.fc44{field_separator}noarch"
        f"{field_separator}updates{field_separator}42{field_separator}Example"
        f"{field_separator}perl(Example){field_separator}injected\n{record_separator}"
    )

    with pytest.raises(RuntimeError, match="unsafe capability-list record"):
        backend._parse_remote_package_records(output, includes_provides=True)


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
    assert "`rpm -qa`" in caplog.text


def test_native_package_scan_skips_gpg_pubkey_pseudo_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Imported RPM signing keys are pseudo-packages, not malformed software."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    monkeypatch.setattr(backend, "get_system_protected_packages", lambda: set())
    transaction_set = SimpleNamespace(dbMatch=lambda: [{"name": "gpg-pubkey"}])
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(TransactionSet=lambda: transaction_set, RPMTAG_NAME="name"),
        raising=False,
    )
    worker = backend.PackageQueryWorker()
    monkeypatch.setattr(
        worker,
        "_query_cli_subprocess",
        lambda _appstream, _comps: pytest.fail(
            "gpg-pubkey must not trigger a fallback"
        ),
    )

    result = worker._query_native_librpm(SimpleNamespace(), SimpleNamespace())

    assert result == []


def test_cli_package_scan_skips_gpg_pubkey_pseudo_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI fallback has the same pseudo-package policy as librpm."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_system_protected_packages", lambda: set())
    record = "\x1f".join(["gpg-pubkey"] + [""] * 15)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=f"{record}\n", stderr=""
        ),
    )
    worker = backend.PackageQueryWorker()

    result = worker._query_cli_subprocess(SimpleNamespace(), SimpleNamespace())

    assert result == []


def test_native_package_scan_discards_a_record_with_missing_identity_fields(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real incomplete record must fall back with an actionable diagnostic."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    monkeypatch.setattr(backend, "get_system_protected_packages", lambda: set())
    transaction_set = SimpleNamespace(
        dbMatch=lambda: [
            {
                "name": "broken-package",
                "version": "",
                "release": "1",
                "arch": "x86_64",
            }
        ]
    )
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(
            TransactionSet=lambda: transaction_set,
            RPMTAG_NAME="name",
            RPMTAG_VERSION="version",
            RPMTAG_RELEASE="release",
            RPMTAG_ARCH="arch",
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
    assert "broken-package" in caplog.text
    assert "missing version" in caplog.text


def test_local_rpm_inspection_does_not_confuse_another_architecture_as_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    local_rpm = tmp_path / "shared-runtime.i686.rpm"
    local_rpm.write_bytes(b"header is provided by the mocked RPM binding")
    tags = SimpleNamespace(
        RPMTAG_NAME="name",
        RPMTAG_VERSION="version",
        RPMTAG_RELEASE="release",
        RPMTAG_ARCH="arch",
        RPMTAG_SUMMARY="summary",
        RPMTAG_DESCRIPTION="description",
        RPMTAG_LICENSE="license",
        RPMTAG_URL="url",
        RPMTAG_SIZE="size",
        RPMTAG_REQUIRENAME="requires",
        RPMTAG_PROVIDENAME="provides",
    )
    local_header = {
        "name": "shared-runtime",
        "version": "4.0",
        "release": "1.fc44",
        "arch": "i686",
        "summary": "Shared runtime",
        "description": "",
        "license": "MIT",
        "url": "",
        "size": 1,
        "requires": [],
        "provides": [],
    }
    installed_other_arch = {
        "version": "4.0",
        "release": "1.fc44",
        "arch": "x86_64",
    }

    class TransactionSet:
        def hdrFromFdno(self, _fd: int) -> dict[str, object]:
            return local_header

        def dbMatch(self, key: str, value: str) -> list[dict[str, object]]:
            assert (key, value) == ("name", "shared-runtime")
            return [installed_other_arch]

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    monkeypatch.setattr(backend, "_classify_local_rpm_manifest", lambda *_args: None)
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(TransactionSet=TransactionSet, **vars(tags)),
        raising=False,
    )

    result = backend.inspect_local_rpm_file(str(local_rpm), raise_on_failure=True)

    assert result is not None
    assert not result.is_already_installed
    assert result.installed_version == ""


def test_local_rpm_cli_fallback_checks_the_matching_architecture(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    local_rpm = tmp_path / "shared-runtime.i686.rpm"
    local_rpm.write_bytes(b"used only as an rpm -qp argument")
    header_record = "\x1f".join(
        [
            "shared-runtime",
            "4.0",
            "1.fc44",
            "i686",
            "Shared runtime",
            "MIT",
            "",
            "1",
        ]
    )
    commands: list[list[str]] = []
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout=header_record + "\n", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout="perl(Shared::Runtime)\n",
                stderr="",
            ),
            SimpleNamespace(returncode=1, stdout="", stderr="not installed"),
        ]
    )

    def rpm_query(command, **_kwargs):
        commands.append(command)
        return next(responses)

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", False)
    monkeypatch.setattr(backend, "_classify_local_rpm_manifest", lambda *_args: None)
    monkeypatch.setattr(backend.subprocess, "run", rpm_query)

    result = backend.inspect_local_rpm_file(str(local_rpm), raise_on_failure=True)

    assert result is not None
    assert result.provides == ["perl(Shared::Runtime)"]
    assert not result.is_already_installed
    assert commands[2][-1] == "shared-runtime.i686"


def test_local_rpm_manifest_classification_uses_third_party_capability_evidence() -> (
    None
):
    decision = backend._classify_local_rpm_manifest(
        "vendor-module-bundle",
        "Vendor module bundle",
        "",
        [],
        ["perl(Vendor::Module)"],
        [],
    )

    assert decision is not None
    assert decision.primary_category == "perl_pkgs"
    assert decision.input_scope.value == "local_rpm_manifest"
    assert decision.secondary_tags == ("Perl",)


def test_native_header_matching_keeps_multilib_records_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(
            RPMTAG_NAME="name",
            RPMTAG_EPOCH="epoch",
            RPMTAG_VERSION="version",
            RPMTAG_RELEASE="release",
            RPMTAG_ARCH="arch",
        ),
        raising=False,
    )
    selected = backend.PackageInfo(
        name="shared-runtime",
        epoch="2",
        version="4.0",
        release="1.fc44",
        arch="i686",
    )
    other_arch = {
        "name": "shared-runtime",
        "epoch": 2,
        "version": "4.0",
        "release": "1.fc44",
        "arch": "x86_64",
    }
    selected_header = {**other_arch, "arch": "i686"}

    assert not backend._header_matches_package(other_arch, selected)
    assert backend._header_matches_package(selected_header, selected)


def test_cli_inventory_preserves_a_nonzero_rpm_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_system_protected_packages", lambda: set())
    fields = [
        "epoch-test",
        "7",
        "1.2",
        "3.fc44",
        "x86_64",
        "General",
        "123",
        "MIT",
        "",
        "",
        "",
        "",
        "Epoch-bearing package",
        "",
        "",
        "",
    ]
    commands: list[list[str]] = []

    def rpm_query(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0, stdout="\x1f".join(fields) + "\n", stderr=""
        )

    monkeypatch.setattr(backend.subprocess, "run", rpm_query)
    worker = backend.PackageQueryWorker()
    packages = worker._query_cli_subprocess(
        backend.AppStreamCatalog(load_catalog=False),
        backend.FedoraCompsCatalog(load_comps=False),
    )

    assert [(package.name, package.epoch) for package in packages] == [
        ("epoch-test", "7")
    ]
    assert "%{EPOCH}" in commands[0][3]


def test_update_worker_keeps_distinct_multilib_update_records() -> None:
    updates = (
        SimpleNamespace(
            name="shared-runtime",
            version="4.1",
            release="1.fc44",
            arch="x86_64",
            epoch="2",
            repository="updates",
            name_arch="shared-runtime.x86_64",
        ),
        SimpleNamespace(
            name="shared-runtime",
            version="4.2",
            release="1.fc44",
            arch="i686",
            epoch="3",
            repository="updates",
            name_arch="shared-runtime.i686",
        ),
    )
    worker = backend.SystemUpdatesCheckWorker(
        client_factory=lambda: SimpleNamespace(
            list_available_updates=lambda **_kwargs: updates
        )
    )
    completed: list[dict[str, backend.AvailableUpdateInfo]] = []
    worker.signals.system_updates_loaded.connect(completed.append)

    worker.run()

    assert set(completed[0]) == {"shared-runtime.x86_64", "shared-runtime.i686"}
    assert completed[0]["shared-runtime.x86_64"].full_version == "2:4.1-1.fc44"
    assert completed[0]["shared-runtime.i686"].full_version == "3:4.2-1.fc44"


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


def test_security_advisory_worker_queries_through_client_factory() -> None:
    advisory = SimpleNamespace(advisory_id="FEDORA-2026-1")
    worker = backend.SecurityAdvisoryWorker(
        client_factory=lambda: SimpleNamespace(
            list_security_advisories=lambda: (advisory,)
        )
    )
    loaded: list[list[object]] = []
    errors: list[tuple[str, str]] = []
    worker.signals.security_advisories_loaded.connect(loaded.append)
    worker.signals.error_occurred.connect(
        lambda title, detail: errors.append((title, detail))
    )

    worker.run()

    assert errors == []
    assert loaded == [[advisory]]


def test_create_rpm_transaction_set_prefers_public_nosignatures_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RPM 6 exposes RPMVSF_MASK_NOSIGNATURES covering OpenPGP v6 headers."""
    applied_flags: list[int] = []

    class FakeTransactionSet:
        def setVSFlags(self, flags: int) -> None:
            applied_flags.append(flags)

    fake_rpm = SimpleNamespace(
        TransactionSet=FakeTransactionSet,
        RPMVSF_MASK_NOSIGNATURES=0xC0C00,
        _RPMVSF_NOSIGNATURES=0x00F00,
    )
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "rpm", fake_rpm, raising=False)

    ts = backend.create_rpm_transaction_set()

    assert isinstance(ts, FakeTransactionSet)
    assert applied_flags == [0xC0C00]


def test_create_rpm_transaction_set_falls_back_to_legacy_nosignatures_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older bindings without RPMVSF_MASK_NOSIGNATURES still receive _RPMVSF_NOSIGNATURES."""
    applied_flags: list[int] = []

    class FakeTransactionSet:
        def setVSFlags(self, flags: int) -> None:
            applied_flags.append(flags)

    fake_rpm = SimpleNamespace(
        TransactionSet=FakeTransactionSet,
        _RPMVSF_NOSIGNATURES=0x00F00,
    )
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "rpm", fake_rpm, raising=False)

    ts = backend.create_rpm_transaction_set()

    assert isinstance(ts, FakeTransactionSet)
    assert applied_flags == [0x00F00]


def test_dbus_history_search_and_userinstalled_succeed_in_flatpak_without_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.dnf5daemon import DnfHistoryTransaction

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: True)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("D-Bus path must not invoke subprocess"),
    )
    monkeypatch.setattr(backend.AppStreamCatalog, "get_instance", lambda: object())
    monkeypatch.setattr(backend.FedoraCompsCatalog, "get_instance", lambda: object())
    monkeypatch.setattr(
        backend.ProductionTaxonomyEngine, "classify", lambda **_kwargs: object()
    )
    monkeypatch.setattr(backend, "classification_package_fields", lambda _decision: {})

    class _FakeDaemon:
        def list_history(self):
            return (
                DnfHistoryTransaction(
                    transaction_id=7,
                    command_line="dnf5 install htop",
                    start_time=1761642000,
                    end_time=1761642010,
                    status="Ok",
                    altered_count=2,
                    actions=("Install", "Upgrade"),
                ),
            )

        def list_packages_fd(self, *, scope: str, **_kwargs):
            if scope == "available":
                return (
                    {
                        "name": "htop",
                        "version": "3.3.0",
                        "release": "1.fc44",
                        "arch": "x86_64",
                        "repo_id": "fedora",
                        "install_size": 450000,
                        "summary": "Interactive process viewer",
                        "provides": ["htop = 3.3.0-1.fc44"],
                    },
                    {
                        "name": "htop",
                        "version": "3.2.0",
                        "release": "1.fc44",
                        "arch": "x86_64",
                        "repo_id": "fedora",
                        "install_size": 440000,
                        "summary": "Duplicate same-arch record",
                        "provides": [],
                    },
                )
            if scope == "installed":
                return (
                    {
                        "name": "gpg-pubkey",
                        "epoch": "0",
                        "version": "deadbeef",
                        "release": "1",
                        "arch": "noarch",
                        "reason": "User",
                    },
                    {
                        "name": "bash",
                        "epoch": "0",
                        "version": "5.2.37",
                        "release": "1.fc44",
                        "arch": "x86_64",
                        "reason": "User",
                    },
                    {
                        "name": "glibc",
                        "epoch": "0",
                        "version": "2.41",
                        "release": "1.fc44",
                        "arch": "x86_64",
                        "reason": "Dependency",
                    },
                    {
                        "name": "shim-x64",
                        "epoch": "1",
                        "version": "15.8",
                        "release": "3.fc44",
                        "arch": "x86_64",
                        "reason": "User",
                    },
                )
            raise AssertionError(f"Unexpected scope: {scope}")

    # 1. History via D-Bus in Flatpak
    history_worker = backend.DnfHistoryWorker(client_factory=_FakeDaemon)
    history_out: list[list[backend.HistoryEntry]] = []
    history_worker.signals.history_loaded.connect(history_out.append)
    history_worker.run()
    assert len(history_out) == 1
    assert history_out[0][0].id == 7
    assert history_out[0][0].action == "Install, Upgrade"

    # 2. Remote search via Rpm.list_fd in Flatpak
    search_worker = backend.RemotePackageSearchWorker(
        "htop", client_factory=_FakeDaemon
    )
    search_out: list[tuple[str, list[backend.PackageInfo]]] = []
    search_worker.signals.remote_search_finished.connect(
        lambda query, items: search_out.append((query, items))
    )
    search_worker.run()
    assert len(search_out) == 1
    assert [pkg.name_arch for pkg in search_out[0][1]] == ["htop.x86_64"]
    assert search_out[0][1][0].provides == ["htop = 3.3.0-1.fc44"]

    # 3. User-installed query via Rpm.list_fd in Flatpak
    user_worker = backend.UserInstalledQueryWorker(client_factory=_FakeDaemon)
    user_out: list[set[str]] = []
    user_worker.signals.userinstalled_loaded.connect(user_out.append)
    user_worker.run()
    assert user_out == [
        {
            "bash-5.2.37-1.fc44.x86_64",
            "shim-x64-1:15.8-3.fc44.x86_64",
        }
    ]


def test_unsupported_daemon_falls_back_to_cli_on_host_and_refuses_in_flatpak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnsupportedDaemon:
        def list_history(self):
            raise backend.Dnf5DaemonUnsupportedError(
                "Unsupported", "History.list missing on DNF5 5.2"
            )

        def list_packages_fd(self, **_kwargs):
            raise backend.Dnf5DaemonUnsupportedError(
                "Unsupported", "Rpm.list_fd missing on DNF5 5.2"
            )

    # Host fallback succeeds cleanly when DNF5 5.2 returns UnknownInterface/UnknownMethod.
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    monkeypatch.setattr(backend, "_is_dnf5_binary", lambda _binary: True)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout='[{"id": 5, "command_line": "install htop", "start_time": 1761642000, "altered_count": 1}]',
            stderr="",
        ),
    )
    host_history: list[list[backend.HistoryEntry]] = []
    host_worker = backend.DnfHistoryWorker(client_factory=_UnsupportedDaemon)
    host_worker.signals.history_loaded.connect(host_history.append)
    host_worker.run()
    assert len(host_history) == 1
    assert host_history[0][0].id == 5

    # Flatpak rejects CLI fallback even if client raises Dnf5DaemonUnsupportedError.
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: True)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Flatpak must never fall back to CLI"),
    )
    for worker in (
        backend.DnfHistoryWorker(client_factory=_UnsupportedDaemon),
        backend.UserInstalledQueryWorker(client_factory=_UnsupportedDaemon),
        backend.RemotePackageSearchWorker("htop", client_factory=_UnsupportedDaemon),
    ):
        errors: list[tuple[str, str]] = []
        worker.signals.error_occurred.connect(
            lambda p, d, err=errors: err.append((p, d))
        )
        worker.run()
        assert len(errors) == 1
        assert "Flatpak" in errors[0][1]


def test_leaf_modules_do_not_import_core_backend() -> None:
    import ast

    repo_root = Path(__file__).resolve().parent.parent
    leaf_paths = [
        repo_root / "core" / "process_utils.py",
        repo_root / "core" / "settings.py",
        repo_root / "core" / "capability_cache.py",
        *sorted((repo_root / "core").glob("flatpak_*.py")),
    ]
    assert len(leaf_paths) >= 10
    for path in leaf_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "core.backend", (
                        f"{path.name} must not import core.backend"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not (
                    module == "core.backend"
                    or (
                        module == "core"
                        and any(alias.name == "backend" for alias in node.names)
                    )
                ), f"{path.name} must not import core.backend"


def test_sqlite_capability_cache_lifecycle_closes_handles_and_singleton(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.capability_cache import SQLiteCapabilityCache

    db_path = tmp_path / "capabilities.sqlite3"
    cache = SQLiteCapabilityCache(db_path=db_path)
    cache.set_batch([("perl(Strict)", True, "perl-libs")])
    assert cache.get("perl(Strict)") == (True, "perl-libs")
    assert len(cache._connections) == 1

    cache.close()
    assert cache._connections == {}
    # Re-reading after close clears L1 or falls through safely via a fresh handle.
    cache._memory_cache.clear()
    assert cache.get("perl(Strict)") == (True, "perl-libs")
    assert len(cache._connections) == 1

    cache.invalidate()
    assert cache._connections == {}
    assert cache.get("perl(Strict)") is None

    # close_instance is safe when unset and closes any initialized singleton handles.
    monkeypatch.setattr(SQLiteCapabilityCache, "_instance", None)
    SQLiteCapabilityCache.close_instance()
    singleton = SQLiteCapabilityCache(db_path=tmp_path / "singleton.sqlite3")
    monkeypatch.setattr(SQLiteCapabilityCache, "_instance", singleton)
    assert singleton.get("missing") is None
    assert len(singleton._connections) == 1
    SQLiteCapabilityCache.close_instance()
    assert singleton._connections == {}


def test_get_app_settings_uses_canonical_organization_and_application() -> None:
    from core.settings import (
        SETTINGS_APPLICATION,
        SETTINGS_ORGANIZATION,
        get_app_settings,
    )

    recorded: list[tuple[str, str]] = []

    def _fake_qsettings(org: str, app: str) -> SimpleNamespace:
        recorded.append((org, app))
        return SimpleNamespace(org=org, app=app)

    result = get_app_settings(_fake_qsettings)  # type: ignore[arg-type]
    assert SETTINGS_ORGANIZATION == "FedoraCommunity"
    assert SETTINGS_APPLICATION == "Dendro"
    assert recorded == [("FedoraCommunity", "Dendro")]
    assert result.org == "FedoraCommunity"
    assert result.app == "Dendro"
