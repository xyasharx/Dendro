"""Regression coverage for visible asynchronous-query failure states."""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtCore import QPersistentModelIndex

from core.backend import HistoryEntry, PackageInfo, PackageState
from core.models import DependencyTreeModel
from ui import main_window as main_window_module
from ui.history_dialog import DnfHistoryDialog
from ui.inspector_panel import PackageInspectorPanel
from ui.main_window import MainWindow


def _package(*, state: PackageState = PackageState.INSTALLED) -> PackageInfo:
    return PackageInfo(
        name="example",
        version="1.0",
        release="1.fc99",
        arch="x86_64",
        summary="Example package",
        state=state,
    )


def test_inspector_replaces_each_loading_state_with_an_explicit_failure(qtbot) -> None:
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    panel.set_package_info(_package())

    assert "Extracting changelog" in panel.changelog_browser.toPlainText()
    assert "Loading maintainer" in panel.scriptlets_browser.toPlainText()
    assert "Loading installed file manifest" in panel.files_status_label.text()

    panel.set_package_files_error("example", "RPM database is locked")
    panel.set_package_changelog_error("example", "header <unavailable>")
    panel.set_package_scriptlets_error("example", "header <unavailable>")
    panel.set_reverse_dependencies_error("example", "RPM database is locked")
    panel.set_package_verification_error("example", "RPM database is locked")

    assert (
        "Could not load the installed file manifest" in panel.files_status_label.text()
    )
    assert panel.files_status_label.toolTip() == "RPM database is locked"
    assert (
        "Could not load the package changelog" in panel.changelog_browser.toPlainText()
    )
    assert "header <unavailable>" in panel.changelog_browser.toPlainText()
    assert "Could not load RPM scriptlets" in panel.scriptlets_browser.toPlainText()
    assert "header <unavailable>" in panel.scriptlets_browser.toPlainText()
    assert panel.rev_status_label.text() == "Could not determine reverse dependencies."
    assert panel.rev_status_label.toolTip() == "RPM database is locked"
    assert (
        panel.reverse_list.item(0).text()
        == "Dependency information could not be loaded."
    )
    assert panel.lbl_verify_status.text() == "Audit: Could not complete"
    assert panel.lbl_verify_status.toolTip() == "RPM database is locked"


def test_inspector_does_not_start_installed_rpm_queries_for_remote_packages(
    qtbot,
) -> None:
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    requests: list[tuple[str, str]] = []
    panel.file_inspection_requested.connect(
        lambda package: requests.append(("files", package))
    )
    panel.changelog_requested.connect(
        lambda package: requests.append(("changelog", package))
    )
    panel.scriptlets_requested.connect(
        lambda package: requests.append(("scriptlets", package))
    )

    panel.set_package_info(_package(state=PackageState.AVAILABLE))

    assert requests == []
    assert not panel.file_search_input.isEnabled()
    assert not panel.btn_verify.isEnabled()
    assert not panel.btn_refresh_rev.isEnabled()
    assert (
        "Available after this package is installed" in panel.files_status_label.text()
    )
    assert (
        "available after this package is installed"
        in panel.changelog_browser.toPlainText().lower()
    )
    assert (
        "available after this package is installed"
        in panel.scriptlets_browser.toPlainText().lower()
    )


def test_stale_auxiliary_results_are_ignored_and_failed_current_queries_keep_cache() -> (
    None
):
    stale_user_worker = object()
    current_user_worker = object()
    stale_orphan_worker = object()
    current_orphan_worker = object()
    reported: list[tuple[str, str]] = []
    window = SimpleNamespace(
        current_userinstalled_worker=current_user_worker,
        current_orphan_worker=current_orphan_worker,
        _user_installed_cache={"known-user"},
        _orphan_cache={"known-orphan"},
        _on_query_error=lambda package, detail: reported.append((package, detail)),
    )

    MainWindow._on_userinstalled_loaded(window, stale_user_worker, {"stale-user"})
    MainWindow._on_orphans_loaded(window, stale_orphan_worker, {"stale-orphan"})

    assert window._user_installed_cache == {"known-user"}
    assert window._orphan_cache == {"known-orphan"}
    assert window.current_userinstalled_worker is current_user_worker
    assert window.current_orphan_worker is current_orphan_worker

    MainWindow._on_userinstalled_query_error(
        window, current_user_worker, "", "user-installed query failed"
    )
    MainWindow._on_orphan_query_error(
        window, current_orphan_worker, "", "orphan query failed"
    )

    assert window._user_installed_cache == {"known-user"}
    assert window._orphan_cache == {"known-orphan"}
    assert window.current_userinstalled_worker is None
    assert window.current_orphan_worker is None
    assert reported == [
        ("", "user-installed query failed"),
        ("", "orphan query failed"),
    ]


