# dendro/ui/dry_run_dialog.py
from __future__ import annotations

from typing import Optional
from PyQt6.QtCore import Qt
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
    پنجره پیش‌نمایش و هشدار شبیه‌سازی اثرات جانبی تراکنش (Dry-Run Impact Preview)
    هشدار فوری در صورت حذف آبشاری بسته‌های حیاتی هسته فدورا
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

        # ۱. بنر هشدار سیستمی در صورت حذف پکیج‌های حساس
        if self.result.has_critical_system_removal:
            warning_frame = QFrame()
            warning_frame.setStyleSheet("""
                QFrame {
                    background-color: #45232e;
                    border: 2px solid #f38ba8;
                    border-radius: 8px;
                    padding: 10px;
                }
            """)
            warn_layout = QVBoxLayout(warning_frame)
            
            warn_title = QLabel("⚠️ CRITICAL SYSTEM RISK DETECTED!")
            warn_title.setStyleSheet("color: #f38ba8; font-weight: 800; font-size: 14px;")
            
            crit_pkgs = ", ".join(self.result.critical_packages)
            warn_desc = QLabel(
                f"This transaction will remove essential Fedora core components: <b>{crit_pkgs}</b>.<br>"
                "Proceeding with this removal might render your graphical desktop or system unbootable!"
            )
            warn_desc.setWordWrap(True)
            warn_desc.setStyleSheet("color: #cdd6f4; font-size: 12px;")

            warn_layout.addWidget(warn_title)
            warn_layout.addWidget(warn_desc)
            layout.addWidget(warning_frame)
        else:
            safe_label = QLabel("✅ Simulation Succeeded: No critical system pillars will be damaged.")
            safe_label.setStyleSheet("color: #a6e3a1; font-weight: bold; font-size: 13px;")
            layout.addWidget(safe_label)

        # ۲. خلاصه خروجی شبیه‌سازی
        summary_title = QLabel("Detailed DNF Simulation Output:")
        summary_title.setStyleSheet("color: #a6adc8; font-weight: bold; font-size: 12px;")
        layout.addWidget(summary_title)

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setPlainText(self.result.raw_output or "No simulation logs available.")
        self.console.setStyleSheet("""
            QTextEdit {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 8px;
                color: #a6adc8;
                font-family: monospace;
                font-size: 11px;
            }
        """)
        layout.addWidget(self.console, stretch=1)

        # ۳. دکمه‌های تایید یا لغو
        btn_layout = QHBoxLayout()
        
        self.cancel_btn = QPushButton("Cancel / Abort")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)

        self.proceed_btn = QPushButton("Proceed & Authenticate")
        self.proceed_btn.setObjectName("ApplyButton")
        self.proceed_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if self.result.has_critical_system_removal:
            self.proceed_btn.setText("Force Proceed (Dangerous)")
            self.proceed_btn.setStyleSheet("background-color: #f38ba8; color: #11111b; font-weight: bold;")
        self.proceed_btn.clicked.connect(self.accept)

        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self.proceed_btn)
        layout.addLayout(btn_layout)
