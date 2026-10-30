"""Regression coverage for Qt icon fallback and dialog API behavior."""

from __future__ import annotations

from typing import ClassVar

from core.backend import LocalRpmInspectionResult
from ui import icons
from ui.icons import theme_icon
from ui.main_window import LocalRpmInstallDialog


class _FakeIcon:
    def __init__(self, name: str = "", *, null: bool = True) -> None:
        self.name = name
        self._null = null

    def isNull(self) -> bool:
        return self._null


class _FakeQIcon(_FakeIcon):
    requested_names: ClassVar[list[str]] = []

    @staticmethod
    def fromTheme(name: str) -> _FakeIcon:
        _FakeQIcon.requested_names.append(name)
        return _FakeIcon(name, null=name != "fallback")


def _local_rpm_result() -> LocalRpmInspectionResult:
    return LocalRpmInspectionResult(
        file_path="/tmp/example.rpm",
        name="example",
        version="1.0",
        release="1.fc45",
        arch="x86_64",
        summary="Example package",
        description="Example package for regression coverage.",
        license="MIT",
        url="https://example.invalid/",
        size_bytes=1024,
    )


def test_theme_icon_uses_first_non_null_theme_icon(monkeypatch) -> None:
    """A null QIcon must not prevent resolution of a later fallback name."""
    _FakeQIcon.requested_names = []
    monkeypatch.setattr(icons, "QIcon", _FakeQIcon)

    selected = theme_icon("missing-primary", "fallback", "unused")

    assert selected.name == "fallback"
    assert _FakeQIcon.requested_names == ["missing-primary", "fallback"]


def test_local_rpm_dialog_keeps_qdialog_result_method_callable(qtbot) -> None:
    """Inspection data must not shadow QDialog.result()."""
    inspection = _local_rpm_result()
    dialog = LocalRpmInstallDialog(inspection)
    qtbot.addWidget(dialog)

    assert dialog.inspection_result is inspection
    assert callable(dialog.result)
    assert dialog.result() == 0
