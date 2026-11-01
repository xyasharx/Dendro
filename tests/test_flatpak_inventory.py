"""Regression coverage for the read-only Flatpak inventory (core layer)."""

from __future__ import annotations

import os
import subprocess

import pytest

from core import flatpak_inventory as fi
from core.flatpak_inventory import (
    FLATPAK_BINARY,
    FlatpakInventoryError,
    build_flatpak_list_command,
    load_flatpak_inventory,
    parse_flatpak_list_output,
)

COLUMNS_ARG = "--columns=name,application,version,branch,arch,origin,installation,ref,size,runtime"

# Real line captured from Flatpak 1.16.6 (`flatpak list --app --columns=...`,
# stdout not a terminal): tab-separated, no header, the size uses a non-breaking space.
REAL_APP_LINE = (
    "Demo\torg.example.Demo\t\tstable\tx86_64\tlocal\tuser\t"
    "org.example.Demo/x86_64/stable\t1.0\u00a0kB\torg.example.Platform/x86_64/stable"
)
REAL_RUNTIME_LINE = (
    "Freedesktop Platform\torg.freedesktop.Platform\t24.08\t24.08\tx86_64\tflathub\tsystem\t"
    "org.freedesktop.Platform/x86_64/24.08\t1.2 GB\t"
)


def _line(**overrides: str) -> str:
    row = {
        "name": "Firefox",
        "application": "org.mozilla.firefox",
        "version": "141.0",
        "branch": "stable",
        "arch": "x86_64",
        "origin": "flathub",
        "installation": "system",
        "ref": "org.mozilla.firefox/x86_64/stable",
        "size": "312.3 MB",
        "runtime": "org.freedesktop.Platform/x86_64/24.08",
    }
    row.update(overrides)
    return "\t".join(row.values())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parses_real_captured_app_line() -> None:
    (entry,) = parse_flatpak_list_output(REAL_APP_LINE + "\n", "app")

    assert entry.kind == "app"
    assert entry.name == "Demo"
    assert entry.application_id == "org.example.Demo"
    assert entry.version == ""
    assert entry.installation == "user"
    assert entry.ref == "app/org.example.Demo/x86_64/stable"
    assert entry.installed_size_text == "1.0\u00a0kB"
    assert entry.installed_size_bytes == 1000
    assert entry.runtime == "org.example.Platform/x86_64/stable"


def test_parses_real_shaped_runtime_line_with_empty_runtime_column() -> None:
    (entry,) = parse_flatpak_list_output(REAL_RUNTIME_LINE + "\n", "runtime")

    assert entry.kind == "runtime"
    assert entry.ref == "runtime/org.freedesktop.Platform/x86_64/24.08"
    assert entry.installed_size_bytes == 1_200_000_000
    assert entry.runtime == ""


def test_empty_output_means_empty_list_not_an_error() -> None:
    assert parse_flatpak_list_output("", "app") == ()
    assert parse_flatpak_list_output("  \n", "runtime") == ()


def test_same_ref_in_user_and_system_installations_is_allowed() -> None:
    text = _line(installation="system") + "\n" + _line(installation="user") + "\n"
    entries = parse_flatpak_list_output(text, "app")
    assert {entry.installation for entry in entries} == {"system", "user"}


@pytest.mark.parametrize(
    ("size_text", "expected_bytes"),
    [
        ("1.0\u00a0kB", 1000),
        ("1.0?kB", 1000),  # separator byte differs between builds and locales
        ("312.3 MB", 312_300_000),
        ("2.5 GB", 2_500_000_000),
        ("999 B", 999),
    ],
)
def test_known_size_formats_convert_to_bytes(
    size_text: str, expected_bytes: int
) -> None:
    (entry,) = parse_flatpak_list_output(_line(size=size_text) + "\n", "app")
    assert entry.installed_size_text == size_text
    assert entry.installed_size_bytes == expected_bytes


def test_unrecognised_size_text_is_shown_but_not_used_for_sorting() -> None:
    (entry,) = parse_flatpak_list_output(_line(size="unknown") + "\n", "app")
    assert entry.installed_size_text == "unknown"
    assert entry.installed_size_bytes is None


def test_empty_size_text_is_allowed() -> None:
    (entry,) = parse_flatpak_list_output(_line(size="") + "\n", "app")
    assert entry.installed_size_bytes is None


def test_name_falls_back_to_application_id_when_empty() -> None:
    (entry,) = parse_flatpak_list_output(_line(name="") + "\n", "app")
    assert entry.name == "org.mozilla.firefox"


