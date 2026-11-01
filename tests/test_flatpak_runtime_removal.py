"""Regression coverage for removing a runtime that an uninstalled app leaves unused."""

from __future__ import annotations

import subprocess

import pytest

from core import flatpak_runtime_removal as rr
from core.flatpak_inventory import FlatpakEntry
from core.flatpak_uninstall import FlatpakUninstallError
from core.operation_gate import HostOperationGate

PLATFORM = "org.gnome.Platform/x86_64/47"


def _entry(
    kind: str = "app",
    application_id: str = "org.mozilla.firefox",
    branch: str = "stable",
    arch: str = "x86_64",
    installation: str = "user",
    runtime: str = "",
    ref: str | None = None,
) -> FlatpakEntry:
    return FlatpakEntry(
        kind=kind,
        name=application_id.rsplit(".", 1)[-1],
        application_id=application_id,
        version="1",
        branch=branch,
        arch=arch,
        origin="flathub",
        installation=installation,
        ref=ref or f"{kind}/{application_id}/{arch}/{branch}",
        installed_size_text="1.0 MB",
        installed_size_bytes=1_000_000,
        runtime=runtime,
    )


def _platform(installation: str = "user", branch: str = "47") -> FlatpakEntry:
    return _entry(
        kind="runtime",
        application_id="org.gnome.Platform",
        branch=branch,
        installation=installation,
        runtime="",
    )


