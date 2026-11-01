"""UI regression coverage for the Flatpak dialog (listing and scope)."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThreadPool
from PyQt6.QtWidgets import QAbstractItemView

from core.flatpak_inventory import FlatpakEntry, FlatpakInventory
from ui import flatpak_dialog as fd
from ui.flatpak_dialog import (
    FlatpakInventoryDialog,
    start_flatpak_refresh,
)


def _entry(
    kind: str = "app",
    size: int | None = 312345678,
    size_text: str = "312.3 MB",
    **overrides,
) -> FlatpakEntry:
    values = {
        "kind": kind,
        "name": "Firefox",
        "application_id": "org.mozilla.firefox",
        "version": "141.0",
        "branch": "stable",
        "arch": "x86_64",
        "origin": "flathub",
        "installation": "system",
        "ref": f"{kind}/org.mozilla.firefox/x86_64/stable",
        "installed_size_text": size_text,
        "installed_size_bytes": size,
        "runtime": "",
    }
    values.update(overrides)
    return FlatpakEntry(**values)


def _inventory(*entries: FlatpakEntry) -> FlatpakInventory:
    return FlatpakInventory(entries=tuple(entries))


def test_size_is_shown_exactly_as_flatpak_printed_it(qtbot) -> None:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_loading()

    dialog.show_inventory_if_current(
        serial, _inventory(_entry(size_text="1.0\u00a0kB", size=1000))
    )

    assert _text(dialog, 0, fd._SIZE_COLUMN) == "1.0\u00a0kB"


def test_table_is_not_editable_and_scope_is_marked_separate_from_rpm(qtbot) -> None:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)

    assert dialog.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert "not part of RPM categories" in fd.SCOPE_NOTICE
    assert "Installing, updating, and system-wide changes" in fd.SCOPE_NOTICE


def test_rows_render_with_kind_and_numeric_size_sort(qtbot) -> None:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_loading()

    accepted = dialog.show_inventory_if_current(
        serial,
        _inventory(
            _entry("app", size=312345678),
            _entry(
                "runtime",
                size=2_000_000,
                size_text="2.0 MB",
                name="Freedesktop Platform",
                application_id="org.freedesktop.Platform",
                ref="runtime/org.freedesktop.Platform/x86_64/24.08",
            ),
        ),
    )

    assert accepted is True
    assert dialog.table.rowCount() == 2
    kinds = {_text(dialog, row, 2): _text(dialog, row, 0) for row in range(2)}
    assert kinds == {
        "org.mozilla.firefox": "App",
        "org.freedesktop.Platform": "Runtime",
    }
    assert dialog.status_label.text() == "1 app(s), 1 runtime(s) installed."
    assert dialog.refresh_btn.isEnabled()

    dialog.table.sortItems(fd._SIZE_COLUMN, Qt.SortOrder.AscendingOrder)
    assert _text(dialog, 0, fd._SIZE_COLUMN) == "2.0 MB"
    assert _text(dialog, 1, fd._SIZE_COLUMN) == "312.3 MB"


def _text(dialog: FlatpakInventoryDialog, row: int, column: int) -> str:
    item = dialog.table.item(row, column)
    assert item is not None
    return item.text()


def test_empty_inventory_is_explained_not_blank(qtbot) -> None:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_loading()

    dialog.show_inventory_if_current(serial, _inventory())

    assert dialog.table.rowCount() == 0
    assert dialog.status_label.text() == "No Flatpak apps or runtimes are installed."


def test_stale_inventory_is_ignored_after_a_newer_refresh(qtbot) -> None:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    old_serial = dialog.begin_loading()
    new_serial = dialog.begin_loading()

    assert dialog.show_inventory_if_current(old_serial, _inventory(_entry())) is False
    assert dialog.table.rowCount() == 0
    assert dialog.refresh_btn.isEnabled() is False  # still waiting on the newer refresh

    assert dialog.show_inventory_if_current(new_serial, _inventory()) is True
    assert dialog.refresh_btn.isEnabled() is True


def test_stale_error_is_ignored_after_a_newer_refresh(qtbot) -> None:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    old_serial = dialog.begin_loading()
    dialog.begin_loading()

    assert dialog.show_error_if_current(old_serial, "old failure") is False
    assert "old failure" not in dialog.status_label.text()


def test_current_error_is_shown_with_full_tooltip(qtbot) -> None:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    serial = dialog.begin_loading()

    dialog.show_error_if_current(
        serial, "Flatpak is not installed at /usr/bin/flatpak."
    )

    assert dialog.status_label.text() == "Flatpak is not installed at /usr/bin/flatpak."
    assert dialog.status_label.toolTip() == dialog.status_label.text()
    assert dialog.refresh_btn.isEnabled()


class _RecordingPool:
    def __init__(self):
        self.started: list[object] = []

    def start(self, worker) -> None:
        self.started.append(worker)


class _BrokenPool:
    def start(self, worker) -> None:
        raise RuntimeError("pool is shutting down")


def test_refresh_hands_one_serialized_worker_to_the_pool(qtbot) -> None:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    pool = _RecordingPool()

    start_flatpak_refresh(dialog, pool)  # type: ignore[arg-type]

    (worker,) = pool.started
    assert worker.request_serial == 1
    assert dialog.refresh_btn.isEnabled() is False


def test_refresh_surfaces_a_failure_to_start_the_worker(qtbot) -> None:
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)

    start_flatpak_refresh(dialog, _BrokenPool())  # type: ignore[arg-type]

    assert "Could not start Flatpak loading: RuntimeError" in dialog.status_label.text()
    assert dialog.refresh_btn.isEnabled()


def test_real_worker_result_reaches_dialog_through_the_pool(qtbot, monkeypatch) -> None:
    """End to end on the real pool: fake loader, real signal delivery to UI-thread slots."""
    dialog = FlatpakInventoryDialog()
    qtbot.addWidget(dialog)
    inventory = _inventory(_entry())
    monkeypatch.setattr(
        fd, "FlatpakInventoryWorker", _worker_with_loader(lambda: inventory)
    )

    start_flatpak_refresh(dialog, QThreadPool.globalInstance())

    qtbot.waitUntil(lambda: dialog.table.rowCount() == 1, timeout=5000)
    assert _text(dialog, 0, 2) == "org.mozilla.firefox"


def _worker_with_loader(loader):
    from core.flatpak_inventory import FlatpakInventoryWorker

    def factory(request_serial: int):
        return FlatpakInventoryWorker(request_serial, loader=loader)

    return factory


def test_header_button_emits_flatpak_request_and_is_labelled_separately(qtbot) -> None:
    from ui.header import HeaderBar

    header = HeaderBar()
    qtbot.addWidget(header)
    requests: list[bool] = []
    header.flatpak_clicked.connect(lambda: requests.append(True))

    header.flatpak_btn.click()

    assert requests == [True]
    assert header.flatpak_btn.text() == "Flatpak"
    assert "separate from RPM packages" in header.flatpak_btn.toolTip()
    assert "read-only" not in header.flatpak_btn.toolTip()


def test_main_window_opens_flatpak_dialog_with_one_refresh(monkeypatch) -> None:
    from types import SimpleNamespace

    from ui import main_window as main_window_module
    from ui.main_window import MainWindow

    created: list[object] = []
    refreshes: list[tuple[object, object]] = []
    executed: list[object] = []

    class _FakeDialog:
        def __init__(self, parent):
            self.parent_widget = parent
            self.scheduled_for_deletion = False
            created.append(self)

        def exec(self) -> int:
            executed.append(self)
            return 0

        def deleteLater(self) -> None:
            self.scheduled_for_deletion = True

    monkeypatch.setattr(main_window_module, "FlatpakInventoryDialog", _FakeDialog)
    monkeypatch.setattr(
        main_window_module,
        "start_flatpak_refresh",
        lambda dialog, pool: refreshes.append((dialog, pool)),
    )
    fake_window = SimpleNamespace(thread_pool=object())

    MainWindow._open_flatpak_dialog(fake_window)

    (dialog,) = created
    assert refreshes == [(dialog, fake_window.thread_pool)]
    assert executed == [dialog]
    assert dialog.scheduled_for_deletion is True  # no hidden window left behind
