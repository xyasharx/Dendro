"""Coverage for updating system-wide Flatpak apps through Flatpak's helper (Phase A)."""

from __future__ import annotations

import subprocess

import pytest
from PyQt6.QtWidgets import QWidget

from core import flatpak_system_update as fsu
from core.flatpak_inventory import FLATPAK_BINARY, FlatpakEntry, FlatpakInventory
from core.flatpak_uninstall import FlatpakUninstallError
from core.operation_gate import HostOperationGate
from ui import flatpak_dialog as fd
from ui.flatpak_dialog import FlatpakInventoryDialog, start_flatpak_update

GEDIT_REF = "app/org.gnome.gedit/x86_64/stable"


def _entry(
    *,
    kind: str = "app",
    installation: str = "system",
    application_id: str = "org.gnome.gedit",
    name: str = "gedit",
    ref: str | None = None,
) -> FlatpakEntry:
    return FlatpakEntry(
        kind=kind,
        name=name,
        application_id=application_id,
        version="47.0",
        branch="stable",
        arch="x86_64",
        origin="flathub",
        installation=installation,
        ref=ref or f"{kind}/{application_id}/x86_64/stable",
        installed_size_text="20.1 MB",
        installed_size_bytes=20_100_000,
        runtime="",
    )


class _Completed:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeFlatpak:
    """Stands in for subprocess.run. Answers ``--version`` and records every call."""

    def __init__(
        self,
        *,
        version: bytes = b"Flatpak 1.18.4\n",
        version_returncode: int = 0,
        update: _Completed | Exception | None = None,
        listings: tuple[bytes | None, ...] = (
            b"org.gnome.gedit\tx86_64\tstable\t47.0\n",
            b"org.gnome.gedit\tx86_64\tstable\t47.0\n",
        ),
    ):
        self.version = version
        self.version_returncode = version_returncode
        self.update = update or _Completed(0)
        self.listings = list(listings)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if command[-1] == "--version":
            return _Completed(self.version_returncode, stdout=self.version)
        if "list" in command:
            output = self.listings.pop(0) if self.listings else b""
            if output is None:
                return _Completed(1, stderr=b"list failed")
            return _Completed(0, stdout=output)
        if isinstance(self.update, Exception):
            raise self.update
        return self.update

    @property
    def update_calls(self) -> list[tuple[list[str], dict]]:
        return [call for call in self.calls if "update" in call[0]]


def _run(entry: FlatpakEntry, gate: HostOperationGate | None, fake: _FakeFlatpak, **kw):
    return fsu.run_flatpak_system_update(
        entry,
        gate=gate,
        runner=fake,
        in_sandbox=kw.pop("in_sandbox", lambda: False),
        binary_present=kw.pop("binary_present", lambda: True),
        **kw,
    )


# ---------------------------------------------------------------------------
# Scope, validation, and the exact command
# ---------------------------------------------------------------------------


def test_command_is_system_update_without_noninteractive() -> None:
    command = fsu.build_system_update_command(_entry())

    assert command == [FLATPAK_BINARY, "--system", "update", "-y", GEDIT_REF]
    assert "--noninteractive" not in command
    assert "--user" not in command
    assert "--delete-data" not in command
    assert "--all" not in command


@pytest.mark.parametrize(
    "entry",
    [
        _entry(installation="user"),
        _entry(installation="my-installation"),
        _entry(kind="runtime", application_id="org.freedesktop.Platform"),
    ],
    ids=["user-app", "named-installation", "runtime"],
)
def test_command_refuses_out_of_scope_entries(entry: FlatpakEntry) -> None:
    with pytest.raises(ValueError):
        fsu.build_system_update_command(entry)


@pytest.mark.parametrize(
    "application_id",
    ["--all", "-y", "org.x", "../../etc.passwd", "org.x.y;rm", "org.gnome.gedit --all"],
)
def test_command_refuses_option_like_or_malformed_ids(application_id: str) -> None:
    entry = _entry(
        application_id=application_id, ref=f"app/{application_id}/x86_64/stable"
    )

    with pytest.raises(ValueError):
        fsu.build_system_update_command(entry)


def test_command_refuses_ref_that_does_not_match_the_entry() -> None:
    entry = _entry(ref="app/org.gnome.Evil/x86_64/stable")

    with pytest.raises(ValueError):
        fsu.build_system_update_command(entry)


