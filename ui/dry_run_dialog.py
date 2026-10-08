# dendro/ui/dry_run_dialog.py
"""
Transaction impact preview and dry-run simulation dialog for Dendro.
Warns users if critical system root pillars are slated for removal.
Exclusively utilizes native FreeDesktop vector icons with zero font emoji glyphs.
"""
from __future__ import annotations

from typing import Optional
from PyQt6.QtCore import Qt
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

from core.backend import DryRunSimulationResult


class DryRunSimulationDialog(QDialog):
    """
    Transaction impact preview and simulation dialog.
    Displays warnings if critical Fedora system pillars are slated for removal.
    """

    def __init__(self, result: DryRunSimulationResult, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Transaction Impact & Dry-Run Simulation")
        self.resize(750, 480)
        self.result = result

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        from PyQt6.QtCore import QSettings
        from ui.styles import get_delegate_palette
        settings = QSettings("FedoraCommunity", "Dendro")
        theme_choice = settings.value("theme", "auto", type=str)
        pal = get_delegate_palette(theme_choice)

        # 1. System Risk Banner or Success Notification
        if self.result.has_critical_system_removal:
            warning_frame = QFrame()
            warning_frame.setStyleSheet(f"""
                QFrame {{
                    background-color: {pal['badge_bg_missing'].name()};
                    border: 2px solid {pal['badge_border_missing'].name()};
                    border-radius: 8px;
                    padding: 10px;
                }}
            """)
            warn_layout = QVBoxLayout(warning_frame)

            warn_header = QHBoxLayout()
            warn_icon_lbl = QLabel()
            warn_icon = QIcon.fromTheme("dialog-warning") or QIcon.fromTheme("security-high")
            if not warn_icon.isNull():
                warn_icon_lbl.setPixmap(warn_icon.pixmap(22, 22))
                warn_header.addWidget(warn_icon_lbl)

            warn_title = QLabel("CRITICAL SYSTEM RISK DETECTED")
            warn_title.setStyleSheet("color: #f38ba8; font-weight: 800;")
            warn_header.addWidget(warn_title, stretch=1)
            warn_layout.addLayout(warn_header)

            crit_pkgs = ", ".join(self.result.critical_packages)
            warn_desc = QLabel(
                f"This transaction will remove essential Fedora core components: <b>{crit_pkgs}</b>.<br>"
                "Proceeding with this removal might render your graphical desktop or system unbootable!"
            )
            warn_desc.setWordWrap(True)
            warn_desc.setStyleSheet("color: #cdd6f4; margin-top: 4px;")
            warn_layout.addWidget(warn_desc)

            layout.addWidget(warning_frame)
        else:
            safe_layout = QHBoxLayout()
            safe_icon_lbl = QLabel()
            safe_icon = QIcon.fromTheme("emblem-ok-symbolic") or QIcon.fromTheme("dialog-ok") or QIcon.fromTheme("emblem-default")
            if not safe_icon.isNull():
                safe_icon_lbl.setPixmap(safe_icon.pixmap(20, 20))
                safe_layout.addWidget(safe_icon_lbl)

            safe_label = QLabel("Simulation Succeeded: No critical system pillars will be damaged.")
            safe_label.setStyleSheet("color: #a6e3a1; font-weight: bold;")
            safe_layout.addWidget(safe_label)
            safe_layout.addStretch(1)
            layout.addLayout(safe_layout)

        # 2. Detailed Simulation Output Console
        summary_title = QLabel("Detailed DNF Simulation Output:")
        summary_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(summary_title)

        self.console = QTextEdit()
        self.console.setObjectName("DryRunConsole")
        self.console.setReadOnly(True)
        self.console.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.console.setPlainText(self.result.raw_output or "No simulation logs available.")
        layout.addWidget(self.console, stretch=1)

        # 3. Action Buttons
        btn_layout = QHBoxLayout()

        self.cancel_btn = QPushButton("Cancel / Abort")
        cancel_icon = QIcon.fromTheme("process-stop") or QIcon.fromTheme("dialog-cancel")
        if not cancel_icon.isNull():
            self.cancel_btn.setIcon(cancel_icon)
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)

        self.proceed_btn = QPushButton("Proceed & Authenticate")
        self.proceed_btn.setObjectName("ApplyButton")
        apply_icon = QIcon.fromTheme("dialog-ok-apply") or QIcon.fromTheme("emblem-default")
        if not apply_icon.isNull():
            self.proceed_btn.setIcon(apply_icon)
        self.proceed_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        if self.result.has_critical_system_removal:
            self.proceed_btn.setText("Force Proceed (Dangerous)")
            self.proceed_btn.setStyleSheet("background-color: #f38ba8; color: #11111b; font-weight: bold;")
        self.proceed_btn.clicked.connect(self.accept)

        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self.proceed_btn)
        layout.addLayout(btn_layout)
