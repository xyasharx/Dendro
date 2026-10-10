# main.py
"""
Application entry point for Dendro.
Configures exception handling, high-DPI scaling, clean font families,
FreeDesktop icon theme search paths, window icon resolution, and fallback theming.
Zero emoji glyphs and zero color emoji font chains to prevent Fontconfig crashes.
"""
from __future__ import annotations

import faulthandler
import os
import signal
import sys
import traceback

# Enable C-level signal crash handler immediately
faulthandler.enable()

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QIcon
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

    # Standard POSIX / Linux CLI argument handling
    import argparse
    parser = argparse.ArgumentParser(
        prog="dendro",
        description="Visual package manager and dependency hierarchy explorer for Fedora Linux.",
    )
    parser.add_argument("-v", "--version", action="version", version="%(prog)s 2.8.1")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable verbose diagnostic logs for troubleshooting.")
    parser.add_argument("file", nargs="?", help="Path to a local .rpm package file to inspect and install.")
    args, unknown = parser.parse_known_args()

    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    # Reject non-RPM or nonexistent files from CLI instead of launching GUI
    if args.file:
        if not args.file.endswith(".rpm") or not os.path.isfile(args.file):
            parser.error(f"'{args.file}' is not a valid or existing .rpm package file.")

    if args.debug:
        os.environ["DENDRO_DEBUG"] = "1"
        print(f"[Dendro] Debug logging enabled on {sys.platform}", file=sys.stderr)

    if hasattr(Qt.HighDpiScaleFactorRoundingPolicy, "PassThrough"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    sys.excepthook = handle_uncaught_exception

    # Configure application identity prior to instantiation to prevent duplicate portal registration
    QApplication.setDesktopFileName("io.github.xyasharx.Dendro")
    QApplication.setApplicationName("Dendro")
    QApplication.setOrganizationName("FedoraCommunity")

    app = QApplication(sys.argv)
    app.setApplicationDisplayName("Dendro Package Tree")

    # Inherit system font point size and accessibility scaling from desktop environment
    app_font = app.font()
    app_font.setFamilies(["Cantarell", "Inter", "Segoe UI", "system-ui", "sans-serif"])
    app.setFont(app_font)

    sigint_timer = QTimer(app)
    sigint_timer.start(500)
    sigint_timer.timeout.connect(lambda: None)

    # Register standard FreeDesktop icon search paths across FHS, Flatpak, and local profiles
    icon_paths = QIcon.themeSearchPaths()
    for search_dir in [
        "/usr/share/icons",
        "/usr/local/share/icons",
        os.path.expanduser("~/.local/share/icons"),
        os.path.expanduser("~/.icons"),
        "/var/lib/flatpak/exports/share/icons",
    ]:
        if os.path.isdir(search_dir) and search_dir not in icon_paths:
            icon_paths.append(search_dir)
    QIcon.setThemeSearchPaths(icon_paths)

    # Set fallback theme name without overriding the active desktop environment theme
    QIcon.setFallbackThemeName("Adwaita")

    # Resolve and set global Dendro window/taskbar icon
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app_icon = QIcon.fromTheme("io.github.xyasharx.Dendro")
    if app_icon.isNull() or not app_icon.availableSizes():
        for icon_path in [
            os.path.join(base_dir, "data", "icons", "256x256", "io.github.xyasharx.Dendro.png"),
            os.path.join(base_dir, "data", "icons", "128x128", "io.github.xyasharx.Dendro.png"),
            os.path.join(base_dir, "io.github.xyasharx.Dendro.svg"),
            "/usr/share/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg",
            "/usr/share/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png",
        ]:
            if os.path.isfile(icon_path):
                app_icon = QIcon(icon_path)
                break
    app.setWindowIcon(app_icon)

    window = MainWindow()
    if args.file and args.file.endswith(".rpm") and os.path.isfile(args.file):
        QTimer.singleShot(300, lambda: window.open_local_rpm(args.file))
    window.show()

    exit_code = app.exec()
    sigint_timer.stop()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
