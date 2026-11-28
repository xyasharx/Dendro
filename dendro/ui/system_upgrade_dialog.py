# dendro/ui/system_upgrade_dialog.py
"""Two-step wizard for Fedora major system release upgrades via DNF5 daemon."""

from __future__ import annotations

from PyQt6.QtCore import QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from dendro.core.dnf5daemon_types import TransactionRequest
from dendro.core.fedora_releases import (
    CurrentFedoraRelease,
    FedoraReleaseDiscoveryResult,
    FedoraTargetRelease,
    build_local_fallback_targets,
    validate_target_releasever,
)
from dendro.core.i18n import tr
from dendro.ui.fonts import apply_labelled_fonts
from dendro.ui.icons import theme_icon


class SystemReleaseUpgradeDialog(QDialog):
    """Guided wizard collecting target releasever and DNF5 upgrade mode."""

    refresh_releases_requested = pyqtSignal()

    def __init__(
        self,
        current_release: CurrentFedoraRelease,
        *,
        initial_discovery: FedoraReleaseDiscoveryResult | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Fedora System Release Upgrade"))
        self.setMinimumSize(560, 480)
        self.resize(640, 560)

        self._current_release = current_release
        self._targets: tuple[FedoraTargetRelease, ...] = ()
        self._discovery_notice = ""
        self._validated_releasever = ""
        self._validation_error = ""
        self._validation_warning = ""
        self._user_edited_target = False

        self._init_ui()
        apply_labelled_fonts(self)

        if initial_discovery is not None:
            self.set_discovery_result(initial_discovery)
        else:
            self.set_discovery_loading()

    def current_step(self) -> int:
        """Return the active 0-based wizard page index."""
        return self.pages.currentIndex()

    def selected_request(self) -> TransactionRequest:
        """Return a validated offline system-upgrade ``TransactionRequest``."""
        target = self._validated_releasever
        if not target:
            normalized, error, _ = validate_target_releasever(
                self.target_input.text(), self._current_release
            )
            if error or not normalized:
                raise ValueError(error or tr("Target Fedora release is required."))
            target = normalized
        mode = "upgrade" if self.upgrade_radio.isChecked() else "distrosync"
        return TransactionRequest(
            system_upgrade_releasever=target,
            system_upgrade_mode=mode,
            allow_erasing=self.allow_erasing_checkbox.isChecked(),
        )

    def set_discovery_loading(self) -> None:
        """Show deterministic local candidates while querying Fedora metadata."""
        self.refresh_btn.setEnabled(False)
        self.discovery_status_label.setText(
            tr("Checking available Fedora releases online…")
        )
        fallback = build_local_fallback_targets(self._current_release)
        self._populate_targets(fallback, preserve_user_input=self._user_edited_target)

    def set_discovery_result(self, result: FedoraReleaseDiscoveryResult) -> None:
        """Apply discovered target releases and status without clobbering manual input."""
        self.refresh_btn.setEnabled(True)
        self._discovery_notice = result.notice
        self.discovery_status_label.setText(
            result.notice or tr("Select the target Fedora release below.")
        )
        self._populate_targets(
            result.targets, preserve_user_input=self._user_edited_target
        )

    def _populate_targets(
        self,
        targets: tuple[FedoraTargetRelease, ...],
        *,
        preserve_user_input: bool,
    ) -> None:
        self._targets = targets
        existing_text = self.target_input.text().strip()

        with QSignalBlocker(self.target_list):
            self.target_list.clear()
            for target in targets:
                text = f"{target.label}  —  {target.status_badge}"
                if target.eol:
                    text = f"{text} ({tr('EOL {date}').format(date=target.eol)})"
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, target)
                item.setToolTip(target.detail)
                self.target_list.addItem(item)

        self.target_list.setEnabled(bool(targets))

        chosen_row = -1
        if preserve_user_input and existing_text:
            for row, target in enumerate(targets):
                if target.releasever.casefold() == existing_text.casefold():
                    chosen_row = row
                    break
        elif targets:
            chosen_row = next(
                (idx for idx, target in enumerate(targets) if not target.is_prerelease),
                0,
            )

        if chosen_row >= 0:
            with QSignalBlocker(self.target_list):
                self.target_list.setCurrentRow(chosen_row)
            selected = targets[chosen_row]
            if not preserve_user_input or not existing_text:
                with QSignalBlocker(self.target_input):
                    self.target_input.setText(selected.releasever)

        self._validate_and_refresh()

    def _selected_target_metadata(self, releasever: str) -> FedoraTargetRelease | None:
        key = releasever.strip().casefold()
        for target in self._targets:
            if target.releasever.casefold() == key:
                return target
        return None

    def _on_target_row_changed(self, row: int) -> None:
        if 0 <= row < len(self._targets):
            target = self._targets[row]
            self._user_edited_target = False
            with QSignalBlocker(self.target_input):
                self.target_input.setText(target.releasever)
        self._validate_and_refresh()

    def _on_target_input_edited(self, text: str) -> None:
        self._user_edited_target = bool(text.strip())
        normalized = text.strip().casefold()
        with QSignalBlocker(self.target_list):
            matched = -1
            for row, target in enumerate(self._targets):
                if target.releasever.casefold() == normalized:
                    matched = row
                    break
            self.target_list.setCurrentRow(matched)
        self._validate_and_refresh()

    def _set_notice(self, card: QFrame, label: QLabel, text: str, tone: str) -> None:
        card.setVisible(bool(text))
        label.setText(text)
        if card.property("noticeTone") != tone:
            card.setProperty("noticeTone", tone)
            card.style().unpolish(card)
            card.style().polish(card)

    def _validate_and_refresh(self) -> None:
        raw_target = self.target_input.text()
        normalized, error, warning = validate_target_releasever(
            raw_target, self._current_release
        )
        self._validated_releasever = "" if error else normalized
        self._validation_error = error
        self._validation_warning = warning

        meta = self._selected_target_metadata(normalized or raw_target)
        if error:
            self._set_notice(
                self.step1_notice_card, self.step1_notice_label, error, "error"
            )
            self.next_button.setEnabled(False)
            self.resolve_button.setEnabled(False)
            return

        if warning:
            notice_text = warning
            tone = "warn"
        elif meta is not None and meta.is_prerelease:
            notice_text = tr(
                "Fedora {target} is currently listed as {badge}. Pre-release upgrades "
                "may encounter changing packages or mirror sync delays."
            ).format(target=meta.releasever, badge=meta.status_badge)
            tone = "warn"
        elif meta is not None:
            notice_text = meta.detail
            tone = "info"
        else:
            notice_text = tr(
                "Custom target releasever '{target}' will be passed to DNF5 daemon."
            ).format(target=normalized)
            tone = "info"

        self._set_notice(
            self.step1_notice_card, self.step1_notice_label, notice_text, tone
        )
        self.next_button.setEnabled(True)
        self.resolve_button.setEnabled(True)

    def _go_to_step(self, step_index: int) -> None:
        if step_index == 1 and not self._validated_releasever:
            return
        self.pages.setCurrentIndex(step_index)
        on_step2 = step_index == 1
        self.step_badge.setText(tr("Step 2 of 2") if on_step2 else tr("Step 1 of 2"))
        self.back_button.setVisible(on_step2)
        self.next_button.setVisible(not on_step2)
        self.resolve_button.setVisible(on_step2)

        if on_step2:
            target = self._validated_releasever
            self.step2_summary_label.setText(
                tr("Upgrading from {current} to Fedora {target}").format(
                    current=self._current_release.display_label,
                    target=target,
                )
            )
            meta = self._selected_target_metadata(target)
            if self._validation_warning:
                self._set_notice(
                    self.step2_warning_card,
                    self.step2_warning_label,
                    self._validation_warning,
                    "warn",
                )
            elif meta is not None and meta.is_prerelease:
                self._set_notice(
                    self.step2_warning_card,
                    self.step2_warning_label,
                    tr(
                        "Fedora {target} is marked {badge}. Review the resolved package "
                        "changes carefully before staging."
                    ).format(target=target, badge=meta.status_badge),
                    "warn",
                )
            else:
                self._set_notice(
                    self.step2_warning_card, self.step2_warning_label, "", "info"
                )
            self.resolve_button.setDefault(True)
            self.distrosync_radio.setFocus(Qt.FocusReason.TabFocusReason)
        else:
            self.next_button.setDefault(True)
            self.target_list.setFocus(Qt.FocusReason.TabFocusReason)

    def _on_resolve_clicked(self) -> None:
        if not self._validated_releasever:
            self._go_to_step(0)
            return
        self.accept()

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(10)

        title_label = QLabel(tr("Fedora System Release Upgrade"))
        title_label.setObjectName("DialogTitleLabel")
        title_label.setTextFormat(Qt.TextFormat.PlainText)
        title_row.addWidget(title_label, stretch=1)

        self.step_badge = QLabel(tr("Step 1 of 2"))
        self.step_badge.setObjectName("SystemUpgradeStepBadge")
        self.step_badge.setTextFormat(Qt.TextFormat.PlainText)
        title_row.addWidget(self.step_badge)
        root.addLayout(title_row)

        current_card = QFrame()
        current_card.setObjectName("StatsFrame")
        current_layout = QHBoxLayout(current_card)
        current_layout.setContentsMargins(14, 10, 14, 10)
        current_layout.setSpacing(12)

        current_text_col = QVBoxLayout()
        current_text_col.setContentsMargins(0, 0, 0, 0)
        current_text_col.setSpacing(3)

        self.current_release_label = QLabel(
            tr("Current system: {release}").format(
                release=self._current_release.display_label
            )
        )
        self.current_release_label.setObjectName("SystemUpgradeCurrentTitle")
        self.current_release_label.setTextFormat(Qt.TextFormat.PlainText)
        self.current_release_label.setWordWrap(True)
        current_text_col.addWidget(self.current_release_label)

        self.discovery_status_label = QLabel()
        self.discovery_status_label.setObjectName("SubtleHelpLabel")
        self.discovery_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.discovery_status_label.setWordWrap(True)
        current_text_col.addWidget(self.discovery_status_label)
        current_layout.addLayout(current_text_col, stretch=1)

        self.refresh_btn = QPushButton(tr("Refresh"))
        refresh_icon = theme_icon("view-refresh")
        if not refresh_icon.isNull():
            self.refresh_btn.setIcon(refresh_icon)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setToolTip(
            tr("Reload available Fedora releases from upstream")
        )
        self.refresh_btn.clicked.connect(self.refresh_releases_requested.emit)
        current_layout.addWidget(self.refresh_btn)
        root.addWidget(current_card)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, stretch=1)

        # Page 0: Target release selection
        page1 = QWidget()
        page1_layout = QVBoxLayout(page1)
        page1_layout.setContentsMargins(0, 0, 0, 0)
        page1_layout.setSpacing(10)

        target_heading = QLabel(tr("Choose target Fedora release"))
        target_heading.setObjectName("SectionHeadingLabel")
        target_heading.setTextFormat(Qt.TextFormat.PlainText)
        page1_layout.addWidget(target_heading)

        self.target_list = QListWidget()
        self.target_list.setObjectName("SystemUpgradeTargetList")
        self.target_list.setAccessibleName(tr("Available Fedora upgrade targets"))
        self.target_list.currentRowChanged.connect(self._on_target_row_changed)
        page1_layout.addWidget(self.target_list, stretch=1)

        manual_row = QHBoxLayout()
        manual_row.setContentsMargins(0, 0, 0, 0)
        manual_row.setSpacing(10)

        manual_label = QLabel(tr("Target release version ($releasever):"))
        manual_label.setTextFormat(Qt.TextFormat.PlainText)
        manual_row.addWidget(manual_label)

        self.target_input = QLineEdit()
        self.target_input.setAccessibleName(tr("Target Fedora release version"))
        self.target_input.setPlaceholderText(tr("e.g. 43 or rawhide"))
        self.target_input.textEdited.connect(self._on_target_input_edited)
        manual_row.addWidget(self.target_input, stretch=1)
        page1_layout.addLayout(manual_row)

        self.step1_notice_card = QFrame()
        self.step1_notice_card.setObjectName("SystemUpgradeNoticeCard")
        self.step1_notice_card.setProperty("noticeTone", "info")
        s1_notice_layout = QVBoxLayout(self.step1_notice_card)
        s1_notice_layout.setContentsMargins(12, 8, 12, 8)
        self.step1_notice_label = QLabel()
        self.step1_notice_label.setObjectName("SystemUpgradeNoticeText")
        self.step1_notice_label.setTextFormat(Qt.TextFormat.PlainText)
        self.step1_notice_label.setWordWrap(True)
        s1_notice_layout.addWidget(self.step1_notice_label)
        page1_layout.addWidget(self.step1_notice_card)

        self.pages.addWidget(page1)

        # Page 1: Upgrade options & offline boot safety
        page2 = QWidget()
        page2_layout = QVBoxLayout(page2)
        page2_layout.setContentsMargins(0, 0, 0, 0)
        page2_layout.setSpacing(10)

        self.step2_summary_label = QLabel()
        self.step2_summary_label.setObjectName("SectionHeadingLabel")
        self.step2_summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.step2_summary_label.setWordWrap(True)
        page2_layout.addWidget(self.step2_summary_label)

        mode_card = QFrame()
        mode_card.setObjectName("StatsFrame")
        mode_layout = QVBoxLayout(mode_card)
        mode_layout.setContentsMargins(14, 12, 14, 12)
        mode_layout.setSpacing(8)

        mode_heading = QLabel(tr("Upgrade resolution mode"))
        mode_heading.setObjectName("SectionHeadingLabel")
        mode_heading.setTextFormat(Qt.TextFormat.PlainText)
        mode_layout.addWidget(mode_heading)

        self.mode_group = QButtonGroup(self)
        self.distrosync_radio = QRadioButton(
            tr("Synchronize distribution packages (distrosync, recommended)")
        )
        self.distrosync_radio.setChecked(True)
        self.mode_group.addButton(self.distrosync_radio)
        mode_layout.addWidget(self.distrosync_radio)

        distrosync_help = QLabel(
            tr(
                "Aligns installed packages to the versions shipped in the target Fedora "
                "repositories, permitting downgrades where needed."
            )
        )
        distrosync_help.setObjectName("SubtleHelpLabel")
        distrosync_help.setTextFormat(Qt.TextFormat.PlainText)
        distrosync_help.setWordWrap(True)
        mode_layout.addWidget(distrosync_help)

        self.upgrade_radio = QRadioButton(tr("Upgrade packages only (upgrade)"))
        self.mode_group.addButton(self.upgrade_radio)
        mode_layout.addWidget(self.upgrade_radio)

        upgrade_help = QLabel(
            tr(
                "Upgrades installed packages toward the target Fedora release without "
                "downgrading existing packages."
            )
        )
        upgrade_help.setObjectName("SubtleHelpLabel")
        upgrade_help.setTextFormat(Qt.TextFormat.PlainText)
        upgrade_help.setWordWrap(True)
        mode_layout.addWidget(upgrade_help)
        page2_layout.addWidget(mode_card)

        conflict_card = QFrame()
        conflict_card.setObjectName("StatsFrame")
        conflict_layout = QVBoxLayout(conflict_card)
        conflict_layout.setContentsMargins(14, 10, 14, 10)
        conflict_layout.setSpacing(4)

        self.allow_erasing_checkbox = QCheckBox(
            tr("Allow erasing conflicting packages if required (--allowerasing)")
        )
        conflict_layout.addWidget(self.allow_erasing_checkbox)

        conflict_help = QLabel(
            tr(
                "Allows DNF5 to remove obsolete or conflicting third-party packages if "
                "dependency resolution fails without erasure."
            )
        )
        conflict_help.setObjectName("SubtleHelpLabel")
        conflict_help.setTextFormat(Qt.TextFormat.PlainText)
        conflict_help.setWordWrap(True)
        conflict_layout.addWidget(conflict_help)
        page2_layout.addWidget(conflict_card)

        self.step2_warning_card = QFrame()
        self.step2_warning_card.setObjectName("SystemUpgradeNoticeCard")
        self.step2_warning_card.setProperty("noticeTone", "warn")
        s2_warn_layout = QVBoxLayout(self.step2_warning_card)
        s2_warn_layout.setContentsMargins(12, 8, 12, 8)
        self.step2_warning_label = QLabel()
        self.step2_warning_label.setObjectName("SystemUpgradeNoticeText")
        self.step2_warning_label.setTextFormat(Qt.TextFormat.PlainText)
        self.step2_warning_label.setWordWrap(True)
        s2_warn_layout.addWidget(self.step2_warning_label)
        self.step2_warning_card.setVisible(False)
        page2_layout.addWidget(self.step2_warning_card)

        safety_card = QFrame()
        safety_card.setObjectName("SystemUpgradeNoticeCard")
        safety_card.setProperty("noticeTone", "info")
        safety_layout = QVBoxLayout(safety_card)
        safety_layout.setContentsMargins(12, 8, 12, 8)
        safety_label = QLabel(
            tr(
                "Dendro resolves the full plan first, prompts before importing any new "
                "Fedora release GPG keys, downloads all packages cleanly, and stages the "
                "system upgrade strictly for your next boot."
            )
        )
        safety_label.setObjectName("SystemUpgradeNoticeText")
        safety_label.setTextFormat(Qt.TextFormat.PlainText)
        safety_label.setWordWrap(True)
        safety_layout.addWidget(safety_label)
        page2_layout.addWidget(safety_card)
        page2_layout.addStretch(1)

        self.pages.addWidget(page2)

        # Footer wizard buttons
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(8)

        self.cancel_button = QPushButton(tr("Cancel"))
        cancel_icon = theme_icon("process-stop")
        if not cancel_icon.isNull():
            self.cancel_button.setIcon(cancel_icon)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_button)

        btn_row.addStretch(1)

        self.back_button = QPushButton(tr("Back"))
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.clicked.connect(lambda: self._go_to_step(0))
        self.back_button.setVisible(False)
        btn_row.addWidget(self.back_button)

        self.next_button = QPushButton(tr("Next"))
        self.next_button.setObjectName("ApplyButton")
        self.next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_button.clicked.connect(lambda: self._go_to_step(1))
        self.next_button.setDefault(True)
        btn_row.addWidget(self.next_button)

        self.resolve_button = QPushButton(tr("Resolve Upgrade Plan"))
        self.resolve_button.setObjectName("ApplyButton")
        ok_icon = theme_icon("dialog-ok-apply")
        if not ok_icon.isNull():
            self.resolve_button.setIcon(ok_icon)
        self.resolve_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resolve_button.clicked.connect(self._on_resolve_clicked)
        self.resolve_button.setVisible(False)
        btn_row.addWidget(self.resolve_button)

        root.addLayout(btn_row)


__all__ = ("SystemReleaseUpgradeDialog",)
