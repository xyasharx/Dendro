"""Ask the user whether to trust one repository OpenPGP signing key.

``dnf5daemon`` blocks the call that loads repository metadata until its client
answers ``Repo.confirm_key_with_options``, so this dialog is shown from the
signal handler on the GUI thread while the resolve waits on a worker.  Trusting
a key is what makes every package a third-party repository ships installable, so
the default button is the safe one and the identifying details are shown rather
than a bare "yes/no".
"""

from __future__ import annotations

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.dnf5daemon import AUTOMATIC_KEY_IMPORT_PREFERENCE, RepositoryKeyPrompt
from core.i18n import tr
from core.settings import get_app_settings
from ui.fonts import apply_labelled_fonts


def _format_fingerprint(value: str) -> str:
    """Group a fingerprint into uppercase four-character blocks.

    Keys are published in blocks; an unbroken 40-character run is where people
    stop being able to compare one character at a time.
    """
    compact = "".join(character for character in value if character.isalnum())
    if not compact:
        return ""
    blocks = [compact[index : index + 4] for index in range(0, len(compact), 4)]
    return " ".join(blocks).upper()


class RepositoryKeyImportDialog(QDialog):
    """One modal decision about trusting a repository's signing key."""

    def __init__(
        self, prompt: RepositoryKeyPrompt, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Repository signing key"))
        self.setModal(True)
        self._prompt = prompt
        self._accepted = False
        self._settings = get_app_settings(QSettings)
        self._init_ui()
        apply_labelled_fonts(self)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        heading = QLabel(tr("A repository wants to add its signing key"))
        heading.setStyleSheet("font-weight: bold;")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        explanation = QLabel(
            tr(
                "The key signs the packages from this repository. If you trust it, "
                "packages the repository publishes can be installed. If you decline, "
                "the repository is left out of this transaction and nothing else "
                "changes."
            )
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("SubtleHelpLabel")
        layout.addWidget(explanation)

        details = QFrame()
        details.setObjectName("StatsFrame")
        details_layout = QGridLayout(details)
        details_layout.setContentsMargins(12, 12, 12, 12)
        details_layout.setHorizontalSpacing(14)
        details_layout.setVerticalSpacing(8)

        rows: list[tuple[str, str]] = []
        if self._prompt.user_ids:
            rows.append((tr("Signed by"), "\n".join(self._prompt.user_ids)))
        if self._prompt.fingerprint:
            rows.append(
                (tr("Fingerprint"), _format_fingerprint(self._prompt.fingerprint))
            )
        if self._prompt.key_url:
            rows.append((tr("Downloaded from"), self._prompt.key_url))
        if self._prompt.key_id:
            rows.append((tr("Key ID"), self._prompt.key_id))

        for index, (label_text, value_text) in enumerate(rows):
            label = QLabel(label_text)
            label.setObjectName("SubtleHelpLabel")
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            value = QLabel(value_text)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            details_layout.addWidget(label, index, 0)
            details_layout.addWidget(value, index, 1)
        details_layout.setColumnStretch(1, 1)
        layout.addWidget(details)

        verification = QLabel(
            tr(
                "Compare the fingerprint with the one the repository publishes. "
                "Importing a key is only as safe as the source you checked it against."
            )
        )
        verification.setWordWrap(True)
        verification.setObjectName("SubtleHelpLabel")
        layout.addWidget(verification)

        self.remember_checkbox = QCheckBox(
            tr("Trust new repositories' signing keys without asking from now on")
        )
        self.remember_checkbox.setToolTip(
            tr(
                "Stores the automatic key import preference in Settings. Leave this "
                "off to decide again the next time a repository needs a key."
            )
        )
        layout.addWidget(self.remember_checkbox)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)

        self.decline_button = QPushButton(tr("Don't Trust"))
        self.decline_button.setToolTip(
            tr(
                "Leave the repository out of this transaction. Nothing is installed "
                "or removed from it."
            )
        )
        self.decline_button.clicked.connect(self.reject)
        buttons.addWidget(self.decline_button)

        self.trust_button = QPushButton(tr("Trust and Import"))
        self.trust_button.setObjectName("ApplyButton")
        # Trusting is the emphasised action but deliberately not the default:
        # an accidental Enter must decline.
        self.trust_button.setAutoDefault(False)
        self.trust_button.setDefault(False)
        self.trust_button.clicked.connect(self._on_trust_clicked)
        buttons.addWidget(self.trust_button)

        layout.addLayout(buttons)

        self.decline_button.setDefault(True)
        self.decline_button.setFocus()

    def _on_trust_clicked(self) -> None:
        # Record the decision before accepting so apply_remembered_choice() can
        # tell a trusted key from a declined one.
        self._accepted = True
        self.accept()

    def apply_remembered_choice(self) -> None:
        """Persist the "stop asking" choice, but only for a trusted key.

        Declining while the checkbox is ticked must not turn on automatic trust:
        the user just said no to this key, and a rejected key is exactly the case
        where silently trusting the next one would be worst.
        """
        if self._accepted and self.remember_checkbox.isChecked():
            self._settings.setValue(AUTOMATIC_KEY_IMPORT_PREFERENCE, True)
