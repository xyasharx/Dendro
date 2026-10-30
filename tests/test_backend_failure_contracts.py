"""Regression coverage for RPM fallback failure contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.backend as backend


def _completed_process(
    returncode: int, *, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _EmptyRpmTransactionSet:
    def dbMatch(self, *_args: object) -> list[object]:
        return []


def test_cli_inventory_rejects_malformed_rpm_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_formats: list[str] = []
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_system_protected_packages", lambda: set())

    def malformed_output(command, **_kwargs):
        query_formats.append(command[3])
        return _completed_process(0, stdout="truncated RPM record\n")

    monkeypatch.setattr(backend.subprocess, "run", malformed_output)
    worker = backend.PackageQueryWorker()

    with pytest.raises(RuntimeError, match="malformed package record"):
        worker._query_cli_subprocess(SimpleNamespace(), SimpleNamespace())

    assert query_formats and "\x1f" in query_formats[0]


def test_native_dependency_query_reports_a_package_that_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", _EmptyRpmTransactionSet)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "missing native package fell through to rpm"
        ),
    )
    worker = backend.DependencyTreeWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object], object]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.dependencies_resolved.connect(
        lambda package, nodes, index: completed.append((package, nodes, index))
    )

    worker.run()

    assert completed == []
    assert errors == [
        ("example", "Dependency error: Package 'example' is not installed.")
    ]


def test_native_file_query_reports_a_package_that_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", _EmptyRpmTransactionSet)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "missing native package fell through to rpm"
        ),
    )
    worker = backend.PackageFilesWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.package_files_loaded.connect(
        lambda package, files: completed.append((package, files))
    )

    worker.run()

    assert completed == []
    assert errors == [
        ("example", "File query error: Package 'example' is not installed.")
    ]


def test_native_reverse_query_reports_a_package_that_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", _EmptyRpmTransactionSet)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "missing native package fell through to rpm"
        ),
    )
    worker = backend.ReverseDependencyWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.reverse_dependencies_resolved.connect(
        lambda package, nodes: completed.append((package, nodes))
    )

    worker.run()

    assert completed == []
    assert errors == [
        ("example", "Reverse dependency error: Package 'example' is not installed.")
    ]


def test_dependency_fallback_reports_a_failed_requirement_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rpm -qR command must not become an empty dependency list."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            2, stderr="RPM database is locked"
        ),
    )
    worker = backend.DependencyTreeWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object], object]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.dependencies_resolved.connect(
        lambda package, nodes, index: completed.append((package, nodes, index))
    )

    worker.run()

    assert completed == []
    assert errors == [
        (
            "example",
            "Dependency error: Could not read requirements for 'example': RPM database is locked",
        )
    ]


def test_provider_fallback_marks_an_unprovided_capability_unsatisfied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rpm's no-provider exit status is an unresolved dependency, not success."""
    worker = backend.DependencyTreeWorker("example")
    cached: list[tuple[str, bool, str]] = []
    worker.cache = SimpleNamespace(set_batch=lambda results: cached.extend(results))
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            1, stderr="no package provides missing-capability"
        ),
    )

    worker._resolve_capabilities_batch(["missing-capability"], None)

    assert cached == [("missing-capability", False, "missing-capability")]


def test_provider_fallback_does_not_cache_a_command_failure_as_a_missing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only rpm's documented no-provider result may produce an unresolved node."""
    worker = backend.DependencyTreeWorker("example")
    worker.cache = SimpleNamespace(
        set_batch=lambda _results: pytest.fail("failed RPM command was cached")
    )
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            2, stderr="RPM database is locked"
        ),
    )

    with pytest.raises(RuntimeError, match="Could not resolve provider"):
        worker._resolve_capabilities_batch(["missing-capability"], None)


def test_file_owner_lookup_can_report_an_rpm_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            2, stderr="RPM database is locked"
        ),
    )

    with pytest.raises(backend.FileOwnerLookupError, match="RPM database is locked"):
        backend.find_package_owning_file("/usr/bin/example", raise_on_failure=True)