def test_stale_dependency_callbacks_do_not_mutate_a_reloaded_tree() -> None:
    attached: list[tuple[str, list[object], object]] = []
    reported: list[tuple[str, str]] = []
    window = SimpleNamespace(
        _package_tree_generation=2,
        tree_model=SimpleNamespace(
            attach_dependencies=lambda package, dependencies, index: attached.append(
                (package, dependencies, index)
            )
        ),
        _on_query_error=lambda package, detail: reported.append((package, detail)),
    )

    MainWindow._on_dependencies_resolved_for_current_tree(
        window, 1, "example", [object()], object()
    )
    MainWindow._on_dependency_query_error(window, 1, "example", "stale error")

    assert attached == []
    assert reported == []

    target = object()
    dependencies = [object()]
    MainWindow._on_dependencies_resolved_for_current_tree(
        window, 2, "example", dependencies, target
    )

    assert attached == [("example", dependencies, target)]


def test_dependency_error_clears_only_its_exact_multilib_loading_state(qapp) -> None:
    model = DependencyTreeModel()
    x86_64 = PackageInfo(
        name="shared-runtime",
        version="4.0",
        release="1.fc44",
        arch="x86_64",
    )
    i686 = PackageInfo(
        name="shared-runtime",
        version="4.0",
        release="1.fc44",
        arch="i686",
    )
    model.set_packages([x86_64, i686])
    x86_64_item = model.item_for_package(x86_64)
    i686_item = model.item_for_package(i686)
    assert x86_64_item is not None
    assert i686_item is not None
    x86_64_item.is_loading_dependencies = True
    i686_item.is_loading_dependencies = True
    messages: list[tuple[str, int]] = []
    window = SimpleNamespace(
        _package_tree_generation=7,
        tree_model=model,
        status_bar=SimpleNamespace(
            showMessage=lambda message, timeout: messages.append((message, timeout))
        ),
    )
    target = QPersistentModelIndex(model.createIndex(i686_item.row(), 0, i686_item))

    MainWindow._on_dependency_query_error(
        window, 7, "shared-runtime", "header query failed", target
    )

    assert x86_64_item.is_loading_dependencies
    assert not i686_item.is_loading_dependencies
    assert messages == [("Error: header query failed", 8000)]


def test_repository_result_does_not_populate_a_reopened_dialog() -> None:
    original_results: list[object] = []
    replacement_results: list[object] = []
    refresh_requests: list[str] = []
    original_dialog = SimpleNamespace(
        set_repositories=lambda repositories: original_results.append(repositories)
    )
    replacement_dialog = SimpleNamespace(
        set_repositories=lambda repositories: replacement_results.append(repositories)
    )
    worker = object()
    window = SimpleNamespace(
        current_repository_worker=worker,
        _repository_refresh_after_current_worker=True,
        _active_repo_dialog=replacement_dialog,
        _load_repositories_from_daemon=lambda: refresh_requests.append("refresh"),
    )

    MainWindow._on_daemon_repositories_loaded(
        window, worker, original_dialog, ["outdated repository list"]
    )

    assert original_results == []
    assert replacement_results == []
    assert window.current_repository_worker is None
    assert refresh_requests == ["refresh"]


