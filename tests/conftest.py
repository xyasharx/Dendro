"""Shared pytest fixtures."""

from __future__ import annotations

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
def _discard_leaked_widgets():
    """Destroy top-level widgets a test left behind, after it finishes.

    Qt parents every widget to nothing when a test builds one and drops the
    Python reference: the C++ object stays alive as a top-level widget of the
    process-wide ``QApplication``. Nothing collected them, so the suite
    accumulated dozens of retained widget trees, and every later
    ``QApplication.setStyleSheet`` -- which repolishes *every* live widget --
    walked all of them.

    That is the shape of the intermittent ``SIGSEGV`` CI reported at
    ``test_card_labels_render_on_the_card_surface``: the crash landed on the
    ``setStyleSheet`` line, before the test had built any widget of its own, so
    the offending object had to come from an earlier test. Measured with a
    teardown hook, one module grew to 59 retained top-level widgets.

    Deleting them after each test keeps the repolish bounded and makes each
    test responsible for only its own widgets. ``DeferredDelete`` events are
    flushed so the objects are really gone before the next test starts.
    """
    yield

    try:
        from PyQt6.QtCore import QCoreApplication, QEvent
        from PyQt6.QtWidgets import QApplication
    except ImportError:  # Qt not installed: nothing to clean up.
        return

    app = QApplication.instance()
    if app is None:
        return

    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    app.processEvents()
