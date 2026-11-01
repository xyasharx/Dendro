"""Icon helpers: Dendro's bundled SVG icons first, FreeDesktop theme icons second."""

from __future__ import annotations

from PyQt6.QtCore import QByteArray
from PyQt6.QtGui import QIcon, QImage, QPixmap

from ui.icon_data import ICON_SVGS


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

    SVG is decoded through Qt's image-format plugin (provided by the Qt SVG
    library, ``qt6-qtsvg`` on Fedora). The separate ``PyQt6.QtSvg`` Python
    binding is not required, and some packaged PyQt6 builds omit it.
    """
    svg = ICON_SVGS.get(name)
    if svg is None:
        return QIcon()
    image = QImage.fromData(QByteArray(svg.encode("utf-8")), "SVG")
    if image.isNull():
        return QIcon()
    return QIcon(QPixmap.fromImage(image))


def app_icon(bundled_name: str, *theme_names: str) -> QIcon:
    """Prefer the bundled icon so every desktop shows the same glyph."""
    icon = bundled_icon(bundled_name)
    if not icon.isNull():
        return icon
    return theme_icon(*theme_names)
