"""Coverage for reading installed Flatpak versions and writing honest update results."""

from __future__ import annotations

import subprocess

import pytest

from core import flatpak_versions as fv
from core.flatpak_inventory import FLATPAK_BINARY, FlatpakEntry


def _entry(
    application_id: str = "org.gnome.Easy", arch: str = "x86_64", branch: str = "stable"
) -> FlatpakEntry:
    return FlatpakEntry(
        kind="app",
        name="Easy Effects",
        application_id=application_id,
        version="",
        branch=branch,
        arch=arch,
        origin="flathub",
        installation="system",
        ref=f"app/{application_id}/{arch}/{branch}",
        installed_size_text="",
        installed_size_bytes=None,
        runtime="",
    )


class _Runner:
    def __init__(
        self,
        stdout: bytes = b"",
        returncode: int = 0,
        error: BaseException | None = None,
    ):
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(
            command, self.returncode, stdout=self.stdout, stderr=b""
        )


def test_reads_the_matching_row_for_the_requested_scope() -> None:
    runner = _Runner(
        stdout=(
            b"org.other.App\tx86_64\tstable\t1.0\n"
            b"org.gnome.Easy\tx86_64\tstable\t7.1.0\n"
        )
    )

    version = fv.read_installed_version(_entry(), scope_flag="--system", runner=runner)

    assert version == "7.1.0"
    command, kwargs = runner.calls[0]
    assert command == [
        FLATPAK_BINARY,
        "--system",
        "list",
        "--app",
        "--columns=application,arch,branch,version",
    ]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == fv.VERSION_READ_TIMEOUT_SECONDS


def test_arch_and_branch_must_also_match() -> None:
    runner = _Runner(
        stdout=b"org.gnome.Easy\taarch64\tstable\t7.1.0\norg.gnome.Easy\tx86_64\tbeta\t8.0\n"
    )

    assert (
        fv.read_installed_version(_entry(), scope_flag="--user", runner=runner) is None
    )


@pytest.mark.parametrize(
    "runner",
    [
        _Runner(returncode=1),
        _Runner(error=subprocess.TimeoutExpired(cmd="flatpak", timeout=30)),
        _Runner(error=OSError("missing")),
        _Runner(stdout=b"org.gnome.Easy\tx86_64\tstable\n"),
        _Runner(stdout=b"org.gnome.Easy\tx86_64\tstable\t\n"),
    ],
    ids=["nonzero", "timeout", "os-error", "short-row", "empty-version"],
)
def test_unknown_versions_are_none_and_never_raise(runner: _Runner) -> None:
    assert (
        fv.read_installed_version(_entry(), scope_flag="--system", runner=runner)
        is None
    )


def test_unknown_scope_is_rejected() -> None:
    with pytest.raises(ValueError):
        fv.read_installed_version(_entry(), scope_flag="--all", runner=_Runner())


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("7.0.0", "7.1.0", "Updated Easy Effects from version 7.0.0 to 7.1.0."),
        (
            "7.1.0",
            "7.1.0",
            "Flatpak finished, but Easy Effects is still at version 7.1.0, so no newer version was installed.",
        ),
        (None, "7.1.0", "Flatpak finished the update of Easy Effects."),
        ("7.0.0", None, "Flatpak finished the update of Easy Effects."),
    ],
    ids=["changed", "unchanged", "unknown-before", "unknown-after"],
)
def test_update_summary_states_only_what_is_known(before, after, expected) -> None:
    assert fv.update_summary("Easy Effects", before, after) == expected
