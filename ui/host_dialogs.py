# dendro/ui/host_dialogs.py
"""Host-environment notices and standalone local RPM inspection dialog."""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.backend import LocalRpmInspectionResult
from core.host_environment import HostEnvironment
from core.i18n import tr
from core.taxonomy import category_label, classification_evidence_label
from ui.fonts import apply_labelled_fonts
from ui.icons import theme_icon
from ui.window_presentation import format_byte_size as _format_byte_size


def _field_name(text: str) -> QLabel:
    """Return a form field name label.

    Field names used to be written as ``<b>Name:</b>`` at each call site, which
    lost them twice over: the extractor treats markup as non-prose, so they
    never reached a catalogue, and translators were handed a tag to reproduce.
    Plain text plus a stylesheet id keeps the emphasis and the message apart.
    """

    label = QLabel(text)
    label.setObjectName("LocalRpmFieldName")
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class LocalRpmInstallDialog(QDialog):
    """
    Dedicated dialog for inspecting and installing a local standalone .rpm file.
    Shows package metadata, version comparison if already installed, and dependency requirements.
    """

    install_requested = pyqtSignal(str)

    def __init__(
        self,
        result: LocalRpmInspectionResult,
        parent: QWidget | None = None,
        *,
        host_environment: HostEnvironment | None = None,
    ):
        super().__init__(parent)
        self.inspection_result = result
        self._host_environment = (
            host_environment
            if host_environment is not None
            else HostEnvironment.traditional()
        )
        self._refresh_window_title()
        self.resize(620, 400)
        self._init_ui()
        apply_labelled_fonts(self)

    def _refresh_window_title(self) -> None:
        """Write this dialog's title for the current language and capability.

        The dialog is modal, so it is always built under the locale in force and
        the catalogue sweep cannot reach it while it is open: there is nothing to
        re-run. The method exists so the caption is chosen once -- the
        capability test and the message id are easy to get out of step, and its
        two captions are the difference between "install" and "inspect".
        """

        caption = (
            tr("Install Package")
            if self._host_environment.supports_dnf5_daemon_mutations
            else tr("Inspect Package")
        )
        # The package name is host metadata: translating it would be wrong.
        self.setWindowTitle(f"{caption}: {self.inspection_result.name}")

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # Header Info Card
        header_card = QFrame()
        header_card.setObjectName("AICard")
        card_layout = QHBoxLayout(header_card)
        card_layout.setContentsMargins(12, 10, 12, 10)

        pkg_icon = QLabel()
        icon = theme_icon("package-x-generic", "application-x-rpm")
        if not icon.isNull():
            pkg_icon.setPixmap(icon.pixmap(36, 36))
        card_layout.addWidget(pkg_icon)

        title_layout = QVBoxLayout()
        esc_name = html.escape(self.inspection_result.name, quote=True)
        esc_ver = html.escape(
            f"{self.inspection_result.version}-{self.inspection_result.release}",
            quote=True,
        )
        title_lbl = QLabel(f"<b>{esc_name}</b> {esc_ver}")
        title_lbl.setTextFormat(Qt.TextFormat.RichText)
        summary_lbl = QLabel(self.inspection_result.summary or tr("Local RPM Package"))
        summary_lbl.setObjectName("LocalRpmSummaryLabel")
        summary_lbl.setTextFormat(Qt.TextFormat.PlainText)
        summary_lbl.setWordWrap(True)
        title_layout.addWidget(title_lbl)
        title_layout.addWidget(summary_lbl)
        card_layout.addLayout(title_layout, stretch=1)

        layout.addWidget(header_card)

        if self.inspection_result.is_already_installed:
            esc_installed = html.escape(
                self.inspection_result.installed_version, quote=True
            )
            status_box = QLabel(
                f"<b>{tr('Status:')}</b> "
                + tr("Already installed (currently installed: {version}).").format(
                    version=f"<code>{esc_installed}</code>"
                )
            )
            status_box.setProperty("statusKind", "installed")
        else:
            status_box = QLabel(
                f"<b>{tr('Status:')}</b> "
                + tr("Not currently installed on this system.")
            )
            status_box.setProperty("statusKind", "available")
        status_box.setObjectName("LocalRpmStatusNotice")
        status_box.setTextFormat(Qt.TextFormat.RichText)
        status_box.setWordWrap(True)
        layout.addWidget(status_box)

        # Package Details Grid
        grid = QGridLayout()
        grid.setSpacing(6)
        arch_lbl = QLabel(self.inspection_result.arch)
        arch_lbl.setTextFormat(Qt.TextFormat.PlainText)
        size_lbl = QLabel(_format_byte_size(self.inspection_result.size_bytes))
        size_lbl.setTextFormat(Qt.TextFormat.PlainText)
        license_lbl = QLabel(self.inspection_result.license or tr("Unknown"))
        license_lbl.setTextFormat(Qt.TextFormat.PlainText)
        license_lbl.setWordWrap(True)
        grid.addWidget(_field_name(tr("Architecture:")), 0, 0)
        grid.addWidget(arch_lbl, 0, 1)
        grid.addWidget(_field_name(tr("Package Size:")), 0, 2)
        grid.addWidget(size_lbl, 0, 3)
        grid.addWidget(_field_name(tr("License:")), 1, 0)
        grid.addWidget(license_lbl, 1, 1, 1, 3)
        if self.inspection_result.classification is not None:
            decision = self.inspection_result.classification
            category = QLabel(category_label(decision.primary_category))
            category.setTextFormat(Qt.TextFormat.PlainText)
            category.setToolTip("\n".join(decision.rationale))
            evidence = QLabel(classification_evidence_label(decision.evidence.value))
            evidence.setTextFormat(Qt.TextFormat.PlainText)
            grid.addWidget(_field_name(tr("Classification:")), 2, 0)
            grid.addWidget(category, 2, 1)
            grid.addWidget(_field_name(tr("Evidence:")), 2, 2)
            grid.addWidget(evidence, 2, 3)
        else:
            grid.addWidget(_field_name(tr("Classification:")), 2, 0)
            grid.addWidget(
                QLabel(tr("Unavailable (package header remains inspectable)")),
                2,
                1,
                1,
                3,
            )
        grid.addWidget(_field_name(tr("File Path:")), 3, 0)
        path_lbl = QLabel(self.inspection_result.file_path)
        path_lbl.setTextFormat(Qt.TextFormat.PlainText)
        path_lbl.setWordWrap(True)
        grid.addWidget(path_lbl, 3, 1, 1, 3)
        layout.addLayout(grid)

        req_title = QLabel(tr("Package Dependencies (Requirements):"))
        req_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(req_title)

        req_box = QTextEdit()
        req_box.setObjectName("LocalRpmReqBox")
        req_box.setReadOnly(True)
        req_box.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        req_box.setPlainText(
            "\n".join(self.inspection_result.requires[:40])
            or tr("No explicit dependencies.")
        )
        layout.addWidget(req_box, stretch=1)

        allows_install = self._host_environment.supports_dnf5_daemon_mutations
        if not allows_install:
            _title, reason = self._host_environment.dnf5_daemon_unavailable(
                "install a local RPM file"
            )
            self.environment_notice_label = QLabel(reason)
            self.environment_notice_label.setObjectName("HostModeBannerDetail")
            self.environment_notice_label.setTextFormat(Qt.TextFormat.PlainText)
            self.environment_notice_label.setWordWrap(True)
            layout.addWidget(self.environment_notice_label)

        btn_bar = QHBoxLayout()
        btn_cancel = QPushButton(tr("Cancel") if allows_install else tr("Close"))
        btn_cancel.clicked.connect(self.reject)

        self.btn_install = QPushButton(tr("Install Local Package"))
        self.btn_install.setObjectName("ApplyButton")
        self.btn_install.setIcon(
            theme_icon("system-software-install", "dialog-ok-apply")
        )
        self.btn_install.setEnabled(allows_install)
        if not allows_install:
            self.btn_install.setToolTip(self.environment_notice_label.text())
        self.btn_install.clicked.connect(self._on_install_clicked)

        btn_bar.addWidget(btn_cancel)
        btn_bar.addStretch(1)
        btn_bar.addWidget(self.btn_install)
        layout.addLayout(btn_bar)

    def _on_install_clicked(self):
        if not self._host_environment.supports_dnf5_daemon_mutations:
            return
        self.install_requested.emit(self.inspection_result.file_path)
        self.accept()