class _Result:
    def __init__(self, returncode: int = 0, stderr: bytes = b"", stdout: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


class _Runner:
    """Records every call; answers --version and uninstall from preset values."""

    def __init__(self, uninstall: _Result | Exception | None = None, version=b"Flatpak 1.18.4\n"):
        self.calls: list[list[str]] = []
        self.uninstall = uninstall or _Result()
        self.version = version

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if command[-1] == "--version":
            return _Result(stdout=self.version)
        if isinstance(self.uninstall, Exception):
            raise self.uninstall
        return self.uninstall


def _run(runner, runtime, gate=None, **kwargs):
    return rr.run_flatpak_runtime_removal(
        runtime,
        gate=gate,
        runner=runner,
        in_sandbox=lambda: False,
        binary_present=lambda: True,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Which runtime may go
# --------------------------------------------------------------------------
def test_runtime_is_offered_when_the_app_is_its_only_user() -> None:
    app = _entry(runtime=PLATFORM)
    entries = (app, _platform())

    assert rr.runtime_left_unused(app, entries) == _platform()


def test_system_app_with_system_runtime_is_offered_too() -> None:
    app = _entry(installation="system", runtime=PLATFORM)
    entries = (app, _platform(installation="system"))

    assert rr.runtime_left_unused(app, entries) == _platform(installation="system")


def test_runtime_is_kept_when_another_app_uses_it() -> None:
    app = _entry(runtime=PLATFORM)
    other = _entry(application_id="org.gimp.GIMP", runtime=PLATFORM)

    assert rr.runtime_left_unused(app, (app, other, _platform())) is None


def test_runtime_is_kept_when_another_installation_app_uses_it() -> None:
    app = _entry(runtime=PLATFORM)
    other = _entry(
        application_id="org.gimp.GIMP", installation="system", runtime=PLATFORM
    )

    assert rr.runtime_left_unused(app, (app, other, _platform())) is None


def test_runtime_is_kept_when_an_installed_extension_names_it() -> None:
    app = _entry(runtime=PLATFORM)
    extension = _entry(
        kind="runtime",
        application_id="org.freedesktop.Platform.GL.nvidia",
        branch="47",
        runtime=PLATFORM,
    )

    assert rr.runtime_left_unused(app, (app, extension, _platform())) is None


def test_runtime_is_kept_when_it_is_only_in_another_installation() -> None:
    app = _entry(runtime=PLATFORM)
    runtime_elsewhere = _platform(installation="system")

    assert rr.runtime_left_unused(app, (app, runtime_elsewhere)) is None


def test_app_without_a_listed_runtime_offers_nothing() -> None:
    app = _entry(runtime="")

    assert rr.runtime_left_unused(app, (app, _platform())) is None


def test_missing_or_ambiguous_runtime_offers_nothing() -> None:
    app = _entry(runtime=PLATFORM)

    assert rr.runtime_left_unused(app, (app,)) is None
    assert rr.runtime_left_unused(app, (app, _platform(), _platform())) is None


def test_runtime_in_an_unknown_installation_is_never_offered() -> None:
    app = _entry(installation="custom-named", runtime=PLATFORM)
    runtime = _platform(installation="custom-named")

    assert rr.runtime_left_unused(app, (app, runtime)) is None


def test_runtime_itself_is_not_treated_as_a_user_of_itself() -> None:
    app = _entry(runtime=PLATFORM)

    # The runtime entry lists no runtime of its own; the app being removed is skipped.
    assert rr.runtime_left_unused(app, (app, _platform())) == _platform()


# --------------------------------------------------------------------------
# Ref validation and command shape
# --------------------------------------------------------------------------
def test_user_runtime_command_is_exact_and_never_uses_unused() -> None:
    command = rr.build_runtime_removal_command(_platform())

    assert command == [
        rr.FLATPAK_BINARY,
        "--user",
        "uninstall",
        "--noninteractive",
        "-y",
        "--no-related",
        "runtime/org.gnome.Platform/x86_64/47",
    ]
    assert "--unused" not in command
    assert "--all" not in command


def test_system_runtime_command_omits_noninteractive_so_polkit_can_prompt() -> None:
    command = rr.build_runtime_removal_command(_platform(installation="system"))

    assert command == [
        rr.FLATPAK_BINARY,
        "--system",
        "uninstall",
        "-y",
        "--no-related",
        "runtime/org.gnome.Platform/x86_64/47",
    ]
    assert "--noninteractive" not in command


@pytest.mark.parametrize(
    "bad",
    [
        _entry(kind="app", application_id="org.gnome.Platform"),
        _platform(installation="custom-named"),
        _entry(
            kind="runtime",
            application_id="--all",
            branch="47",
        ),
        _entry(
            kind="runtime",
            application_id="org.gnome.Platform",
            branch="../47",
        ),
        _entry(
            kind="runtime",
            application_id="org.gnome.Platform",
            branch="47",
            ref="runtime/org.gnome.Platform/x86_64/46",
        ),
    ],
)
def test_invalid_runtime_entries_are_refused_before_any_command(bad) -> None:
    with pytest.raises(ValueError):
        rr.build_runtime_removal_command(bad)


# --------------------------------------------------------------------------
# Running the removal
# --------------------------------------------------------------------------
def test_user_runtime_removal_runs_the_exact_command_and_reports_it() -> None:
    runner = _Runner()

    message = _run(runner, _platform(), gate=None)

    assert runner.calls == [rr.build_runtime_removal_command(_platform())]
    assert message == "Removed the unused runtime org.gnome.Platform 47."


def test_user_runtime_failure_is_reported_with_flatpak_excerpt() -> None:
    runner = _Runner(uninstall=_Result(returncode=1, stderr=b"still in use\n"))

    with pytest.raises(FlatpakUninstallError) as caught:
        _run(runner, _platform(), gate=None)

    assert "could not remove org.gnome.Platform 47" in caught.value.detail
    assert "still in use" in caught.value.detail


def test_user_runtime_timeout_is_reported_not_raised_raw() -> None:
    runner = _Runner(uninstall=subprocess.TimeoutExpired(cmd="flatpak", timeout=1))

    with pytest.raises(FlatpakUninstallError, match="did not finish"):
        _run(runner, _platform(), gate=None)


def test_sandbox_refuses_before_running_anything() -> None:
    runner = _Runner()

    with pytest.raises(FlatpakUninstallError):
        rr.run_flatpak_runtime_removal(
            _platform(),
            gate=None,
            runner=runner,
            in_sandbox=lambda: True,
            binary_present=lambda: True,
        )
    assert runner.calls == []


def test_missing_binary_refuses_before_running_anything() -> None:
    runner = _Runner()

    with pytest.raises(FlatpakUninstallError, match="not installed"):
        rr.run_flatpak_runtime_removal(
            _platform(),
            gate=None,
            runner=runner,
            in_sandbox=lambda: False,
            binary_present=lambda: False,
        )
    assert runner.calls == []


def test_system_runtime_is_refused_without_a_gate() -> None:
    runner = _Runner()

    with pytest.raises(FlatpakUninstallError, match="could not confirm"):
        _run(runner, _platform(installation="system"), gate=None)
    assert runner.calls == []


def test_system_runtime_is_refused_while_another_host_operation_runs() -> None:
    gate = HostOperationGate()
    assert gate.claim("dnf", "DNF transaction") is None
    runner = _Runner()

    with pytest.raises(FlatpakUninstallError, match="Another system operation"):
        _run(runner, _platform(installation="system"), gate=gate)
    assert runner.calls == []


def test_system_runtime_is_refused_on_an_old_flatpak() -> None:
    gate = HostOperationGate()
    runner = _Runner(version=b"Flatpak 1.15.0\n")

    with pytest.raises(FlatpakUninstallError, match="older than"):
        _run(runner, _platform(installation="system"), gate=gate)
    assert [c[-1] for c in runner.calls] == ["--version"]
    assert gate.active is None


def test_system_runtime_gate_is_released_on_success_and_on_failure() -> None:
    gate = HostOperationGate()
    runner = _Runner()
    message = _run(runner, _platform(installation="system"), gate=gate)
    assert message == (
        "Removed the unused runtime org.gnome.Platform 47 for everyone."
    )
    assert gate.active is None

    failing = _Runner(uninstall=_Result(returncode=126, stderr=b"denied"))
    with pytest.raises(FlatpakUninstallError):
        _run(failing, _platform(installation="system"), gate=gate)
    assert gate.active is None


def test_system_runtime_is_refused_by_policy_with_flatpak_message() -> None:
    gate = HostOperationGate()
    runner = _Runner(uninstall=_Result(returncode=1, stderr=b"Not authorized\n"))

    with pytest.raises(FlatpakUninstallError) as caught:
        _run(runner, _platform(installation="system"), gate=gate)
    assert "Not authorized" in caught.value.detail
    assert gate.active is None


# --------------------------------------------------------------------------
# Combined app + runtime action
# --------------------------------------------------------------------------
def test_app_failure_means_the_runtime_is_never_touched() -> None:
    runner = _Runner()

    def failing_app_removal() -> str:
        raise FlatpakUninstallError("Flatpak could not uninstall Firefox.")

    with pytest.raises(FlatpakUninstallError):
        rr.uninstall_app_and_runtime(
            failing_app_removal, _platform(), gate=None, runner=runner
        )
    assert runner.calls == []


def test_app_and_runtime_are_both_removed_and_both_reported() -> None:
    runner = _Runner()

    message = rr.uninstall_app_and_runtime(
        lambda: "Uninstalled Firefox.", _platform(), gate=None, runner=runner
    )

    assert message == (
        "Uninstalled Firefox. Removed the unused runtime org.gnome.Platform 47."
    )
    assert runner.calls == [rr.build_runtime_removal_command(_platform())]


def test_runtime_failure_after_app_removal_says_the_runtime_was_kept() -> None:
    runner = _Runner(uninstall=_Result(returncode=1, stderr=b"in use"))

    message = rr.uninstall_app_and_runtime(
        lambda: "Uninstalled Firefox.", _platform(), gate=None, runner=runner
    )

    assert message.startswith("Uninstalled Firefox. The runtime was kept:")
    assert "in use" in message


def test_without_a_runtime_only_the_app_step_runs() -> None:
    runner = _Runner()

    assert (
        rr.uninstall_app_and_runtime(
            lambda: "Uninstalled Firefox.", None, gate=None, runner=runner
        )
        == "Uninstalled Firefox."
    )
    assert runner.calls == []


# --------------------------------------------------------------------------
# Plain-language text
# --------------------------------------------------------------------------
def test_user_description_names_the_runtime_and_scope() -> None:
    text = rr.describe_runtime_removal(_platform())

    assert "org.gnome.Platform 47" in text
    assert "not used by any other installed app" in text
    assert "your user installation" in text


def test_system_description_warns_about_everyone_and_the_password_prompt() -> None:
    text = rr.describe_runtime_removal(_platform(installation="system"))

    assert "for everyone on this computer" in text
    assert "administrator password" in text
