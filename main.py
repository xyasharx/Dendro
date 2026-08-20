# main.py
"""
Dendro Application Bootstrap and Entry Point.
Configures High-DPI scaling, POSIX signal heartbeat timer, and uncaught exception handling.
"""

from __future__ import annotations

import os
import signal
import sys
import traceback
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

# اصلاح ایمپورت بر اساس ساختار پوشه ui
from ui.main_window import MainWindow


def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    """Global exception hook to prevent silent UI crashes."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    print(f"[FATAL UNCAUGHT EXCEPTION]\n{error_msg}", file=sys.stderr)

    if QApplication.instance():
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Icon.Critical)
        msg_box.setWindowTitle("Dendro - System Error")
        msg_box.setText("An unexpected internal error occurred.")
        msg_box.setDetailedText(error_msg)
        msg_box.exec()


def main() -> int:
    # 1. POSIX SIGINT handler
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # 2. High-DPI Scaling for Wayland / 4K
    if hasattr(Qt.HighDpiScaleFactorRoundingPolicy, "PassThrough"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    # 3. Register Global Exception Hook
    sys.excepthook = handle_uncaught_exception

    # 4. Initialize Application
    app = QApplication(sys.argv)
    app.setApplicationName("Dendro")
    app.setApplicationDisplayName("Dendro Package Tree")
    app.setOrganizationName("FedoraCommunity")
    app.setDesktopFileName("dendro.desktop")

    # 5. Heartbeat timer for Python GIL signal processing
    sigint_timer = QTimer()
    sigint_timer.start(500)
    sigint_timer.timeout.connect(lambda: None)

    # 6. Show Main Window
    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
