"""Dialog coverage for the Cancel button on system-wide Flatpak actions."""

from __future__ import annotations

from core import flatpak_cancel as fc
from core.flatpak_inventory import FlatpakEntry, FlatpakInventory
from ui import flatpak_dialog as fd
from ui.flatpak_dialog import FlatpakInventoryDialog


def _entry(installation: str = "system", application_id: str = "org.mozilla.firefox") -> FlatpakEntry:
    return FlatpakEntry(
        kind="app",
        name="Firefox",
        application_id=application_id,
        version="141.0",
        branch="stable",
        arch="x86_64",
        origin="flathub",
        installation=installation,
        ref=f"app/{application_id}/x86_64/stable",
        installed_size_text="312.3 MB",
        installed_size_bytes=312_300_000,
        runtime="",
    )


class _RecordingPool:
    """Records workers without running them, so a test controls their outcome."""

    def __init__(self) -> None:
        self.started: list = []

    def start(self, worker) -> None:
        self.started.append(worker)


class _SyncPool(_RecordingPool):
    def start(self, worker) -> None:
        self.started.append(worker)
        worker.run()


def _loaded(qtbot, entry: FlatpakEntry) -> FlatpakInventoryDialog:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_loading()
    dialog.show_inventory_if_current(serial, FlatpakInventory(entries=(entry,)))
    for row in range(dialog.table.rowCount()):
        if dialog.table.item(row, fd._APP_ID_COLUMN).text() == entry.application_id:
            dialog.table.selectRow(row)
    return dialog


def test_cancel_button_is_hidden_when_idle(qtbot) -> None:
    dialog = _loaded(qtbot, _entry())

    assert dialog.cancel_btn.isHidden()


def test_cancel_button_is_hidden_for_per_user_actions(qtbot, monkeypatch) -> None:
    dialog = _loaded(qtbot, _entry(installation="user"))
    dialog.begin_action(_entry(installation="user"), "update")

    assert dialog.cancel_btn.isHidden()


def test_cancel_button_shows_during_a_system_action_and_calls_cancel(qtbot) -> None:
    dialog = _loaded(qtbot, _entry())
    calls: list[str] = []
    dialog.begin_action(_entry(), "update", cancel=lambda: calls.append("cancel"))

    assert not dialog.cancel_btn.isHidden()
    assert dialog.cancel_btn.isEnabled()

    dialog.cancel_btn.click()

    assert calls == ["cancel"]
    assert dialog.status_label.text() == "Cancelling…"
    assert not dialog.cancel_btn.isEnabled()


def test_second_cancel_click_does_not_call_cancel_again(qtbot) -> None:
    dialog = _loaded(qtbot, _entry())
    calls: list[str] = []
    dialog.begin_action(_entry(), "update", cancel=lambda: calls.append("cancel"))

    dialog._on_cancel_clicked()
    dialog._on_cancel_clicked()

    assert calls == ["cancel"]


def test_cancel_button_hides_again_when_the_action_finishes(qtbot) -> None:
    dialog = _loaded(qtbot, _entry())
    serial = dialog.begin_action(_entry(), "update", cancel=lambda: None)

    dialog.show_update_failed_if_current(serial, fc.CANCELLED_DETAIL)

    assert dialog.cancel_btn.isHidden()
    assert dialog._cancel is None


def test_cancelled_result_is_shown_on_the_status_line(qtbot, monkeypatch) -> None:
    dialog = _loaded(qtbot, _entry())
    monkeypatch.setattr(fd, "start_flatpak_refresh", lambda *a, **k: None)
    serial = dialog.begin_action(_entry(), "update", cancel=lambda: None)

    dialog.show_update_failed_if_current(serial, fc.CANCELLED_DETAIL)

    assert dialog.status_label.text() == fc.CANCELLED_DETAIL


def test_stale_cancel_result_does_not_clear_a_newer_action(qtbot) -> None:
    dialog = _loaded(qtbot, _entry())
    old = dialog.begin_action(_entry(), "update", cancel=lambda: None)
    dialog.begin_action(_entry(), "update", cancel=lambda: None)

    assert dialog.show_update_failed_if_current(old, fc.CANCELLED_DETAIL) is False
    assert not dialog.cancel_btn.isHidden()


def test_cancel_button_cancels_the_runner_that_the_system_update_uses(
    qtbot, monkeypatch
) -> None:
    dialog = _loaded(qtbot, _entry())
    monkeypatch.setattr(fd, "start_flatpak_refresh", lambda *a, **k: None)
    seen: dict = {}

    def fake_update(entry, *, gate, runner):
        seen["runner"] = runner
        raise fc.FlatpakCancelled()

    monkeypatch.setattr(fd, "run_flatpak_system_update", fake_update)
    pool = _RecordingPool()

    fd.start_flatpak_system_update(dialog, pool, _entry())
    assert not dialog.cancel_btn.isHidden()

    dialog.cancel_btn.click()
    assert seen == {}  # the action has not run yet
    (worker,) = pool.started
    worker.run()

    assert seen["runner"] is not None
    assert seen["runner"].cancel_requested is True
    assert dialog.status_label.text() == fc.CANCELLED_DETAIL
    assert dialog.cancel_btn.isHidden()
