"""Inline lifecycle banner and actions for ``org.rpm.dnf.v0.Offline`` updates."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QPoint, QRect, QSignalBlocker, QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dendro.core.dnf5daemon_types import (
    OfflineTransactionPhase,
    OfflineTransactionStatus,
)
from dendro.core.host_environment import HostEnvironment
from dendro.core.i18n import tr
from dendro.core.system_reboot import RebootCapability
from dendro.ui.fonts import apply_labelled_fonts
from dendro.ui.icons import theme_icon


class _ActionFlowLayout(QLayout):
    """Wrap-on-narrow flow layout keeping action controls within narrow tiles."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        h_spacing: int = 8,
        v_spacing: int = 6,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, max(1, width), 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def _visible_items(self) -> list[QLayoutItem]:
        visible: list[QLayoutItem] = []
        for item in self._items:
            widget = item.widget()
            if widget is not None:
                if not widget.isHidden():
                    visible.append(item)
            elif not item.isEmpty():
                visible.append(item)
        return visible

    def sizeHint(self) -> QSize:
        left, top, right, bottom = self.getContentsMargins()
        visible = self._visible_items()
        width = 0
        height = 0
        for idx, item in enumerate(visible):
            hint = item.sizeHint()
            width += hint.width()
            if idx > 0:
                width += self._h_spacing
            height = max(height, hint.height())
        return QSize(width + left + right, height + top + bottom)

    def minimumSize(self) -> QSize:
        left, top, right, bottom = self.getContentsMargins()
        max_w = 0
        max_h = 0
        for item in self._visible_items():
            hint = item.sizeHint()
            max_w = max(max_w, hint.width())
            max_h = max(max_h, hint.height())
        return QSize(max_w + left + right, max_h + top + bottom)

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective = rect.adjusted(left, top, -right, -bottom)
        avail_w = max(1, effective.width())

        x = effective.x()
        y = effective.y()
        row_height = 0

        for item in self._visible_items():
            hint = item.sizeHint()
            item_w = min(hint.width(), avail_w)
            item_h = hint.height()
            if x > effective.x() and x - effective.x() + item_w > avail_w:
                x = effective.x()
                y += row_height + self._v_spacing
                row_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(item_w, item_h)))
            x += item_w + self._h_spacing
            row_height = max(row_height, item_h)

        total_height = (y - effective.y()) + row_height
        return top + total_height + bottom


