# dendro/ui/transaction_drawer.py
"""
Slide-out drawer and log viewer for reviewing queued package actions and executing DNF transactions.
Includes smart carriage-return processing for real-time terminal output.
Uses native FreeDesktop theme icons.
"""

from __future__ import annotations

import html

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from dendro.core.i18n import format_number, tr
from dendro.ui.fonts import apply_labelled_fonts, scaled_fixed_font
from dendro.ui.icons import theme_icon


class TransactionDrawer(QWidget):
    """
    Detailed drawer showing transaction details, progress bar,
    terminal output, and transaction controls.
    """

    cancel_requested = pyqtSignal()
    commit_requested = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TransactionDrawer")
        self._cancel_state = "cancel"
        self._summary_state = "empty"
        self._preview_data: tuple[list[str], list[str], list[str]] | None = None
        self._init_ui()
        apply_labelled_fonts(self)
        self.reset()

    def _init_ui(self):
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(16, 12, 16, 12)
        self.layout.setSpacing(10)

        # Header Info Bar
        header_layout = QHBoxLayout()
        self.title_label = QLabel(tr("Transaction Queue & Progress"))
        self.title_label.setObjectName("DrawerTitleLabel")

        self.close_btn = QPushButton()
        self.close_btn.setObjectName("DrawerCloseBtn")
        close_icon = theme_icon("window-close", "dialog-close")
        if not close_icon.isNull():
            self.close_btn.setIcon(close_icon)
        else:
            self.close_btn.setText("x")
        self.close_btn.setAccessibleName(tr("Close transaction drawer"))
        self.close_btn.setToolTip(tr("Close drawer"))
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.closed.emit)

        header_layout.addWidget(self.title_label, stretch=1)
        header_layout.addWidget(self.close_btn)
        self.layout.addLayout(header_layout)

        # Transaction Summary Metrics
        self.summary_label = QLabel(tr("No operations queued."))
        self.summary_label.setObjectName("DrawerSummaryLabel")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.summary_label.setWordWrap(True)
        self.layout.addWidget(self.summary_label)

        # Separator line
        line = QFrame()
        line.setObjectName("StatSeparator")
        line.setFixedHeight(1)
        self.layout.addWidget(line)

        # Progress Bar (Hidden by default, shown during run)
        self.progress_bar = QProgressBar()
        self.progress_bar.setAccessibleName(tr("Transaction progress"))
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.hide()
        self.layout.addWidget(self.progress_bar)

        # Real-time Terminal Log Console
        self.console = QTextEdit()
        self.console.setObjectName("ConsoleOutput")
        self.console.setAccessibleName(tr("Transaction execution log"))
        self.console.setReadOnly(True)
        self.console.setFont(scaled_fixed_font())
        self.console.setPlaceholderText(
            tr("DNF5 daemon progress and authorization status will appear here…")
        )
        self.console.document().setMaximumBlockCount(
            5000
        )  # Prevents unbound RAM growth
        self.layout.addWidget(self.console, stretch=1)

        # Bottom Action Bar
        action_layout = QHBoxLayout()
        action_layout.addStretch(1)

        self.cancel_btn = QPushButton(tr("Cancel"))
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)

        self.commit_btn = QPushButton(tr("Commit and Authenticate"))
        self.commit_btn.setObjectName("ApplyButton")
        self.commit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.commit_btn.clicked.connect(self.commit_requested.emit)

        action_layout.addWidget(self.cancel_btn)
        action_layout.addWidget(self.commit_btn)
        self.layout.addLayout(action_layout)
        self._sync_scaled_chrome()
        self.setTabOrder(self.close_btn, self.cancel_btn)
        self.setTabOrder(self.cancel_btn, self.commit_btn)
        self.setTabOrder(self.commit_btn, self.console)

    def _sync_scaled_chrome(self) -> None:
        side = max(28, self.fontMetrics().height() + 8)
        self.close_btn.setFixedSize(side, side)
        self.progress_bar.setMinimumHeight(max(18, self.fontMetrics().height() + 2))
        self.console.setFont(scaled_fixed_font())

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "console"):
            self._sync_scaled_chrome()

    def _render_preview(self) -> None:
        if self._preview_data is None:
            return
        installs, removals, upgrades = self._preview_data

        def _format_group(heading: str, items: list[str], limit: int = 8) -> str:
            shown = ", ".join(html.escape(pkg, quote=True) for pkg in items[:limit])
            remainder = len(items) - limit
            if remainder > 0:
                shown = tr("{shown}, and {remainder} more").format(
                    shown=shown, remainder=format_number(remainder)
                )
            return f"<b>{heading} ({format_number(len(items))}):</b> {shown}"

        parts = []
        if upgrades:
            parts.append(_format_group(tr("Upgrade"), upgrades))
        if installs:
            parts.append(_format_group(tr("Install"), installs))
        if removals:
            parts.append(_format_group(tr("Remove"), removals))

        self.summary_label.setText("<br>".join(parts))

    def set_transaction_preview(
        self,
        installs: list[str],
        removals: list[str],
        upgrades: list[str] | None = None,
    ):
        """Populates the summary text with package diffs."""
        if upgrades is None:
            upgrades = []
        total_ops = len(installs) + len(removals) + len(upgrades)
        if total_ops == 0:
            self._preview_data = None
            self._summary_state = "no_changes"
            self.summary_label.setText(tr("No changes queued."))
            self.commit_btn.setEnabled(False)
            return

        self._preview_data = (list(installs), list(removals), list(upgrades))
        self._summary_state = "preview"
        self._render_preview()
        self.commit_btn.setEnabled(True)

    def start_execution_mode(self):
        """Prepares the drawer for live execution output."""
        self.progress_bar.show()
        self.progress_bar.setValue(0)
        self.commit_btn.setEnabled(False)
        self._cancel_state = "abort"
        self.cancel_btn.setText(tr("Abort"))
        self.console.clear()

    def set_progress(self, percent: int):
        self.progress_bar.setValue(percent)

    def append_log(self, text: str):
        """Handles carriage return cleanups for DNF live progress rendering."""
        if not text:
            return

        # Guarantee insertion occurs strictly at the end of the document, even if user clicked in the console
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.console.setTextCursor(cursor)

        # Replace active block in place if carriage return is present
        if "\r" in text:
            lines = text.split("\r")
            for i, line in enumerate(lines):
                if i == 0 and line:
                    self.console.insertPlainText(line)
                elif line:
                    cur = self.console.textCursor()
                    cur.movePosition(QTextCursor.MoveOperation.End)
                    cur.select(QTextCursor.SelectionType.BlockUnderCursor)
                    cur.removeSelectedText()
                    cur.insertText(line)
        else:
            self.console.insertPlainText(text)

        self.console.ensureCursorVisible()

    def finish_execution_mode(self, success: bool):
        self.progress_bar.setValue(100 if success else 0)
        self._cancel_state = "close"
        self.cancel_btn.setText(tr("Close"))
        self.commit_btn.setEnabled(False)
        if success:
            self._summary_state = "finished"
            self._preview_data = None
            self.summary_label.setText(tr("Transaction finished successfully."))

    def reset(self):
        """Resets drawer state."""
        self.progress_bar.hide()
        self.progress_bar.setValue(0)
        self._cancel_state = "cancel"
        self.cancel_btn.setText(tr("Cancel"))
        self.commit_btn.setEnabled(True)
        self.console.clear()

    def retranslate(self) -> None:
        """Refresh drawer labels and active preview text for the current locale."""
        self.title_label.setText(tr("Transaction Queue & Progress"))
        self.close_btn.setAccessibleName(tr("Close transaction drawer"))
        self.close_btn.setToolTip(tr("Close drawer"))
        self.progress_bar.setAccessibleName(tr("Transaction progress"))
        self.console.setAccessibleName(tr("Transaction execution log"))
        self.console.setPlaceholderText(
            tr("DNF5 daemon progress and authorization status will appear here…")
        )
        self.commit_btn.setText(tr("Commit and Authenticate"))
        cancel_labels = {
            "abort": "Abort",
            "close": "Close",
            "cancel": "Cancel",
        }
        self.cancel_btn.setText(tr(cancel_labels.get(self._cancel_state, "Cancel")))
        if self._summary_state == "preview" and self._preview_data is not None:
            self._render_preview()
        elif self._summary_state == "finished":
            self.summary_label.setText(tr("Transaction finished successfully."))
        elif self._summary_state == "no_changes":
            self.summary_label.setText(tr("No changes queued."))
        else:
            self.summary_label.setText(tr("No operations queued."))