def test_native_file_query_reports_a_malformed_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    fake_rpm = SimpleNamespace(
        RPMTAG_FILENAMES="filenames",
        RPMTAG_FILESIZES="filesizes",
        RPMTAG_FILEMODES="filemodes",
    )
    monkeypatch.setattr(backend, "rpm", fake_rpm, raising=False)
    header = {
        "filenames": ["/usr/bin/example"],
        "filesizes": [],
        "filemodes": [0o100755],
    }
    transaction_set = SimpleNamespace(dbMatch=lambda *_args: [header])
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: transaction_set)
    worker = backend.PackageFilesWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.package_files_loaded.connect(
        lambda package, files: completed.append((package, files))
    )

    worker.run()

    assert completed == []
    assert errors == [
        (
            "example",
            "File query error: Native RPM file manifest has mismatched path, size, or mode fields.",
        )
    ]


def test_file_owner_lookup_reports_malformed_successful_rpm_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0, stdout="unexpected RPM diagnostic text\\n"
        ),
    )

    with pytest.raises(
        backend.FileOwnerLookupError, match="one valid package identity"
    ):
        backend.find_package_owning_file("/usr/bin/example", raise_on_failure=True)


def test_file_list_fallback_reports_a_failed_command_instead_of_empty_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            1, stderr="package example is not installed"
        ),
    )
    worker = backend.PackageFilesWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.package_files_loaded.connect(
        lambda package, files: completed.append((package, files))
    )

    worker.run()

    assert completed == []
    assert errors == [
        (
            "example",
            "File query error: Could not read files for 'example': package example is not installed",
        )
    ]


def test_file_list_fallback_reports_malformed_output_instead_of_a_partial_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0,
            stdout="/usr/bin/example\x1f12\x1f100755\ntruncated record\n",
        ),
    )
    worker = backend.PackageFilesWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.package_files_loaded.connect(
        lambda package, files: completed.append((package, files))
    )

    worker.run()

    assert completed == []
    assert errors == [
        (
            "example",
            "File query error: rpm file-list query returned a malformed file record; refusing to display a partial manifest.",
        )
    ]


