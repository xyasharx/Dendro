# main.py
"""
Application entry point for Dendro.
Configures exception handling, high-DPI scaling, clean font families,
FreeDesktop icon theme search paths, window icon resolution, and fallback theming.
Zero emoji glyphs and zero color emoji font chains to prevent Fontconfig crashes.
"""

from __future__ import annotations

import faulthandler
from importlib.metadata import PackageNotFoundError, version as distribution_version
import os
from pathlib import Path
import signal
import sys
import tomllib
import traceback


def _application_version() -> str:
    """Read the source version when present, otherwise installed wheel metadata."""
    source_metadata = Path(__file__).with_name("pyproject.toml")
    try:
        with source_metadata.open("rb") as stream:
            version = tomllib.load(stream)["project"]["version"]
        if isinstance(version, str) and version:
            return version
    except (FileNotFoundError, KeyError, OSError, tomllib.TOMLDecodeError):
        pass

    try:
        return distribution_version("dendro")
    except PackageNotFoundError:
        # Both source metadata and installed distribution metadata being absent
        # means the launcher has been copied outside a supported build form.
        return "unknown"


APPLICATION_VERSION = _application_version()

# Enable C-level signal crash handler immediately
faulthandler.enable()


def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    print(f"[FATAL UNCAUGHT EXCEPTION]\n{error_msg}", file=sys.stderr)

    # ``--version`` and argument validation intentionally work without the GUI
    # dependency installed. A failure while importing Qt must therefore retain
    # the textual diagnostic rather than replacing it with a second exception.
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        return

    if QApplication.instance():
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Icon.Critical)
        msg_box.setWindowTitle("Dendro - System Error")
        msg_box.setText("An unexpected internal error occurred.")
        msg_box.setDetailedText(error_msg)
        msg_box.exec()


def _application_dependency_error_message(exc: ImportError) -> str:
    """Describe a missing application dependency without mislabelling it as Qt."""
    if isinstance(exc, ModuleNotFoundError) and exc.name == "defusedxml":
        return (
            "Dendro cannot start because its required XML safety dependency "
            "'defusedxml' is unavailable. Install the project's dependencies or "
            "Fedora's python3-defusedxml package."
        )
    return f"Dendro cannot start because a required Python dependency is unavailable: {exc}"


def _argument_parser_options() -> dict[str, object]:
    """Return argparse options compatible with every supported Python version."""
    options: dict[str, object] = {
        "prog": "dendro",
        "description": "Visual package manager and dependency hierarchy explorer for Fedora Linux.",
    }

    # Python 3.14 enables ANSI help coloring by default. Earlier supported
    # Python versions reject the ``color`` argument, so add it only where it
    # is supported.
    if sys.version_info >= (3, 14):
        options["color"] = False

    return options


def main() -> int:
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Standard POSIX / Linux CLI argument handling
    import argparse

    parser = argparse.ArgumentParser(**_argument_parser_options())
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {APPLICATION_VERSION}"
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable verbose diagnostic logs for troubleshooting.",
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Path to a local .rpm package file to inspect and install.",
    )
    args, unknown = parser.parse_known_args()

    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    # Reject non-RPM or nonexistent files from CLI instead of launching GUI
    if args.file:
        if not args.file.endswith(".rpm") or not os.path.isfile(args.file):
            parser.error(f"'{args.file}' is not a valid or existing .rpm package file.")

    if args.debug:
        from datetime import datetime

        os.environ["DENDRO_DEBUG"] = "1"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        print(
            f"[{ts}] [Dendro {APPLICATION_VERSION}] Debug logging enabled on {sys.platform}",
            file=sys.stderr,
        )

    # Keep non-GUI CLI actions such as ``dendro --version`` usable in minimal
    # packaging environments. Import the Qt runtime separately from Dendro's
    # application modules so a missing non-Qt dependency is reported honestly.
    try:
        from PyQt6.QtCore import Qt, QTimer
        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QApplication
    except ImportError as exc:
        print(
            f"Dendro cannot start because its PyQt6 GUI runtime is unavailable: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        from ui.main_window import MainWindow
    except ImportError as exc:
        print(_application_dependency_error_message(exc), file=sys.stderr)
        return 1

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
    app.setApplicationVersion(APPLICATION_VERSION)
    app.setApplicationDisplayName("Dendro Package Tree")

    # Inherit system font point size and accessibility scaling from desktop environment
    app_font = app.font()
    app_font.setFamilies(["Cantarell", "Inter", "Segoe UI", "system-ui", "sans-serif"])
    app.setFont(app_font)

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
            os.path.join(
                base_dir, "data", "icons", "256x256", "io.github.xyasharx.Dendro.png"
            ),
            os.path.join(
                base_dir, "data", "icons", "128x128", "io.github.xyasharx.Dendro.png"
            ),
            os.path.join(base_dir, "io.github.xyasharx.Dendro.svg"),
            "/usr/share/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg",
            "/usr/share/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png",
        ]:
            if os.path.isfile(icon_path):
                app_icon = QIcon(icon_path)
                break
    app.setWindowIcon(app_icon)

    window = MainWindow()
    window.show()

    # Post local RPM opener to the event queue immediately after initial window map (0ms)
    if args.file and args.file.endswith(".rpm") and os.path.isfile(args.file):
        QTimer.singleShot(0, lambda: window.open_local_rpm(args.file))

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
