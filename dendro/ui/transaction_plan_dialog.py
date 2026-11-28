"""Review dialog for one structured DNF5 daemon transaction plan.

The dialog receives the in-memory ``TransactionPlan`` capability returned by
``Goal.resolve``.  It does not parse a command-line simulation or reconstruct a
transaction from display text.
"""

from __future__ import annotations

import html

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from dendro.core.dnf5daemon import TransactionPlan, _signed_size, format_byte_size
from dendro.core.i18n import format_number, tr, tr_plural
from dendro.ui.fonts import apply_labelled_fonts, scaled_fixed_font


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
        """Report whether the user chose to apply or pre-stage this plan offline."""
        return self.stage_checkbox.isChecked()

    def stage_download_only(self) -> bool:
        """Report whether offline staging should only pre-download packages."""
        return (
            self.stage_checkbox.isChecked() and self.download_only_checkbox.isChecked()
        )

    def stage_finish_action(self) -> str:
        """Return ``'reboot'`` or ``'poweroff'`` for offline boot completion."""
        if not self.stage_checkbox.isChecked():
            return "reboot"
        return str(self.finish_action_combo.currentData() or "reboot")

    def _sync_offline_controls(self) -> None:
        staged = self.stage_checkbox.isChecked()
        self.offline_options_frame.setVisible(staged)
        if not self._plan.has_critical_system_removal:
            if not staged:
                self.execute_button.setText(tr("Execute and Authenticate"))
            elif self.download_only_checkbox.isChecked():
                self.execute_button.setText(tr("Download and Pre-Stage"))
            else:
                self.execute_button.setText(tr("Stage for Next Boot"))

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
            text.setObjectName("StatFieldValue")
            cell_layout.addWidget(text)
        return cell

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        heading = QLabel(tr("DNF5 daemon resolved the following changes"))
        heading.setObjectName("DialogTitleLabel")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        if self._plan.has_critical_system_removal:
            warning_frame = QFrame()
            warning_frame.setObjectName("PlanWarningCard")
            warning_layout = QVBoxLayout(warning_frame)
            warning_layout.setContentsMargins(14, 12, 14, 12)
            warning_layout.setSpacing(6)
            header_layout = QHBoxLayout()
            icon_label = QLabel()
            warning_icon = QIcon.fromTheme("dialog-warning")
            if not warning_icon.isNull():
                icon_label.setPixmap(warning_icon.pixmap(22, 22))
                header_layout.addWidget(icon_label)
            title = QLabel(tr("Protected package removal detected"))
            title.setObjectName("PlanWarningTitle")
            header_layout.addWidget(title, stretch=1)
            warning_layout.addLayout(header_layout)

            packages = ", ".join(
                html.escape(pkg, quote=True) for pkg in self._plan.critical_removals
            )
            description = QLabel(
                tr(
                    "The daemon-resolved plan removes protected packages: {packages}. "
                    "Review every package change before continuing; this may leave the "
                    "system unbootable or without a graphical session."
                ).format(packages=f"<b>{packages}</b>")
            )
            description.setTextFormat(Qt.TextFormat.RichText)
            description.setWordWrap(True)
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
            safe_label.setObjectName("PlanSafeLabel")
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
            self._summary_cell(
                tr("Changes"),
                tr_plural("{count} package", "{count} packages", len(active)).format(
                    count=format_number(len(active))
                ),
            )
        )
        summary_layout.addWidget(
            self._summary_cell(
                tr("Install / Remove"),
                f"{format_number(additions)} / {format_number(removals)}",
            )
        )
        summary_layout.addWidget(
            self._summary_cell(
                tr("Download"),
                format_byte_size(self._plan.download_size_total_bytes),
            )
        )
        delta = self._plan.install_size_delta_bytes
        disk_label = QLabel(tr("no change") if delta == 0 else _signed_size(delta))
        disk_label.setObjectName("StatFieldValue")
        disk_label.setToolTip(
            tr(
                "Net change in installed size. Removals free the space they report, "
                "so this is the effect on your disk rather than the download size."
            )
        )
        summary_layout.addWidget(
            self._summary_cell(tr("Disk"), disk_label, widget=True)
        )
        summary_layout.addStretch(1)
        layout.addWidget(summary)

        detail_label = QLabel(tr("Resolved package changes"))
        detail_label.setObjectName("SectionHeadingLabel")
        layout.addWidget(detail_label)

        self.plan_text = QTextEdit()
        self.plan_text.setObjectName("TransactionPlanText")
        self.plan_text.setAccessibleName(tr("Resolved transaction plan"))
        self.plan_text.setReadOnly(True)
        self.plan_text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.plan_text.setFont(scaled_fixed_font())
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

        self.offline_options_frame = QFrame()
        self.offline_options_frame.setObjectName("StatsFrame")
        offline_layout = QVBoxLayout(self.offline_options_frame)
        offline_layout.setContentsMargins(12, 8, 12, 8)
        offline_layout.setSpacing(8)

        self.download_only_checkbox = QCheckBox(
            tr("Download packages only (pre-stage without scheduling next boot)")
        )
        self.download_only_checkbox.setToolTip(
            tr(
                "Download packages and run the offline test transaction without "
                "arming the next-boot trigger yet. You can schedule or clean it "
                "later from Dendro."
            )
        )
        offline_layout.addWidget(self.download_only_checkbox)

        finish_row = QHBoxLayout()
        finish_row.setContentsMargins(0, 0, 0, 0)
        finish_row.setSpacing(8)
        self.finish_action_label = QLabel(tr("After boot update completes:"))
        self.finish_action_label.setTextFormat(Qt.TextFormat.PlainText)
        finish_row.addWidget(self.finish_action_label)

        self.finish_action_combo = QComboBox()
        self.finish_action_combo.setAccessibleName(tr("After boot update completes"))
        self.finish_action_combo.addItem(tr("Restart system"), "reboot")
        self.finish_action_combo.addItem(tr("Power off system"), "poweroff")
        self.finish_action_combo.setToolTip(
            tr(
                "Choose whether the computer restarts normally or shuts down "
                "after installing offline packages at boot."
            )
        )
        finish_row.addWidget(self.finish_action_combo)
        finish_row.addStretch(1)
        offline_layout.addLayout(finish_row)

        self.offline_options_frame.setVisible(False)
        layout.addWidget(self.offline_options_frame)

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

        self.stage_checkbox.toggled.connect(self._sync_offline_controls)
        self.download_only_checkbox.toggled.connect(self._sync_offline_controls)
        self._sync_offline_controls()

        self.setTabOrder(self.plan_text, self.stage_checkbox)
        self.setTabOrder(self.stage_checkbox, self.download_only_checkbox)
        self.setTabOrder(self.download_only_checkbox, self.finish_action_combo)
        self.setTabOrder(self.finish_action_combo, cancel_button)
        self.setTabOrder(cancel_button, self.execute_button)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "plan_text"):
            self.plan_text.setFont(scaled_fixed_font())
