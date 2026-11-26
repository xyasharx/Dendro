# dendro/main.py
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
import tomllib
import traceback
from collections.abc import MutableMapping
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path

if __package__ in (None, ""):
    _PACKAGE_PARENT = str(Path(__file__).resolve().parents[1])
    if _PACKAGE_PARENT not in sys.path:
        sys.path.insert(0, _PACKAGE_PARENT)

from dendro.core.i18n import tr


def _application_version() -> str:
    """Read the source version when present, otherwise installed wheel metadata."""
    here = Path(__file__).resolve()
    for source_metadata in (
        here.parents[1] / "pyproject.toml",
        here.with_name("pyproject.toml"),
    ):
        try:
            with source_metadata.open("rb") as stream:
                version = tomllib.load(stream)["project"]["version"]
            if isinstance(version, str) and version:
                return version
        except (FileNotFoundError, KeyError, OSError, tomllib.TOMLDecodeError):
            continue

    try:
        return distribution_version("dendro")
    except PackageNotFoundError:
        # Both source metadata and installed distribution metadata being absent
        # means the launcher has been copied outside a supported build form.
        return "unknown"


APPLICATION_VERSION = _application_version()


def _application_identity() -> str:
    """Return the release version plus its exact source identity when known."""
    try:
        from dendro.core.build_provenance import (
            discover_build_provenance,
            format_version_identity,
        )
    except ImportError:
        # A deliberately minimal copied launcher may contain neither Dendro's
        # package nor Git metadata. Its version remains usable, but its source
        # identity must not be guessed.
        return f"{APPLICATION_VERSION} (source revision unavailable)"

    provenance = discover_build_provenance(Path(__file__).resolve().parents[1])
    return format_version_identity(APPLICATION_VERSION, provenance)


APPLICATION_IDENTITY = _application_identity()

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
        msg_box.setWindowTitle(tr("Dendro - System Error"))
        msg_box.setText(tr("An unexpected internal error occurred."))
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


def _activate_saved_language() -> None:
    """Install the language chosen in Preferences for this process.

    Reading the stored preference is deliberately silent about failure: if the
    settings backend is unavailable the application still starts, in English.
    """

    try:
        from dendro.core.i18n import SYSTEM_LOCALE, resolve_locale
        from dendro.core.settings import get_app_settings
        from dendro.ui.i18n import install

        saved = get_app_settings().value("language", SYSTEM_LOCALE, type=str)
        resolved = resolve_locale(saved or SYSTEM_LOCALE, os.environ.get("LANG", ""))
        install(resolved.locale)
    except Exception:  # startup must never fail on a translation problem
        # A broken catalog or settings store must not prevent Dendro starting;
        # English remains the active locale.
        return


def _is_truthy_env(value: str | None) -> bool:
    """Return whether an environment toggle has a truthy non-zero value."""
    return bool(value and value.strip().lower() not in {"0", "false", "no", "off"})


def _configure_qt_logging_environment(
    env: MutableMapping[str, str], *, debug_flag: bool
) -> str | None:
    """Ensure Qt plugin and Wayland decoration logs are not silenced by qtlogging.ini.

    Fedora's system-wide ``qtlogging.ini`` disables ``*.debug=false`` by
    default, which suppresses ``QT_DEBUG_PLUGINS=1`` (``qt.core.plugin.*``)
    and Wayland decoration selection logs. Must run before ``PyQt6`` is
    imported.
    """
    if debug_flag:
        env["DENDRO_DEBUG"] = "1"

    debug_active = debug_flag or _is_truthy_env(env.get("DENDRO_DEBUG"))
    plugin_debug_active = _is_truthy_env(env.get("QT_DEBUG_PLUGINS"))

    needed_rules: list[tuple[str, str]] = []
    if plugin_debug_active or debug_active:
        needed_rules.append(("qt.core.plugin", "qt.core.plugin.*=true"))
    if debug_active:
        needed_rules.extend(
            [
                ("qt.qpa.wayland", "qt.qpa.wayland.debug=true"),
                (
                    "qt.qpa.qwaylandadwaitadecoration",
                    "qt.qpa.qwaylandadwaitadecoration.debug=true",
                ),
            ]
        )

    if not needed_rules:
        return env.get("QT_LOGGING_RULES")

    existing = (env.get("QT_LOGGING_RULES") or "").strip()
    existing_parts = [part.strip() for part in existing.split(";") if part.strip()]
    for prefix, rule in needed_rules:
        if not any(part.startswith(prefix) for part in existing_parts):
            existing_parts.append(rule)

    merged = ";".join(existing_parts)
    env["QT_LOGGING_RULES"] = merged
    return merged


