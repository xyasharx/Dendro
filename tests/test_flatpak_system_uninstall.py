"""Coverage for uninstalling system-wide Flatpak apps through Flatpak's helper (Phase B)."""

from __future__ import annotations

import subprocess

import pytest
from PyQt6.QtWidgets import QWidget

from core import flatpak_system_uninstall as fsu
from core.flatpak_inventory import FLATPAK_BINARY, FlatpakEntry, FlatpakInventory
from core.flatpak_uninstall import FlatpakUninstallError, FlatpakUserActionWorker
from core.operation_gate import HostOperationGate
from ui import flatpak_dialog as fd
from ui.flatpak_dialog import FlatpakInventoryDialog, start_flatpak_uninstall

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
    """Answers ``--version`` and records every call. Uninstall results are configurable."""

    def __init__(
        self,
        *,
        version: bytes = b"Flatpak 1.18.4\n",
        uninstall: _Completed | Exception | None = None,
    ):
        self.version = version
        self.uninstall = uninstall or _Completed(0)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if command[-1] == "--version":
            return _Completed(0, stdout=self.version)
        if isinstance(self.uninstall, Exception):
            raise self.uninstall
        return self.uninstall

    @property
    def uninstall_calls(self) -> list[tuple[list[str], dict]]:
        return [call for call in self.calls if "uninstall" in call[0]]


def _run(
    entry: FlatpakEntry, gate: HostOperationGate | None, fake: _FakeFlatpak, **kw
) -> str:
    return fsu.run_flatpak_system_uninstall(
        entry,
        delete_data=kw.pop("delete_data", False),
        gate=gate,
        runner=fake,
        in_sandbox=kw.pop("in_sandbox", lambda: False),
        binary_present=kw.pop("binary_present", lambda: True),
        **kw,
    )


# ---------------------------------------------------------------------------
# Command and scope
# ---------------------------------------------------------------------------


def test_command_is_system_uninstall_without_noninteractive() -> None:
    command = fsu.build_system_uninstall_command(_entry(), delete_data=False)

    assert command == [FLATPAK_BINARY, "--system", "uninstall", "-y", GEDIT_REF]
    assert "--noninteractive" not in command
    assert "--user" not in command
    assert "--all" not in command
    assert "--unused" not in command


def test_delete_data_is_added_only_when_chosen() -> None:
    command = fsu.build_system_uninstall_command(_entry(), delete_data=True)

    assert "--delete-data" in command
    assert command[-1] == GEDIT_REF


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
        fsu.build_system_uninstall_command(entry, delete_data=False)


@pytest.mark.parametrize(
    "application_id",
    ["--all", "-y", "org.x", "../../etc.passwd", "org.gnome.gedit --all"],
)
def test_command_refuses_option_like_or_malformed_ids(application_id: str) -> None:
    entry = _entry(
        application_id=application_id, ref=f"app/{application_id}/x86_64/stable"
    )

    with pytest.raises(ValueError):
        fsu.build_system_uninstall_command(entry, delete_data=False)


# ---------------------------------------------------------------------------
# Confirmation text
# ---------------------------------------------------------------------------


def test_system_description_says_everyone_and_admin_password() -> None:
    text = fsu.describe_system_uninstall(_entry(), delete_data=False)

    assert "for everyone on this computer" in text
    assert "administrator password" in text
    assert "Your data for this app will be kept." in text


def test_system_description_limits_data_deletion_to_the_caller() -> None:
    text = fsu.describe_system_uninstall(_entry(), delete_data=True)

    assert fsu.SYSTEM_DATA_DIRECTORY_NOTE in text
    assert "This cannot be undone." in text


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def test_success_runs_one_uninstall_with_safe_process_settings(monkeypatch) -> None:
    monkeypatch.setenv("LD_LIBRARY_PATH", "/evil")
    gate = HostOperationGate()
    fake = _FakeFlatpak()

    message = _run(_entry(), gate, fake)

    assert message == "Uninstalled gedit for everyone. Your data was kept."
    assert len(fake.uninstall_calls) == 1
    command, kwargs = fake.uninstall_calls[0]
    assert command == [FLATPAK_BINARY, "--system", "uninstall", "-y", GEDIT_REF]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == fsu.SYSTEM_UNINSTALL_TIMEOUT_SECONDS
    assert "shell" not in kwargs
    assert "LD_LIBRARY_PATH" not in kwargs["env"]
    assert gate.active is None