def test_stale_worker_status_messages_are_ignored() -> None:
    stale_worker = object()
    current_worker = object()
    status_messages: list[str] = []
    window = SimpleNamespace(
        current_query_worker=current_worker,
        current_updates_worker=current_worker,
        _update_request_serial=2,
        status_bar=SimpleNamespace(showMessage=status_messages.append),
    )

    MainWindow._show_status_for_current_worker(
        window, "current_query_worker", stale_worker, "stale package status"
    )
    MainWindow._on_update_check_status(window, stale_worker, 1, "stale update status")

    assert status_messages == []

    MainWindow._show_status_for_current_worker(
        window, "current_query_worker", current_worker, "current package status"
    )
    MainWindow._on_update_check_status(
        window, current_worker, 2, "current update status"
    )

    assert status_messages == ["current package status", "current update status"]


def test_file_owner_search_is_dispatched_without_blocking_the_search_handler() -> None:
    query_calls: list[str] = []
    status_messages: list[tuple[str, int]] = []
    started_workers: list[object] = []
    window = SimpleNamespace(
        current_file_owner_worker=None,
        _file_owner_request_serial=0,
        proxy_model=SimpleNamespace(
            set_search_query=lambda query: query_calls.append(query)
        ),
        status_bar=SimpleNamespace(
            showMessage=lambda message, timeout=0: status_messages.append(
                (message, timeout)
            )
        ),
        thread_pool=SimpleNamespace(start=started_workers.append),
    )

    MainWindow._on_search_query_changed(window, "file:/tmp")

    assert query_calls == ["file:/tmp"]
    assert len(started_workers) == 1
    assert window.current_file_owner_worker is started_workers[0]
    assert status_messages == [("Looking up the RPM owner of '/tmp'…", 0)]


def test_stale_file_owner_result_is_ignored() -> None:
    stale_worker = object()
    current_worker = object()
    navigated: list[str] = []
    status_messages: list[tuple[str, int]] = []
    window = SimpleNamespace(
        current_file_owner_worker=current_worker,
        _file_owner_request_serial=2,
        proxy_model=SimpleNamespace(set_search_query=lambda _query: None),
        status_bar=SimpleNamespace(
            showMessage=lambda message, timeout=0: status_messages.append(
                (message, timeout)
            )
        ),
        _navigate_to_package=navigated.append,
    )

    MainWindow._on_file_owner_lookup_resolved(
        window, stale_worker, 1, "file:/tmp", "/tmp", "example"
    )

    assert navigated == []
    assert status_messages == []


def test_file_owner_lookup_passes_the_exact_rpm_record_to_navigation() -> None:
    owner = PackageInfo(
        name="shared-runtime",
        version="4.0",
        release="1.fc44",
        arch="i686",
        epoch="2",
    )
    worker = object()
    navigated: list[object] = []
    query_calls: list[str] = []
    status_messages: list[tuple[str, int]] = []
    window = SimpleNamespace(
        current_file_owner_worker=worker,
        _file_owner_request_serial=1,
        proxy_model=SimpleNamespace(set_search_query=query_calls.append),
        status_bar=SimpleNamespace(
            showMessage=lambda message, timeout=0: status_messages.append(
                (message, timeout)
            )
        ),
        _navigate_to_package=navigated.append,
    )

    MainWindow._on_file_owner_lookup_resolved(
        window, worker, 1, "file:/tmp/library", "/tmp/library", owner
    )

    assert query_calls == ["shared-runtime"]
    assert navigated == [owner]
    assert status_messages == [
        (
            "File '/tmp/library' is owned by package "
            "'shared-runtime-2:4.0-1.fc44.i686'.",
            4000,
        )
    ]


def test_file_owner_lookup_reports_no_matching_installed_rpm() -> None:
    status_messages: list[tuple[str, int]] = []
    worker = object()
    window = SimpleNamespace(
        current_file_owner_worker=worker,
        _file_owner_request_serial=1,
        proxy_model=SimpleNamespace(set_search_query=lambda _query: None),
        status_bar=SimpleNamespace(
            showMessage=lambda message, timeout=0: status_messages.append(
                (message, timeout)
            )
        ),
    )

    MainWindow._on_file_owner_lookup_resolved(
        window, worker, 1, "file:/tmp", "/tmp", None
    )

    assert status_messages == [
        (
            "Dendro could not identify an installed RPM package that owns '/tmp'.",
            5000,
        )
    ]


