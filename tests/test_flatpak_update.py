"""Regression coverage for updating per-user Flatpak apps (core and dialog)."""

from __future__ import annotations

import subprocess

import pytest
from PyQt6.QtCore import QThreadPool

from core import flatpak_uninstall as fu
from core.flatpak_inventory import FLATPAK_BINARY, FlatpakEntry, FlatpakInventory
from core.flatpak_uninstall import (
    UPDATE_TIMEOUT_SECONDS,
    FlatpakUninstallError,
    FlatpakUserActionWorker,
    build_update_command,
    run_flatpak_update,
    run_flatpak_user_action,
)
from ui import flatpak_dialog as fd
from ui.flatpak_dialog import FlatpakInventoryDialog, start_flatpak_update

FIREFOX_REF = "app/org.mozilla.firefox/x86_64/stable"


def _entry(
    *,
    kind: str = "app",
    installation: str = "user",
    application_id: str = "org.mozilla.firefox",
    name: str = "Firefox",
    ref: str | None = None,
) -> FlatpakEntry:
    return FlatpakEntry(
        kind=kind,
        name=name,
        application_id=application_id,
        version="141.0",
        branch="stable",
        arch="x86_64",
        origin="flathub",
        installation=installation,
        ref=ref or f"{kind}/{application_id}/x86_64/stable",
        installed_size_text="312.3 MB",
        installed_size_bytes=312_345_678,
        runtime="",
    )


def _inventory(*entries: FlatpakEntry) -> FlatpakInventory:
    return FlatpakInventory(entries=tuple(entries))


