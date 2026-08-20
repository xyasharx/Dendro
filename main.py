# main.py
"""
Application Bootstrap and Entry Point.
Configures High-DPI scaling, signal interception, and global exception logging.
"""

from __future__ import annotations

import os
import signal
import sys
import traceback
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from ui.main_window import MainWindow


def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    """
    Global exception hook to prevent silent UI crashes and display error context.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    print(f"[FATAL UNCAUGHT EXCEPTION]\n{error_msg}", file=sys.stderr)

    # If QApplication instance exists, show a GUI dialog
    if QApplication.instance():
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Icon.Critical)
        msg_box.setWindowTitle("Application Error")
        msg_box.setText("An unexpected internal error occurred.")
        msg_box.setDetailedText(error_msg)
        msg_box.exec()


def main():
    # 1. Enable POSIX Ctrl+C (SIGINT) termination
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # 2. Configure High-DPI scaling for 2K/4K/Wayland displays
    if hasattr(Qt.HighDpiScaleFactorRoundingPolicy, "PassThrough"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    # 3. Register global exception handler
    sys.excepthook = handle_uncaught_exception

    # 4. Initialize Application
    app = QApplication(sys.argv)
    app.setApplicationName("Dendro")
    app.setOrganizationName("FedoraCommunity")
    app.setDesktopFileName("dendro.desktop")

    # 5. Launch Main Window
    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