def _log_startup_diagnostics(app: object, window: object) -> None:
    """Emit platform, Wayland decoration, screen, and window-constraint diagnostics."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    platform = getattr(app, "platformName", lambda: "unknown")()
    session_type = os.environ.get("XDG_SESSION_TYPE", "")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    decoration_env = os.environ.get("QT_WAYLAND_DECORATION", "<auto>")
    logging_rules = os.environ.get("QT_LOGGING_RULES", "<unset>")
    min_size = window.minimumSize()
    win_size = window.size()
    header = getattr(window, "header", None)
    header_height = header.height() if header is not None else -1
    print(
        f"[{ts}] [Dendro:debug] Qt platform={platform!r} session={session_type!r} "
        f"desktop={desktop!r} QT_WAYLAND_DECORATION={decoration_env!r} "
        f"QT_LOGGING_RULES={logging_rules!r}",
        file=sys.stderr,
    )
    win_state = window.windowState()
    state_label = getattr(win_state, "name", None) or str(win_state)
    print(
        f"[{ts}] [Dendro:debug] Window size={win_size.width()}x{win_size.height()} "
        f"minimumSize={min_size.width()}x{min_size.height()} headerHeight={header_height} "
        f"state={state_label}",
        file=sys.stderr,
    )
    screens = getattr(app, "screens", list)()
    for idx, screen in enumerate(screens):
        geom = screen.geometry()
        avail = screen.availableGeometry()
        print(
            f"[{ts}] [Dendro:debug] Screen[{idx}] name={screen.name()!r} "
            f"geom={geom.width()}x{geom.height()} avail={avail.width()}x{avail.height()} "
            f"dpr={screen.devicePixelRatio():.2f}",
            file=sys.stderr,
        )


def main() -> int:
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Standard POSIX / Linux CLI argument handling
    import argparse

    parser = argparse.ArgumentParser(**_argument_parser_options())
    parser.add_argument(
        "-v",
        "--version",
        action="store_true",
        help="Print the release version and exact source provenance.",
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

    # ``argparse`` wraps long ``action='version'`` output. Print explicitly so
    # the full commit and tree IDs remain one machine-copyable line.
    if args.version:
        print(f"{parser.prog} {APPLICATION_IDENTITY}")
        return 0

    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    # Reject non-RPM or nonexistent files from CLI instead of launching GUI
    if args.file and (not args.file.endswith(".rpm") or not os.path.isfile(args.file)):
        parser.error(f"'{args.file}' is not a valid or existing .rpm package file.")

    _configure_qt_logging_environment(os.environ, debug_flag=args.debug)
    if args.debug:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        print(
            f"[{ts}] [Dendro {APPLICATION_IDENTITY}] Debug logging enabled on {sys.platform}",
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
        from dendro.ui.main_window import MainWindow
    except ImportError as exc:
        print(_application_dependency_error_message(exc), file=sys.stderr)
        return 1

    if hasattr(Qt.HighDpiScaleFactorRoundingPolicy, "PassThrough"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    sys.excepthook = handle_uncaught_exception

    from dendro.core.settings import SETTINGS_APPLICATION, SETTINGS_ORGANIZATION

    # Configure application identity prior to instantiation to prevent duplicate portal registration
    QApplication.setDesktopFileName("io.github.xyasharx.Dendro")
    QApplication.setApplicationName(SETTINGS_APPLICATION)
    QApplication.setOrganizationName(SETTINGS_ORGANIZATION)

    app = QApplication(sys.argv)
    app.setApplicationVersion(APPLICATION_VERSION)

    # Activate the saved language before any widget reads a translated string.
    # This must run after QApplication exists and before MainWindow is built, so
    # the first paint already uses the chosen locale.
    _activate_saved_language()

    app.setApplicationDisplayName(tr("Dendro Package Tree"))

    # Preserve the desktop environment's UI font family and accessibility scaling.
    app_font = app.font()
    if app_font.pointSizeF() <= 0 and app_font.pixelSize() <= 0:
        app_font.setPointSize(10)
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
    package_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(package_dir)
    app_icon = QIcon.fromTheme("io.github.xyasharx.Dendro")
    if app_icon.isNull() or not app_icon.availableSizes():
        for icon_path in [
            os.path.join(
                root_dir, "data", "icons", "256x256", "io.github.xyasharx.Dendro.png"
            ),
            os.path.join(
                root_dir, "data", "icons", "128x128", "io.github.xyasharx.Dendro.png"
            ),
            os.path.join(root_dir, "io.github.xyasharx.Dendro.svg"),
            os.path.join(
                package_dir, "data", "icons", "256x256", "io.github.xyasharx.Dendro.png"
            ),
            "/usr/share/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg",
            "/usr/share/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png",
        ]:
            if os.path.isfile(icon_path):
                app_icon = QIcon(icon_path)
                break
    app.setWindowIcon(app_icon)

    window = MainWindow()
    # Apply the resolved icon to the top-level window as well as QApplication.
    # Some Wayland/desktop combinations do not inherit the application default
    # reliably when they construct client-side decorations.
    window.setWindowIcon(app_icon)
    window.show()
    if args.debug:
        _log_startup_diagnostics(app, window)

    # Post local RPM opener to the event queue immediately after initial window map (0ms)
    if args.file and args.file.endswith(".rpm") and os.path.isfile(args.file):
        QTimer.singleShot(0, lambda: window.open_local_rpm(args.file))

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