class _Completed:
    def __init__(self, returncode: int = 0, stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = stderr


class _RecordingRunner:
    def __init__(self, result=None, error: BaseException | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self._result = result or _Completed()
        self._error = error

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if self._error is not None:
            raise self._error
        return self._result


class _NeverCalled:
    def __call__(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("runner must not be called")


# ----------------------------------------------------------------------
# Command contract and scope
# ----------------------------------------------------------------------
def test_update_argv_is_fixed_user_update_for_one_ref_only() -> None:
    assert build_update_command(_entry()) == [
        FLATPAK_BINARY,
        "--user",
        "update",
        "--noninteractive",
        "-y",
        FIREFOX_REF,
    ]


def test_update_never_requests_runtime_cleanup_or_data_deletion() -> None:
    command = build_update_command(_entry())

    assert "--delete-data" not in command
    assert "--unused" not in command
    assert "--all" not in command
    assert command[-1] == FIREFOX_REF


@pytest.mark.parametrize(
    "entry",
    [
        _entry(installation="default"),
        _entry(kind="runtime", application_id="org.freedesktop.Platform"),
    ],
    ids=["system-installation", "runtime"],
)
def test_update_refuses_out_of_scope_entries(entry: FlatpakEntry) -> None:
    with pytest.raises(ValueError):
        build_update_command(entry)


@pytest.mark.parametrize(
    "application_id", ["-y", "org.x", "../../etc.passwd", "org.x.y;rm"]
)
def test_update_refuses_invalid_application_ids(application_id: str) -> None:
    entry = _entry(
        application_id=application_id, ref=f"app/{application_id}/x86_64/stable"
    )

    with pytest.raises(ValueError):
        build_update_command(entry)


def test_unknown_operation_is_refused() -> None:
    with pytest.raises(ValueError):
        run_flatpak_user_action(_entry(), "install", runner=_NeverCalled())


# ----------------------------------------------------------------------
# Execution boundary
# ----------------------------------------------------------------------
def test_update_sandbox_refusal_runs_nothing() -> None:
    with pytest.raises(FlatpakUninstallError, match="unavailable in Flatpak"):
        run_flatpak_update(_entry(), runner=_NeverCalled(), in_sandbox=lambda: True)


def test_update_missing_binary_runs_nothing() -> None:
    with pytest.raises(FlatpakUninstallError, match="Nothing was changed"):
        run_flatpak_update(
            _entry(),
            runner=_NeverCalled(),
            in_sandbox=lambda: False,
            binary_present=lambda: False,
        )


def test_update_uses_argv_closed_stdin_clean_env_and_update_timeout(
    monkeypatch,
) -> None:
    runner = _RecordingRunner()
    sentinel_env = {"PATH": "/usr/bin"}
    monkeypatch.setattr(fu, "get_clean_env", lambda: sentinel_env)

    run_flatpak_update(
        _entry(),
        runner=runner,
        in_sandbox=lambda: False,
        binary_present=lambda: True,
    )

    update_calls = [call for call in runner.calls if "update" in call[0]]
    ((command, kwargs),) = update_calls
    assert command == build_update_command(_entry())
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] == UPDATE_TIMEOUT_SECONDS
    assert kwargs["env"] is sentinel_env
    assert "shell" not in kwargs


def test_update_failure_reports_status_and_sanitized_stderr() -> None:
    runner = _RecordingRunner(result=_Completed(1, b"error: network\x00\n unreachable"))

    with pytest.raises(FlatpakUninstallError) as caught:
        run_flatpak_update(
            _entry(),
            runner=runner,
            in_sandbox=lambda: False,
            binary_present=lambda: True,
        )

    assert "could not update Firefox" in caught.value.detail
    assert "status 1" in caught.value.detail
    assert "error: network unreachable" in caught.value.detail


def test_update_timeout_is_mapped_to_a_clear_message() -> None:
    runner = _RecordingRunner(
        error=subprocess.TimeoutExpired(cmd="flatpak", timeout=900)
    )

    with pytest.raises(FlatpakUninstallError, match="did not finish the update"):
        run_flatpak_update(
            _entry(),
            runner=runner,
            in_sandbox=lambda: False,
            binary_present=lambda: True,
        )


# ----------------------------------------------------------------------
# Worker contract
# ----------------------------------------------------------------------
class _Collector:
    def __init__(self, signals):
        self.ok: list[tuple[int, str]] = []
        self.bad: list[tuple[int, str]] = []
        signals.succeeded.connect(lambda serial, name: self.ok.append((serial, name)))
        signals.failed.connect(lambda serial, detail: self.bad.append((serial, detail)))


def test_update_worker_reports_success_once(qtbot) -> None:
    worker = FlatpakUserActionWorker(
        5, _entry(), operation="update", action=lambda: None
    )
    collector = _Collector(worker.signals)

    worker.run()

    assert collector.ok == [(5, "Updated Firefox.")]
    assert collector.bad == []


def test_update_worker_reports_unexpected_errors_without_internals(qtbot) -> None:
    def boom() -> None:
        raise RuntimeError("internal detail")

    worker = FlatpakUserActionWorker(6, _entry(), operation="update", action=boom)
    collector = _Collector(worker.signals)

    worker.run()

    assert collector.bad == [
        (6, "Unexpected error while trying to update: RuntimeError.")
    ]


def test_worker_rejects_an_unknown_operation() -> None:
    with pytest.raises(ValueError):
        FlatpakUserActionWorker(1, _entry(), operation="install")


# ----------------------------------------------------------------------
# Dialog behaviour
# ----------------------------------------------------------------------
class _RecordingPool:
    def __init__(self):
        self.started: list[object] = []

    def start(self, worker) -> None:
        self.started.append(worker)


class _BrokenPool:
    def start(self, worker) -> None:
        raise RuntimeError("pool is shutting down")


@pytest.fixture
def no_refresh(monkeypatch):
    """Record refresh requests instead of starting a real `flatpak list`."""
    calls: list[object] = []
    monkeypatch.setattr(
        fd, "start_flatpak_refresh", lambda dialog, pool: calls.append(dialog)
    )
    return calls


def _loaded_dialog(qtbot, *entries: FlatpakEntry) -> FlatpakInventoryDialog:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_loading()
    dialog.show_inventory_if_current(serial, _inventory(*entries))
    return dialog


def _select_app(dialog: FlatpakInventoryDialog, application_id: str) -> None:
    for row in range(dialog.table.rowCount()):
        item = dialog.table.item(row, fd._APP_ID_COLUMN)
        if item is not None and item.text() == application_id:
            dialog.table.selectRow(row)
            return
    raise AssertionError(f"{application_id} not in table")


def test_update_button_is_enabled_for_user_and_system_apps(qtbot) -> None:
    dialog = _loaded_dialog(
        qtbot,
        _entry(),
        _entry(application_id="org.gnome.gedit", name="gedit", installation="default"),
    )
    assert dialog.update_btn.isEnabled() is False

    _select_app(dialog, "org.mozilla.firefox")
    assert dialog.update_btn.isEnabled() is True

    _select_app(dialog, "org.gnome.gedit")
    assert dialog.update_btn.isEnabled() is True
    assert (
        dialog.uninstall_btn.isEnabled() is False
    )  # system apps are never uninstalled here


def test_update_locks_controls_and_hands_one_worker_to_pool(qtbot) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    pool = _RecordingPool()

    start_flatpak_update(dialog, pool, _entry())  # type: ignore[arg-type]

    ((worker),) = pool.started
    assert worker.operation == "update"
    assert worker.request_serial == dialog._request_serial
    assert dialog.refresh_btn.isEnabled() is False
    assert dialog.update_btn.isEnabled() is False
    assert dialog.uninstall_btn.isEnabled() is False
    assert "Updating Firefox" in dialog.status_label.text()


def test_successful_update_reports_and_requests_a_refresh(qtbot, no_refresh) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    serial = dialog.begin_update(_entry())

    assert dialog.show_update_done_if_current(serial, "Updated Firefox.") is True
    assert dialog.status_label.text() == "Updated Firefox. Refreshing the list…"
    assert no_refresh == [dialog]


def test_failed_update_shows_detail_and_requests_a_refresh(qtbot, no_refresh) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    serial = dialog.begin_update(_entry())

    assert dialog.show_update_failed_if_current(serial, "network unreachable") is True
    assert dialog.status_label.text() == "network unreachable"
    assert no_refresh == [dialog]


def test_update_result_is_ignored_when_an_uninstall_is_pending(
    qtbot, no_refresh
) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    serial = dialog.begin_uninstall(_entry())

    assert dialog.show_update_done_if_current(serial, "Firefox") is False
    assert dialog.show_update_failed_if_current(serial, "stale") is False
    assert no_refresh == []


def test_stale_update_result_is_ignored(qtbot, no_refresh) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    old_serial = dialog.begin_update(_entry())
    dialog.begin_loading()

    assert dialog.show_update_done_if_current(old_serial, "Firefox") is False
    assert no_refresh == []


def test_broken_pool_restores_controls_with_a_message(qtbot, no_refresh) -> None:
    dialog = _loaded_dialog(qtbot, _entry())

    start_flatpak_update(dialog, _BrokenPool(), _entry())  # type: ignore[arg-type]

    assert "Could not start update: RuntimeError" in dialog.status_label.text()


def test_update_button_click_routes_only_a_user_app(qtbot, monkeypatch) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    _select_app(dialog, "org.mozilla.firefox")
    started: list[str] = []
    monkeypatch.setattr(
        fd,
        "start_flatpak_update",
        lambda dialog_arg, pool, entry: started.append(entry.application_id),
    )

    dialog._on_update_clicked()

    assert started == ["org.mozilla.firefox"]


def test_real_update_result_reaches_dialog_through_the_pool(qtbot, monkeypatch) -> None:
    """End to end on the real pool with a fake action and a fake refresh loader."""
    from core.flatpak_inventory import FlatpakInventoryWorker

    dialog = _loaded_dialog(qtbot, _entry())
    original_init = FlatpakUserActionWorker.__init__

    def init_with_fake_action(
        self, serial, entry, *, operation="uninstall", delete_data=False, action=None
    ):
        original_init(
            self,
            serial,
            entry,
            operation=operation,
            delete_data=delete_data,
            action=lambda: None,
        )

    def refresh_worker(serial):
        return FlatpakInventoryWorker(serial, loader=lambda: _inventory())

    monkeypatch.setattr(FlatpakUserActionWorker, "__init__", init_with_fake_action)
    monkeypatch.setattr(fd, "FlatpakInventoryWorker", refresh_worker)

    start_flatpak_update(dialog, QThreadPool.globalInstance(), _entry())

    qtbot.waitUntil(
        lambda: dialog.status_label.text().endswith(
            "No Flatpak apps or runtimes are installed."
        ),
        timeout=5000,
    )