def test_accepts_nine_fields_when_only_the_trailing_runtime_cell_is_missing() -> None:
    nine_fields = "\t".join(_line().split("\t")[:9])  # runtime cell dropped entirely
    (entry,) = parse_flatpak_list_output(nine_fields + "\n", "app")
    assert entry.runtime == ""
    assert entry.installed_size_text == "312.3 MB"


def test_nine_fields_still_fail_the_ref_cross_check_when_misaligned() -> None:
    shifted = "\t".join(
        [
            "Firefox",
            "org.mozilla.firefox",
            "141.0",
            "stable",
            "x86_64",
            "flathub",
            "system",
            "org.mozilla.firefox/x86_64/stable",
            "312.3 MB",
        ]
    )
    # Drop a middle cell so the fields no longer line up with the columns.
    misaligned = shifted.replace("141.0\tstable\t", "stable\t", 1)
    with pytest.raises(FlatpakInventoryError, match="disagree with its ref|fields"):
        parse_flatpak_list_output(misaligned + "\n", "app")


def test_rejects_line_with_wrong_field_count_and_shows_an_excerpt() -> None:
    with pytest.raises(
        FlatpakInventoryError, match="has 7 fields; expected 10"
    ) as info:
        parse_flatpak_list_output("only\tseven\tfields\there\tx\tx\tx\n", "app")
    assert "only\tseven" not in info.value.detail  # tabs are not echoed raw
    assert "'only" in info.value.detail


def test_rejects_malformed_ref() -> None:
    with pytest.raises(FlatpakInventoryError, match="malformed ref"):
        parse_flatpak_list_output(_line(ref="org.mozilla.firefox/x86_64") + "\n", "app")


def test_rejects_full_ref_with_kind_prefix_in_partial_ref_column() -> None:
    # The ref column is documented as the partial form; a kind prefix is not expected.
    with pytest.raises(FlatpakInventoryError, match="malformed ref"):
        parse_flatpak_list_output(
            _line(ref="app/org.mozilla.firefox/x86_64/stable") + "\n", "app"
        )


def test_rejects_columns_that_disagree_with_their_ref() -> None:
    with pytest.raises(FlatpakInventoryError, match="disagree with its ref"):
        parse_flatpak_list_output(_line(application="org.example.Other") + "\n", "app")


def test_rejects_row_without_installation() -> None:
    with pytest.raises(FlatpakInventoryError, match="no installation"):
        parse_flatpak_list_output(_line(installation="") + "\n", "app")


def test_rejects_unknown_kind_argument() -> None:
    with pytest.raises(ValueError):
        parse_flatpak_list_output("", "extension")


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["app", "runtime"])
def test_command_is_fixed_binary_argv_with_explicit_columns(kind: str) -> None:
    command = build_flatpak_list_command(kind)

    assert command == [FLATPAK_BINARY, "list", f"--{kind}", COLUMNS_ARG]
    assert command[0] == "/usr/bin/flatpak"
    assert "--json" not in command
    assert "--bytes" not in command


def test_command_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        build_flatpak_list_command("extension")


# ---------------------------------------------------------------------------
# Loader behaviour (fake runner, no real Flatpak)
# ---------------------------------------------------------------------------
class _FakeRunner:
    """Answers each listing by the --app/--runtime flag it was called with."""

    def __init__(self, outputs=None, error=None, returncode=0, stderr=b""):
        self._outputs = outputs or {}
        self._error = error
        self._returncode = returncode
        self._stderr = stderr
        self.calls: list[dict] = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        if self._error is not None:
            raise self._error
        kind = "app" if "--app" in command else "runtime"
        return subprocess.CompletedProcess(
            args=command,
            returncode=self._returncode,
            stdout=self._outputs.get(kind, "").encode("utf-8"),
            stderr=self._stderr,
        )


def _load(runner, **kwargs):
    return load_flatpak_inventory(
        runner=runner,
        in_sandbox=kwargs.pop("in_sandbox", lambda: False),
        binary_present=kwargs.pop("binary_present", lambda: True),
        **kwargs,
    )


def test_loader_refuses_inside_flatpak_sandbox_without_running_anything() -> None:
    runner = _FakeRunner()
    with pytest.raises(FlatpakInventoryError, match="unavailable in Flatpak"):
        _load(runner, in_sandbox=lambda: True)
    assert runner.calls == []


def test_loader_reports_missing_binary_without_running_anything() -> None:
    runner = _FakeRunner()
    with pytest.raises(
        FlatpakInventoryError, match="not installed at /usr/bin/flatpak"
    ):
        _load(runner, binary_present=lambda: False)
    assert runner.calls == []


