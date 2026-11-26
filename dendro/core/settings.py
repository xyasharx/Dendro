# dendro/core/settings.py
"""Shared Qt settings factory for Dendro preferences and window state."""

from __future__ import annotations

from typing import Any, Final

from PyQt6.QtCore import QSettings

SETTINGS_ORGANIZATION: Final[str] = "FedoraCommunity"
SETTINGS_APPLICATION: Final[str] = "Dendro"


def get_app_settings(settings_factory: Any = QSettings) -> QSettings:
    """Return Dendro's persistent :class:`QSettings` instance."""
    return settings_factory(SETTINGS_ORGANIZATION, SETTINGS_APPLICATION)


__all__ = (
    "SETTINGS_APPLICATION",
    "SETTINGS_ORGANIZATION",
    "get_app_settings",
)