def test_success_with_data_deletion_says_so() -> None:
    message = _run(_entry(), HostOperationGate(), _FakeFlatpak(), delete_data=True)

    assert message == "Uninstalled gedit for everyone, and deleted your data for it."


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

    with pytest.raises(FlatpakUninstallError, match=r"could not confirm"):
        _run(_entry(), None, fake)

    assert fake.calls == []


def test_refuses_flatpak_older_than_the_minimum() -> None:
    gate = HostOperationGate()
    fake = _FakeFlatpak(version=b"Flatpak 1.16.3\n")

    with pytest.raises(FlatpakUninstallError, match=r"older than 1\.16\.4"):
        _run(_entry(), gate, fake)

    assert fake.uninstall_calls == []
    assert gate.active is None


def test_accepts_the_minimum_version_exactly() -> None:
    fake = _FakeFlatpak(version=b"Flatpak 1.16.4\n")

    assert "Uninstalled" in _run(_entry(), HostOperationGate(), fake)


def test_refuses_while_another_host_operation_holds_the_gate() -> None:
    gate = HostOperationGate()
    gate.claim("dnf-transaction", "Installing packages")
    fake = _FakeFlatpak()

    with pytest.raises(FlatpakUninstallError, match="Installing packages"):
        _run(_entry(), gate, fake)

    assert fake.calls == []
    assert gate.active is not None
    assert gate.active.owner == "dnf-transaction"


@pytest.mark.parametrize(
    "uninstall",
    [
        _Completed(
            1, stderr=b"Flatpak system operation Uninstall not allowed for user"
        ),
        subprocess.TimeoutExpired(cmd="flatpak", timeout=600),
        OSError("boom"),
    ],
    ids=["nonzero-exit", "timeout", "os-error"],
)
def test_gate_is_released_on_every_failure_path(uninstall) -> None:
    gate = HostOperationGate()

    with pytest.raises(FlatpakUninstallError):
        _run(_entry(), gate, _FakeFlatpak(uninstall=uninstall))

    assert gate.active is None


def test_nonzero_exit_reports_sanitized_flatpak_message() -> None:
    fake = _FakeFlatpak(
        uninstall=_Completed(1, stderr=b"error:\x1b[31m not allowed\x1b[0m\n")
    )

    with pytest.raises(FlatpakUninstallError) as caught:
        _run(_entry(), HostOperationGate(), fake)

    assert "status 1" in caught.value.detail
    assert "not allowed" in caught.value.detail
    assert "\x1b" not in caught.value.detail


def test_timeout_message_names_the_app_and_says_to_refresh() -> None:
    fake = _FakeFlatpak(uninstall=subprocess.TimeoutExpired(cmd="flatpak", timeout=600))

    with pytest.raises(FlatpakUninstallError) as caught:
        _run(_entry(), HostOperationGate(), fake)

    assert "gedit" in caught.value.detail
    assert "Refresh" in caught.value.detail


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _collect(worker: fsu.FlatpakSystemUninstallWorker):
    succeeded: list[tuple] = []
    failed: list[tuple] = []
    worker.signals.succeeded.connect(lambda *args: succeeded.append(args))
    worker.signals.failed.connect(lambda *args: failed.append(args))
    worker.run()
    return succeeded, failed


def test_worker_reports_success_once_with_serial() -> None:
    worker = fsu.FlatpakSystemUninstallWorker(
        9,
        _entry(),
        delete_data=False,
        gate=None,
        action=lambda: "Uninstalled gedit for everyone. Your data was kept.",
    )

    succeeded, failed = _collect(worker)

    assert succeeded == [(9, "Uninstalled gedit for everyone. Your data was kept.")]
    assert failed == []


