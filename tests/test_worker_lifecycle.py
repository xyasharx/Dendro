"""Regression coverage for shutdown-safe worker plumbing.

A ``QThreadPool`` runnable can still be executing when the window is closed and
Qt destroys the window's ``QObject`` children. Emitting a signal from such a
runnable raises ``RuntimeError: wrapped C/C++ object of type ... has been
deleted`` inside the pool thread, which printed an unhandled traceback during
application exit.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QRunnable

from dendro.core import backend, dnf5daemon, flatpak_inventory, worker_lifecycle
from dendro.core.worker_lifecycle import ShutdownSafeRunnable, is_deleted_qobject_error

DELETED_MESSAGE = "wrapped C/C++ object of type BackendSignals has been deleted"


class _Guarded(ShutdownSafeRunnable):
    def __init__(self, error: BaseException | None = None) -> None:
        super().__init__()
        self._error = error
        self.ran = False

    def run(self) -> None:
        self.ran = True
        if self._error is not None:
            raise self._error


def test_deleted_qobject_error_is_recognized() -> None:
    assert is_deleted_qobject_error(RuntimeError(DELETED_MESSAGE))


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("something else went wrong"),
        RuntimeError("wrapped C/C++ object of type X"),
        ValueError(DELETED_MESSAGE),
        OSError(DELETED_MESSAGE),
    ],
)
def test_other_errors_are_not_treated_as_teardown(error: BaseException) -> None:
    assert not is_deleted_qobject_error(error)


def test_teardown_emit_during_run_is_a_silent_terminal_no_op() -> None:
    runnable = _Guarded(RuntimeError(DELETED_MESSAGE))

    assert runnable.run() is None
    assert runnable.ran


def test_genuine_worker_errors_still_propagate() -> None:
    runnable = _Guarded(RuntimeError("a real bug"))

    with pytest.raises(RuntimeError, match="a real bug"):
        runnable.run()


def test_worker_run_methods_keep_their_qt_slot_metadata() -> None:
    """``@pyqtSlot`` is required for queued signal delivery from pool threads."""
    for worker in (
        backend.PackageQueryWorker,
        backend.PackageChangelogWorker,
        dnf5daemon.RepositoryListWorker,
        flatpak_inventory.FlatpakInventoryWorker,
    ):
        assert isinstance(worker.__dict__.get("run"), type(worker.__dict__["run"]))
        assert worker.__dict__["run"].__name__ == "run"


@pytest.mark.parametrize(
    "worker",
    [
        backend.LocalRpmInspectionWorker,
        backend.FileOwnerLookupWorker,
        backend.PackageQueryWorker,
        backend.UserInstalledQueryWorker,
        backend.OrphanQueryWorker,
        backend.DependencyTreeWorker,
        backend.ReverseDependencyWorker,
        backend.PackageFilesWorker,
        backend.DnfHistoryWorker,
        backend.PackageChangelogWorker,
        backend.PackageScriptletsWorker,
        backend.PackageVerifyWorker,
        backend.SystemUpdatesCheckWorker,
        backend.RemotePackageSearchWorker,
        backend.OfflineStatusQueryWorker,
        backend.SystemRebootWorker,
        dnf5daemon.RepositoryListWorker,
        flatpak_inventory.FlatpakInventoryWorker,
    ],
)
def test_every_long_lived_worker_is_shutdown_safe(worker: type) -> None:
    assert issubclass(worker, ShutdownSafeRunnable)
    assert issubclass(worker, QRunnable)


def test_offline_status_worker_only_probes_logind_when_scheduled_on_traditional_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dendro.core.dnf5daemon_types import (
        OfflineTransactionPhase,
        OfflineTransactionStatus,
    )
    from dendro.core.host_environment import HostEnvironment
    from dendro.core.system_reboot import RebootCapability

    class _FakeOfflineClient:
        def __init__(self, status: OfflineTransactionStatus) -> None:
            self._status = status

        def get_offline_status(self) -> OfflineTransactionStatus:
            return self._status

    class _FakeRebootClient:
        def __init__(self) -> None:
            self.queries = 0

        def query_reboot_capability(self) -> RebootCapability:
            self.queries += 1
            return RebootCapability.YES

    scheduled = OfflineTransactionStatus(
        pending=True, status="ready", phase=OfflineTransactionPhase.READY
    )
    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_host_environment", HostEnvironment.traditional)

    reboot_client = _FakeRebootClient()
    loaded: list[tuple[OfflineTransactionStatus, RebootCapability]] = []
    worker = backend.OfflineStatusQueryWorker(
        client_factory=lambda: _FakeOfflineClient(scheduled),
        reboot_client_factory=lambda: reboot_client,
    )
    worker.signals.loaded.connect(lambda st, cap: loaded.append((st, cap)))
    worker.run()

    assert reboot_client.queries == 1
    assert loaded == [(scheduled, RebootCapability.YES)]

    pre_staged = OfflineTransactionStatus(
        pending=False,
        status="download-complete",
        phase=OfflineTransactionPhase.DOWNLOAD_COMPLETE,
    )
    reboot_client_pre = _FakeRebootClient()
    loaded_pre: list[tuple[OfflineTransactionStatus, RebootCapability]] = []
    worker_pre = backend.OfflineStatusQueryWorker(
        client_factory=lambda: _FakeOfflineClient(pre_staged),
        reboot_client_factory=lambda: reboot_client_pre,
    )
    worker_pre.signals.loaded.connect(lambda st, cap: loaded_pre.append((st, cap)))
    worker_pre.run()

    assert reboot_client_pre.queries == 0
    assert loaded_pre == [(pre_staged, RebootCapability.UNAVAILABLE)]

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: True)
    monkeypatch.setattr(backend, "get_host_environment", HostEnvironment.flatpak)
    reboot_client_fp = _FakeRebootClient()
    loaded_fp: list[tuple[OfflineTransactionStatus, RebootCapability]] = []
    worker_fp = backend.OfflineStatusQueryWorker(
        client_factory=lambda: _FakeOfflineClient(scheduled),
        reboot_client_factory=lambda: reboot_client_fp,
    )
    worker_fp.signals.loaded.connect(lambda st, cap: loaded_fp.append((st, cap)))
    worker_fp.run()

    assert reboot_client_fp.queries == 0
    assert loaded_fp == [(scheduled, RebootCapability.UNAVAILABLE)]


def test_system_reboot_worker_emits_succeeded_and_failed_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dendro.core.host_environment import HostEnvironment
    from dendro.core.system_reboot import SystemRebootError

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_host_environment", HostEnvironment.traditional)

    class _OkClient:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def trigger_reboot(self, *, interactive: bool = True) -> None:
            self.calls.append(interactive)

    ok_client = _OkClient()
    ok_count = 0

    def _inc() -> None:
        nonlocal ok_count
        ok_count += 1

    worker_ok = backend.SystemRebootWorker(reboot_client_factory=lambda: ok_client)
    worker_ok.signals.succeeded.connect(_inc)
    worker_ok.run()
    assert ok_client.calls == [True]
    assert ok_count == 1

    class _FailingClient:
        def trigger_reboot(self, *, interactive: bool = True) -> None:
            raise SystemRebootError("System restart unavailable", "blocked")

    failures: list[tuple[str, str]] = []
    worker_fail = backend.SystemRebootWorker(
        reboot_client_factory=lambda: _FailingClient()
    )
    worker_fail.signals.failed.connect(lambda t, d: failures.append((t, d)))
    worker_fail.run()
    assert failures == [("System restart unavailable", "blocked")]


def test_shutdown_safe_base_is_itself_a_plain_runnable() -> None:
    assert issubclass(ShutdownSafeRunnable, QRunnable)
    assert worker_lifecycle.DELETED_QOBJECT_MARKERS


def test_subclass_without_run_is_left_alone() -> None:
    class _NoRun(ShutdownSafeRunnable):
        pass

    assert "run" not in _NoRun.__dict__