def test_dnf_history_accepts_its_documented_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf")
    monkeypatch.setattr(backend, "_is_dnf5_binary", lambda _binary: False)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0,
            stdout=(
                "ID | Command line | Date and time | Action(s) | Altered\n"
                "------------------------------------------------------\n"
                "1 | install example | 2026-10-28 09:00 | Install | 1\n"
            ),
        ),
    )
    worker = backend.DnfHistoryWorker()
    errors: list[tuple[str, str]] = []
    completed: list[list[object]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.history_loaded.connect(completed.append)

    worker.run()

    assert errors == []
    assert len(completed) == 1
    assert [(entry.id, entry.altered_count) for entry in completed[0]] == [(1, 1)]


def test_dnf5_history_uses_documented_json_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    monkeypatch.setattr(backend, "_is_dnf5_binary", lambda _binary: True)
    commands: list[list[str]] = []

    def dnf5_json(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return _completed_process(
            0,
            stdout=(
                "["
                '{"id": 42, "command_line": "upgrade --refresh", '
                '"start_time": 1761642000, "end_time": 1761642012, '
                '"user_id": 1000, "status": "Ok", "releasever": "45", '
                '"altered_count": 3}'
                "]"
            ),
        )

    monkeypatch.setattr(backend.subprocess, "run", dnf5_json)
    worker = backend.DnfHistoryWorker()
    errors: list[tuple[str, str]] = []
    completed: list[list[object]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.history_loaded.connect(completed.append)

    worker.run()

    assert commands == [["/usr/bin/dnf5", "history", "list", "--json"]]
    assert errors == []
    assert len(completed) == 1
    entry = completed[0][0]
    assert isinstance(entry, backend.HistoryEntry)
    assert entry.id == 42
    assert entry.command_line == "upgrade --refresh"
    assert entry.date_time == backend.datetime.fromtimestamp(1761642000).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert entry.action == "Ok"
    assert entry.altered_count == 3


def test_dnf5_history_reports_invalid_json_as_a_visible_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    monkeypatch.setattr(backend, "_is_dnf5_binary", lambda _binary: True)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(0, stdout="not JSON"),
    )
    worker = backend.DnfHistoryWorker()
    errors: list[tuple[str, str]] = []
    completed: list[list[object]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.history_loaded.connect(completed.append)

    worker.run()

    assert completed == []
    assert errors == [
        (
            "",
            "Could not read DNF history: dnf5 history returned invalid JSON: Expecting value.",
        )
    ]


def test_dnf5_history_falls_back_when_an_old_cli_rejects_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    monkeypatch.setattr(backend, "_is_dnf5_binary", lambda _binary: True)
    calls: list[tuple[list[str], dict[str, object]]] = []
    responses = iter(
        [
            _completed_process(
                2,
                stderr='Unknown argument "--json" for command "history".',
            ),
            _completed_process(
                0,
                stdout=(
                    "ID | Command line | Date and time | Action(s) | Altered\n"
                    "------------------------------------------------------\n"
                    "1 | install example | 2026-10-28 09:00 | Install | 1\n"
                ),
            ),
        ]
    )

    def old_dnf5(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return next(responses)

    monkeypatch.setattr(backend.subprocess, "run", old_dnf5)
    worker = backend.DnfHistoryWorker()
    errors: list[tuple[str, str]] = []
    completed: list[list[object]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.history_loaded.connect(completed.append)

    worker.run()

    assert [command for command, _kwargs in calls] == [
        ["/usr/bin/dnf5", "history", "list", "--json"],
        ["/usr/bin/dnf5", "history", "list"],
    ]
    fallback_environment = calls[1][1]["env"]
    assert isinstance(fallback_environment, dict)
    assert fallback_environment["LC_ALL"] == "C"
    assert fallback_environment["LANG"] == "C"
    assert errors == []
    assert len(completed) == 1
    assert [(entry.id, entry.altered_count) for entry in completed[0]] == [(1, 1)]


def test_dnf_history_reports_malformed_successful_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf")
    monkeypatch.setattr(backend, "_is_dnf5_binary", lambda _binary: False)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0,
            stdout=(
                "ID | Command line | Date and time | Action(s) | Altered\n"
                "------------------------------------------------------\n"
                "1 | install example | 2026-10-28 09:00 | Install | 1\n"
                "unexpected history diagnostic\n"
            ),
        ),
    )
    worker = backend.DnfHistoryWorker()
    errors: list[tuple[str, str]] = []
    completed: list[list[object]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.history_loaded.connect(completed.append)

    worker.run()

    assert completed == []
    assert errors == [
        (
            "",
            "Could not read DNF history: dnf history returned an unrecognized record: unexpected history diagnostic",
        )
    ]


def test_reverse_dependency_fallback_reports_an_unexpected_rpm_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    responses = iter(
        [
            _completed_process(0, stdout="example\n"),
            _completed_process(2, stderr="RPM database is locked"),
        ]
    )
    monkeypatch.setattr(
        backend.subprocess, "run", lambda *_args, **_kwargs: next(responses)
    )
    worker = backend.ReverseDependencyWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.reverse_dependencies_resolved.connect(
        lambda package, nodes: completed.append((package, nodes))
    )

    worker.run()

    assert completed == []
    assert errors == [
        (
            "example",
            "Reverse dependency error: Could not find dependents for 'example': RPM database is locked",
        )
    ]


def test_reverse_dependency_fallback_rejects_malformed_name_only_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    commands: list[list[str]] = []
    responses = iter(
        [
            _completed_process(0, stdout="example\n"),
            _completed_process(0, stdout="unexpected diagnostic text\n"),
        ]
    )

    def run(command, **_kwargs):
        commands.append(command)
        return next(responses)

    monkeypatch.setattr(backend.subprocess, "run", run)
    worker = backend.ReverseDependencyWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.reverse_dependencies_resolved.connect(
        lambda package, nodes: completed.append((package, nodes))
    )

    worker.run()

    assert completed == []
    assert commands[1][3:5] == ["--queryformat", "%{NAME}" + "\\" + "n"]
    assert errors == [
        (
            "example",
            "Reverse dependency error: rpm reverse-dependency query returned an invalid package-name record: unexpected diagnostic text",
        )
    ]


def test_reverse_dependency_fallback_accepts_rpms_documented_no_match_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rpm -q --whatrequires uses exit status one for a real empty result."""
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    responses = iter(
        [
            _completed_process(0, stdout="example\n"),
            _completed_process(1, stderr="no package requires example"),
        ]
    )
    monkeypatch.setattr(
        backend.subprocess, "run", lambda *_args, **_kwargs: next(responses)
    )
    worker = backend.ReverseDependencyWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.reverse_dependencies_resolved.connect(
        lambda package, nodes: completed.append((package, nodes))
    )

    worker.run()

    assert errors == []
    assert completed == [("example", [])]


def test_verification_reports_a_package_that_disappeared_before_rpm_v(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            1, stderr="package example is not installed"
        ),
    )
    worker = backend.PackageVerifyWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.package_verification_finished.connect(
        lambda package, results: completed.append((package, results))
    )

    worker.run()

    assert completed == []
    assert errors == [
        (
            "example",
            "Could not verify package files: package example is not installed",
        )
    ]


def test_changelog_worker_reports_unrecognized_nonempty_rpm_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0, stdout="unrecognized changelog output\n"
        ),
    )
    worker = backend.PackageChangelogWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.package_changelog_loaded.connect(
        lambda package, entries: completed.append((package, entries))
    )

    worker.run()

    assert completed == []
    assert errors == [
        (
            "example",
            "Could not read package changelog: rpm changelog output did not contain recognizable entries.",
        )
    ]


def test_scriptlet_worker_reports_unrecognized_nonempty_rpm_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0, stdout="unrecognized scriptlet output\n"
        ),
    )
    worker = backend.PackageScriptletsWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, object]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.package_scriptlets_loaded.connect(
        lambda package, scripts: completed.append((package, scripts))
    )

    worker.run()

    assert completed == []
    assert errors == [
        (
            "example",
            "Could not read RPM scriptlets: rpm scriptlet output did not contain recognizable scriptlet headings.",
        )
    ]


def test_file_inspector_fallback_uses_the_selected_multilib_nevra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = backend.PackageInfo(
        name="shared-runtime",
        epoch="2",
        version="4.0",
        release="1.fc44",
        arch="i686",
    )
    commands: list[list[str]] = []

    def rpm_query(command, **_kwargs):
        commands.append(command)
        return _completed_process(
            0,
            stdout="/usr/lib/libshared.so\x1f10\x1f100755\n",
        )

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    monkeypatch.setattr(backend.subprocess, "run", rpm_query)
    worker = backend.PackageFilesWorker("shared-runtime", package_info=target)
    completed: list[tuple[str, list[object]]] = []
    worker.signals.package_files_loaded.connect(
        lambda package, files: completed.append((package, files))
    )

    worker.run()

    assert commands == [
        [
            "rpm",
            "-q",
            "--queryformat",
            "[%{FILENAMES}\x1f%{FILESIZES}\x1f%{FILEMODES:octal}\n]",
            "--",
            "shared-runtime-2:4.0-1.fc44.i686",
        ]
    ]
    assert completed[0][0] == "shared-runtime"
    assert completed[0][1][0].path == "/usr/lib/libshared.so"


def test_file_list_cli_fallback_preserves_paths_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", lambda: None)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0, stdout="/usr/share/example files/read me.txt\x1f12\x1f100644\n"
        ),
    )
    worker = backend.PackageFilesWorker("example")
    completed: list[tuple[str, list[object]]] = []
    worker.signals.package_files_loaded.connect(
        lambda package, files: completed.append((package, files))
    )

    worker.run()

    assert completed[0][1][0].path == "/usr/share/example files/read me.txt"
    assert completed[0][1][0].size_bytes == 12
    assert completed[0][1][0].mode == "100644"


def test_changelog_worker_discards_partial_native_entries_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TransactionSet:
        def dbMatch(self, *_args: object) -> list[object]:
            return [
                {
                    "names": ["Native One", "Native Two"],
                    "times": [1, "not-a-timestamp"],
                    "texts": ["partial native entry", "broken entry"],
                }
            ]

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", TransactionSet)
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(
            RPMTAG_CHANGELOGNAME="names",
            RPMTAG_CHANGELOGTIME="times",
            RPMTAG_CHANGELOGTEXT="texts",
        ),
        raising=False,
    )
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0, stdout="* Fallback Maintainer\nfallback changelog entry\n"
        ),
    )
    worker = backend.PackageChangelogWorker("example")
    completed: list[tuple[str, list[object]]] = []
    errors: list[tuple[str, str]] = []
    worker.signals.package_changelog_loaded.connect(
        lambda package, entries: completed.append((package, entries))
    )
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )

    worker.run()

    assert errors == []
    assert len(completed) == 1
    assert [entry.author for entry in completed[0][1]] == ["Fallback Maintainer"]


def test_scriptlet_worker_discards_partial_native_fields_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PartialHeader(dict):
        def __getitem__(self, key: object) -> object:
            if key == "postin":
                raise RuntimeError("malformed post-install tag")
            return super().__getitem__(key)

    class TransactionSet:
        def dbMatch(self, *_args: object) -> list[object]:
            return [
                PartialHeader(
                    prein="native partial script",
                    postin="",
                    preun="",
                    postun="",
                )
            ]

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "create_rpm_transaction_set", TransactionSet)
    monkeypatch.setattr(
        backend,
        "rpm",
        SimpleNamespace(
            RPMTAG_PREIN="prein",
            RPMTAG_POSTIN="postin",
            RPMTAG_PREUN="preun",
            RPMTAG_POSTUN="postun",
        ),
        raising=False,
    )
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0,
            stdout="preinstall scriptlet (using /bin/sh):\nfallback full script\n",
        ),
    )
    worker = backend.PackageScriptletsWorker("example")
    completed: list[tuple[str, object]] = []
    errors: list[tuple[str, str]] = []
    worker.signals.package_scriptlets_loaded.connect(
        lambda package, scripts: completed.append((package, scripts))
    )
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )

    worker.run()

    assert errors == []
    assert len(completed) == 1
    assert completed[0][1].prein == "fallback full script"


def test_verification_worker_preserves_missing_config_markers_and_spaced_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            1,
            stdout=(
                "missing   cmng /etc/example config.conf\n"
                "S.5....T. c /etc/another config.conf\n"
            ),
        ),
    )
    worker = backend.PackageVerifyWorker("example")
    completed: list[tuple[str, list[object]]] = []
    errors: list[tuple[str, str]] = []
    worker.signals.package_verification_finished.connect(
        lambda package, results: completed.append((package, results))
    )
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )

    worker.run()

    assert errors == []
    assert len(completed) == 1
    _, results = completed[0]
    assert [
        (result.path, result.is_config, result.is_missing) for result in results
    ] == [
        ("/etc/example config.conf", True, True),
        ("/etc/another config.conf", True, False),
    ]


def test_verification_worker_reports_unrecognized_nonempty_rpm_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            1, stdout="unrecognized verification output\n"
        ),
    )
    worker = backend.PackageVerifyWorker("example")
    errors: list[tuple[str, str]] = []
    completed: list[tuple[str, list[object]]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    worker.signals.package_verification_finished.connect(
        lambda package, results: completed.append((package, results))
    )

    worker.run()

    assert completed == []
    assert errors == [
        (
            "example",
            "Could not verify package files: rpm verification output contained an unrecognized line: unrecognized verification output",
        )
    ]


@pytest.mark.parametrize(
    ("worker_factory", "completed_signal", "failure_prefix"),
    [
        (
            backend.UserInstalledQueryWorker,
            "userinstalled_loaded",
            "Could not identify user-installed packages",
        ),
        (
            backend.OrphanQueryWorker,
            "orphans_loaded",
            "Could not identify unneeded packages",
        ),
    ],
)
def test_auxiliary_package_queries_do_not_replace_metadata_with_empty_results_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    worker_factory,
    completed_signal: str,
    failure_prefix: str,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            1, stderr="metadata is unavailable"
        ),
    )
    worker = worker_factory()
    errors: list[tuple[str, str]] = []
    completed: list[set[str]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    getattr(worker.signals, completed_signal).connect(
        lambda package_set: completed.append(package_set)
    )

    worker.run()

    assert completed == []
    assert errors == [("", f"{failure_prefix}: metadata is unavailable")]


@pytest.mark.parametrize(
    ("worker_factory", "completed_signal", "failure_prefix"),
    [
        (
            backend.UserInstalledQueryWorker,
            "userinstalled_loaded",
            "Could not identify user-installed packages",
        ),
        (
            backend.OrphanQueryWorker,
            "orphans_loaded",
            "Could not identify unneeded packages",
        ),
    ],
)
def test_auxiliary_package_queries_report_malformed_identity_output(
    monkeypatch: pytest.MonkeyPatch,
    worker_factory,
    completed_signal: str,
    failure_prefix: str,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0, stdout="unexpected diagnostic text\n"
        ),
    )
    worker = worker_factory()
    errors: list[tuple[str, str]] = []
    completed: list[set[str]] = []
    worker.signals.error_occurred.connect(
        lambda package, detail: errors.append((package, detail))
    )
    getattr(worker.signals, completed_signal).connect(completed.append)

    worker.run()

    assert completed == []
    assert errors == [
        (
            "",
            f"{failure_prefix}: RuntimeError: "
            "DNF "
            + (
                "user-installed package query"
                if worker_factory is backend.UserInstalledQueryWorker
                else "unneeded-package query"
            )
            + " returned an invalid package-identity record: unexpected diagnostic text",
        )
    ]


@pytest.mark.parametrize(
    ("worker_factory", "completed_signal"),
    [
        (backend.UserInstalledQueryWorker, "userinstalled_loaded"),
        (backend.OrphanQueryWorker, "orphans_loaded"),
    ],
)
def test_auxiliary_package_queries_preserve_exact_multilib_identities(
    monkeypatch: pytest.MonkeyPatch,
    worker_factory,
    completed_signal: str,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_dnf_binary_path", lambda: "/usr/bin/dnf5")
    commands: list[list[str]] = []

    def repoquery(command, **_kwargs):
        commands.append(command)
        return _completed_process(
            0,
            stdout=(
                "gpg-pubkey\x1f(none)\x1fdeadbeef\x1f1\x1f\n"
                "shared-runtime\x1f(none)\x1f4.0\x1f1.fc44\x1fx86_64\n"
                "shared-runtime\x1f2\x1f4.0\x1f1.fc44\x1fi686\n"
            ),
        )

    monkeypatch.setattr(backend.subprocess, "run", repoquery)
    worker = worker_factory()
    completed: list[set[str]] = []
    getattr(worker.signals, completed_signal).connect(completed.append)

    worker.run()

    assert completed == [
        {
            "shared-runtime-4.0-1.fc44.x86_64",
            "shared-runtime-2:4.0-1.fc44.i686",
        }
    ]
    assert "%{name}\x1f%{epoch}\x1f%{version}\x1f%{release}\x1f%{arch}\n" in commands[0]


def test_local_rpm_inspection_worker_publishes_the_inspection_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = object()
    monkeypatch.setattr(
        backend, "inspect_local_rpm_file", lambda _path, **_kwargs: result
    )
    worker = backend.LocalRpmInspectionWorker("/tmp/example.rpm")
    completed: list[tuple[str, object]] = []
    failures: list[tuple[str, str]] = []
    worker.signals.inspected.connect(
        lambda path, inspected: completed.append((path, inspected))
    )
    worker.signals.failed.connect(lambda path, detail: failures.append((path, detail)))

    worker.run()

    assert completed == [("/tmp/example.rpm", result)]
    assert failures == []


def test_local_rpm_inspection_worker_reports_a_typed_inspection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_path: str, **_kwargs: object) -> None:
        raise backend.LocalRpmInspectionError("RPM database is locked")

    monkeypatch.setattr(backend, "inspect_local_rpm_file", fail)
    worker = backend.LocalRpmInspectionWorker("/tmp/example.rpm")
    completed: list[tuple[str, object]] = []
    failures: list[tuple[str, str]] = []
    worker.signals.inspected.connect(
        lambda path, inspected: completed.append((path, inspected))
    )
    worker.signals.failed.connect(lambda path, detail: failures.append((path, detail)))

    worker.run()

    assert completed == []
    assert failures == [("/tmp/example.rpm", "RPM database is locked")]


def test_local_rpm_fallback_rejects_malformed_successful_header_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", False)
    monkeypatch.setattr(backend.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed_process(
            0, stdout="truncated package-header output\\n"
        ),
    )

    with pytest.raises(
        backend.LocalRpmInspectionError, match="malformed package record"
    ):
        backend.inspect_local_rpm_file("/tmp/example.rpm", raise_on_failure=True)


def test_local_rpm_fallback_reports_failed_capability_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header_record = "\x1f".join(
        ("example", "1", "1.fc44", "noarch", "Example", "MIT", "", "42")
    )
    responses = iter(
        (
            _completed_process(0, stdout=header_record + "\n"),
            _completed_process(1, stderr="payload metadata is unavailable"),
        )
    )
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "HAS_NATIVE_RPM", False)
    monkeypatch.setattr(backend.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(
        backend.LocalRpmInspectionError,
        match="Could not read local RPM capabilities: payload metadata is unavailable",
    ):
        backend.inspect_local_rpm_file("/tmp/example.rpm", raise_on_failure=True)


def test_local_rpm_inspection_worker_reports_an_unexpected_parser_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_path: str, **_kwargs: object) -> None:
        raise RuntimeError("bad RPM headers")

    monkeypatch.setattr(backend, "inspect_local_rpm_file", fail)
    worker = backend.LocalRpmInspectionWorker("/tmp/example.rpm")
    completed: list[tuple[str, object]] = []
    failures: list[tuple[str, str]] = []
    worker.signals.inspected.connect(
        lambda path, inspected: completed.append((path, inspected))
    )
    worker.signals.failed.connect(lambda path, detail: failures.append((path, detail)))

    worker.run()

    assert completed == []
    assert failures == [("/tmp/example.rpm", "RuntimeError: bad RPM headers")]