def test_file_owner_lookup_reports_an_rpm_query_failure() -> None:
    status_messages: list[tuple[str, int]] = []
    worker = object()
    window = SimpleNamespace(
        current_file_owner_worker=worker,
        _file_owner_request_serial=1,
        status_bar=SimpleNamespace(
            showMessage=lambda message, timeout=0: status_messages.append(
                (message, timeout)
            )
        ),
    )

    MainWindow._on_file_owner_lookup_failed(
        window, worker, 1, "/tmp", "RPM database is locked"
    )

    assert status_messages == [
        (
            "Could not inspect the RPM owner for '/tmp': RPM database is locked",
            8000,
        )
    ]


def test_stale_inspector_query_result_for_the_same_package_is_ignored() -> None:
    rendered: list[tuple[str, object]] = []
    panel = SimpleNamespace(_current_package=_package())
    window = SimpleNamespace(
        inspector_panel=panel,
        _inspector_generation=4,
        _inspector_request_serials={"files": 2},
    )
    window._is_current_inspector_request = lambda kind, generation, serial, package: (
        MainWindow._is_current_inspector_request(
            window, kind, generation, serial, package
        )
    )

    MainWindow._on_inspector_query_result(
        window,
        "files",
        4,
        1,
        "example",
        ["stale file"],
        lambda package, result: rendered.append((package, result)),
    )
    MainWindow._on_inspector_query_result(
        window,
        "files",
        4,
        2,
        "example",
        ["current file"],
        lambda package, result: rendered.append((package, result)),
    )

    assert rendered == [("example", ["current file"])]


def test_stale_inspector_query_error_does_not_replace_current_status() -> None:
    rendered: list[tuple[str, str]] = []
    status_messages: list[tuple[str, int]] = []
    panel = SimpleNamespace(_current_package=_package())
    window = SimpleNamespace(
        inspector_panel=panel,
        _inspector_generation=4,
        _inspector_request_serials={"verification": 2},
        status_bar=SimpleNamespace(
            showMessage=lambda message, timeout=0: status_messages.append(
                (message, timeout)
            )
        ),
    )
    window._is_current_inspector_request = lambda kind, generation, serial, package: (
        MainWindow._is_current_inspector_request(
            window, kind, generation, serial, package
        )
    )
    window._on_query_error = lambda package, detail: MainWindow._on_query_error(
        window, package, detail
    )

    MainWindow._on_inspector_query_error(
        window,
        "verification",
        4,
        1,
        "example",
        "stale RPM failure",
        lambda package, detail: rendered.append((package, detail)),
    )

    assert rendered == []
    assert status_messages == []


def test_local_rpm_inspection_is_dispatched_without_blocking_the_open_handler(
    monkeypatch,
) -> None:
    monkeypatch.setattr(main_window_module, "is_running_in_flatpak", lambda: False)
    status_messages: list[tuple[str, int]] = []
    started_workers: list[object] = []
    window = SimpleNamespace(
        current_local_rpm_inspection_worker=None,
        _local_rpm_inspection_request_serial=0,
        status_bar=SimpleNamespace(
            showMessage=lambda message, timeout=0: status_messages.append(
                (message, timeout)
            )
        ),
        thread_pool=SimpleNamespace(start=started_workers.append),
    )

    MainWindow.open_local_rpm(window, "/tmp/example.rpm")

    assert len(started_workers) == 1
    assert window.current_local_rpm_inspection_worker is started_workers[0]
    assert status_messages == [("Inspecting local package: example.rpm...", 0)]


def test_stale_local_rpm_inspection_failure_is_ignored() -> None:
    stale_worker = object()
    current_worker = object()
    status_messages: list[tuple[str, int]] = []
    window = SimpleNamespace(
        current_local_rpm_inspection_worker=current_worker,
        _local_rpm_inspection_request_serial=2,
        status_bar=SimpleNamespace(
            showMessage=lambda message, timeout=0: status_messages.append(
                (message, timeout)
            )
        ),
    )

    MainWindow._on_local_rpm_inspection_failed(
        window, stale_worker, 1, "/tmp/example.rpm", "stale failure"
    )

    assert status_messages == []


