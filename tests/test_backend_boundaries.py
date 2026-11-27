"""Boundary tests for host-RPM-only backend workers and fallback integrity."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import dendro.core.backend as backend


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
                field_separator.join(
                    [
                        "shared-runtime",
                        "0",
                        "4.0",
                        "1.fc44",
                        "x86_64",
                        "updates",
                        "1",
                        "Runtime",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "perl(Runtime)\n",
                        "",
                        "",
                    ]
                )
                + record_separator
                + field_separator.join(
                    [
                        "shared-runtime",
                        "0",
                        "4.0",
                        "1.fc44",
                        "i686",
                        "updates",
                        "1",
                        "Runtime",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "perl(Runtime)\n",
                        "",
                        "",
                    ]
                )
                + record_separator
                + field_separator.join(
                    [
                        "shared-runtime",
                        "0",
                        "4.1",
                        "1.fc44",
                        "x86_64",
                        "updates",
                        "1",
                        "Runtime",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "perl(Runtime)\n",
                        "",
                        "",
                    ]
                )
                + record_separator
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
                field_separator.join(
                    [
                        "perl-Example",
                        "0",
                        "1.0",
                        "1.fc44",
                        "noarch",
                        "updates",
                        "42",
                        "Example Perl module",
                        "Full description",
                        "Development/Libraries",
                        "perl-Example-1.0-1.fc44.src.rpm",
                        "Fedora Project",
                        "MIT",
                        "https://example.org",
                        "perl(Example::Module)\nperl(:MODULE_COMPAT_5.40.0)\n",
                        "perl(strict)\n",
                        "/usr/share/perl5/vendor_perl/Example.pm\n",
                    ]
                )
                + record_separator
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

    with caplog.at_level("WARNING", logger="dendro.core.backend"):
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
    record = "\x1f".join(["gpg-pubkey"] + [""] * 19)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=f"{record}\x1e", stderr=""
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

    with caplog.at_level("WARNING", logger="dendro.core.backend"):
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
            "",
            "MIT",
            "",
            "1",
            "",
            "",
            "",
            "",
            "",
        ]
    )
    commands: list[list[str]] = []
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout=header_record + "\x1e", stderr=""),
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
        "",
        "Epoch-bearing package",
        "",
        "",
        "",
        "",
        "",
        "",
    ]
    commands: list[list[str]] = []

    def rpm_query(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0, stdout="\x1f".join(fields) + "\x1e", stderr=""
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
    with caplog.at_level("WARNING", logger="dendro.core.backend"):
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
    from dendro.core.dnf5daemon import DnfHistoryTransaction

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
        repo_root / "dendro" / "core" / "process_utils.py",
        repo_root / "dendro" / "core" / "settings.py",
        repo_root / "dendro" / "core" / "capability_cache.py",
        *sorted((repo_root / "dendro" / "core").glob("flatpak_*.py")),
    ]
    assert len(leaf_paths) >= 10
    for path in leaf_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "dendro.core.backend", (
                        f"{path.name} must not import dendro.core.backend"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not (
                    module == "dendro.core.backend"
                    or (
                        module == "dendro.core"
                        and any(alias.name == "backend" for alias in node.names)
                    )
                ), f"{path.name} must not import dendro.core.backend"


def test_sqlite_capability_cache_lifecycle_closes_handles_and_singleton(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from dendro.core.capability_cache import SQLiteCapabilityCache

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
    from dendro.core.settings import (
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


def test_get_clean_env_restores_host_path_and_strips_appimage_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appdir = "/tmp/.mount_DendroTest"
    monkeypatch.setenv("DENDRO_APPRUN_ENV_SAVED", "1")
    monkeypatch.setenv("DENDRO_APPDIR", appdir)
    monkeypatch.setenv("APPDIR", appdir)
    monkeypatch.setenv("APPIMAGE", "/home/user/Dendro-x86_64.AppImage")
    monkeypatch.setenv("ARGV0", "./Dendro-x86_64.AppImage")
    monkeypatch.setenv("OWD", "/home/user")
    monkeypatch.setenv("DENDRO_ORIG_PATH", "/home/user/.local/bin:/usr/bin:/bin")
    monkeypatch.setenv("DENDRO_ORIG_LD_LIBRARY_PATH", "/opt/host/lib")
    monkeypatch.setenv("PATH", f"{appdir}/usr/bin:/usr/bin:/bin")
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{appdir}/usr/lib")
    monkeypatch.setenv("QT_PLUGIN_PATH", f"{appdir}/usr/plugins")
    monkeypatch.setenv("QT_QPA_PLATFORM_PLUGIN_PATH", f"{appdir}/usr/plugins/platforms")
    monkeypatch.setenv("PYTHONHOME", f"{appdir}/usr")
    monkeypatch.setenv("PYTHONPATH", f"{appdir}/usr/app")

    env = backend.get_clean_env()

    assert env["PATH"] == "/home/user/.local/bin:/usr/bin:/bin"
    for key in (
        "APPDIR",
        "APPIMAGE",
        "ARGV0",
        "OWD",
        "DENDRO_APPRUN_ENV_SAVED",
        "DENDRO_APPDIR",
        "DENDRO_ORIG_PATH",
        "DENDRO_ORIG_LD_LIBRARY_PATH",
        "LD_LIBRARY_PATH",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        assert key not in env


def test_remote_package_search_enriches_comps_via_daemon_list_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dendro.core.dnf5daemon import DnfCompsGroup, DnfCompsPackage

    comps = backend.FedoraCompsCatalog(load_comps=False)
    empty_appstream = backend.AppStreamCatalog(load_catalog=False)
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: True)
    monkeypatch.setattr(
        backend.AppStreamCatalog, "get_instance", lambda: empty_appstream
    )
    monkeypatch.setattr(backend.FedoraCompsCatalog, "get_instance", lambda: comps)

    group_calls: list[dict[str, object]] = []

    class _DaemonWithGroups:
        def list_packages_fd(self, **_kwargs):
            return (
                {
                    "name": "glx-utils",
                    "version": "9.0.0",
                    "release": "5.fc44",
                    "arch": "x86_64",
                    "repo_id": "fedora",
                    "install_size": 120000,
                    "summary": "GLX utilities",
                    "provides": [],
                },
            )

        def list_groups(self, **kwargs):
            group_calls.append(kwargs)
            return (
                DnfCompsGroup(
                    groupid="base-graphical",
                    packages=(DnfCompsPackage(name="glx-utils"),),
                ),
            )

    worker = backend.RemotePackageSearchWorker(
        "glx-utils", client_factory=_DaemonWithGroups
    )
    results_out: list[tuple[str, list[backend.PackageInfo]]] = []
    worker.signals.remote_search_finished.connect(
        lambda q, items: results_out.append((q, items))
    )
    worker.run()

    assert len(group_calls) == 1
    assert group_calls[0]["scope"] == "all"
    assert group_calls[0]["with_hidden"] is True
    assert len(results_out) == 1
    assert results_out[0][1][0].primary_category == "graphics_drivers"
    assert (
        results_out[0][1][0].classification_evidence
        == backend.ClassificationEvidence.COMPS.value
    )
    assert comps.daemon_groups_loaded is True

    # Subsequent searches reuse the loaded daemon Comps groups without re-querying.
    worker2 = backend.RemotePackageSearchWorker(
        "glx-utils", client_factory=_DaemonWithGroups
    )
    worker2.run()
    assert len(group_calls) == 1


def test_native_package_scan_uses_from_rpm_header_for_desktop_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    xdg_share = tmp_path / "share"
    apps_kde4 = xdg_share / "applications" / "kde4"
    apps_kde4.mkdir(parents=True)
    (apps_kde4 / "kwrite.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=KWrite\n"
        "Exec=kwrite\n"
        "Categories=Utility;TextEditor;\n",
        encoding="utf-8",
    )
    (xdg_share / "applications" / "legacyapp.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=LegacyApp\n"
        "Exec=legacyapp\n"
        "Categories=Graphics;2DGraphics;\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_DIRS", str(xdg_share))
    backend.SystemDesktopIndex.reset_instance()

    fake_rpm_tags = SimpleNamespace(
        RPMTAG_NAME="name",
        RPMTAG_VERSION="version",
        RPMTAG_RELEASE="release",
        RPMTAG_ARCH="arch",
        RPMTAG_EPOCH="epoch",
        RPMTAG_GROUP="group",
        RPMTAG_SUMMARY="summary",
        RPMTAG_DESCRIPTION="description",
        RPMTAG_LICENSE="license",
        RPMTAG_URL="url",
        RPMTAG_PACKAGER="packager",
        RPMTAG_VENDOR="vendor",
        RPMTAG_BUILDTIME="buildtime",
        RPMTAG_INSTALLTIME="installtime",
        RPMTAG_SIZE="size",
        RPMTAG_DIRNAMES="dirnames",
        RPMTAG_BASENAMES="basenames",
        RPMTAG_DIRINDEXES="dirindexes",
        RPMTAG_OLDFILENAMES="oldfilenames",
        RPMTAG_PROVIDENAME="provides",
        RPMTAG_REQUIRENAME="requires",
    )
    # 1. Owns /usr/share/applications directory and an autostart .desktop only -> NOT a desktop app.
    autostart_header = {
        "name": "helper-daemon",
        "version": "1.0",
        "release": "1.fc44",
        "arch": "x86_64",
        "epoch": None,
        "group": "System",
        "summary": "Background helper daemon",
        "description": "",
        "license": "MIT",
        "url": "",
        "packager": "Fedora",
        "vendor": "Fedora",
        "buildtime": 0,
        "installtime": 0,
        "size": 100,
        "dirnames": ["/usr/bin/", "/usr/share/applications/", "/etc/xdg/autostart/"],
        "basenames": ["helper-daemon", "helper-daemon.desktop"],
        "dirindexes": [0, 2],
        "provides": ["helper-daemon = 1.0-1.fc44"],
        "requires": [],
    }
    # 2. Owns /usr/share/applications/kde4/kwrite.desktop -> Desktop File ID "kde4-kwrite.desktop".
    subdir_desktop_header = {
        "name": "kwrite-sub",
        "version": "1.0",
        "release": "1.fc44",
        "arch": "x86_64",
        "epoch": None,
        "group": "Applications",
        "summary": "Text editor",
        "description": "",
        "license": "GPLv2",
        "url": "",
        "packager": "Fedora",
        "vendor": "Fedora",
        "buildtime": 0,
        "installtime": 0,
        "size": 100,
        "dirnames": ["/usr/bin/", "/usr/share/applications/kde4/"],
        "basenames": ["kwrite", "kwrite.desktop"],
        "dirindexes": [0, 1],
        "provides": ["kwrite-sub = 1.0-1.fc44"],
        "requires": [],
    }
    # 3. Legacy RPM v3 header using OLDFILENAMES.
    legacy_header = {
        "name": "legacyapp",
        "version": "0.9",
        "release": "1",
        "arch": "noarch",
        "epoch": None,
        "group": "Applications",
        "summary": "Legacy graphics editor",
        "description": "",
        "license": "MIT",
        "url": "",
        "packager": "Vendor",
        "vendor": "Vendor",
        "buildtime": 0,
        "installtime": 0,
        "size": 100,
        "oldfilenames": [
            "/usr/bin/legacyapp",
            "/usr/share/applications/legacyapp.desktop",
        ],
        "provides": ["legacyapp = 0.9-1"],
        "requires": [],
    }

    transaction_set = SimpleNamespace(
        dbMatch=lambda: [autostart_header, subdir_desktop_header, legacy_header]
    )
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    monkeypatch.setattr(backend, "get_system_protected_packages", lambda: set())
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(TransactionSet=lambda: transaction_set, **vars(fake_rpm_tags)),
        raising=False,
    )
    worker = backend.PackageQueryWorker()
    packages = worker._query_native_librpm(
        backend.AppStreamCatalog(load_catalog=False),
        backend.FedoraCompsCatalog(load_comps=False),
    )

    by_name = {pkg.name: pkg for pkg in packages}
    assert by_name["helper-daemon"].primary_category == "cli_tools"
    assert by_name["kwrite-sub"].primary_category == "user_apps"
    assert by_name["kwrite-sub"].sub_category == "desktop_development"
    assert by_name["legacyapp"].primary_category == "user_apps"
    assert by_name["legacyapp"].sub_category == "desktop_graphics"
    backend.SystemDesktopIndex.reset_instance()


def test_cli_package_scan_verifies_full_desktop_file_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    xdg_share = tmp_path / "share"
    apps_kde4 = xdg_share / "applications" / "kde4"
    apps_kde4.mkdir(parents=True)
    (apps_kde4 / "kwrite.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=KWrite\n"
        "Exec=kwrite\n"
        "Categories=Utility;TextEditor;\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_DIRS", str(xdg_share))
    backend.SystemDesktopIndex.reset_instance()
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_system_protected_packages", lambda: set())

    autostart_row = "\x1f".join(
        [
            "helper-daemon",
            "(none)",
            "1.0",
            "1.fc44",
            "x86_64",
            "System",
            "100",
            "MIT",
            "",
            "",
            "",
            "1700000000",
            "1700000100",
            "Background helper",
            "Autostart service helper",
            "helper-daemon-1.0-1.fc44.src.rpm",
            "/usr/bin/\x07/usr/share/applications/\x07/etc/xdg/autostart/\x07",
            "",
            "",
            "/usr/bin/helper-daemon\x07/etc/xdg/autostart/helper-daemon.desktop\x07",
        ]
    )
    subdir_row = "\x1f".join(
        [
            "kwrite-sub",
            "(none)",
            "1.0",
            "1.fc44",
            "x86_64",
            "Applications",
            "100",
            "GPLv2",
            "",
            "",
            "",
            "1700000000",
            "1700000100",
            "Text editor",
            "KDE text editor",
            "kwrite-1.0-1.fc44.src.rpm",
            "/usr/bin/\x07/usr/share/applications/kde4/\x07",
            "",
            "",
            "/usr/bin/kwrite\x07/usr/share/applications/kde4/kwrite.desktop\x07",
        ]
    )
    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        return SimpleNamespace(
            returncode=0, stdout=f"{autostart_row}\x1e{subdir_row}\x1e", stderr=""
        )

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    worker = backend.PackageQueryWorker()
    packages = worker._query_cli_subprocess(
        backend.AppStreamCatalog(load_catalog=False),
        backend.FedoraCompsCatalog(load_comps=False),
    )

    by_name = {pkg.name: pkg for pkg in packages}
    assert by_name["helper-daemon"].primary_category == "cli_tools"
    assert by_name["kwrite-sub"].primary_category == "user_apps"
    assert by_name["kwrite-sub"].sub_category == "desktop_development"
    assert "%{FILENAMES}" in commands[0][3]
    assert "%{OLDFILENAMES}" in commands[0][3]
    backend.SystemDesktopIndex.reset_instance()


def test_local_rpm_inspection_verifies_dirindexes_and_desktop_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    xdg_share = tmp_path / "share"
    apps_kde4 = xdg_share / "applications" / "kde4"
    apps_kde4.mkdir(parents=True)
    (apps_kde4 / "viewer.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Viewer\n"
        "Exec=viewer\n"
        "Categories=Graphics;Viewer;\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_DIRS", str(xdg_share))
    backend.SystemDesktopIndex.reset_instance()

    local_rpm = tmp_path / "viewer.noarch.rpm"
    local_rpm.write_bytes(b"header provided by fake librpm")

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
        RPMTAG_DIRNAMES="dirnames",
        RPMTAG_BASENAMES="basenames",
        RPMTAG_DIRINDEXES="dirindexes",
    )
    local_header = {
        "name": "viewer-pkg",
        "version": "1.0",
        "release": "1.fc44",
        "arch": "noarch",
        "summary": "Image viewer",
        "description": "",
        "license": "MIT",
        "url": "",
        "size": 42,
        "requires": [],
        "provides": ["viewer-pkg = 1.0-1.fc44"],
        "dirnames": ["/usr/bin/", "/usr/share/applications/kde4/"],
        "basenames": ["viewer", "viewer.desktop"],
        "dirindexes": [0, 1],
    }

    class _FakeTS:
        def hdrFromFdno(self, _fd: int):
            return local_header

        def dbMatch(self, *_args):
            return []

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(TransactionSet=_FakeTS, **vars(tags)),
        raising=False,
    )

    result = backend.inspect_local_rpm_file(str(local_rpm), raise_on_failure=True)
    assert result is not None
    assert result.classification is not None
    assert result.classification.primary_category == "user_apps"
    assert result.classification.sub_category == "desktop_graphics"
    backend.SystemDesktopIndex.reset_instance()


def test_local_rpm_native_inspection_forwards_source_rpm_and_requires(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    local_rpm = tmp_path / "custom-selinux.noarch.rpm"
    local_rpm.write_bytes(b"header provided by fake librpm")

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
        RPMTAG_DIRNAMES="dirnames",
        RPMTAG_BASENAMES="basenames",
        RPMTAG_DIRINDEXES="dirindexes",
        RPMTAG_SOURCERPM="sourcerpm",
    )
    local_header = {
        "name": "custom-selinux",
        "version": "1.0",
        "release": "1.fc44",
        "arch": "noarch",
        "summary": "Custom policy module",
        "description": "",
        "license": "MIT",
        "url": "",
        "size": 42,
        "requires": ["selinux-policy >= 41.0"],
        "provides": ["custom-selinux = 1.0-1.fc44"],
        "dirnames": [],
        "basenames": [],
        "dirindexes": [],
        "sourcerpm": "custom-1.0-1.fc44.src.rpm",
    }

    class _FakeTS:
        def hdrFromFdno(self, _fd: int):
            return local_header

        def dbMatch(self, *_args):
            return []

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(TransactionSet=_FakeTS, **vars(tags)),
        raising=False,
    )

    result = backend.inspect_local_rpm_file(str(local_rpm), raise_on_failure=True)
    assert result is not None
    assert result.classification is not None
    assert result.classification.primary_category == "security_pkgs"
    assert "Source package: custom" in result.classification.rationale


def test_native_package_scan_releases_rpm_lock_before_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RPM_GLOBAL_LOCK`` protects only librpm header iteration, not desktop/taxonomy scans."""
    fake_rpm_tags = SimpleNamespace(
        RPMTAG_NAME="name",
        RPMTAG_VERSION="version",
        RPMTAG_RELEASE="release",
        RPMTAG_ARCH="arch",
        RPMTAG_EPOCH="epoch",
        RPMTAG_GROUP="group",
        RPMTAG_SUMMARY="summary",
        RPMTAG_DESCRIPTION="description",
        RPMTAG_LICENSE="license",
        RPMTAG_URL="url",
        RPMTAG_PACKAGER="packager",
        RPMTAG_VENDOR="vendor",
        RPMTAG_BUILDTIME="buildtime",
        RPMTAG_INSTALLTIME="installtime",
        RPMTAG_LONGSIZE="longsize",
        RPMTAG_SIZE="size",
        RPMTAG_PROVIDENAME="provides",
        RPMTAG_REQUIRENAME="requires",
        RPMTAG_DIRNAMES="dirnames",
        RPMTAG_BASENAMES="basenames",
        RPMTAG_DIRINDEXES="dirindexes",
        RPMTAG_SOURCERPM="sourcerpm",
    )
    header = {
        "name": "ripgrep",
        "version": "14.1.0",
        "release": "1.fc44",
        "arch": "x86_64",
        "epoch": None,
        "group": "Unspecified",
        "summary": "Fast line-oriented search tool",
        "description": "Recursively searches directories for a regex pattern.",
        "license": "MIT",
        "url": "https://github.com/BurntSushi/ripgrep",
        "packager": "Fedora Project",
        "vendor": "Fedora Project",
        "buildtime": 1700000000,
        "installtime": 1700000100,
        "longsize": 5_368_709_120,
        "size": 1_073_741_824,
        "dirnames": ["/usr/bin/"],
        "basenames": ["rg"],
        "dirindexes": [0],
        "provides": ["bundled(crate(ripgrep))"],
        "requires": ["libc.so.6()(64bit)"],
        "sourcerpm": "rust-ripgrep-14.1.0-1.fc44.src.rpm",
    }

    lock_owned_inside_classify: list[bool] = []
    real_classify = backend.ProductionTaxonomyEngine.classify

    def spy_classify(**kwargs):
        lock_owned_inside_classify.append(backend.RPM_GLOBAL_LOCK._is_owned())
        return real_classify(**kwargs)

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)
    monkeypatch.setattr(backend, "get_system_protected_packages", lambda: set())
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(
            TransactionSet=lambda: SimpleNamespace(dbMatch=lambda: [header]),
            **vars(fake_rpm_tags),
        ),
        raising=False,
    )
    monkeypatch.setattr(backend.ProductionTaxonomyEngine, "classify", spy_classify)

    worker = backend.PackageQueryWorker()
    packages = worker._query_native_librpm(
        backend.AppStreamCatalog(load_catalog=False),
        backend.FedoraCompsCatalog(load_comps=False),
    )

    assert lock_owned_inside_classify == [False]
    assert len(packages) == 1
    assert packages[0].size_bytes == 5_368_709_120
    assert packages[0].primary_category == "cli_tools"
    assert "Rust" in packages[0].secondary_tags


