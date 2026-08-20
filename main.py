# main.py
from __future__ import annotations

import os
import signal
import sys
import traceback
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from ui.main_window import MainWindow


def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
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
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    if hasattr(Qt.HighDpiScaleFactorRoundingPolicy, "PassThrough"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    sys.excepthook = handle_uncaught_exception

    app = QApplication(sys.argv)
    app.setApplicationName("Dendro")
    app.setApplicationDisplayName("Dendro Package Tree")
    app.setOrganizationName("FedoraCommunity")
    app.setDesktopFileName("dendro.desktop")

    sigint_timer = QTimer(app)
    sigint_timer.start(500)
    sigint_timer.timeout.connect(lambda: None)

    window = MainWindow()
    window.show()

    exit_code = app.exec()
    sigint_timer.stop()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
