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
