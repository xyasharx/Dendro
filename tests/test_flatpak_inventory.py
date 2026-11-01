"""Regression coverage for the read-only Flatpak inventory (core layer)."""

from __future__ import annotations

import json
import subprocess

import pytest

from core import flatpak_inventory as fi
from core.flatpak_inventory import (
    FLATPAK_BINARY,
    FlatpakInventoryError,
    build_flatpak_list_command,
    load_flatpak_inventory,
    parse_flatpak_list_json,
)


def _row(**overrides: str) -> dict[str, str]:
    row = {
        "name": "Firefox",
        "application_id": "org.mozilla.firefox",
        "version": "141.0",
        "branch": "stable",
        "arch": "x86_64",
        "origin": "flathub",
        "installation": "system",
        "ref": "app/org.mozilla.firefox/x86_64/stable",
        "installed_size": "312345678",
        "runtime": "org.freedesktop.Platform/x86_64/24.08",
    }
    row.update(overrides)
    return row


RUNTIME_ROW = {
    "name": "Freedesktop Platform",
    "application_id": "org.freedesktop.Platform",
    "version": "24.08",
    "branch": "24.08",
    "arch": "x86_64",
    "origin": "flathub",
    "installation": "system",
    "ref": "runtime/org.freedesktop.Platform/x86_64/24.08",
    "installed_size": "1000000000",
    "runtime": "",
}


def _json(*rows: dict[str, str]) -> str:
    return json.dumps(list(rows), indent=4)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parses_apps_and_runtimes_from_real_shaped_output() -> None:
    entries = parse_flatpak_list_json(_json(_row(), RUNTIME_ROW))

    assert [entry.kind for entry in entries] == ["app", "runtime"]
    app = entries[0]
    assert app.name == "Firefox"
    assert app.application_id == "org.mozilla.firefox"
    assert app.installed_size_bytes == 312345678
    assert app.installation == "system"
    assert app.runtime == "org.freedesktop.Platform/x86_64/24.08"
    assert entries[1].runtime == ""


def test_empty_stdout_means_empty_inventory_not_an_error() -> None:
    assert parse_flatpak_list_json("") == ()
    assert parse_flatpak_list_json("  \n") == ()


def test_empty_array_is_an_empty_inventory() -> None:
    assert parse_flatpak_list_json("[]") == ()


def test_same_ref_in_user_and_system_installations_is_allowed() -> None:
    user_row = _row(installation="user")
    entries = parse_flatpak_list_json(_json(_row(), user_row))
    assert {entry.installation for entry in entries} == {"system", "user"}


def test_missing_size_is_none_and_missing_runtime_defaults_to_empty() -> None:
    row = _row(installed_size="")
    del row["runtime"]
    (entry,) = parse_flatpak_list_json(_json(row))
    assert entry.installed_size_bytes is None
    assert entry.runtime == ""


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"name": "Firefox"}',
        '"just a string"',
        '["not an object"]',
    ],
)
def test_rejects_malformed_or_unexpected_top_level_shapes(text: str) -> None:
    with pytest.raises(FlatpakInventoryError):
        parse_flatpak_list_json(text)


def test_rejects_row_missing_a_required_field() -> None:
    row = _row()
    del row["ref"]
    with pytest.raises(FlatpakInventoryError, match="missing fields: ref"):
        parse_flatpak_list_json(_json(row))


def test_rejects_non_text_cell_values() -> None:
    row = _row(version="1.0")
    row["version"] = 141  # type: ignore[assignment]
    with pytest.raises(FlatpakInventoryError, match="non-text 'version'"):
        parse_flatpak_list_json(_json(row))


def test_rejects_unknown_kind_in_ref() -> None:
    row = _row(ref="extension/org.mozilla.firefox/x86_64/stable")
    with pytest.raises(FlatpakInventoryError, match="unknown kind"):
        parse_flatpak_list_json(_json(row))


def test_rejects_malformed_ref() -> None:
    row = _row(ref="app/org.mozilla.firefox/x86_64")
    with pytest.raises(FlatpakInventoryError, match="malformed ref"):
        parse_flatpak_list_json(_json(row))


def test_rejects_columns_that_disagree_with_their_ref() -> None:
    row = _row(application_id="org.example.Other")
    with pytest.raises(FlatpakInventoryError, match="disagree with its ref"):
        parse_flatpak_list_json(_json(row))


@pytest.mark.parametrize("size", ["-1", "1.5", "12 MB", "١٢"])
def test_rejects_non_plain_digit_sizes(size: str) -> None:
    with pytest.raises(FlatpakInventoryError, match="invalid installed size"):
        parse_flatpak_list_json(_json(_row(installed_size=size)))


def test_rejects_duplicate_ref_within_one_installation() -> None:
    with pytest.raises(FlatpakInventoryError, match="twice"):
        parse_flatpak_list_json(_json(_row(), _row()))


def test_rejects_row_without_installation() -> None:
    with pytest.raises(FlatpakInventoryError, match="no installation"):
        parse_flatpak_list_json(_json(_row(installation="")))


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------
def test_command_is_fixed_binary_argv_with_json_bytes_and_explicit_columns() -> None:
    command = build_flatpak_list_command()

    assert command[0] == FLATPAK_BINARY == "/usr/bin/flatpak"
    assert command[1:3] == ["list", "--json"]
    assert "--bytes" in command
    columns = [part for part in command if part.startswith("--columns=")]
    assert columns == [
        "--columns=name,application,version,branch,arch,origin,installation,ref,size,runtime"
    ]
    assert all(isinstance(part, str) for part in command)


