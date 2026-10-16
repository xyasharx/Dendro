"""Review dialog for one structured DNF5 daemon transaction plan.

The dialog receives the in-memory ``TransactionPlan`` capability returned by
``Goal.resolve``.  It does not parse a command-line simulation or reconstruct a
transaction from display text.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QFontDatabase, QIcon
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.dnf5daemon import TransactionPlan
from ui.styles import get_delegate_palette


class TransactionPlanReviewDialog(QDialog):
    """Present a daemon-resolved plan and require an explicit execution choice."""

    def __init__(self, plan: TransactionPlan, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review DNF5 Transaction Plan")
        self.setMinimumSize(520, 360)
        self.resize(760, 500)
        self._plan = plan
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        theme_choice = QSettings("FedoraCommunity", "Dendro").value("theme", "auto", type=str)
        palette = get_delegate_palette(theme_choice)

        heading = QLabel("DNF5 daemon resolved the following changes")
        heading.setStyleSheet("font-weight: bold;")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        if self._plan.has_critical_system_removal:
            warning_frame = QFrame()
            warning_frame.setStyleSheet(
                "QFrame {"
                f"background-color: {palette['badge_bg_missing'].name()};"
                f"border: 2px solid {palette['badge_border_missing'].name()};"
                "border-radius: 8px; padding: 10px;"
                "}"
            )
            warning_layout = QVBoxLayout(warning_frame)
            header_layout = QHBoxLayout()
            icon_label = QLabel()
            warning_icon = QIcon.fromTheme("dialog-warning")
            if not warning_icon.isNull():
                icon_label.setPixmap(warning_icon.pixmap(22, 22))
                header_layout.addWidget(icon_label)
            title = QLabel("Protected package removal detected")
            title.setStyleSheet(f"color: {palette['badge_fg_missing'].name()}; font-weight: 800;")
            header_layout.addWidget(title, stretch=1)
            warning_layout.addLayout(header_layout)

            packages = ", ".join(self._plan.critical_removals)
            description = QLabel(
                "The daemon-resolved plan removes protected packages: "
                f"<b>{packages}</b>. Review every package change before continuing; "
                "this may leave the system unbootable or without a graphical session."
            )
            description.setTextFormat(Qt.TextFormat.RichText)
            description.setWordWrap(True)
            description.setStyleSheet(f"color: {palette['text_main'].name()};")
            warning_layout.addWidget(description)
            layout.addWidget(warning_frame)
        else:
            safe_layout = QHBoxLayout()
            safe_icon = QLabel()
            icon = QIcon.fromTheme("emblem-ok-symbolic")
            if not icon.isNull():
                safe_icon.setPixmap(icon.pixmap(20, 20))
                safe_layout.addWidget(safe_icon)
            safe_label = QLabel("No protected system package is scheduled for removal.")
            safe_label.setStyleSheet(f"color: {palette['badge_fg_installed'].name()}; font-weight: bold;")
            safe_label.setWordWrap(True)
            safe_layout.addWidget(safe_label, stretch=1)
            layout.addLayout(safe_layout)

        detail_label = QLabel("Resolved package changes")
        detail_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(detail_label)

        self.plan_text = QTextEdit()
        self.plan_text.setObjectName("TransactionPlanText")
        self.plan_text.setReadOnly(True)
        self.plan_text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.plan_text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.plan_text.setPlainText(self._plan.preview_text())
        layout.addWidget(self.plan_text, stretch=1)

        button_layout = QHBoxLayout()
        cancel_button = QPushButton("Discard Plan")
        cancel_icon = QIcon.fromTheme("process-stop")
        if not cancel_icon.isNull():
            cancel_button.setIcon(cancel_icon)
        cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)

        self.execute_button = QPushButton("Execute and Authenticate")
        self.execute_button.setObjectName("ApplyButton")
        execute_icon = QIcon.fromTheme("dialog-ok-apply")
        if not execute_icon.isNull():
            self.execute_button.setIcon(execute_icon)
        self.execute_button.setCursor(Qt.CursorShape.PointingHandCursor)
        if self._plan.has_critical_system_removal:
            self.execute_button.setText("Execute Protected Removal")
        self.execute_button.clicked.connect(self.accept)

        button_layout.addWidget(cancel_button)
        button_layout.addStretch(1)
        button_layout.addWidget(self.execute_button)
        layout.addLayout(button_layout)
