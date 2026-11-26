# dendro/core/worker_lifecycle.py
"""Shutdown-safe QRunnable plumbing shared by every Dendro background worker.

Workers outlive the widgets that created them: a ``QThreadPool`` runnable can
still be executing when the window is closed and Qt destroys the window's
``QObject`` children.  Emitting a signal from such a runnable then raises
``RuntimeError: wrapped C/C++ object of type ... has been deleted`` inside the
pool thread, which prints an unhandled traceback during application exit.

``ShutdownSafeRunnable`` converts that specific teardown-time error into a
silent, terminal no-op so application shutdown stays clean while genuine
programming errors keep propagating.
"""

from __future__ import annotations

import functools
from typing import Any

from PyQt6.QtCore import QRunnable, pyqtSlot

DELETED_QOBJECT_MARKERS = (
    "wrapped C/C++ object",
    "has been deleted",
)


def is_deleted_qobject_error(exc: BaseException) -> bool:
    """Return True when PyQt rejects an access to a QObject destroyed at shutdown."""
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc)
    return all(marker in message for marker in DELETED_QOBJECT_MARKERS)


class ShutdownSafeRunnable(QRunnable):
    """``QRunnable`` whose ``run()`` tolerates deleted-QObject signal errors."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        raw_run = cls.__dict__.get("run")
        if raw_run is None:
            return

        @pyqtSlot()
        @functools.wraps(raw_run)
        def _guarded_run(self: Any) -> Any:
            try:
                return raw_run(self)
            except RuntimeError as exc:
                if is_deleted_qobject_error(exc):
                    return None
                raise

        cls.run = _guarded_run  # type: ignore[method-assign]
