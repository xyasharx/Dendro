"""Regression coverage for Flatpak uninstall of per-user apps (core and dialog)."""

from __future__ import annotations

import subprocess

import pytest
from PyQt6.QtCore import Qt, QThreadPool

from core import flatpak_uninstall as fu
from core.flatpak_inventory import FLATPAK_BINARY, FlatpakEntry, FlatpakInventory
from core.flatpak_uninstall import (
    FlatpakUninstallError,
    FlatpakUninstallWorker,
    build_uninstall_command,
    describe_uninstall,
    is_user_app,
    run_flatpak_uninstall,
)
from ui import flatpak_dialog as fd
from ui.flatpak_dialog import FlatpakInventoryDialog, start_flatpak_uninstall

FIREFOX_REF = "app/org.mozilla.firefox/x86_64/stable"


def _entry(
    *,
    kind: str = "app",
    installation: str = "user",
    application_id: str = "org.mozilla.firefox",
    arch: str = "x86_64",
    branch: str = "stable",
    name: str = "Firefox",
    ref: str | None = None,
) -> FlatpakEntry:
    return FlatpakEntry(
        kind=kind,
        name=name,
        application_id=application_id,
        version="141.0",
        branch=branch,
        arch=arch,
        origin="flathub",
        installation=installation,
        ref=ref or f"{kind}/{application_id}/{arch}/{branch}",
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
# Scope and command contract
# ----------------------------------------------------------------------
def test_user_app_is_the_only_scope_handled() -> None:
    assert is_user_app(_entry()) is True
    assert is_user_app(_entry(installation="default")) is False
    assert (
        is_user_app(_entry(kind="runtime", application_id="org.freedesktop.Platform"))
        is False
    )


def test_uninstall_argv_is_fixed_user_uninstall_without_data_deletion() -> None:
    assert build_uninstall_command(_entry(), delete_data=False) == [
        FLATPAK_BINARY,
        "--user",
        "uninstall",
        "--noninteractive",
        "-y",
        FIREFOX_REF,
    ]


def test_delete_data_is_added_only_when_requested_and_ref_stays_last() -> None:
    command = build_uninstall_command(_entry(), delete_data=True)

    assert "--delete-data" in command
    assert command[-1] == FIREFOX_REF
    assert command.index("--delete-data") < len(command) - 1


@pytest.mark.parametrize(
    "entry",
    [
        _entry(installation="default"),
        _entry(installation="system"),
        _entry(kind="runtime", application_id="org.freedesktop.Platform"),
    ],
    ids=["default-installation", "system-label", "runtime"],
)
def test_out_of_scope_entries_are_refused(entry: FlatpakEntry) -> None:
    with pytest.raises(ValueError):
        build_uninstall_command(entry, delete_data=False)


@pytest.mark.parametrize(
    "application_id",
    [
        "-y",
        "--all",
        "org.x",
        "org..x.y",
        "../../etc.passwd",
        "org.x.y;rm",
        "org.x.y --all",
        "",
    ],
)
def test_invalid_application_ids_are_refused_before_argv_is_built(
    application_id: str,
) -> None:
    entry = _entry(
        application_id=application_id, ref=f"app/{application_id}/x86_64/stable"
    )

    with pytest.raises(ValueError):
        build_uninstall_command(entry, delete_data=False)


def test_invalid_arch_or_branch_is_refused() -> None:
    with pytest.raises(ValueError):
        build_uninstall_command(
            _entry(arch="x86 64", ref="app/org.mozilla.firefox/x86 64/stable"),
            delete_data=False,
        )
    with pytest.raises(ValueError):
        build_uninstall_command(
            _entry(
                branch="stable/../x", ref="app/org.mozilla.firefox/x86_64/stable/../x"
            ),
            delete_data=False,
        )


def test_mismatched_ref_is_refused() -> None:
    entry = _entry(ref="app/org.other.App/x86_64/stable")

    with pytest.raises(ValueError):
        build_uninstall_command(entry, delete_data=False)


def test_description_mentions_data_only_when_it_will_be_deleted() -> None:
    kept = describe_uninstall(_entry(), delete_data=False)
    deleted = describe_uninstall(_entry(), delete_data=True)

    assert "will be kept" in kept
    assert "cannot be undone" not in kept
    assert "will also be deleted" in deleted
    assert "~/.var/app/org.mozilla.firefox" in deleted


# ----------------------------------------------------------------------
# Execution boundary
# ----------------------------------------------------------------------
def test_sandbox_refuses_without_running_anything() -> None:
    with pytest.raises(FlatpakUninstallError, match="unavailable in Flatpak"):
        run_flatpak_uninstall(
            _entry(),
            delete_data=False,
            runner=_NeverCalled(),
            in_sandbox=lambda: True,
        )


def test_missing_binary_refuses_without_running_anything() -> None:
    with pytest.raises(FlatpakUninstallError, match="Nothing was removed"):
        run_flatpak_uninstall(
            _entry(),
            delete_data=False,
            runner=_NeverCalled(),
            in_sandbox=lambda: False,
            binary_present=lambda: False,
        )


def test_run_uses_argv_closed_stdin_clean_env_and_timeout(monkeypatch) -> None:
    runner = _RecordingRunner()
    sentinel_env = {"PATH": "/usr/bin"}
    monkeypatch.setattr(fu, "get_clean_env", lambda: sentinel_env)

    run_flatpak_uninstall(
        _entry(),
        delete_data=True,
        runner=runner,
        in_sandbox=lambda: False,
        binary_present=lambda: True,
        timeout=123,
    )

    ((command, kwargs),) = runner.calls
    assert command == build_uninstall_command(_entry(), delete_data=True)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 123
    assert kwargs["env"] is sentinel_env
    assert "shell" not in kwargs


def test_nonzero_exit_reports_status_and_sanitized_stderr() -> None:
    runner = _RecordingRunner(result=_Completed(1, b"error: app is running\x00\n  now"))

    with pytest.raises(FlatpakUninstallError) as caught:
        run_flatpak_uninstall(
            _entry(),
            delete_data=False,
            runner=runner,
            in_sandbox=lambda: False,
            binary_present=lambda: True,
        )

    assert "could not uninstall Firefox" in caught.value.detail
    assert "status 1" in caught.value.detail
    assert "error: app is running" in caught.value.detail
    assert "\x00" not in caught.value.detail


def test_timeout_is_mapped_to_a_clear_message() -> None:
    runner = _RecordingRunner(
        error=subprocess.TimeoutExpired(cmd="flatpak", timeout=300)
    )

    with pytest.raises(FlatpakUninstallError, match="did not finish uninstalling"):
        run_flatpak_uninstall(
            _entry(),
            delete_data=False,
            runner=runner,
            in_sandbox=lambda: False,
            binary_present=lambda: True,
        )


def test_os_error_does_not_leak_paths() -> None:
    runner = _RecordingRunner(error=PermissionError("/home/alice/secret"))

    with pytest.raises(FlatpakUninstallError) as caught:
        run_flatpak_uninstall(
            _entry(),
            delete_data=False,
            runner=runner,
            in_sandbox=lambda: False,
            binary_present=lambda: True,
        )

    assert "/home/alice" not in caught.value.detail
    assert "PermissionError" in caught.value.detail


# ----------------------------------------------------------------------
# Worker result contract
# ----------------------------------------------------------------------
class _Collector:
    def __init__(self, signals):
        self.ok: list[tuple[int, str]] = []
        self.bad: list[tuple[int, str]] = []
        signals.succeeded.connect(lambda serial, name: self.ok.append((serial, name)))
        signals.failed.connect(lambda serial, detail: self.bad.append((serial, detail)))


def test_worker_reports_success_once_with_serial(qtbot) -> None:
    worker = FlatpakUninstallWorker(7, _entry(), delete_data=False, action=lambda: None)
    collector = _Collector(worker.signals)

    worker.run()

    assert collector.ok == [(7, "Firefox")]
    assert collector.bad == []


def test_worker_reports_user_message_for_expected_failures(qtbot) -> None:
    def boom() -> None:
        raise FlatpakUninstallError("Flatpak could not uninstall Firefox (status 1).")

    worker = FlatpakUninstallWorker(3, _entry(), delete_data=False, action=boom)
    collector = _Collector(worker.signals)

    worker.run()

    assert collector.bad == [(3, "Flatpak could not uninstall Firefox (status 1).")]
    assert collector.ok == []


def test_worker_never_leaves_the_dialog_busy_on_unexpected_errors(qtbot) -> None:
    def boom() -> None:
        raise RuntimeError("unexpected internals")

    worker = FlatpakUninstallWorker(4, _entry(), delete_data=False, action=boom)
    collector = _Collector(worker.signals)

    worker.run()

    assert collector.bad == [(4, "Unexpected error while uninstalling: RuntimeError.")]
    assert "internals" not in collector.bad[0][1]


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


def test_uninstall_button_is_enabled_only_for_a_selected_user_app(qtbot) -> None:
    dialog = _loaded_dialog(
        qtbot,
        _entry(),
        _entry(
            application_id="org.gnome.gedit",
            name="gedit",
            installation="default",
        ),
        _entry(
            kind="runtime",
            application_id="org.freedesktop.Platform",
            name="Platform",
            ref="runtime/org.freedesktop.Platform/x86_64/stable",
        ),
    )
    assert dialog.uninstall_btn.isEnabled() is False  # nothing selected

    _select_app(dialog, "org.mozilla.firefox")
    assert dialog.uninstall_btn.isEnabled() is True

    _select_app(dialog, "org.gnome.gedit")
    assert dialog.uninstall_btn.isEnabled() is False

    _select_app(dialog, "org.freedesktop.Platform")
    assert dialog.uninstall_btn.isEnabled() is False


def test_declined_confirmation_starts_no_worker(qtbot, monkeypatch) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    _select_app(dialog, "org.mozilla.firefox")
    pool = _RecordingPool()
    monkeypatch.setattr(fd, "confirm_uninstall", lambda entry, parent: None)
    monkeypatch.setattr(
        fd, "start_flatpak_uninstall", lambda *a, **k: pytest.fail("started")
    )

    dialog._on_uninstall_clicked()

    assert pool.started == []
    assert dialog.uninstall_btn.isEnabled() is True


@pytest.fixture
def no_refresh(monkeypatch):
    """Record refresh requests instead of starting a real `flatpak list`."""
    calls: list[object] = []
    monkeypatch.setattr(
        fd, "start_flatpak_refresh", lambda dialog, pool: calls.append(dialog)
    )
    return calls


def test_confirmed_uninstall_locks_controls_and_hands_one_worker_to_pool(
    qtbot, monkeypatch
) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    _select_app(dialog, "org.mozilla.firefox")
    pool = _RecordingPool()

    start_flatpak_uninstall(dialog, pool, _entry(), delete_data=True)  # type: ignore[arg-type]

    (worker,) = pool.started
    assert isinstance(worker, FlatpakUninstallWorker)
    assert worker.request_serial == dialog._request_serial
    assert dialog.refresh_btn.isEnabled() is False
    assert dialog.uninstall_btn.isEnabled() is False
    assert "Uninstalling Firefox" in dialog.status_label.text()


def test_successful_uninstall_reports_and_requests_a_refresh(qtbot, no_refresh) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    serial = dialog.begin_uninstall(_entry())

    assert dialog.show_uninstall_done_if_current(serial, "Firefox") is True
    assert "Uninstalled Firefox" in dialog.status_label.text()
    assert no_refresh == [dialog]


def test_failed_uninstall_shows_detail_and_requests_a_refresh(
    qtbot, no_refresh
) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    serial = dialog.begin_uninstall(_entry())

    assert dialog.show_uninstall_failed_if_current(serial, "app is running") is True
    assert dialog.status_label.text() == "app is running"
    assert no_refresh == [dialog]  # the real state is re-read after a failure


def test_stale_uninstall_result_is_ignored(qtbot, no_refresh) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    old_serial = dialog.begin_uninstall(_entry())
    dialog.begin_loading()  # a newer request supersedes the uninstall

    assert dialog.show_uninstall_done_if_current(old_serial, "Firefox") is False
    assert dialog.show_uninstall_failed_if_current(old_serial, "stale") is False
    assert "stale" not in dialog.status_label.text()
    assert no_refresh == []


def test_broken_pool_restores_controls_with_a_message(qtbot, no_refresh) -> None:
    dialog = _loaded_dialog(qtbot, _entry())

    start_flatpak_uninstall(dialog, _BrokenPool(), _entry(), delete_data=False)  # type: ignore[arg-type]

    assert "Could not start uninstall: RuntimeError" in dialog.status_label.text()


def test_real_worker_result_reaches_dialog_through_the_pool(qtbot, monkeypatch) -> None:
    """End to end on the real pool with a fake action and a fake refresh loader."""
    from core.flatpak_inventory import FlatpakInventoryWorker

    dialog = _loaded_dialog(qtbot, _entry())
    original_init = FlatpakUninstallWorker.__init__

    def init_with_fake_action(self, serial, entry, *, delete_data):
        original_init(self, serial, entry, delete_data=delete_data, action=lambda: None)

    def refresh_worker(serial):
        return FlatpakInventoryWorker(serial, loader=lambda: _inventory())

    monkeypatch.setattr(FlatpakUninstallWorker, "__init__", init_with_fake_action)
    monkeypatch.setattr(fd, "FlatpakInventoryWorker", refresh_worker)

    start_flatpak_uninstall(
        dialog, QThreadPool.globalInstance(), _entry(), delete_data=False
    )

    qtbot.waitUntil(
        lambda: (
            dialog.status_label.text() == "No Flatpak apps or runtimes are installed."
        ),
        timeout=5000,
    )
    assert dialog.table.rowCount() == 0


def test_dialog_accepts_only_flatpak_entries_from_its_own_table(qtbot) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    _select_app(dialog, "org.mozilla.firefox")

    entry = dialog.selected_entry()

    assert entry is not None
    assert entry.application_id == "org.mozilla.firefox"
    assert dialog.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == entry


def test_confirmed_click_passes_the_delete_data_choice_through(
    qtbot, monkeypatch
) -> None:
    dialog = _loaded_dialog(qtbot, _entry())
    _select_app(dialog, "org.mozilla.firefox")
    started: list[dict] = []
    monkeypatch.setattr(fd, "confirm_uninstall", lambda entry, parent: True)
    monkeypatch.setattr(
        fd,
        "start_flatpak_uninstall",
        lambda dialog_arg, pool, entry, *, delete_data: started.append(
            {"entry": entry.application_id, "delete_data": delete_data}
        ),
    )

    dialog._on_uninstall_clicked()

    assert started == [{"entry": "org.mozilla.firefox", "delete_data": True}]