def test_worker_reports_flatpak_error_detail() -> None:
    def fail() -> str:
        raise FlatpakUninstallError("nope")

    succeeded, failed = _collect(
        fsu.FlatpakSystemUninstallWorker(
            4, _entry(), delete_data=False, gate=None, action=fail
        )
    )

    assert succeeded == []
    assert failed == [(4, "nope")]


def test_worker_turns_unexpected_errors_into_a_generic_message() -> None:
    def explode() -> str:
        raise RuntimeError("internal detail")

    succeeded, failed = _collect(
        fsu.FlatpakSystemUninstallWorker(
            5, _entry(), delete_data=False, gate=None, action=explode
        )
    )

    assert succeeded == []
    assert failed == [(5, "Unexpected error while trying to uninstall: RuntimeError.")]
    assert "internal detail" not in failed[0][1]


# ---------------------------------------------------------------------------
# Dialog routing
# ---------------------------------------------------------------------------


class _RecordingPool:
    def __init__(self) -> None:
        self.started: list[object] = []

    def start(self, worker) -> None:
        self.started.append(worker)


_KEEP_ALIVE: list[QWidget] = []


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


def test_system_app_uninstall_is_routed_to_the_system_worker(qtbot) -> None:
    gate = HostOperationGate()
    dialog = _loaded_dialog(qtbot, _parent_with_gate(gate), _entry())
    pool = _RecordingPool()

    start_flatpak_uninstall(dialog, pool, _entry(), delete_data=True)  # type: ignore[arg-type]

    ((worker),) = pool.started
    assert isinstance(worker, fsu.FlatpakSystemUninstallWorker)
    assert worker.request_serial == dialog._request_serial
    assert dialog.refresh_btn.isEnabled() is False
    assert dialog.uninstall_btn.isEnabled() is False
    assert "Uninstalling gedit" in dialog.status_label.text()


def test_user_app_uninstall_still_uses_the_user_worker(qtbot) -> None:
    dialog = _loaded_dialog(
        qtbot,
        _parent_with_gate(HostOperationGate()),
        _entry(installation="user", ref=None),
    )
    pool = _RecordingPool()

    start_flatpak_uninstall(
        dialog, pool, _entry(installation="user"), delete_data=False
    )  # type: ignore[arg-type]

    ((worker),) = pool.started
    assert isinstance(worker, FlatpakUserActionWorker)


def test_system_uninstall_without_a_gate_refuses_when_run(qtbot) -> None:
    dialog = _loaded_dialog(qtbot, _parent_with_gate(None), _entry())
    pool = _RecordingPool()
    start_flatpak_uninstall(dialog, pool, _entry(), delete_data=False)  # type: ignore[arg-type]
    ((worker),) = pool.started
    failed: list[tuple] = []
    worker.signals.failed.connect(lambda *args: failed.append(args))

    worker.run()

    assert failed and "could not confirm" in failed[0][1]


def test_successful_system_uninstall_reports_and_requests_refresh(
    qtbot, monkeypatch
) -> None:
    refreshed: list[object] = []
    monkeypatch.setattr(
        fd, "start_flatpak_refresh", lambda dialog, pool: refreshed.append(dialog)
    )
    dialog = _loaded_dialog(qtbot, _parent_with_gate(HostOperationGate()), _entry())
    serial = dialog.begin_action(_entry(), "uninstall")

    assert (
        dialog.show_uninstall_done_if_current(
            serial, "Uninstalled gedit for everyone. Your data was kept."
        )
        is True
    )
    assert dialog.status_label.text() == (
        "Uninstalled gedit for everyone. Your data was kept. Refreshing the list…"
    )
    assert refreshed == [dialog]


def test_scope_notice_mentions_system_wide_uninstall() -> None:
    assert "system-wide" in fd.SCOPE_NOTICE
    assert "uninstall" in fd.SCOPE_NOTICE