class OfflineUpdateBanner(QFrame):
    """Actionable banner surfacing ``org.rpm.dnf.v0.Offline`` boot update state."""

    schedule_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    clean_requested = pyqtSignal()
    finish_action_requested = pyqtSignal(str)
    reboot_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("OfflineUpdateBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("offlinePhase", "ready")

        self._status: OfflineTransactionStatus | None = None
        self._environment: HostEnvironment = HostEnvironment.traditional()
        self._reboot_capability: RebootCapability = RebootCapability.UNAVAILABLE
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(6)

        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(10)

        self.icon_label = QLabel()
        self.icon_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )
        info_row.addWidget(self.icon_label)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)

        self.title_label = QLabel()
        self.title_label.setObjectName("OfflineBannerTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        title_row.addWidget(self.title_label)

        self.phase_badge = QLabel()
        self.phase_badge.setObjectName("OfflineBannerBadge")
        self.phase_badge.setTextFormat(Qt.TextFormat.PlainText)
        title_row.addWidget(self.phase_badge)
        title_row.addStretch(1)
        text_col.addLayout(title_row)

        self.detail_label = QLabel()
        self.detail_label.setObjectName("OfflineBannerDetail")
        self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_label.setWordWrap(True)
        text_col.addWidget(self.detail_label)

        self.meta_label = QLabel()
        self.meta_label.setObjectName("OfflineBannerMeta")
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label.setWordWrap(True)
        self.meta_label.setVisible(False)
        text_col.addWidget(self.meta_label)

        info_row.addLayout(text_col, stretch=1)
        layout.addLayout(info_row)

        self.actions_widget = QWidget(self)
        actions_flow = _ActionFlowLayout(self.actions_widget, h_spacing=8, v_spacing=6)
        actions_flow.setContentsMargins(0, 0, 0, 0)

        self.poweroff_after_checkbox = QCheckBox(tr("Power off after update"))
        self.poweroff_after_checkbox.setObjectName("OfflineFinishPoweroffCheck")
        self.poweroff_after_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.poweroff_after_checkbox.clicked.connect(self._on_poweroff_clicked)
        actions_flow.addWidget(self.poweroff_after_checkbox)

        self.schedule_btn = QPushButton(tr("Schedule for Next Boot"))
        self.schedule_btn.setObjectName("ApplyButton")
        self.schedule_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.schedule_btn.clicked.connect(self.schedule_requested.emit)
        actions_flow.addWidget(self.schedule_btn)

        self.reboot_btn = QPushButton(tr("Restart & Apply Update"))
        self.reboot_btn.setObjectName("ApplyButton")
        self.reboot_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reboot_btn.clicked.connect(self.reboot_requested.emit)
        actions_flow.addWidget(self.reboot_btn)

        self.cancel_btn = QPushButton(tr("Cancel Boot Update"))
        self.cancel_btn.setObjectName("HeaderToolBtn")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)
        actions_flow.addWidget(self.cancel_btn)

        self.clean_btn = QPushButton(tr("Clean Staged Cache"))
        self.clean_btn.setObjectName("HeaderToolBtn")
        self.clean_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clean_btn.clicked.connect(self.clean_requested.emit)
        actions_flow.addWidget(self.clean_btn)

        layout.addWidget(self.actions_widget)
        self._apply_button_metadata()
        apply_labelled_fonts(self)
        self.setVisible(False)

    def _on_poweroff_clicked(self, checked: bool) -> None:
        self.finish_action_requested.emit("poweroff" if checked else "reboot")

    def _apply_button_metadata(self) -> None:
        self.poweroff_after_checkbox.setText(tr("Power off after update"))
        self.poweroff_after_checkbox.setToolTip(
            tr(
                "Shut down automatically after applying the staged offline packages "
                "during the next boot instead of restarting normally."
            )
        )
        self.schedule_btn.setText(tr("Schedule for Next Boot"))
        self.schedule_btn.setToolTip(
            tr(
                "Activate the pre-staged offline transaction so systemd applies it "
                "during the next system restart."
            )
        )
        self.reboot_btn.setText(tr("Restart & Apply Update"))
        self.cancel_btn.setText(tr("Cancel Boot Update"))
        self.cancel_btn.setToolTip(
            tr(
                "Unschedule the next-boot update without deleting downloaded offline "
                "packages."
            )
        )
        self.clean_btn.setText(tr("Clean Staged Cache"))
        self.clean_btn.setToolTip(
            tr(
                "Cancel any pending next-boot trigger and remove downloaded offline "
                "packages and transaction metadata."
            )
        )

    def set_busy(self, busy: bool) -> None:
        """Disable mutating offline controls while an operation is in flight."""
        self._busy = bool(busy)
        self._sync_interactive_states()

    def update_status(
        self,
        status: OfflineTransactionStatus | None,
        environment: HostEnvironment,
        reboot_capability: RebootCapability = RebootCapability.UNAVAILABLE,
    ) -> None:
        """Update banner content and controls from daemon and logind status."""
        self._status = status
        self._environment = environment
        self._reboot_capability = reboot_capability
        self._refresh_presentation()

    def retranslate(self) -> None:
        """Refresh labels and tooltips on live locale changes."""
        self._apply_button_metadata()
        self._refresh_presentation()

    def _sync_interactive_states(self) -> None:
        status = self._status
        mutations = self._environment.supports_dnf5_daemon_mutations
        enabled_controls = not self._busy and mutations
        self.poweroff_after_checkbox.setEnabled(enabled_controls)
        self.schedule_btn.setEnabled(enabled_controls)
        self.cancel_btn.setEnabled(enabled_controls)
        self.clean_btn.setEnabled(enabled_controls)

        can_reboot = bool(
            enabled_controls
            and status is not None
            and status.is_scheduled_for_boot
            and self._environment.supports_host_reboot_control
            and self._reboot_capability.can_trigger_immediately
        )
        self.reboot_btn.setEnabled(can_reboot)
        if status is not None and status.is_scheduled_for_boot:
            if self._reboot_capability.is_inhibited:
                self.reboot_btn.setToolTip(
                    tr(
                        "Restart is currently blocked by an active systemd inhibitor "
                        "lock. Close blocking tasks or restart from your desktop menu."
                    )
                )
            elif not self._reboot_capability.can_trigger_immediately:
                self.reboot_btn.setToolTip(
                    tr(
                        "Direct restart via systemd-logind is not permitted in this "
                        "session. Restart from your desktop menu to apply the staged update."
                    )
                )
            elif status.poweroff_after:
                self.reboot_btn.setToolTip(
                    tr(
                        "Restart now to install staged packages at boot, then power off "
                        "automatically when finished."
                    )
                )
            else:
                self.reboot_btn.setToolTip(
                    tr(
                        "Restart now to install staged packages at boot and return to your "
                        "system automatically."
                    )
                )

    def _set_phase_property(self, value: str) -> None:
        if self.property("offlinePhase") != value:
            self.setProperty("offlinePhase", value)
            self.style().unpolish(self)
            self.style().polish(self)

    def _refresh_presentation(self) -> None:
        status = self._status
        env = self._environment
        if (
            not env.supports_dnf5_daemon_queries
            or status is None
            or not status.has_state
        ):
            self.title_label.clear()
            self.phase_badge.clear()
            self.detail_label.clear()
            self.meta_label.clear()
            self.meta_label.setVisible(False)
            self.actions_widget.setVisible(False)
            self.setVisible(False)
            return

        if status.is_scheduled_for_boot:
            phase_prop = "ready"
            icon = theme_icon("system-reboot", "software-update-available")
            self.title_label.setText(tr("Boot Update Scheduled for Next Restart"))
            self.phase_badge.setText(tr("SCHEDULED"))
            detail_parts = [
                tr(
                    "Staged packages will be installed automatically during the next "
                    "system restart."
                )
            ]
        elif status.is_pre_staged:
            phase_prop = "download-complete"
            icon = theme_icon("software-update-available", "folder-download")
            self.title_label.setText(tr("Offline Update Pre-Staged"))
            self.phase_badge.setText(tr("PRE-STAGED"))
            detail_parts = [
                tr(
                    "Packages are downloaded and verified, but not yet scheduled for "
                    "the next boot."
                )
            ]
        elif status.phase is OfflineTransactionPhase.TRANSACTION_INCOMPLETE:
            phase_prop = "warning"
            icon = theme_icon("dialog-warning", "dialog-error")
            self.title_label.setText(tr("Previous Boot Update Did Not Complete"))
            self.phase_badge.setText(tr("INCOMPLETE"))
            detail_parts = [
                tr(
                    "The previous offline boot transaction did not complete cleanly. "
                    "Clean the staged cache before scheduling another update."
                )
            ]
        elif status.phase is OfflineTransactionPhase.DOWNLOAD_INCOMPLETE:
            phase_prop = "warning"
            icon = theme_icon("dialog-warning", "dialog-information")
            self.title_label.setText(tr("Incomplete Offline Update Download"))
            self.phase_badge.setText(tr("INCOMPLETE"))
            detail_parts = [
                tr(
                    "An earlier offline package download was interrupted. Clean the "
                    "staged cache before trying again."
                )
            ]
        else:
            phase_prop = "warning"
            icon = theme_icon("dialog-information", "software-update-available")
            self.title_label.setText(tr("Offline Update State Detected"))
            self.phase_badge.setText(tr("STAGED"))
            detail_parts = [
                tr("DNF5 reports an offline transaction state ({status}).").format(
                    status=status.status or status.phase.value
                )
            ]

        if status.is_scheduled_for_boot:
            if env.is_flatpak:
                detail_parts.append(
                    tr("Restart from your desktop menu to apply this staged update.")
                )
            elif (
                env.supports_host_reboot_control
                and self._reboot_capability.is_inhibited
            ):
                detail_parts.append(
                    tr(
                        "Automatic restart is currently blocked by an active system "
                        "inhibitor."
                    )
                )

        self._set_phase_property(phase_prop)
        if not icon.isNull():
            self.icon_label.setPixmap(icon.pixmap(18, 18))
            self.icon_label.setVisible(True)
        else:
            self.icon_label.setVisible(False)
        self.detail_label.setText(" ".join(detail_parts))

        meta_parts: list[str] = []
        if status.cmd_line:
            meta_parts.append(tr("Command: {command}").format(command=status.cmd_line))
        elif status.verb:
            meta_parts.append(tr("Action: {verb}").format(verb=status.verb))

        if (
            status.target_releasever
            and status.system_releasever
            and status.target_releasever != status.system_releasever
        ):
            meta_parts.append(
                tr("Release: Fedora {source} -> {target}").format(
                    source=status.system_releasever,
                    target=status.target_releasever,
                )
            )
        elif status.target_releasever:
            meta_parts.append(
                tr("Target release: Fedora {target}").format(
                    target=status.target_releasever
                )
            )

        if status.is_scheduled_for_boot or status.is_pre_staged:
            meta_parts.append(
                tr("After boot update: Power off")
                if status.poweroff_after
                else tr("After boot update: Restart")
            )
        if status.cachedir:
            meta_parts.append(tr("Cache: {path}").format(path=status.cachedir))

        if meta_parts:
            self.meta_label.setText("  |  ".join(meta_parts))
            self.meta_label.setVisible(True)
        else:
            self.meta_label.clear()
            self.meta_label.setVisible(False)

        mutations = env.supports_dnf5_daemon_mutations
        can_adjust_finish = mutations and (
            status.is_scheduled_for_boot or status.is_pre_staged
        )
        with QSignalBlocker(self.poweroff_after_checkbox):
            self.poweroff_after_checkbox.setChecked(status.poweroff_after)
        self.poweroff_after_checkbox.setVisible(can_adjust_finish)
        self.schedule_btn.setVisible(mutations and status.is_pre_staged)
        self.cancel_btn.setVisible(mutations and status.is_scheduled_for_boot)
        self.clean_btn.setVisible(mutations and status.has_state)
        self.reboot_btn.setVisible(
            mutations
            and status.is_scheduled_for_boot
            and env.supports_host_reboot_control
        )
        self.actions_widget.setVisible(mutations)
        self._sync_interactive_states()
        self.setVisible(True)
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
        self.updateGeometry()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.LayoutDirectionChange,
        ):
            layout = self.layout()
            if layout is not None:
                layout.invalidate()
            self.updateGeometry()


__all__ = ("OfflineUpdateBanner",)