@pytest.mark.parametrize("installation", ["system", "default"])
def test_system_wide_installation_names_are_accepted(installation: str) -> None:
    entry = _entry(installation=installation)

    assert fsu.is_system_app(entry) is True
    assert fsu.build_system_update_command(entry)[1:3] == ["--system", "update"]


@pytest.mark.parametrize("installation", ["user", "my-installation", ""])
def test_other_installations_are_not_system_apps(installation: str) -> None:
    assert fsu.is_system_app(_entry(installation=installation)) is False


def test_runtimes_are_never_system_apps() -> None:
    assert fsu.is_system_app(_entry(kind="runtime")) is False


# ---------------------------------------------------------------------------
# Version parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Flatpak 1.16.6\n", (1, 16, 6)),
        ("Flatpak 1.18.4", (1, 18, 4)),
        ("Flatpak 1.18\n", (1, 18, 0)),
        ("garbage", None),
        ("", None),
        ("flatpak 1.16.6", None),
    ],
)
def test_parse_flatpak_version(text: str, expected) -> None:
    assert fsu.parse_flatpak_version(text) == expected


# ---------------------------------------------------------------------------
# Execution: success and refusals
# ---------------------------------------------------------------------------


def test_success_runs_version_probe_then_one_system_update() -> None:
    gate = HostOperationGate()
    fake = _FakeFlatpak()

    result = _run(_entry(), gate, fake)

    assert result.flatpak_version == (1, 18, 4)
    assert fake.calls[0][0] == [FLATPAK_BINARY, "--version"]
    assert len(fake.update_calls) == 1
    command, kwargs = fake.update_calls[0]
    assert command == [FLATPAK_BINARY, "--system", "update", "-y", GEDIT_REF]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == fsu.SYSTEM_UPDATE_TIMEOUT_SECONDS
    assert "shell" not in kwargs
    assert gate.active is None  # released after success


def test_child_environment_is_the_clean_environment(monkeypatch) -> None:
    monkeypatch.setenv("LD_LIBRARY_PATH", "/evil")
    monkeypatch.setenv("PYTHONPATH", "/evil")
    fake = _FakeFlatpak()

    _run(_entry(), HostOperationGate(), fake)

    env = fake.update_calls[0][1]["env"]
    assert "LD_LIBRARY_PATH" not in env
    assert "PYTHONPATH" not in env


def test_refuses_inside_flatpak_sandbox_without_running_anything() -> None:
    fake = _FakeFlatpak()

    with pytest.raises(FlatpakUninstallError):
        _run(_entry(), HostOperationGate(), fake, in_sandbox=lambda: True)

    assert fake.calls == []


def test_refuses_when_flatpak_binary_is_missing() -> None:
    fake = _FakeFlatpak()

    with pytest.raises(FlatpakUninstallError, match="not installed"):
        _run(_entry(), HostOperationGate(), fake, binary_present=lambda: False)

    assert fake.calls == []


def test_refuses_without_a_gate_so_nothing_runs_unguarded() -> None:
    fake = _FakeFlatpak()

    with pytest.raises(FlatpakUninstallError, match="could not confirm"):
        _run(_entry(), None, fake)

    assert fake.calls == []


@pytest.mark.parametrize("version", [b"Flatpak 1.16.3\n", b"Flatpak 1.10.0\n"])
def test_refuses_flatpak_older_than_the_minimum(version: bytes) -> None:
    gate = HostOperationGate()
    fake = _FakeFlatpak(version=version)

    with pytest.raises(FlatpakUninstallError, match=r"older than 1.16.4"):
        _run(_entry(), gate, fake)

    assert fake.update_calls == []
    assert gate.active is None


def test_accepts_the_minimum_version_exactly() -> None:
    fake = _FakeFlatpak(version=b"Flatpak 1.16.4\n")

    assert _run(_entry(), HostOperationGate(), fake).flatpak_version == (1, 16, 4)


def test_refuses_unreadable_version_output() -> None:
    fake = _FakeFlatpak(version=b"something else\n")

    with pytest.raises(
        FlatpakUninstallError, match=r"Could not read the Flatpak version"
    ):
        _run(_entry(), HostOperationGate(), fake)

    assert fake.update_calls == []


def test_version_probe_nonzero_exit_is_refused() -> None:
    fake = _FakeFlatpak(version_returncode=1)

    with pytest.raises(
        FlatpakUninstallError, match=r"Could not read the Flatpak version"
    ):
        _run(_entry(), HostOperationGate(), fake)


