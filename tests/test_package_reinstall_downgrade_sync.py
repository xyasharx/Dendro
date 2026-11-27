"""Qt/model/worker tests for per-package reinstall, downgrade, and distro-sync."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from PyQt6.QtCore import Qt

from dendro.core.dnf5daemon import (
    Dnf5DaemonError,
    DnfPackageRecord,
    PackageRepoVersions,
    TransactionItem,
    TransactionPlan,
    TransactionRequest,
)
from dendro.core.dnf_workers import PackageVersionsQueryWorker
from dendro.core.host_environment import HostEnvironment, HostEnvironmentKind
from dendro.core.models import (
    STATUS_LABELS,
    DependencyTreeModel,
    PackageFilterProxyModel,
)
from dendro.core.package_types import (
    FileVerificationResult,
    PackageInfo,
    PackageState,
)
from dendro.ui.downgrade_dialog import DowngradePackageDialog
from dendro.ui.inspector_panel import PackageInspectorPanel
from dendro.ui.main_window import MainWindow
from dendro.ui.transaction_drawer import TransactionDrawer


def _installed_pkg(
    name: str = "mesa-dri-drivers",
    version: str = "24.3.1",
    release: str = "1.fc44",
    *,
    has_update: bool = False,
    update_version: str = "",
) -> PackageInfo:
    return PackageInfo(
        name=name,
        version=version,
        release=release,
        arch="x86_64",
        epoch="0",
        repository="updates",
        state=PackageState.INSTALLED,
        has_update=has_update,
        available_update_version=update_version,
    )


def test_tree_model_stages_reinstall_downgrade_sync_and_preserves_compat(qapp) -> None:
    pkg_reinstall = _installed_pkg("bash", "5.2.37", "1.fc44")
    pkg_downgrade = _installed_pkg("mesa", "24.3.1", "1.fc44")
    pkg_sync = _installed_pkg("nano", "8.2", "1.fc44")
    pkg_up = _installed_pkg(
        "vim", "9.1", "1.fc44", has_update=True, update_version="9.1-2.fc44"
    )

    model = DependencyTreeModel()
    model.set_packages([pkg_reinstall, pkg_downgrade, pkg_sync, pkg_up])

    assert STATUS_LABELS[PackageState.QUEUED_REINSTALL] == "To Reinstall"
    assert STATUS_LABELS[PackageState.QUEUED_DOWNGRADE] == "To Downgrade"
    assert STATUS_LABELS[PackageState.QUEUED_DISTRO_SYNC] == "To Sync"

    assert model.set_package_queue_state(
        model.index(0, 0), PackageState.QUEUED_REINSTALL
    )
    assert model.set_package_queue_state(
        model.index(1, 0),
        PackageState.QUEUED_DOWNGRADE,
        target_spec="mesa-24.2.5-1.fc44.x86_64",
        target_version="24.2.5-1.fc44",
    )
    assert model.set_package_queue_state(
        model.index(2, 0),
        PackageState.QUEUED_DISTRO_SYNC,
        target_spec="nano.x86_64",
        target_version="8.1-1.fc44",
    )
    model.toggle_queue_state(model.index(3, 0))

    specs = model.get_queued_transaction_specs()
    assert specs.reinstalls == ("bash-5.2.37-1.fc44.x86_64",)
    assert specs.downgrades == ("mesa-24.2.5-1.fc44.x86_64",)
    assert specs.distro_syncs == ("nano.x86_64",)
    assert specs.upgrades == ("vim.x86_64",)
    assert specs.total_count == 4

    # Backward-compatible 3-tuple still returns installs, removals, upgrades
    assert model.get_queued_packages() == ([], [], ["vim.x86_64"])

    # Version column shows current -> target path for downgrade and sync
    assert (
        model.index(1, DependencyTreeModel.COL_VERSION).data(
            Qt.ItemDataRole.DisplayRole
        )
        == "24.3.1-1.fc44 -> 24.2.5-1.fc44"
    )
    assert (
        model.index(2, DependencyTreeModel.COL_VERSION).data(
            Qt.ItemDataRole.DisplayRole
        )
        == "8.2-1.fc44 -> 8.1-1.fc44"
    )

    # Filter model recognizes queued and installed tokens for new states
    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)
    proxy.set_search_query("status:queued")
    assert proxy.rowCount() == 4
    proxy.set_search_query("status:installed")
    assert proxy.rowCount() == 4

    # Toggling any staged installed action restores INSTALLED cleanly
    model.toggle_queue_state(model.index(1, 0))
    assert pkg_downgrade.state == PackageState.INSTALLED
    assert pkg_downgrade.queued_target_spec == ""
    assert pkg_downgrade.queued_target_version == ""

    model.clear_all_queued()
    assert model.get_queued_transaction_specs().total_count == 0
    assert pkg_reinstall.state == PackageState.INSTALLED
    assert pkg_sync.state == PackageState.INSTALLED
    assert pkg_sync.queued_target_version == ""


def test_package_versions_worker_emits_loaded_error_and_respects_cancel(qapp) -> None:
    sample = PackageRepoVersions(
        package_name="mesa",
        arch="x86_64",
        downgrades=(
            DnfPackageRecord(
                name="mesa",
                version="24.2.0",
                release="1.fc44",
                arch="x86_64",
                repository="fedora",
            ),
        ),
    )

    class _FakeClient:
        def __init__(self, *, fail: Dnf5DaemonError | None = None) -> None:
            self._fail = fail
            self.closed = False

        def list_package_versions(self, *_a, **_kw) -> PackageRepoVersions:
            if self._fail is not None:
                raise self._fail
            return sample

        def close(self) -> None:
            self.closed = True

    client = _FakeClient()
    worker = PackageVersionsQueryWorker(
        "mesa", arch="x86_64", client_factory=lambda: client
    )
    loaded: list[tuple[str, PackageRepoVersions]] = []
    worker.signals.loaded.connect(lambda name, res: loaded.append((name, res)))
    worker.run()
    assert client.closed is True
    assert loaded == [("mesa", sample)]

    err_client = _FakeClient(fail=Dnf5DaemonError("Daemon error", "Cannot read repo"))
    err_worker = PackageVersionsQueryWorker(
        "mesa", arch="x86_64", client_factory=lambda: err_client
    )
    failures: list[tuple[str, str]] = []
    err_worker.signals.failed.connect(
        lambda name, detail: failures.append((name, detail))
    )
    err_worker.run()
    assert err_client.closed is True
    assert failures == [("mesa", "Daemon error: Cannot read repo")]

    cancelled_worker = PackageVersionsQueryWorker(
        "mesa", arch="x86_64", client_factory=lambda: _FakeClient()
    )
    cancelled_worker.cancel()
    cancelled_loaded: list[object] = []
    cancelled_worker.signals.loaded.connect(lambda _n, r: cancelled_loaded.append(r))
    cancelled_worker.run()
    assert cancelled_loaded == []


def test_downgrade_dialog_populates_selects_and_handles_empty_or_error(qtbot) -> None:
    pkg = _installed_pkg("mesa", "24.3.1", "1.fc44")
    dialog = DowngradePackageDialog(pkg)
    qtbot.addWidget(dialog)

    assert dialog.queue_button.isEnabled() is False
    assert dialog.progress_bar.isHidden() is False

    older_newer = DnfPackageRecord(
        name="mesa",
        version="24.2.5",
        release="2.fc44",
        arch="x86_64",
        repository="updates",
        download_size_bytes=512000,
    )
    older_base = DnfPackageRecord(
        name="mesa",
        version="24.2.0",
        release="1.fc44",
        arch="x86_64",
        repository="fedora",
        download_size_bytes=480000,
    )
    dialog.set_versions(
        PackageRepoVersions(
            package_name="mesa",
            arch="x86_64",
            downgrades=(older_newer, older_base),
        )
    )
    assert dialog.progress_bar.isHidden() is True
    assert dialog.table.rowCount() == 2
    assert dialog.queue_button.isEnabled() is True
    assert dialog.selected_record() == older_newer

    dialog.table.selectRow(1)
    assert dialog.selected_record() == older_base

    dialog.set_versions(PackageRepoVersions(package_name="mesa", arch="x86_64"))
    assert dialog.table.isHidden() is True
    assert dialog.queue_button.isEnabled() is False
    assert "No older versions" in dialog.status_label.text()

    dialog.set_error("Connection failed")
    assert dialog.queue_button.isEnabled() is False
    assert "Connection failed" in dialog.status_label.text()


def test_inspector_verification_shows_reinstall_only_for_actionable_diffs(
    qtbot,
) -> None:
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    pkg = _installed_pkg("bash", "5.2.37", "1.fc44")
    panel.set_package_info(pkg)
    assert panel.btn_verify_reinstall.isHidden() is True

    # Clean audit keeps Queue Reinstall hidden
    panel.set_package_verification("bash", [])
    assert panel.btn_verify_reinstall.isHidden() is True

    # Unprivileged unreadable config file alone keeps Queue Reinstall hidden
    panel.set_package_verification(
        "bash",
        [
            FileVerificationResult(
                path="/etc/shadow",
                status_flags="??5??????",
                is_config=True,
                is_missing=False,
                size_differs=False,
                mode_differs=False,
                digest_differs=False,
                mtime_differs=False,
                raw_line="??5?????? c /etc/shadow",
                annotation="Unreadable",
                is_unreadable=True,
            )
        ],
    )
    assert panel.btn_verify_reinstall.isHidden() is True

    # Modified file reveals Queue Reinstall and emits reinstall_requested
    emitted: list[PackageInfo] = []
    panel.reinstall_requested.connect(emitted.append)
    panel.set_package_verification(
        "bash",
        [
            FileVerificationResult(
                path="/usr/bin/bash",
                status_flags="S.5....T.",
                is_config=False,
                is_missing=False,
                size_differs=True,
                mode_differs=False,
                digest_differs=True,
                mtime_differs=True,
                raw_line="S.5....T. /usr/bin/bash",
                annotation="Size and digest differ",
                is_unreadable=False,
            )
        ],
    )
    assert panel.btn_verify_reinstall.isHidden() is False
    assert panel.btn_verify_reinstall.text() == "Queue Reinstall"
    panel.btn_verify_reinstall.click()
    assert emitted == [pkg]

    # When package state turns QUEUED_REINSTALL, both inspector controls flip to Cancel
    pkg.state = PackageState.QUEUED_REINSTALL
    panel.refresh_package_status(pkg)
    assert panel.btn_verify_reinstall.text() == "Cancel Reinstall"
    assert panel.queue_btn.text() == "Cancel Reinstall"

    # Read-only immutable host disables Queue Reinstall with clear tooltip
    panel.set_host_environment(
        HostEnvironment(kind=HostEnvironmentKind.ATOMIC_OSTREE, ostree_booted=True)
    )
    assert panel.btn_verify_reinstall.isEnabled() is False
    assert panel.btn_verify_reinstall.toolTip()


def test_transaction_drawer_renders_reinstall_and_downgrade_groups(qtbot) -> None:
    drawer = TransactionDrawer()
    qtbot.addWidget(drawer)

    drawer.set_transaction_preview(
        ["htop-3.3.0-1.fc44.x86_64"],
        ["nano-8.0-1.fc44.x86_64"],
        ["vim-9.1-2.fc44.x86_64"],
        ["bash-5.2.37-1.fc44.x86_64"],
        ["mesa-24.2.0-1.fc44.x86_64"],
    )
    summary = drawer.summary_label.text()
    assert "Reinstall (1):" in summary
    assert "Downgrade (1):" in summary
    assert "Upgrade (1):" in summary
    assert "Install (1):" in summary
    assert "Remove (1):" in summary


def test_main_window_handlers_stage_and_resolve_reinstall_downgrade_sync(qapp) -> None:
    pkg = _installed_pkg("bash", "5.2.37", "1.fc44")
    model = DependencyTreeModel()
    model.set_packages([pkg])
    captured_requests: list[TransactionRequest] = []
    status_messages: list[str] = []
    refreshed: list[bool] = []

    window = SimpleNamespace(
        host_environment=HostEnvironment.traditional(),
        tree_model=model,
        status_bar=SimpleNamespace(
            showMessage=lambda msg, *_a: status_messages.append(msg)
        ),
        _pending_updates_map={},
        _refresh_active_inspector_status=lambda: refreshed.append(True),
        _begin_transaction_resolution=lambda req, kind: captured_requests.append(req),
        _show_transaction_error=lambda *_a: None,
    )

    MainWindow._on_inspector_reinstall_requested(window, pkg)
    assert pkg.state == PackageState.QUEUED_REINSTALL
    assert refreshed == [True]

    from dendro.ui.main_window_transactions import MainWindowTransactionsMixin

    MainWindowTransactionsMixin._on_header_apply_clicked(window)
    assert len(captured_requests) == 1
    assert captured_requests[0].reinstalls == ("bash-5.2.37-1.fc44.x86_64",)

    # Toggling via inspector again unqueues it back to INSTALLED
    MainWindow._on_inspector_reinstall_requested(window, pkg)
    assert pkg.state == PackageState.INSTALLED

    # Plan ready forwards grouped reinstall & downgrade buckets into drawer
    drawer_calls: list[tuple[Any, ...]] = []
    plan = TransactionPlan(
        token="p1",
        session_path="/org/rpm/dnf/v0/session/1",
        request=TransactionRequest.from_values(
            reinstalls=["bash-5.2.37-1.fc44.x86_64"]
        ),
        items=(
            TransactionItem(
                object_type="Package",
                action="Reinstall",
                reason="User",
                attributes={},
                package={
                    "name": "bash",
                    "version": "5.2.37",
                    "release": "1.fc44",
                    "arch": "x86_64",
                },
            ),
        ),
        resolver_result=0,
        warnings=(),
        critical_removals=(),
    )

    class _FakeDialog:
        def __init__(self, **_kw) -> None:
            pass

        def exec(self) -> int:
            from PyQt6.QtWidgets import QDialog

            return QDialog.DialogCode.Accepted

        def stage_for_next_boot(self) -> bool:
            return False

    plan_window = SimpleNamespace(
        transaction_controller=SimpleNamespace(
            execute_ready_plan=lambda: None,
            discard_ready_plan=lambda: None,
        ),
        transaction_drawer=SimpleNamespace(
            set_transaction_preview=lambda *args: drawer_calls.append(args),
            start_execution_mode=lambda: None,
        ),
        status_bar=SimpleNamespace(showMessage=lambda *_a: None),
    )
    import dendro.ui.main_window as mw_mod

    orig_cls = mw_mod.TransactionPlanReviewDialog
    mw_mod.TransactionPlanReviewDialog = _FakeDialog
    try:
        MainWindowTransactionsMixin._on_dnf_plan_ready(plan_window, plan)
    finally:
        mw_mod.TransactionPlanReviewDialog = orig_cls

    assert drawer_calls == [([], [], [], ["bash-5.2.37-1.fc44.x86_64"], [])]
    # Cancel event sanity check for static import cleanliness
    assert isinstance(threading.Event(), threading.Event)


def test_main_window_downgrade_and_distro_sync_flows(qapp) -> None:
    from PyQt6.QtWidgets import QDialog

    pkg = _installed_pkg("mesa", "24.3.1", "1.fc44")
    model = DependencyTreeModel()
    model.set_packages([pkg])
    target = DnfPackageRecord(
        name="mesa",
        version="24.2.5",
        release="2.fc44",
        arch="x86_64",
        repository="updates",
    )
    versions_for_downgrade = PackageRepoVersions(
        package_name="mesa",
        arch="x86_64",
        downgrades=(target,),
        sync_target=target,
        sync_needed=True,
        sync_direction="downgrade",
    )
    versions_matched = PackageRepoVersions(
        package_name="mesa",
        arch="x86_64",
        sync_target=target,
        sync_needed=False,
        sync_direction="match",
    )

    class _FakeSignals:
        def __init__(self) -> None:
            self.loaded_cb: list[Any] = []
            self.failed_cb: list[Any] = []

        @property
        def loaded(self):
            callbacks = self.loaded_cb

            class _Sig:
                def connect(self, fn) -> None:
                    callbacks.append(fn)

            return _Sig()

        @property
        def failed(self):
            callbacks = self.failed_cb

            class _Sig:
                def connect(self, fn) -> None:
                    callbacks.append(fn)

            return _Sig()

    class _FakeWorker:
        last_instance = None

        def __init__(self, *_a, **_kw) -> None:
            self.signals = _FakeSignals()
            self.cancelled = False
            _FakeWorker.last_instance = self

        def cancel(self) -> None:
            self.cancelled = True

    class _FakeDowngradeDlg:
        def __init__(self, package, parent=None) -> None:
            self.package = package
            self._chosen: DnfPackageRecord | None = None

        def set_versions(self, versions: PackageRepoVersions) -> None:
            self._chosen = versions.downgrades[0] if versions.downgrades else None

        def set_error(self, detail: str) -> None:
            pass

        def exec(self) -> int:
            for cb in _FakeWorker.last_instance.signals.loaded_cb:
                cb(self.package.name, versions_for_downgrade)
            return QDialog.DialogCode.Accepted

        def selected_record(self) -> DnfPackageRecord | None:
            return self._chosen

    class _FakePool:
        def start(self, _worker) -> None:
            pass

    messages: list[str] = []
    fake_win = SimpleNamespace(
        host_environment=HostEnvironment.traditional(),
        tree_model=model,
        thread_pool=_FakePool(),
        status_bar=SimpleNamespace(showMessage=lambda m, *_a: messages.append(m)),
        current_package_versions_worker=None,
        _package_versions_request_serial=0,
        _resolve_window_hook=lambda name, default: (
            _FakeDowngradeDlg
            if name == "DowngradePackageDialog"
            else _FakeWorker
            if name == "PackageVersionsQueryWorker"
            else default
        ),
    )

    source_idx = model.index(0, 0)
    MainWindow._open_downgrade_dialog(fake_win, source_idx)
    assert pkg.state == PackageState.QUEUED_DOWNGRADE
    assert pkg.queued_target_spec == "mesa-24.2.5-2.fc44.x86_64"
    assert pkg.queued_target_version == "24.2.5-2.fc44"

    # Match repo version -> keeps state untouched and informs user
    model.set_package_queue_state(source_idx, PackageState.INSTALLED)
    MainWindow._sync_package_to_repo_version(fake_win, source_idx)
    for cb in _FakeWorker.last_instance.signals.loaded_cb:
        cb("mesa", versions_matched)
    assert pkg.state == PackageState.INSTALLED
    assert any("already matches" in msg for msg in messages)

    # Sync needed -> queues QUEUED_DISTRO_SYNC with target version
    MainWindow._sync_package_to_repo_version(fake_win, source_idx)
    for cb in _FakeWorker.last_instance.signals.loaded_cb:
        cb("mesa", versions_for_downgrade)
    assert pkg.state == PackageState.QUEUED_DISTRO_SYNC
    assert pkg.transaction_spec == "mesa.x86_64"
    assert pkg.queued_target_version == "24.2.5-2.fc44"

    # Read-only immutable host refuses downgrade and sync cleanly
    model.set_package_queue_state(source_idx, PackageState.INSTALLED)
    fake_win.host_environment = HostEnvironment(
        kind=HostEnvironmentKind.ATOMIC_OSTREE, ostree_booted=True
    )
    MainWindow._open_downgrade_dialog(fake_win, source_idx)
    MainWindow._sync_package_to_repo_version(fake_win, source_idx)
    assert pkg.state == PackageState.INSTALLED
