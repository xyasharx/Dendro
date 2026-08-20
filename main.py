# dendro/main.py
"""
Dendro Application Bootstrap and Entry Point (State-of-the-Art Linux Systems Standard).

Configures High-DPI scaling for Wayland/X11, POSIX signal interception via Python GIL
heartbeat timer, and centralized uncaught exception handling.
"""

from __future__ import annotations

import os
import signal
import sys
import traceback
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from dendro.ui.main_window import MainWindow


def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    """
    Global exception hook to prevent silent UI crashes and provide diagnostic context.
    """
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
    # 1. Register POSIX SIGINT handler for terminal Ctrl+C termination
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # 2. Configure High-DPI Fractional Scaling for modern Wayland / 4K displays
    if hasattr(Qt.HighDpiScaleFactorRoundingPolicy, "PassThrough"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    # 3. Intercept uncaught exceptions
    sys.excepthook = handle_uncaught_exception

    # 4. Initialize Core Application
    app = QApplication(sys.argv)
    app.setApplicationName("Dendro")
    app.setApplicationDisplayName("Dendro Package Tree")
    app.setOrganizationName("FedoraCommunity")
    app.setDesktopFileName("dendro.desktop")

    # 5. POSIX Signal Heartbeat Timer:
    # Periodically yields execution to the Python interpreter so SIGINT is processed instantly
    # without freezing the Qt C++ event loop.
    sigint_timer = QTimer()
    sigint_timer.start(500)
    sigint_timer.timeout.connect(lambda: None)

    # 6. Bootstrap Window
    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