def test_loader_runs_both_listings_with_argv_no_shell_clean_env_and_timeout() -> None:
    runner = _FakeRunner(
        outputs={"app": REAL_APP_LINE + "\n", "runtime": REAL_RUNTIME_LINE + "\n"}
    )
    inventory = _load(runner, timeout=7)

    assert [entry.kind for entry in inventory.entries] == ["app", "runtime"]
    assert [call["command"] for call in runner.calls] == [
        build_flatpak_list_command("app"),
        build_flatpak_list_command("runtime"),
    ]
    for call in runner.calls:
        assert "shell" not in call
        assert 0 < call["timeout"] <= 7
        assert call["capture_output"] is True
        assert "PYTHONPATH" not in call["env"]
        assert "LD_LIBRARY_PATH" not in call["env"]


def test_both_listings_share_one_time_budget(monkeypatch) -> None:
    clock = iter([0.0, 3.0, 6.0])  # deadline set at 0, then 3 s and 6 s elapse
    monkeypatch.setattr(fi, "_monotonic", lambda: next(clock))
    runner = _FakeRunner()

    _load(runner, timeout=7)

    assert [call["timeout"] for call in runner.calls] == [4.0, 1.0]


def test_expired_budget_stops_before_the_second_listing(monkeypatch) -> None:
    clock = iter([0.0, 0.0, 100.0])  # the first listing overran the whole budget
    monkeypatch.setattr(fi, "_monotonic", lambda: next(clock))
    runner = _FakeRunner()

    with pytest.raises(FlatpakInventoryError, match="did not finish within 60 seconds"):
        _load(runner)
    assert len(runner.calls) == 1


def test_loader_treats_empty_listings_as_an_empty_inventory() -> None:
    inventory = _load(_FakeRunner())
    assert inventory.entries == ()


def test_loader_maps_timeout_to_user_message() -> None:
    runner = _FakeRunner(error=subprocess.TimeoutExpired(cmd="flatpak", timeout=60))
    with pytest.raises(FlatpakInventoryError, match="did not finish within 60 seconds"):
        _load(runner)


def test_loader_maps_os_error_without_leaking_paths() -> None:
    runner = _FakeRunner(error=PermissionError("/usr/bin/flatpak: denied"))
    with pytest.raises(FlatpakInventoryError) as info:
        _load(runner)
    assert "PermissionError" in info.value.detail
    assert "/usr/bin/flatpak" not in info.value.detail


def test_nonzero_exit_reports_sanitized_stderr_excerpt() -> None:
    stderr = b"error: remote\x1b[31m broke\x1b[0m\n" + b"x" * 1000
    runner = _FakeRunner(returncode=1, stderr=stderr)
    with pytest.raises(FlatpakInventoryError) as info:
        _load(runner)
    detail = info.value.detail
    assert detail.startswith("Flatpak exited with status 1.")
    assert "\x1b" not in detail
    assert (
        len(detail)
        <= len("Flatpak exited with status 1. ") + fi.MAX_STDERR_EXCERPT_CHARS
    )


def test_unknown_option_error_from_old_flatpak_is_shown_not_parsed() -> None:
    runner = _FakeRunner(returncode=1, stderr=b"error: Unknown option --bytes\n")
    with pytest.raises(FlatpakInventoryError, match="Unknown option --bytes"):
        _load(runner)


def test_oversized_output_is_rejected_before_parsing() -> None:
    runner = _FakeRunner(outputs={"app": " " * (fi.MAX_FLATPAK_OUTPUT_BYTES + 1)})
    with pytest.raises(FlatpakInventoryError, match="too large"):
        _load(runner)


def test_invalid_utf8_stdout_is_rejected() -> None:
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=b"\xff\xfe", stderr=b""
        )

    with pytest.raises(FlatpakInventoryError, match="not valid UTF-8"):
        _load(runner)


def test_duplicate_ref_within_one_installation_is_rejected_across_the_loader() -> None:
    runner = _FakeRunner(outputs={"app": REAL_APP_LINE + "\n" + REAL_APP_LINE + "\n"})
    with pytest.raises(FlatpakInventoryError, match="twice"):
        _load(runner)


def test_worker_reports_exactly_one_failure_with_its_serial(qtbot) -> None:
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


@pytest.mark.skipif(
    not os.path.isfile(FLATPAK_BINARY), reason="flatpak is not installed"
)
def test_real_flatpak_binary_lists_without_error_outside_a_sandbox() -> None:
    """Integration: the real binary, the real flags, whatever is installed here."""
    inventory = load_flatpak_inventory(in_sandbox=lambda: False)
    assert all(entry.kind in ("app", "runtime") for entry in inventory.entries)