# ---------------------------------------------------------------------------
# The shared host-operation gate
# ---------------------------------------------------------------------------


def test_refuses_while_another_host_operation_holds_the_gate() -> None:
    gate = HostOperationGate()
    gate.claim("dnf-transaction", "Installing packages")
    fake = _FakeFlatpak()

    with pytest.raises(FlatpakUninstallError, match="Installing packages"):
        _run(_entry(), gate, fake)

    assert fake.calls == []
    assert gate.active is not None
    assert gate.active.owner == "dnf-transaction"  # not stolen


@pytest.mark.parametrize(
    "update",
    [
        _Completed(1, stderr=b"Flatpak system operation Deploy not allowed for user"),
        subprocess.TimeoutExpired(cmd="flatpak", timeout=900),
        OSError("boom"),
    ],
    ids=["nonzero-exit", "timeout", "os-error"],
)
def test_gate_is_released_on_every_failure_path(update) -> None:
    gate = HostOperationGate()
    fake = _FakeFlatpak(update=update)

    with pytest.raises(FlatpakUninstallError):
        _run(_entry(), gate, fake)

    assert gate.active is None


def test_gate_is_released_even_if_the_version_probe_fails() -> None:
    gate = HostOperationGate()
    fake = _FakeFlatpak(version_returncode=1)

    with pytest.raises(FlatpakUninstallError):
        _run(_entry(), gate, fake)

    assert gate.active is None


# ---------------------------------------------------------------------------
# Failure messages
# ---------------------------------------------------------------------------


def test_nonzero_exit_reports_sanitized_flatpak_message() -> None:
    fake = _FakeFlatpak(
        update=_Completed(1, stderr=b"error:\x1b[31m not allowed\x1b[0m\n\tfor   user")
    )

    with pytest.raises(FlatpakUninstallError) as caught:
        _run(_entry(), HostOperationGate(), fake)

    assert "status 1" in caught.value.detail
    assert "not allowed" in caught.value.detail
    assert "\x1b" not in caught.value.detail
    assert "\n" not in caught.value.detail


def test_long_stderr_is_truncated() -> None:
    fake = _FakeFlatpak(update=_Completed(1, stderr=b"x" * 5000))

    with pytest.raises(FlatpakUninstallError) as caught:
        _run(_entry(), HostOperationGate(), fake)

    assert len(caught.value.detail) < fsu.MAX_STDERR_EXCERPT_CHARS + 200


def test_timeout_message_names_the_app_and_says_to_refresh() -> None:
    fake = _FakeFlatpak(update=subprocess.TimeoutExpired(cmd="flatpak", timeout=900))

    with pytest.raises(FlatpakUninstallError) as caught:
        _run(_entry(), HostOperationGate(), fake)

    assert "gedit" in caught.value.detail
    assert "Refresh" in caught.value.detail


# ---------------------------------------------------------------------------
# Worker: exactly one result, carrying the serial
# ---------------------------------------------------------------------------


def _collect(worker: fsu.FlatpakSystemUpdateWorker):
    succeeded: list[tuple] = []
    failed: list[tuple] = []
    worker.signals.succeeded.connect(lambda *args: succeeded.append(args))
    worker.signals.failed.connect(lambda *args: failed.append(args))
    worker.run()
    return succeeded, failed


_CHANGED = "Updated gedit from version 47.0 to 47.1."


def _result(version: tuple[int, int, int], summary: str = _CHANGED):
    return fsu.SystemUpdateResult(flatpak_version=version, summary=summary)


def test_worker_reports_success_without_advisory_on_recommended_version() -> None:
    worker = fsu.FlatpakSystemUpdateWorker(
        7, _entry(), gate=None, action=lambda: _result((1, 18, 4))
    )

    succeeded, failed = _collect(worker)

    assert succeeded == [(7, _CHANGED)]
    assert failed == []


def test_worker_adds_advisory_for_an_older_but_supported_version() -> None:
    worker = fsu.FlatpakSystemUpdateWorker(
        3, _entry(), gate=None, action=lambda: _result((1, 16, 6))
    )

    succeeded, _ = _collect(worker)

    assert succeeded[0][0] == 3
    assert succeeded[0][1].startswith(_CHANGED)
    assert "1.16.6" in succeeded[0][1]
    assert "1.18.4" in succeeded[0][1]