def test_cli_package_scan_parity_with_native_64bit_semicolons_and_iso_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI fallback handles >4GiB LONGSIZE, ``;`` in filenames, multiline descriptions, and Unix timestamps."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_system_protected_packages", lambda: set())

    row = "\x1f".join(
        [
            "ansible",
            "0",
            "10.5.0",
            "1.fc44",
            "noarch",
            "Applications/System",
            "5368709120",
            "GPL-3.0-or-later",
            "https://ansible.com",
            "Fedora Project",
            "Fedora Project",
            "1700000000",
            "1700000100",
            "Radically simple IT automation",
            "Line one of description\nLine two with ; semicolon",
            "ansible-10.5.0-1.fc44.src.rpm",
            "/usr/bin/\x07/usr/share/doc/semi;colon/\x07",
            "python3.13dist(ansible)\x07special(a;b)\x07",
            "python(abi) = 3.13\x07",
            "/usr/bin/ansible\x07/usr/share/doc/semi;colon/README;1.txt\x07",
        ]
    )
    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout=row + "\x1e", stderr="")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    worker = backend.PackageQueryWorker()
    packages = worker._query_cli_subprocess(
        backend.AppStreamCatalog(load_catalog=False),
        backend.FedoraCompsCatalog(load_comps=False),
    )

    assert len(packages) == 1
    pkg = packages[0]
    assert pkg.size_bytes == 5_368_709_120
    assert pkg.description == "Line one of description\nLine two with ; semicolon"
    assert pkg.build_time and ":" in pkg.build_time and "-" in pkg.build_time
    assert pkg.install_time and ":" in pkg.install_time and "-" in pkg.install_time
    assert pkg.primary_category == "cli_tools"
    assert "Python" in pkg.secondary_tags
    assert "%{LONGSIZE}" in commands[0][3]
    assert "%{DESCRIPTION}" in commands[0][3]
    assert "%{SOURCERPM}" in commands[0][3]
    assert "%{REQUIRENAME}" in commands[0][3]