def _reject_worker_start(_worker: object) -> None:
    raise RuntimeError("executor is shutting down")


def test_package_scan_scheduler_rejection_clears_the_loading_state() -> None:
    busy_states: list[tuple[bool, str]] = []
    reported: list[tuple[str, str]] = []
    window = SimpleNamespace(
        _package_tree_generation=0,
        current_query_worker=None,
        current_orphan_worker=None,
        current_userinstalled_worker=None,
        current_updates_worker=None,
        _update_request_serial=0,
        _set_busy_state=lambda busy, reason: busy_states.append((busy, reason)),
        status_bar=SimpleNamespace(showMessage=lambda *_args: None),
        thread_pool=SimpleNamespace(start=_reject_worker_start),
        _on_query_error=lambda package, detail: reported.append((package, detail)),
    )

    MainWindow._load_packages(window)

    assert window.current_query_worker is None
    assert busy_states == [(True, "package-load"), (False, "package-load")]
    assert reported == [
        (
            "",
            "Could not start the installed-package scan: "
            "RuntimeError: executor is shutting down",
        )
    ]


def test_remote_search_scheduler_rejection_clears_its_busy_state() -> None:
    busy_states: list[tuple[bool, str]] = []
    reported: list[tuple[str, str]] = []
    window = SimpleNamespace(
        current_remote_search_worker=None,
        _set_busy_state=lambda busy, reason: busy_states.append((busy, reason)),
        status_bar=SimpleNamespace(showMessage=lambda *_args: None),
        thread_pool=SimpleNamespace(start=_reject_worker_start),
        _on_query_error=lambda package, detail: reported.append((package, detail)),
    )
    window._on_remote_search_failed = lambda worker, package, detail: (
        MainWindow._on_remote_search_failed(window, worker, package, detail)
    )

    MainWindow._on_remote_search_requested(window, "example")

    assert window.current_remote_search_worker is None
    assert busy_states == [(True, "remote-search"), (False, "remote-search")]
    assert reported == [
        (
            "",
            "Could not start remote repository search: "
            "RuntimeError: executor is shutting down",
        )
    ]


def test_inspector_scheduler_rejection_replaces_the_loading_placeholder() -> None:
    reported: list[tuple[str, str]] = []
    rendered: list[tuple[str, str]] = []
    panel = SimpleNamespace(_current_package=_package())
    window = SimpleNamespace(
        inspector_panel=panel,
        _inspector_generation=4,
        _inspector_request_serials={"files": 2},
        thread_pool=SimpleNamespace(start=_reject_worker_start),
        _on_query_error=lambda package, detail: reported.append((package, detail)),
    )
    window._is_current_inspector_request = lambda kind, generation, serial, package: (
        MainWindow._is_current_inspector_request(
            window, kind, generation, serial, package
        )
    )
    window._on_inspector_query_error = (
        lambda kind, generation, serial, package, detail, renderer: (
            MainWindow._on_inspector_query_error(
                window, kind, generation, serial, package, detail, renderer
            )
        )
    )

    MainWindow._start_inspector_worker(
        window,
        object(),
        "files",
        4,
        2,
        "example",
        lambda package, detail: rendered.append((package, detail)),
    )

    expected_detail = (
        "Could not start RPM inspection: RuntimeError: executor is shutting down"
    )
    assert reported == [("example", expected_detail)]
    assert rendered == [("example", expected_detail)]


class _CancellableWorker:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def test_close_cancels_file_owner_and_local_rpm_workers() -> None:
    file_owner_worker = _CancellableWorker()
    local_rpm_worker = _CancellableWorker()
    accepted: list[bool] = []
    cleared: list[bool] = []
    state = SimpleNamespace(saveState=lambda: b"state")
    window = SimpleNamespace(
        transaction_controller=SimpleNamespace(is_busy=False, has_ready_plan=False),
        repository_addition_runner=SimpleNamespace(is_running=False),
        repository_deletion_runner=SimpleNamespace(is_running=False),
        settings=SimpleNamespace(setValue=lambda *_args: None),
        saveGeometry=lambda: b"geometry",
        main_splitter=state,
        workspace_splitter=state,
        tree_view=SimpleNamespace(header=lambda: state),
        current_query_worker=None,
        current_orphan_worker=None,
        current_userinstalled_worker=None,
        current_updates_worker=None,
        current_remote_search_worker=None,
        current_file_owner_worker=file_owner_worker,
        current_local_rpm_inspection_worker=local_rpm_worker,
        _inspector_generation=0,
        thread_pool=SimpleNamespace(clear=lambda: cleared.append(True)),
    )
    event = SimpleNamespace(accept=lambda: accepted.append(True))

    MainWindow.closeEvent(window, event)

    assert file_owner_worker.cancelled
    assert local_rpm_worker.cancelled
    assert window._inspector_generation == 1
    assert cleared == [True]
    assert accepted == [True]