def test_worker_reports_flatpak_error_detail() -> None:
    def fail() -> tuple[int, int, int]:
        raise FlatpakUninstallError("nope")

    succeeded, failed = _collect(
        fsu.FlatpakSystemUpdateWorker(4, _entry(), gate=None, action=fail)
    )

    assert succeeded == []
    assert failed == [(4, "nope")]


def test_worker_turns_unexpected_errors_into_a_generic_message() -> None:
    def explode() -> tuple[int, int, int]:
        raise RuntimeError("internal detail")

    succeeded, failed = _collect(
        fsu.FlatpakSystemUpdateWorker(5, _entry(), gate=None, action=explode)
    )

    assert succeeded == []
    assert failed == [(5, "Unexpected error while trying to update: RuntimeError.")]
    assert "internal detail" not in failed[0][1]


# ---------------------------------------------------------------------------
# Dialog: the update button and routing
# ---------------------------------------------------------------------------


class _RecordingPool:
    def __init__(self) -> None:
        self.started: list[object] = []

    def start(self, worker) -> None:
        self.started.append(worker)


_KEEP_ALIVE: list[QWidget] = []  # parents must outlive the dialogs they own


def _parent_with_gate(gate: HostOperationGate | None) -> QWidget:
    parent = QWidget()
    _KEEP_ALIVE.append(parent)
    if gate is not None:
        parent.host_operation_gate = gate  # type: ignore[attr-defined]
    return parent


def _loaded_dialog(
    qtbot, parent: QWidget, *entries: FlatpakEntry
) -> FlatpakInventoryDialog:
    dialog = FlatpakInventoryDialog(parent)
    qtbot.addWidget(dialog)
    serial = dialog.begin_loading()
    dialog.show_inventory_if_current(serial, FlatpakInventory(entries=tuple(entries)))
    return dialog


def _select(dialog: FlatpakInventoryDialog, application_id: str) -> None:
    for row in range(dialog.table.rowCount()):
        item = dialog.table.item(row, fd._APP_ID_COLUMN)
        if item is not None and item.text() == application_id:
            dialog.table.selectRow(row)
            return
    raise AssertionError(f"{application_id} not in table")


def test_dialog_takes_the_gate_from_its_parent(qtbot) -> None:
    gate = HostOperationGate()
    dialog = _loaded_dialog(qtbot, _parent_with_gate(gate))

    assert dialog.host_operation_gate is gate


def test_dialog_without_a_gate_still_builds_but_refuses_system_updates(
    qtbot, flatpak_binary_installed
) -> None:
    dialog = _loaded_dialog(qtbot, _parent_with_gate(None), _entry())
    pool = _RecordingPool()

    start_flatpak_update(dialog, pool, _entry())  # type: ignore[arg-type]

    ((worker),) = pool.started
    assert isinstance(worker, fsu.FlatpakSystemUpdateWorker)
    failed: list[tuple] = []
    worker.signals.failed.connect(lambda *args: failed.append(args))
    worker.run()  # gate is None, so the action refuses without running flatpak
    assert failed and "could not confirm" in failed[0][1]


def test_system_app_gets_both_update_and_uninstall(qtbot) -> None:
    dialog = _loaded_dialog(qtbot, _parent_with_gate(HostOperationGate()), _entry())

    _select(dialog, "org.gnome.gedit")

    assert dialog.update_btn.isEnabled() is True
    assert dialog.uninstall_btn.isEnabled() is True


def test_system_update_locks_controls_and_routes_one_system_worker(qtbot) -> None:
    gate = HostOperationGate()
    dialog = _loaded_dialog(qtbot, _parent_with_gate(gate), _entry())
    pool = _RecordingPool()

    start_flatpak_update(dialog, pool, _entry())  # type: ignore[arg-type]

    ((worker),) = pool.started
    assert isinstance(worker, fsu.FlatpakSystemUpdateWorker)
    assert worker.request_serial == dialog._request_serial
    assert dialog.refresh_btn.isEnabled() is False
    assert dialog.update_btn.isEnabled() is False
    assert "Updating gedit" in dialog.status_label.text()


