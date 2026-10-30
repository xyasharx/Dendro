"""Small, Qt-correct helpers for resolving FreeDesktop theme icons."""

from __future__ import annotations

from PyQt6.QtGui import QIcon


def theme_icon(*names: str) -> QIcon:
    """Return the first available themed icon from ``names``.

    ``QIcon`` instances are truthy even when they are null, so Python's
    ``QIcon.fromTheme("first") or QIcon.fromTheme("fallback")`` cannot provide
    a working fallback. Check ``isNull()`` explicitly instead.
    """
    for name in names:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    return QIcon()
