"""Shared pytest fixtures."""

from __future__ import annotations

import gc
import os

import pytest

from core.flatpak_inventory import FLATPAK_BINARY


@pytest.fixture
def flatpak_binary_installed(monkeypatch):
    """Make the Flatpak binary look installed, whatever this machine has.

    Some code paths check the binary before the rule under test. Without this
    fixture, those tests depend on whether Flatpak is installed on the machine
    (for example, a CI image without it).
    """
    real_isfile = os.path.isfile
    real_access = os.access

    def isfile(path):
        return True if path == FLATPAK_BINARY else real_isfile(path)

    def access(path, mode, *args, **kwargs):
        if path == FLATPAK_BINARY:
            return True
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(os.path, "isfile", isfile)
    monkeypatch.setattr(os, "access", access)


@pytest.fixture(autouse=True)
def _collect_widget_cycles():
    """Break the reference cycles that outlive a test and retain its widgets.

    Qt parents a widget to nothing when a test builds one and drops the Python
    reference. The wrapper usually dies with that reference and takes the C++
    object with it, but a widget wired to a signal, a lambda or its own child
    layout sits in a reference cycle, so it survives until the collector runs --
    and CPython's collector is threshold-driven, not per-test.

    Retained trees therefore accumulated: measured over
    tests/test_visual_regressions.py, the number of live top-level widgets grew
    from 8 to 59. Every later ``QApplication.setStyleSheet`` repolishes *every*
    live widget, so each restyle walked all of them, and
    ``test_card_labels_render_on_the_card_surface`` crashed inside that walk --
    on its ``setStyleSheet`` line, before it had built a widget of its own, which
    is why the fault had to come from an earlier test. The same test faults on
    main at 6545be4, so this predates the manifest work.

    Collecting after each test keeps the population at zero. A collection pass is
    the safe instrument here: it only reclaims objects Python already considers
    unreachable, so a window that a pending callback still references is left
    alone. Tearing widgets down explicitly instead is not safe -- it deletes
    windows whose workers have not finished, and the next callback then raises on
    a dangling ``QStatusBar``.
    """
    yield

    # A retained cycle is visible here: its C++ object is still a top-level
    # widget, so an empty list means there is nothing to reclaim and the
    # collection pass (which walks the whole heap) can be skipped. Only about a
    # fifth of the suite builds widgets, and calling collect() unconditionally
    # tripled the suite's run time.
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:  # Qt not installed: no widgets, nothing to reclaim.
        return

    app = QApplication.instance()
    if app is not None and app.topLevelWidgets():
        gc.collect()


@pytest.fixture(scope="module", autouse=True)
def _retire_qt_state_before_shutdown():
    """Tear down Qt's process-wide state while Python can still run.

    A module that builds widgets leaves a ``QApplication``, a stylesheet set on
    it, and the C++ objects behind both alive until the interpreter exits. Qt
    then destroys them during module cleanup, when Python's own objects are
    already being freed, and the ordering between the two runtimes is not
    guaranteed. When it goes wrong the process dies with ``SIGSEGV`` *after*
    pytest has printed its final line -- the suite reports success and the exit
    status is still 139.

    That is the signature CI has been failing on for
    ``tests/test_visual_regressions.py``: the fault annotation carries
    ``status=0 ... 94 passed`` and then ``SIGSEGV (status 139)``, and the name it
    prints as "inflight" is only the last line of a run that had already
    finished. It also explains why the named test kept changing and why some of
    those tests had passed.

    Doing the teardown here means the destruction happens in a defined order,
    with the interpreter healthy, instead of depending on shutdown ordering.
    """
    yield

    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:  # Qt not installed: no Qt state to retire.
        return

    application = QApplication.instance()
    if application is None:
        return

    # Delete every window while the event loop is still usable.
    for widget in application.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    from PyQt6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    application.processEvents()

    # Drop the application-wide stylesheet. It holds a parsed style tree that
    # references every polished widget, so clearing it releases that graph
    # before the interpreter finalises rather than during it.
    if application.styleSheet():
        application.setStyleSheet("")
        application.processEvents()

    # Collect the cycles that hold the remaining wrappers; a wrapper whose
    # Python object is already gone at shutdown is what leaves Qt holding a
    # half-destroyed object.
    gc.collect()