class HostModeBanner(QFrame):
    """Compact inline notice shown on Atomic, read-only ``/usr``, and container hosts."""

    flatpak_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HostModeBanner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 7, 14, 7)
        layout.setSpacing(10)

        self.icon_label = QLabel()
        info_icon = theme_icon("dialog-information", "security-high")
        if not info_icon.isNull():
            self.icon_label.setPixmap(info_icon.pixmap(18, 18))
            self.icon_label.setAlignment(
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
            )
            layout.addWidget(self.icon_label)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self.title_label = QLabel()
        self.title_label.setObjectName("HostModeBannerTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        text_col.addWidget(self.title_label)

        self.detail_label = QLabel()
        self.detail_label.setObjectName("HostModeBannerDetail")
        self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_label.setWordWrap(True)
        text_col.addWidget(self.detail_label)

        self.deployment_label = QLabel()
        self.deployment_label.setObjectName("HostModeBannerStatus")
        self.deployment_label.setTextFormat(Qt.TextFormat.PlainText)
        self.deployment_label.setWordWrap(True)
        self.deployment_label.setVisible(False)
        text_col.addWidget(self.deployment_label)

        layout.addLayout(text_col, stretch=1)

        self.flatpak_action_btn = QPushButton(tr("Manage Flatpaks"))
        self.flatpak_action_btn.setObjectName("HeaderToolBtn")
        self.flatpak_action_btn.setIcon(
            theme_icon("application-x-flatpak", "system-software-install")
        )
        self.flatpak_action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.flatpak_action_btn.setToolTip(
            tr(
                "Open installed Flatpak applications and runtimes for updates or removal"
            )
        )
        self.flatpak_action_btn.clicked.connect(self.flatpak_requested.emit)
        self.flatpak_action_btn.setVisible(False)
        layout.addWidget(
            self.flatpak_action_btn, alignment=Qt.AlignmentFlag.AlignVCenter
        )

        self.setVisible(False)

    def configure(self, environment: HostEnvironment) -> None:
        banner = environment.mode_banner()
        if banner is None:
            self.deployment_label.clear()
            self.deployment_label.setVisible(False)
            self.flatpak_action_btn.setVisible(False)
            self.setVisible(False)
            return
        title, detail = banner
        self.title_label.setText(title)
        self.detail_label.setText(detail)
        self.deployment_label.clear()
        self.deployment_label.setVisible(False)
        self.flatpak_action_btn.setVisible(
            environment.is_immutable_host and environment.supports_flatpak_management
        )
        self.setVisible(True)

    def set_deployment_summary(self, summary: str) -> None:
        clean = summary.strip()
        self.deployment_label.setText(clean)
        self.deployment_label.setVisible(bool(clean))


__all__ = (
    "HostModeBanner",
    "LocalRpmInstallDialog",
    "_field_name",
)