# ---------------------------------------------------------------------------
# Loader behaviour (fake runner, no real Flatpak)
# ---------------------------------------------------------------------------
class _FakeRunner:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls: list[dict] = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        if self._error is not None:
            raise self._error
        return self._result


def _completed(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_loader_refuses_inside_flatpak_sandbox_without_running_anything() -> None:
    runner = _FakeRunner(_completed(b"[]"))
    with pytest.raises(FlatpakInventoryError, match="unavailable in Flatpak"):
        load_flatpak_inventory(
            runner=runner, in_sandbox=lambda: True, binary_present=lambda: True
        )
    assert runner.calls == []


def test_loader_reports_missing_binary_without_running_anything() -> None:
    runner = _FakeRunner(_completed(b"[]"))
    with pytest.raises(
        FlatpakInventoryError, match="not installed at /usr/bin/flatpak"
    ):
        load_flatpak_inventory(
            runner=runner, in_sandbox=lambda: False, binary_present=lambda: False
        )
    assert runner.calls == []


def test_loader_uses_argv_no_shell_clean_env_and_timeout() -> None:
    runner = _FakeRunner(_completed(_json(_row(), RUNTIME_ROW).encode()))
    inventory = load_flatpak_inventory(
        runner=runner, in_sandbox=lambda: False, binary_present=lambda: True, timeout=7
    )

    assert len(inventory.entries) == 2
    (call,) = runner.calls
    assert call["command"] == build_flatpak_list_command()
    assert "shell" not in call
    assert call["timeout"] == 7
    assert call["capture_output"] is True
    assert "PYTHONPATH" not in call["env"]
    assert "LD_LIBRARY_PATH" not in call["env"]


def test_loader_maps_timeout_to_user_message() -> None:
    runner = _FakeRunner(error=subprocess.TimeoutExpired(cmd="flatpak", timeout=60))
    with pytest.raises(FlatpakInventoryError, match="did not finish within 60 seconds"):
        load_flatpak_inventory(
            runner=runner, in_sandbox=lambda: False, binary_present=lambda: True
        )


def test_loader_maps_os_error_without_leaking_paths() -> None:
    runner = _FakeRunner(error=PermissionError("/usr/bin/flatpak: denied"))
    with pytest.raises(FlatpakInventoryError) as info:
        load_flatpak_inventory(
            runner=runner, in_sandbox=lambda: False, binary_present=lambda: True
        )
    assert "PermissionError" in info.value.detail
    assert "/usr/bin/flatpak" not in info.value.detail


def test_nonzero_exit_reports_sanitized_stderr_excerpt_and_ignores_stdout() -> None:
    stderr = b"error: remote\x1b[31m broke\x1b[0m\n" + b"x" * 1000
    runner = _FakeRunner(_completed(b"[]", stderr=stderr, returncode=1))
    with pytest.raises(FlatpakInventoryError) as info:
        load_flatpak_inventory(
            runner=runner, in_sandbox=lambda: False, binary_present=lambda: True
        )
    detail = info.value.detail
    assert detail.startswith("Flatpak exited with status 1.")
    assert "\x1b" not in detail
    assert (
        len(detail)
        <= len("Flatpak exited with status 1. ") + fi.MAX_STDERR_EXCERPT_CHARS
    )


def test_oversized_output_is_rejected_before_parsing() -> None:
    oversized = b" " * (fi.MAX_FLATPAK_OUTPUT_BYTES + 1)
    runner = _FakeRunner(_completed(oversized))
    with pytest.raises(FlatpakInventoryError, match="too large"):
        load_flatpak_inventory(
            runner=runner, in_sandbox=lambda: False, binary_present=lambda: True
        )


def test_invalid_utf8_stdout_is_rejected() -> None:
    runner = _FakeRunner(_completed(b"\xff\xfe[]"))
    with pytest.raises(FlatpakInventoryError, match="not valid UTF-8"):
        load_flatpak_inventory(
            runner=runner, in_sandbox=lambda: False, binary_present=lambda: True
        )


def test_worker_reports_exactly_one_result_with_its_serial(qtbot) -> None:
    def _loader():
        raise FlatpakInventoryError("Flatpak did not finish within 60 seconds.")

    worker = fi.FlatpakInventoryWorker(42, loader=_loader)
    seen: list[tuple[int, str]] = []
    worker.signals.failed.connect(lambda serial, detail: seen.append((serial, detail)))
    worker.run()
    assert seen == [(42, "Flatpak did not finish within 60 seconds.")]


def test_worker_turns_unexpected_exceptions_into_a_failure_not_a_hang(qtbot) -> None:
    def _loader():
        raise RuntimeError("boom")

    worker = fi.FlatpakInventoryWorker(7, loader=_loader)
    seen: list[tuple[int, str]] = []
    worker.signals.failed.connect(lambda serial, detail: seen.append((serial, detail)))
    worker.run()
    assert seen == [(7, "Unexpected error while reading Flatpak: RuntimeError.")]