def test_local_rpm_cli_fallback_achieves_classification_parity_with_native(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``rpm -qp`` fallback forwards description, sourcerpm, requires, and file paths."""
    local_rpm = tmp_path / "custom-selinux.noarch.rpm"
    local_rpm.write_bytes(b"used as path argument only")
    header_record = "\x1f".join(
        [
            "custom-selinux",
            "1.0",
            "1.fc44",
            "noarch",
            "Custom policy module",
            "Multiline SELinux\npolicy description",
            "MIT",
            "https://example.org",
            "42",
            "custom-1.0-1.fc44.src.rpm",
            "/usr/share/selinux/packages/\x07",
            "custom.pp.bz2\x07",
            "selinux-policy >= 41.0\x07",
            "/usr/share/selinux/packages/custom.pp.bz2\x07",
        ]
    )
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout=header_record + "\x1e", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout="custom-selinux = 1.0-1.fc44\n",
                stderr="",
            ),
            SimpleNamespace(returncode=1, stdout="", stderr="not installed"),
        ]
    )

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", False)
    monkeypatch.setattr(backend.subprocess, "run", lambda *_a, **_kw: next(responses))

    result = backend.inspect_local_rpm_file(str(local_rpm), raise_on_failure=True)
    assert result is not None
    assert result.description == "Multiline SELinux\npolicy description"
    assert result.requires == ["selinux-policy >= 41.0"]
    assert result.classification is not None
    assert result.classification.primary_category == "security_pkgs"
    assert "Source package: custom" in result.classification.rationale


def test_remote_search_daemon_and_cli_classify_ansible_and_ripgrep_equally_with_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote DNF5 search forwards files, description, sourcerpm, requires, and vendor."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(
        backend.AppStreamCatalog,
        "get_instance",
        classmethod(lambda cls: cls(load_catalog=False)),
    )
    monkeypatch.setattr(
        backend.FedoraCompsCatalog,
        "get_instance",
        classmethod(lambda cls: cls(load_comps=False)),
    )

    captured_kwargs: dict[str, object] = {}

    class _FakeDaemon:
        def list_packages_fd(self, **kwargs):
            captured_kwargs.update(kwargs)
            return (
                {
                    "name": "ansible",
                    "epoch": "0",
                    "version": "10.5.0",
                    "release": "1.fc44",
                    "arch": "noarch",
                    "repo_id": "fedora",
                    "install_size": 1000,
                    "summary": "IT automation",
                    "description": "Automates provisioning",
                    "group": "Applications/System",
                    "sourcerpm": "ansible-10.5.0-1.fc44.src.rpm",
                    "vendor": "Fedora Project",
                    "license": "GPL-3.0-or-later",
                    "url": "https://ansible.com",
                    "provides": ["python3.13dist(ansible)"],
                    "requires": ["python(abi) = 3.13"],
                    "files": ["/usr/bin/ansible"],
                },
                {
                    "name": "ripgrep",
                    "epoch": "0",
                    "version": "14.1.0",
                    "release": "1.fc44",
                    "arch": "x86_64",
                    "repo_id": "fedora",
                    "install_size": 2000,
                    "summary": "Line-oriented search tool",
                    "description": "Fast grep replacement",
                    "group": "Unspecified",
                    "sourcerpm": "rust-ripgrep-14.1.0-1.fc44.src.rpm",
                    "vendor": "Fedora Project",
                    "license": "MIT",
                    "url": "https://github.com/BurntSushi/ripgrep",
                    "provides": ["bundled(crate(ripgrep))"],
                    "requires": ["libc.so.6()(64bit)"],
                    "files": ["/usr/bin/rg"],
                },
            )

        def close(self):
            pass

    worker = backend.RemotePackageSearchWorker(
        "ansible", client_factory=lambda: _FakeDaemon()
    )
    completed: list[tuple[str, list[backend.PackageInfo]]] = []
    worker.signals.remote_search_finished.connect(
        lambda query, values: completed.append((query, values))
    )
    worker.run()

    assert len(completed) == 1
    assert captured_kwargs["optional_metadata_types"] == ("filelists",)
    assert "files" in captured_kwargs["package_attrs"]
    assert "description" in captured_kwargs["package_attrs"]
    assert "requires" in captured_kwargs["package_attrs"]
    assert "sourcerpm" in captured_kwargs["package_attrs"]
    assert "vendor" in captured_kwargs["package_attrs"]
    assert "group" in captured_kwargs["package_attrs"]
    by_name = {pkg.name: pkg for pkg in completed[0][1]}
    assert by_name["ansible"].primary_category == "cli_tools"
    assert "Python" in by_name["ansible"].secondary_tags
    assert by_name["ansible"].description == "Automates provisioning"
    assert by_name["ripgrep"].primary_category == "cli_tools"
    assert "Rust" in by_name["ripgrep"].secondary_tags


def test_get_system_protected_packages_scans_all_dnf_protected_dirs_and_running_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Protected-package detection covers DNF5/DNF4/Yum dirs and kernel subpackages."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    from dendro.core import rpm_inspection

    dnf5_share = tmp_path / "usr-share-dnf5"
    etc_dnf = tmp_path / "etc-dnf"
    etc_yum = tmp_path / "etc-yum"
    for directory in (dnf5_share, etc_dnf, etc_yum):
        directory.mkdir()
    (dnf5_share / "dnf5.conf").write_text("# comment\ndnf5\n", encoding="utf-8")
    (etc_dnf / "systemd.conf").write_text(
        "systemd # inline comment\n", encoding="utf-8"
    )
    (etc_yum / "legacy.conf").write_text("yum-protected\n", encoding="utf-8")

    real_isdir = rpm_inspection.os.path.isdir
    real_listdir = rpm_inspection.os.listdir
    real_open = open
    dir_map = {
        "/usr/share/dnf5/protected.d": str(dnf5_share),
        "/etc/dnf/protected.d": str(etc_dnf),
        "/etc/yum/protected.d": str(etc_yum),
    }

    monkeypatch.setattr(
        rpm_inspection.os.path,
        "isdir",
        lambda path: real_isdir(dir_map.get(path, path)),
    )
    monkeypatch.setattr(
        rpm_inspection.os,
        "listdir",
        lambda path: real_listdir(dir_map.get(path, path)),
    )
    monkeypatch.setattr(
        rpm_inspection,
        "open",
        lambda path, *args, **kwargs: real_open(
            path.replace("/usr/share/dnf5/protected.d", str(dnf5_share))
            .replace("/etc/dnf/protected.d", str(etc_dnf))
            .replace("/etc/yum/protected.d", str(etc_yum)),
            *args,
            **kwargs,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        rpm_inspection.os,
        "uname",
        lambda: SimpleNamespace(release="6.12.1-300.fc44.x86_64"),
    )

    protected = rpm_inspection.get_system_protected_packages()
    assert {"dnf5", "systemd", "yum-protected"} <= protected
    assert {
        "kernel-6.12.1-300.fc44.x86_64",
        "kernel-core-6.12.1-300.fc44.x86_64",
        "kernel-modules-6.12.1-300.fc44.x86_64",
        "kernel-modules-core-6.12.1-300.fc44.x86_64",
    } <= protected


def test_package_query_worker_overlaps_catalogs_and_native_librpm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup loads AppStream, Comps, desktop entries, and librpm headers concurrently."""
    import threading

    from dendro.core import rpm_workers

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)

    barrier = threading.Barrier(4, timeout=3.0)
    empty_appstream = backend.AppStreamCatalog(load_catalog=False)
    empty_comps = backend.FedoraCompsCatalog(load_comps=False)

    monkeypatch.setattr(
        backend.AppStreamCatalog,
        "get_instance",
        classmethod(lambda cls: (barrier.wait(), empty_appstream)[1]),
    )
    monkeypatch.setattr(
        backend.FedoraCompsCatalog,
        "get_instance",
        classmethod(lambda cls: (barrier.wait(), empty_comps)[1]),
    )
    monkeypatch.setattr(
        backend.SystemDesktopIndex,
        "get_instance",
        classmethod(lambda cls: (barrier.wait(), None)[1]),
    )
    monkeypatch.setattr(backend, "get_system_protected_packages", lambda: set())

    worker = backend.PackageQueryWorker()
    raw_record = rpm_workers._RawInstalledRpmRecord(
        name="bash",
        version="5.2.37",
        release="1.fc44",
        arch="x86_64",
        epoch="0",
        group="System Environment/Shells",
        summary="The GNU Bourne Again shell",
        description="Command-line shell",
        license="GPL-3.0-or-later",
        url="https://www.gnu.org/software/bash/",
        packager="Fedora Project",
        vendor="Fedora Project",
        build_time="2026-01-01",
        install_time="2026-01-02",
        size_bytes=2048,
        source_rpm="bash-5.2.37-1.fc44.src.rpm",
        provides=[],
        requires=["libc.so.6()(64bit)"],
        anatomy=backend.PackagePhysicalAnatomy.from_manifest_data(["/usr/bin"], []),
        desktop_entry_files=[],
    )

    monkeypatch.setattr(
        worker,
        "_read_native_librpm_records",
        lambda: (barrier.wait(), ([raw_record], False))[1],
    )

    loaded: list[list[object]] = []
    errors: list[tuple[str, str]] = []
    worker.signals.packages_loaded.connect(loaded.append)
    worker.signals.error_occurred.connect(lambda pkg, msg: errors.append((pkg, msg)))

    worker.run()

    assert not errors
    assert len(loaded) == 1
    assert len(loaded[0]) == 1
    assert loaded[0][0].name == "bash"
    assert loaded[0][0].primary_category == "fedora_core"


