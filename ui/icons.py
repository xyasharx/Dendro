"""Icon helpers: Dendro's bundled SVG icons first, FreeDesktop theme icons second."""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QSize, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap

from ui.icon_data import ICON_SVGS

try:
    from PyQt6.QtSvg import QSvgRenderer
except ImportError:  # The bundled set degrades to theme icons without QtSvg.
    QSvgRenderer = None

_ICON_SIZES = (16, 24, 32, 48, 64)


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


def bundled_icon(name: str) -> QIcon:
    """Return a Dendro-bundled icon, or a null icon if the name is unknown.

    Names are looked up in ``ICON_SVGS`` only. There is no file-system lookup,
    so an arbitrary string cannot reach outside the bundled set.
    """
    svg = ICON_SVGS.get(name)
    if svg is None or QSvgRenderer is None:
        return QIcon()
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        return QIcon()
    icon = QIcon()
    for size in _ICON_SIZES:
        pixmap = QPixmap(QSize(size, size))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def app_icon(bundled_name: str, *theme_names: str) -> QIcon:
    """Prefer the bundled icon so every desktop shows the same glyph."""
    icon = bundled_icon(bundled_name)
    if not icon.isNull():
        return icon
    return theme_icon(*theme_names)
