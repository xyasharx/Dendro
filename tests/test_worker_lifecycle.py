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
        dnf5daemon.RepositoryListWorker,
        flatpak_inventory.FlatpakInventoryWorker,
    ],
)
def test_every_long_lived_worker_is_shutdown_safe(worker: type) -> None:
    assert issubclass(worker, ShutdownSafeRunnable)
    assert issubclass(worker, QRunnable)


def test_shutdown_safe_base_is_itself_a_plain_runnable() -> None:
    assert issubclass(ShutdownSafeRunnable, QRunnable)
    assert worker_lifecycle.DELETED_QOBJECT_MARKERS


def test_subclass_without_run_is_left_alone() -> None:
    class _NoRun(ShutdownSafeRunnable):
        pass

    assert "run" not in _NoRun.__dict__