def test_history_dialog_ignores_stale_refresh_results(qtbot) -> None:
    dialog = DnfHistoryDialog()
    qtbot.addWidget(dialog)
    stale_request = dialog.set_history_loading()
    current_request = dialog.set_history_loading()
    entry = HistoryEntry(
        id=99,
        command_line="dnf install example",
        date_time="2026-10-28 09:00",
        action="Install",
        altered_count=1,
        return_code=0,
    )

    dialog.set_history_entries_if_current(stale_request, [entry])
    dialog.set_history_error_if_current(stale_request, "", "stale failure")

    assert dialog.table.rowCount() == 0
    assert dialog.status_label.text() == "Loading DNF transaction history…"

    dialog.set_history_entries_if_current(current_request, [entry])

    assert dialog.table.rowCount() == 1
    assert dialog.status_label.text() == "Loaded 1 DNF transaction."


def test_history_callbacks_ignore_stale_global_status(qtbot) -> None:
    dialog = DnfHistoryDialog()
    qtbot.addWidget(dialog)
    stale_request = dialog.set_history_loading()
    current_request = dialog.set_history_loading()
    status_messages: list[tuple[str, int]] = []
    reported: list[tuple[str, str]] = []
    window = SimpleNamespace(
        status_bar=SimpleNamespace(
            showMessage=lambda message, timeout: status_messages.append(
                (message, timeout)
            )
        ),
        _on_query_error=lambda package, detail: reported.append((package, detail)),
    )
    entry = HistoryEntry(
        id=100,
        command_line="dnf install example",
        date_time="2026-10-28 09:00",
        action="Install",
        altered_count=1,
        return_code=0,
    )

    MainWindow._on_dnf_history_failed(
        window, dialog, stale_request, "", "stale daemon failure"
    )

    assert reported == []
    assert status_messages == []
    assert dialog.status_label.text() == "Loading DNF transaction history…"

    MainWindow._on_dnf_history_loaded(window, dialog, current_request, [entry])

    assert status_messages == [("DNF transaction history loaded.", 3000)]
    assert dialog.table.rowCount() == 1


def test_history_dialog_distinguishes_a_failure_from_an_empty_history(qtbot) -> None:
    dialog = DnfHistoryDialog()
    qtbot.addWidget(dialog)

    dialog.set_history_loading()
    assert dialog.status_label.text() == "Loading DNF transaction history…"
    assert not dialog.refresh_btn.isEnabled()

    dialog.set_history_error("", "DNF database is unavailable")
    assert dialog.status_label.text() == "Could not load DNF history."
    assert dialog.status_label.toolTip() == "DNF database is unavailable"
    assert dialog.table.rowCount() == 0
    assert dialog.refresh_btn.isEnabled()
    assert not dialog.undo_btn.isEnabled()

    entry = HistoryEntry(
        id=42,
        command_line="dnf upgrade",
        date_time="2026-10-28 09:00",
        action="Upgrade",
        altered_count=3,
        return_code=0,
    )
    dialog.set_history_entries([entry])
    assert dialog.status_label.text() == "Loaded 1 DNF transaction."
    assert dialog.table.rowCount() == 1

    dialog.set_history_loading()
    dialog.set_history_error("", "Temporary daemon failure")
    assert dialog.status_label.text().endswith(
        "Showing the last successfully loaded entries."
    )
    assert dialog.table.rowCount() == 1
    assert dialog.undo_btn.isEnabled()