def test_inspect_local_rpm_file_releases_rpm_global_lock_before_classification(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Native local-RPM inspection releases RPM_GLOBAL_LOCK before taxonomy lookup."""
    import threading

    from dendro.core import rpm_inspection

    local_rpm = tmp_path / "sample.rpm"
    local_rpm.write_bytes(b"fake-rpm-payload")

    class _TrackedLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()
            self.depth = 0
            self.enter_count = 0

        def __enter__(self) -> None:
            self._lock.acquire()
            self.depth += 1
            self.enter_count += 1

        def __exit__(self, exc_type, exc, tb) -> None:
            self.depth -= 1
            self._lock.release()

    tracked_lock = _TrackedLock()
    monkeypatch.setattr(rpm_inspection, "RPM_GLOBAL_LOCK", tracked_lock)
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", True)

    fake_rpm = SimpleNamespace(
        RPMTAG_NAME=1,
        RPMTAG_VERSION=2,
        RPMTAG_RELEASE=3,
        RPMTAG_ARCH=4,
        RPMTAG_SUMMARY=5,
        RPMTAG_DESCRIPTION=6,
        RPMTAG_LICENSE=7,
        RPMTAG_URL=8,
        RPMTAG_LONGSIZE=9,
        RPMTAG_REQUIRENAME=10,
        RPMTAG_PROVIDENAME=11,
        RPMTAG_DIRNAMES=12,
        RPMTAG_BASENAMES=13,
        RPMTAG_DIRINDEXES=14,
        RPMTAG_SOURCERPM=15,
    )
    fake_header = {
        fake_rpm.RPMTAG_NAME: b"sample-cli",
        fake_rpm.RPMTAG_VERSION: b"1.0",
        fake_rpm.RPMTAG_RELEASE: b"1.fc44",
        fake_rpm.RPMTAG_ARCH: b"x86_64",
        fake_rpm.RPMTAG_SUMMARY: b"Sample CLI",
        fake_rpm.RPMTAG_DESCRIPTION: b"Sample tool",
        fake_rpm.RPMTAG_LICENSE: b"MIT",
        fake_rpm.RPMTAG_URL: b"https://example.invalid",
        fake_rpm.RPMTAG_LONGSIZE: 4096,
        fake_rpm.RPMTAG_REQUIRENAME: [b"libc.so.6()(64bit)"],
        fake_rpm.RPMTAG_PROVIDENAME: [],
        fake_rpm.RPMTAG_DIRNAMES: [b"/usr/bin/"],
        fake_rpm.RPMTAG_BASENAMES: [b"sample-cli"],
        fake_rpm.RPMTAG_DIRINDEXES: [0],
        fake_rpm.RPMTAG_SOURCERPM: b"sample-cli-1.0-1.fc44.src.rpm",
    }

    class _FakeTs:
        def hdrFromFdno(self, _fd: int):
            assert tracked_lock.depth > 0
            return fake_header

        def dbMatch(self, *_args):
            assert tracked_lock.depth > 0
            return ()

    monkeypatch.setattr(backend, "rpm", fake_rpm)
    monkeypatch.setattr(
        rpm_inspection,
        "_create_read_only_rpm_transaction_set_unlocked",
        lambda: _FakeTs(),
    )

    observed_lock_depths_in_classify: list[int] = []
    original_classify = backend._classify_local_rpm_manifest

    def spy_classify(*args, **kwargs):
        observed_lock_depths_in_classify.append(tracked_lock.depth)
        return original_classify(*args, **kwargs)

    monkeypatch.setattr(backend, "_classify_local_rpm_manifest", spy_classify)

    result = backend.inspect_local_rpm_file(str(local_rpm), raise_on_failure=True)
    assert result is not None
    assert result.name == "sample-cli"
    assert tracked_lock.enter_count == 1
    assert tracked_lock.depth == 0
    assert observed_lock_depths_in_classify == [0]
