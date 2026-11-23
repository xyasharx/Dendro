"""Review dialog for one structured DNF5 daemon transaction plan.

The dialog receives the in-memory ``TransactionPlan`` capability returned by
``Goal.resolve``.  It does not parse a command-line simulation or reconstruct a
transaction from display text.
"""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase, QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.dnf5daemon import TransactionPlan, _signed_size, format_byte_size
from core.i18n import tr
from core.settings import get_app_settings
from ui.flatpak_dialog import _count_label
from ui.fonts import apply_labelled_fonts
from ui.styles import get_delegate_palette


class TransactionPlanReviewDialog(QDialog):
    """Present a daemon-resolved plan and require an explicit execution choice."""

    def __init__(self, plan: TransactionPlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Review DNF5 Transaction Plan"))
        self.setMinimumSize(520, 360)
        self.resize(760, 500)
        self._plan = plan
        self._init_ui()
        apply_labelled_fonts(self)

    def stage_for_next_boot(self) -> bool:
        """Report whether the user chose to apply this plan during boot."""
        return self.stage_checkbox.isChecked()

    def _summary_cell(self, title: str, value, *, widget: bool = False) -> QWidget:
        """One label/value pair in the plan summary strip."""
        cell = QWidget()
        cell_layout = QVBoxLayout(cell)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setSpacing(2)
        caption = QLabel(title)
        caption.setObjectName("SubtleHelpLabel")
        cell_layout.addWidget(caption)
        if widget:
            cell_layout.addWidget(value)
        else:
            text = QLabel(str(value))
            text.setStyleSheet("font-weight: bold;")
            cell_layout.addWidget(text)
        return cell

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        theme_choice = get_app_settings().value("theme", "auto", type=str)
        palette = get_delegate_palette(theme_choice)

        heading = QLabel(tr("DNF5 daemon resolved the following changes"))
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
            title = QLabel(tr("Protected package removal detected"))
            title.setStyleSheet(
                f"color: {palette['badge_fg_missing'].name()}; font-weight: 800;"
            )
            header_layout.addWidget(title, stretch=1)
            warning_layout.addLayout(header_layout)

            packages = ", ".join(
                html.escape(pkg, quote=True) for pkg in self._plan.critical_removals
            )
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
            safe_label = QLabel(
                tr("No protected system package is scheduled for removal.")
            )
            safe_label.setStyleSheet(
                f"color: {palette['badge_fg_installed'].name()}; font-weight: bold;"
            )
            safe_label.setWordWrap(True)
            safe_layout.addWidget(safe_label, stretch=1)
            layout.addLayout(safe_layout)

        summary = QFrame()
        summary.setObjectName("StatsFrame")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(12, 8, 12, 8)
        summary_layout.setSpacing(18)

        active = [item for item in self._plan.items if not item.is_skipped]
        removals = sum(1 for item in active if item.is_removal)
        additions = sum(1 for item in active if not item.is_removal)
        summary_layout.addWidget(
            self._summary_cell("Changes", _count_label(len(active), "package"))
        )
        summary_layout.addWidget(
            self._summary_cell("Install / Remove", f"{additions} / {removals}")
        )
        summary_layout.addWidget(
            self._summary_cell(
                "Download",
                format_byte_size(self._plan.download_size_total_bytes),
            )
        )
        delta = self._plan.install_size_delta_bytes
        disk_label = QLabel("no change" if delta == 0 else _signed_size(delta))
        disk_label.setStyleSheet("font-weight: bold;")
        disk_label.setToolTip(
            tr(
                "Net change in installed size. Removals free the space they report, "
                "so this is the effect on your disk rather than the download size."
            )
        )
        summary_layout.addWidget(self._summary_cell("Disk", disk_label, widget=True))
        summary_layout.addStretch(1)
        layout.addWidget(summary)

        detail_label = QLabel(tr("Resolved package changes"))
        detail_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(detail_label)

        self.plan_text = QTextEdit()
        self.plan_text.setObjectName("TransactionPlanText")
        self.plan_text.setReadOnly(True)
        self.plan_text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.plan_text.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.plan_text.setPlainText(self._plan.preview_text())
        layout.addWidget(self.plan_text, stretch=1)

        # Applying at the next boot is the safer choice for a large update:
        # nothing changes in the running session and the reboot applies the
        # exact plan shown here.
        self.stage_checkbox = QCheckBox(tr("Apply during the next boot instead of now"))
        self.stage_checkbox.setToolTip(
            tr(
                "Download now and install while the system starts, before the "
                "desktop loads. Use this for large updates or when the current "
                "session should not change."
            )
        )
        layout.addWidget(self.stage_checkbox)

        button_layout = QHBoxLayout()
        cancel_button = QPushButton(tr("Discard Plan"))
        cancel_icon = QIcon.fromTheme("process-stop")
        if not cancel_icon.isNull():
            cancel_button.setIcon(cancel_icon)
        cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)

        self.execute_button = QPushButton(tr("Execute and Authenticate"))
        self.execute_button.setObjectName("ApplyButton")
        execute_icon = QIcon.fromTheme("dialog-ok-apply")
        if not execute_icon.isNull():
            self.execute_button.setIcon(execute_icon)
        self.execute_button.setCursor(Qt.CursorShape.PointingHandCursor)
        if self._plan.has_critical_system_removal:
            self.execute_button.setText(tr("Execute Protected Removal"))
            # Keep the destructive action visually distinct from a routine
            # apply; the accent colour is otherwise identical to "Execute and
            # Authenticate" on a harmless plan.
            self.execute_button.setObjectName("DangerButton")
            self.execute_button.setAutoDefault(False)
            cancel_button.setDefault(True)
        self.execute_button.clicked.connect(self.accept)

        button_layout.addWidget(cancel_button)
        button_layout.addStretch(1)
        button_layout.addWidget(self.execute_button)
        layout.addLayout(button_layout)
