"""Progress bar behaviour for Flatpak updates: dialog state, stale results, and wiring."""

from __future__ import annotations

from core.flatpak_inventory import FlatpakEntry
from core.flatpak_progress import ProgressTracker
from ui.flatpak_dialog import FlatpakInventoryDialog

REAL_LINES = [
    "Looking for updates…",
    " 1.\t   \torg.test.App\tstable\tu\tlocaltest\t< 40.0\xa0MB",
    "Updating…",
    "Updating…                        0%  0 bytes/s",
    "Updating… ███████████████▍      77%  6.3\xa0MB/s  00:02",
    "Updates complete.",
]


def _entry() -> FlatpakEntry:
    return FlatpakEntry(
        kind="app",
        name="Test App",
        application_id="org.test.App",
        version="1",
        branch="stable",
        arch="x86_64",
        origin="localtest",
        installation="user",
        ref="app/org.test.App/x86_64/stable",
        installed_size_text="1.0 MB",
        installed_size_bytes=1000000,
        runtime="",
    )


def _snapshots(lines: list[str]):
    tracker = ProgressTracker()
    return [snap for line in lines if (snap := tracker.feed(line)) is not None]


def test_update_shows_a_busy_bar_until_flatpak_reports_a_percentage(qtbot):
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    dialog.begin_action(_entry(), "update")

    assert not dialog.progress_bar.isHidden()
    assert dialog.progress_bar.maximum() == 0  # busy (moving) bar


def test_uninstall_does_not_show_the_progress_bar(qtbot):
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    dialog.begin_action(_entry(), "uninstall")

    assert dialog.progress_bar.isHidden()


def test_progress_snapshot_sets_bar_steps_and_status(qtbot):
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_action(_entry(), "update")

    snapshot = _snapshots(REAL_LINES[:5])[-1]
    assert dialog.show_update_progress_if_current(serial, snapshot) is True

    assert dialog.progress_bar.maximum() == 100
    assert dialog.progress_bar.value() == 77
    assert dialog.progress_steps_label.text() == "▶ org.test.App stable"
    assert not dialog.progress_steps_label.isHidden()
    assert "77%" in dialog.status_label.text()
    assert "6.3 MB/s" in dialog.status_label.text()


def test_snapshot_without_percentage_keeps_the_bar_busy(qtbot):
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_action(_entry(), "update")

    snapshot = _snapshots(["Updating app/org.test.App/x86_64/stable"])[-1]
    dialog.show_update_progress_if_current(serial, snapshot)

    assert dialog.progress_bar.maximum() == 0
    assert dialog.status_label.text() == "Updating org.test.App"


def test_stale_progress_is_ignored_after_a_newer_action(qtbot):
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    old_serial = dialog.begin_action(_entry(), "update")
    dialog.begin_action(_entry(), "update")

    snapshot = _snapshots(REAL_LINES[:5])[-1]
    assert dialog.show_update_progress_if_current(old_serial, snapshot) is False
    assert dialog.progress_bar.maximum() == 0


def test_progress_after_the_update_finished_is_ignored(qtbot):
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_action(_entry(), "update")
    dialog.show_update_done_if_current(serial, "Updated Test App.")

    snapshot = _snapshots(REAL_LINES[:5])[-1]
    assert dialog.show_update_progress_if_current(serial, snapshot) is False


def test_progress_is_hidden_when_the_update_finishes(qtbot):
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_action(_entry(), "update")
    dialog.show_update_progress_if_current(serial, _snapshots(REAL_LINES[:5])[-1])

    dialog.show_update_done_if_current(serial, "Updated Test App.")

    assert dialog.progress_bar.isHidden()
    assert dialog.progress_steps_label.isHidden()


def test_progress_is_hidden_when_the_update_fails(qtbot):
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_action(_entry(), "update")
    dialog.show_update_progress_if_current(serial, _snapshots(REAL_LINES[:5])[-1])

    dialog.show_update_failed_if_current(serial, "Flatpak could not update Test App.")

    assert dialog.progress_bar.isHidden()


def test_cancel_text_is_not_overwritten_by_later_progress(qtbot):
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_action(_entry(), "update", cancel=lambda: None)
    dialog._on_cancel_clicked()
    assert dialog.status_label.text() == "Cancelling…"

    dialog.show_update_progress_if_current(serial, _snapshots(REAL_LINES[:5])[-1])

    assert dialog.status_label.text() == "Cancelling…"
    assert dialog.progress_bar.value() == 77


def test_worker_reports_progress_with_its_serial(qtbot):
    from core.flatpak_system_update import FlatpakSystemUpdateWorker

    received = []
    worker = FlatpakSystemUpdateWorker(7, _entry(), gate=None, action=lambda: None)
    worker.signals.progress.connect(
        lambda serial, snap: received.append((serial, snap))
    )

    worker.report_progress(_snapshots(REAL_LINES[:5])[-1])

    assert received and received[0][0] == 7
    assert received[0][1].percent == 77
