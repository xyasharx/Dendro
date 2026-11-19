"""The repository signing-key prompt must be safe by default.

Trusting a key is what makes every package a third-party repository publishes
installable, so the dialog has to show identifying details and make the
accidental answer the safe one.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QLabel

from core.dnf5daemon import AUTOMATIC_KEY_IMPORT_PREFERENCE, RepositoryKeyPrompt
from ui import key_import_dialog
from ui.key_import_dialog import RepositoryKeyImportDialog, _format_fingerprint


def _prompt(**overrides) -> RepositoryKeyPrompt:
    values = {
        "session_path": "/org/rpm/dnf/v0/session/1",
        "key_id": "8ACFA0B7",
        "fingerprint": "abcdef0123456789abcdef0123456789abcdef01",
        "key_url": "https://example.invalid/key.asc",
        "user_ids": ("Example Project <releng@example.invalid>",),
    }
    values.update(overrides)
    return RepositoryKeyPrompt(**values)


class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def value(self, key, default=None, *, type=None):
        value = self.values.get(key, default)
        return type(value) if type is not None and value is not None else value

    def setValue(self, key, value) -> None:
        self.values[key] = value


@pytest.fixture
def settings(monkeypatch):
    fake = _Settings()
    monkeypatch.setattr(key_import_dialog, "QSettings", lambda *_args: fake)
    return fake


def test_dialog_shows_the_details_a_person_needs_to_verify(qtbot, settings) -> None:
    dialog = RepositoryKeyImportDialog(_prompt())
    qtbot.addWidget(dialog)

    texts = [label.text() for label in dialog.findChildren(QLabel)]
    joined = "\n".join(texts)
    assert "Example Project <releng@example.invalid>" in joined
    assert "https://example.invalid/key.asc" in joined
    assert "8ACFA0B7" in joined
    # The fingerprint is grouped so it can be compared block by block.
    assert "ABCD EF01 2345 6789 ABCD EF01 2345 6789 ABCD EF01" in joined


def test_dialog_declines_by_default_so_a_stray_enter_is_safe(qtbot, settings) -> None:
    dialog = RepositoryKeyImportDialog(_prompt())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    # Let Qt finish assigning initial focus before asserting on it.
    qtbot.waitUntil(lambda: dialog.decline_button.hasFocus(), timeout=2000)

    assert dialog.decline_button.text() == "Don't Trust"
    assert dialog.decline_button.isDefault() is True
    assert dialog.trust_button.isDefault() is False
    assert dialog.decline_button.hasFocus()
    assert dialog.trust_button.objectName() == "ApplyButton"
    # Escape is the other accidental key, and it must also decline.
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_accepting_trusts_the_key_and_remembering_is_opt_in(qtbot, settings) -> None:
    dialog = RepositoryKeyImportDialog(_prompt())
    qtbot.addWidget(dialog)
    assert dialog.remember_checkbox.isChecked() is False

    dialog.trust_button.click()
    dialog.apply_remembered_choice()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert AUTOMATIC_KEY_IMPORT_PREFERENCE not in settings.values

    dialog2 = RepositoryKeyImportDialog(_prompt())
    qtbot.addWidget(dialog2)
    dialog2.remember_checkbox.setChecked(True)
    dialog2.trust_button.click()
    dialog2.apply_remembered_choice()
    assert settings.values[AUTOMATIC_KEY_IMPORT_PREFERENCE] is True


def test_declining_never_stores_the_automatic_preference(qtbot, settings) -> None:
    dialog = RepositoryKeyImportDialog(_prompt())
    qtbot.addWidget(dialog)
    dialog.remember_checkbox.setChecked(True)

    dialog.decline_button.click()
    dialog.apply_remembered_choice()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert AUTOMATIC_KEY_IMPORT_PREFERENCE not in settings.values


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abcdef", "ABCD EF"),
        ("", ""),
        ("  ", ""),
        ("ABCDEF0123456789", "ABCD EF01 2345 6789"),
        ("aa:bb:cc:dd", "AABB CCDD"),
    ],
)
def test_fingerprint_formatting(raw, expected) -> None:
    assert _format_fingerprint(raw) == expected


def test_dialog_survives_a_prompt_with_no_optional_details(qtbot, settings) -> None:
    dialog = RepositoryKeyImportDialog(
        _prompt(fingerprint="", key_url="", user_ids=(), key_id="")
    )
    qtbot.addWidget(dialog)
    assert dialog.decline_button.text() == "Don't Trust"
    labels = [label.text() for label in dialog.findChildren(QLabel)]
    assert "Fingerprint" not in labels
    assert Qt.TextInteractionFlag.TextSelectableByMouse
