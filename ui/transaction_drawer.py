# ui/transaction_drawer.py
"""
Slide-out drawer and log viewer for reviewing pending changes and executing DNF transactions.
"""

from __future__ import annotations

from typing import List, Optional
from PyQt6.QtCore import Qt, pyqtSignal
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


class TransactionDrawer(QWidget):
    """
    Detailed drawer showing transaction details, progress bar,
    terminal output, and transaction controls.
    """

    cancel_requested = pyqtSignal()
    commit_requested = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._init_ui()
        self.reset()

    def _init_ui(self):
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(16, 12, 16, 12)
        self.layout.setSpacing(10)

        # Header Info Bar
        header_layout = QHBoxLayout()
        self.title_label = QLabel("Pending Transaction Details")
        self.title_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #89b4fa;")
        
        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.closed.emit)

        header_layout.addWidget(self.title_label, stretch=1)
        header_layout.addWidget(self.close_btn)
        self.layout.addLayout(header_layout)

        # Transaction Summary Metrics
        self.summary_label = QLabel("No operations queued.")
        self.summary_label.setStyleSheet("color: #a6adc8; font-size: 12px;")
        self.layout.addWidget(self.summary_label)

        # Separator line
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("border-top: 1px solid #313244;")
        self.layout.addWidget(line)

        # Progress Bar (Hidden by default, shown during run)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 4px;
                text-align: center;
                height: 16px;
                font-size: 11px;
                color: #cdd6f4;
            }
            QProgressBar::chunk {
                background-color: #89b4fa;
                border-radius: 3px;
            }
        """)
        self.progress_bar.hide()
        self.layout.addWidget(self.progress_bar)

        # Real-time Terminal Log Console
        self.console = QTextEdit()
        self.console.setObjectName("ConsoleOutput")
        self.console.setReadOnly(True)
        self.console.setPlaceholderText("Transaction logs and Polkit authorization output will appear here...")
        self.layout.addWidget(self.console, stretch=1)

        # Bottom Action Bar
        action_layout = QHBoxLayout()
        action_layout.addStretch(1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)

        self.commit_btn = QPushButton("Commit & Authenticate")
        self.commit_btn.setObjectName("ApplyButton")
        self.commit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.commit_btn.clicked.connect(self.commit_requested.emit)

        action_layout.addWidget(self.cancel_btn)
        action_layout.addWidget(self.commit_btn)
        self.layout.addLayout(action_layout)

    def set_transaction_preview(self, installs: List[str], removals: List[str]):
        """Populates the summary text with package diffs."""
        total_ops = len(installs) + len(removals)
        if total_ops == 0:
            self.summary_label.setText("No changes queued.")
            self.commit_btn.setEnabled(False)
            return

        parts = []
        if installs:
            parts.append(f"<b>Install ({len(installs)}):</b> {', '.join(installs)}")
        if removals:
            parts.append(f"<b>Remove ({len(removals)}):</b> {', '.join(removals)}")

        self.summary_label.setText("<br>".join(parts))
        self.commit_btn.setEnabled(True)

    def start_execution_mode(self):
        """Prepares the drawer for live execution output."""
        self.progress_bar.show()
        self.progress_bar.setValue(0)
        self.commit_btn.setEnabled(False)
        self.cancel_btn.setText("Abort")
        self.console.clear()

    def set_progress(self, percent: int):
        self.progress_bar.setValue(percent)

    def append_log(self, text: str):
        self.console.insertPlainText(text)
        self.console.ensureCursorVisible()

    def finish_execution_mode(self, success: bool):
        self.progress_bar.setValue(100 if success else 0)
        self.cancel_btn.setText("Close")
        self.commit_btn.setEnabled(False)

    def reset(self):
        """Resets drawer state."""
        self.progress_bar.hide()
        self.progress_bar.setValue(0)
        self.cancel_btn.setText("Cancel")
        self.commit_btn.setEnabled(True)
        self.console.clear()