def test_successful_system_update_shows_advisory_and_requests_refresh(
    qtbot, monkeypatch
) -> None:
    refreshed: list[object] = []
    monkeypatch.setattr(
        fd, "start_flatpak_refresh", lambda dialog, pool: refreshed.append(dialog)
    )
    dialog = _loaded_dialog(qtbot, _parent_with_gate(HostOperationGate()), _entry())
    serial = dialog.begin_action(_entry(), "update")

    accepted = dialog.show_system_update_done_if_current(
        serial,
        "Updated gedit from version 47.0 to 47.1. Flatpak 1.16.6 is older than the "
        "recommended 1.18.4.",
    )

    assert accepted is True
    assert dialog.status_label.text() == (
        "Updated gedit from version 47.0 to 47.1. Flatpak 1.16.6 is older than the "
        "recommended 1.18.4. Refreshing the list…"
    )
    assert refreshed == [dialog]


def test_stale_system_update_result_is_ignored(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(fd, "start_flatpak_refresh", lambda dialog, pool: None)
    dialog = _loaded_dialog(qtbot, _parent_with_gate(HostOperationGate()), _entry())
    old = dialog.begin_action(_entry(), "update")
    dialog.begin_action(_entry(), "update")

    assert dialog.show_system_update_done_if_current(old, "Updated gedit.") is False


# ---------------------------------------------------------------------------
# Honest result: report only what the version before and after shows
# ---------------------------------------------------------------------------


def test_changed_version_is_reported_with_both_versions() -> None:
    fake = _FakeFlatpak(
        listings=(
            b"org.gnome.gedit\tx86_64\tstable\t47.0\n",
            b"org.gnome.gedit\tx86_64\tstable\t47.1\n",
        )
    )

    result = _run(_entry(), HostOperationGate(), fake)

    assert result.summary == _CHANGED


def test_unchanged_version_says_no_newer_version_was_installed() -> None:
    fake = _FakeFlatpak()  # both listings report 47.0

    result = _run(_entry(), HostOperationGate(), fake)

    assert result.summary == (
        "Flatpak finished, but gedit is still at version 47.0, "
        "so no newer version was installed."
    )


def test_unreadable_version_makes_no_claim_about_the_result() -> None:
    fake = _FakeFlatpak(listings=(None, None))

    result = _run(_entry(), HostOperationGate(), fake)

    assert result.summary == "Flatpak finished the update of gedit."


def test_app_missing_from_listing_makes_no_claim() -> None:
    fake = _FakeFlatpak(listings=(b"org.other.App\tx86_64\tstable\t1.0\n", b""))

    result = _run(_entry(), HostOperationGate(), fake)

    assert result.summary == "Flatpak finished the update of gedit."


def test_version_listing_uses_the_system_scope_only() -> None:
    fake = _FakeFlatpak()

    _run(_entry(), HostOperationGate(), fake)

    list_calls = [call for call, _ in fake.calls if "list" in call]
    assert len(list_calls) == 2
    assert all(call[:2] == [FLATPAK_BINARY, "--system"] for call in list_calls)
    assert all("--user" not in call for call in list_calls)


def test_result_message_stays_visible_after_the_automatic_refresh(
    qtbot, monkeypatch
) -> None:
    monkeypatch.setattr(fd, "start_flatpak_refresh", lambda dialog, pool: None)
    dialog = _loaded_dialog(qtbot, _parent_with_gate(HostOperationGate()), _entry())
    serial = dialog.begin_action(_entry(), "update")
    dialog.show_system_update_done_if_current(
        serial, "Updated gedit from version 47.0 to 47.1."
    )

    # The refresh that follows the result must not erase it.
    refresh_serial = dialog.begin_loading()
    dialog.show_inventory_if_current(
        refresh_serial, FlatpakInventory(entries=(_entry(),))
    )

    assert dialog.status_label.text().startswith(
        "Updated gedit from version 47.0 to 47.1."
    )
    assert "1 app(s)" in dialog.status_label.text()


def test_result_message_clears_when_the_user_refreshes(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(fd, "start_flatpak_refresh", lambda dialog, pool: None)
    dialog = _loaded_dialog(qtbot, _parent_with_gate(HostOperationGate()), _entry())
    serial = dialog.begin_action(_entry(), "update")
    dialog.show_system_update_done_if_current(
        serial, "Updated gedit from version 47.0 to 47.1."
    )

    dialog._on_refresh_clicked()
    refresh_serial = dialog._request_serial
    dialog.show_inventory_if_current(
        refresh_serial, FlatpakInventory(entries=(_entry(),))
    )

    assert "Updated gedit" not in dialog.status_label.text()
