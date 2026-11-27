"""Theme coordination: the appearance state of one window.

Extracted from ``dendro/ui/main_window.py``, where 243 lines of portal subscription,
native colour-scheme tracking, icon-theme pairing and stylesheet application sat
in the middle of a 3,700-line module.  They share five pieces of state that
nothing else in the window touches -- the current preference, a re-entrancy
guard, a coalescing flag, the pending key, and the native override -- so they
are moved as a unit rather than scattered.

Written as a mixin because these methods are called from the window's own signal
connections (``_on_system_color_scheme_changed`` is a Qt slot, and a slot on a
separate object would need the window passed in on every emission).  A mixin
keeps the method resolution and ``self`` unchanged, which is what makes this a
move rather than a redesign: the state lives in :meth:`init_theme_state`, which
the window calls from its constructor.

The three appearance inputs arrive from outside Qt's event loop -- the
``colorSchemeChanged`` signal, the FreeDesktop portal's ``SettingChanged``, and
the Preferences dialog -- so a cut-down version of this module is worth testing
without a display server.  ``ThemeState`` holds the parts that are pure logic.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from PyQt6.QtCore import (
    QObject,
    QSettings,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtDBus import (
    QDBusConnection,
    QDBusMessage,
    QDBusPendingCallWatcher,
    QDBusPendingReply,
)
from PyQt6.QtGui import QColor, QGuiApplication, QIcon, QPalette
from PyQt6.QtWidgets import QApplication

from dendro.core.worker_lifecycle import ShutdownSafeRunnable
from dendro.ui.styles import (
    HAS_QTDBUS,
    PORTAL_SETTINGS_TIMEOUT_MS,
    THEMES_CONFIG,
    _query_gsettings_dark_mode,
    _set_gsettings_dark_mode_cache,
    _set_portal_color_scheme_cache,
    _set_portal_contrast_cache,
    _set_portal_reduced_motion_cache,
    _should_query_gsettings,
    get_delegate_palette,
    get_resolved_theme_key,
    get_theme_stylesheet,
    has_non_gsettings_color_scheme,
    is_system_high_contrast,
    is_system_reduced_motion,
    portal_color_scheme_to_theme_key,
    portal_contrast_to_bool,
    portal_reduced_motion_to_bool,
)
from dendro.ui.window_presentation import select_matching_icon_theme

LOGGER = logging.getLogger(__name__)

#: The appearance preferences Dendro accepts.  Anything else is ignored rather
#: than persisted, so a corrupt settings value cannot leave the window unstyled.
THEME_CHOICES = frozenset({"auto", "dark", "light"})

#: The concrete keys a theme resolves to.  "auto" is a preference, not a key:
#: it is resolved against the desktop before anything is drawn.
RESOLVED_THEME_KEYS = frozenset({"dark", "light", "dark_hc", "light_hc"})

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INTERFACE = "org.freedesktop.portal.Settings"


def resolve_theme_choice(candidate: object, settings: QSettings) -> str:
    """Return one of :data:`THEME_CHOICES`, repairing a bad stored value.

    A value written by a future version, or by hand, must not be trusted: the
    window would apply an unknown key and fall back to an unthemed palette.
    """

    if isinstance(candidate, str) and candidate in THEME_CHOICES:
        return candidate
    settings.setValue("theme", "auto")
    return "auto"


@dataclass
class ThemeState:
    """The appearance state that belongs to one window.

    Kept as a dataclass so the coalescing rules can be tested as plain values,
    without constructing a window, a QSettings or a display.
    """

    choice: str = "auto"
    applying: bool = False
    pending_key: str | None = None
    queued: bool = False
    force_contrast: bool = False
    reduced_motion: bool = False
    applied_key: str | None = None
    native_override: object = field(default=None)

    def begin_apply(self) -> bool:
        """Claim the re-entrancy guard, or report that an apply is in flight."""

        if self.applying:
            return False
        self.applying = True
        return True

    def end_apply(self) -> None:
        self.applying = False

    def queue(self, key: str | None) -> None:
        """Record the newest system theme key and coalesce the refresh.

        Two signals can arrive in the same event-loop turn (Qt's
        ``colorSchemeChanged`` and the portal's ``SettingChanged``).  The portal
        value is authoritative, so the last *recognised* key wins and only one
        refresh is scheduled.

        Qt's signal carries no key, so a keyless event must leave the portal's
        value alone: clearing it here would make the deferred refresh re-resolve
        the theme from a stale Qt palette and undo the portal's answer.
        """

        if key in RESOLVED_THEME_KEYS:
            self.pending_key = key
        self.queued = True

    def take_queued(self) -> tuple[bool, str | None]:
        """Consume the queued refresh, returning whether there was one."""

        if not self.queued:
            return False, None
        self.queued = False
        key, self.pending_key = self.pending_key, None
        return True, key


class _AsyncDarkModeSignals(QObject):
    """Terminal signal emitted after an asynchronous ``gsettings`` lookup."""

    resolved = pyqtSignal(int, object)


class _AsyncGsettingsDarkModeWorker(ShutdownSafeRunnable):
    """Off-thread worker resolving dark mode via ``gsettings`` without blocking UI."""

    def __init__(
        self,
        serial: int,
        *,
        loader: Callable[[], bool | None] = _query_gsettings_dark_mode,
    ) -> None:
        super().__init__()
        self.signals = _AsyncDarkModeSignals()
        self._serial = serial
        self._loader = loader
        self._is_cancelled = threading.Event()

    def cancel(self) -> None:
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self) -> None:
        if self._is_cancelled.is_set():
            return
        try:
            result = self._loader()
        except Exception:  # Defensive worker boundary.
            LOGGER.debug("Async gsettings dark-mode fallback failed.", exc_info=True)
            result = None
        if not self._is_cancelled.is_set():
            self.signals.resolved.emit(self._serial, result)


class ThemeCoordinator:
    #: The five pieces of appearance state, created by the window and read here.
    _theme_state: ThemeState
    current_theme: str = "auto"
    settings: QSettings

    #: Widgets this coordinator themes. Declared for type checkers; the window
    #: creates them before the first theme application.
    tree_view: object
    tree_delegate: object
    sidebar: object
    inspector_panel: object

    def init_theme_state(self, settings: QSettings) -> None:
        """Create the appearance state. Called from the window constructor."""

        self.settings = settings
        self.current_theme = resolve_theme_choice(
            settings.value("theme", "auto", type=str), settings
        )
        self._theme_state = ThemeState(
            choice=self.current_theme,
            # Qt 6.8+ can synchronize native window decorations with a custom
            # application theme. Track the override we requested to avoid
            # creating a color-scheme signal loop while the app follows the
            # system theme.
            native_override=None,
        )
        self._portal_watchers: set[object] = set()
        self._gsettings_theme_worker: _AsyncGsettingsDarkModeWorker | None = None
        self._gsettings_request_serial: int = 0

    def _init_theming(self):
        # Resolve initial theme and motion preferences without blocking D-Bus or
        # subprocesses on the GUI thread so first paint never stalls.
        initial_key = None
        if self.current_theme in THEME_CHOICES:
            initial_key = get_resolved_theme_key(
                self.current_theme, allow_blocking=False
            )
        self._apply_theme(self.current_theme, resolved_key=initial_key)

        sync_motion = getattr(self, "_sync_motion_preference", None)
        if callable(sync_motion):
            sync_motion(allow_blocking=False)

        # 1. Native Qt 6 colorSchemeChanged and QAccessibilityHints signals
        app = QGuiApplication.instance()
        if app and hasattr(app, "styleHints"):
            style_hints = app.styleHints()
            color_scheme_sig = getattr(style_hints, "colorSchemeChanged", None)
            if callable(getattr(color_scheme_sig, "connect", None)):
                color_scheme_sig.connect(self._on_system_color_scheme_changed)
            accessibility_fn = getattr(style_hints, "accessibility", None)
            if callable(accessibility_fn):
                accessibility = accessibility_fn()
                contrast_sig = getattr(accessibility, "contrastPreferenceChanged", None)
                if callable(getattr(contrast_sig, "connect", None)):
                    contrast_sig.connect(self._on_system_contrast_changed)
                motion_sig = getattr(accessibility, "motionPreferenceChanged", None)
                motion_handler = getattr(self, "_on_system_motion_changed", None)
                if callable(getattr(motion_sig, "connect", None)) and callable(
                    motion_handler
                ):
                    motion_sig.connect(motion_handler)

        # 2. Direct FreeDesktop Portal D-Bus SettingChanged event subscription.
        # This is the primary live-update path on desktops where Qt does not
        # emit colorSchemeChanged for every GNOME appearance transition.
        self._subscribe_to_portal_color_scheme_changes()

        # 3. Non-blocking async portal startup query via QDBusPendingCallWatcher.
        start_async = getattr(self, "_start_async_portal_sync", None)
        if callable(start_async):
            start_async()

    def _start_async_portal_sync(self) -> None:
        """Query FreeDesktop portal appearance settings asynchronously at startup."""
        app = QGuiApplication.instance()
        style_hints = app.styleHints() if app and hasattr(app, "styleHints") else None

        needs_color_scheme = self.current_theme == "auto"
        if needs_color_scheme and style_hints is not None:
            color_scheme_fn = getattr(style_hints, "colorScheme", None)
            if (
                callable(color_scheme_fn)
                and color_scheme_fn() != Qt.ColorScheme.Unknown
            ):
                needs_color_scheme = False

        bus_ready = False
        if HAS_QTDBUS:
            try:
                bus = QDBusConnection.sessionBus()
                bus_ready = bool(
                    bus.isConnected() and callable(getattr(bus, "asyncCall", None))
                )
            except (RuntimeError, TypeError):
                bus_ready = False

        if not bus_ready:
            if needs_color_scheme:
                self._trigger_gsettings_fallback_if_available()
            return

        if needs_color_scheme:
            self._query_portal_setting_async("color-scheme")

        self._query_portal_setting_async("contrast")
        self._query_portal_setting_async("reduced-motion")

    def _trigger_gsettings_fallback_if_available(self) -> None:
        fallback = getattr(self, "_maybe_start_async_gsettings_fallback", None)
        if callable(fallback):
            fallback()

    def _cancel_async_gsettings_fallback(self) -> None:
        self._gsettings_request_serial = (
            getattr(self, "_gsettings_request_serial", 0) + 1
        )
        worker = getattr(self, "_gsettings_theme_worker", None)
        if worker is not None:
            worker.cancel()
            self._gsettings_theme_worker = None

    def _maybe_start_async_gsettings_fallback(self) -> None:
        """Run the gated ``gsettings`` dark-mode check off-thread when portal fails."""
        if getattr(self, "current_theme", "auto") != "auto":
            return
        state = getattr(self, "_theme_state", None)
        if state is not None and state.applying:
            return
        if has_non_gsettings_color_scheme():
            return
        if not _should_query_gsettings():
            return
        if getattr(self, "_gsettings_theme_worker", None) is not None:
            return

        serial = getattr(self, "_gsettings_request_serial", 0) + 1
        self._gsettings_request_serial = serial
        worker = _AsyncGsettingsDarkModeWorker(serial)
        self._gsettings_theme_worker = worker
        worker.signals.resolved.connect(self._on_async_gsettings_dark_mode_result)
        pool = getattr(self, "thread_pool", None)
        if pool is None or not callable(getattr(pool, "start", None)):
            pool = QThreadPool.globalInstance()
        pool.start(worker)

    @pyqtSlot(int, object)
    def _on_async_gsettings_dark_mode_result(
        self, serial: int, is_dark: object
    ) -> None:
        """Apply a completed async ``gsettings`` fallback result on the GUI thread."""
        if serial != getattr(self, "_gsettings_request_serial", 0):
            return
        self._gsettings_theme_worker = None
        if not isinstance(is_dark, bool):
            return
        if getattr(self, "current_theme", "auto") != "auto":
            return
        if has_non_gsettings_color_scheme():
            return
        _set_gsettings_dark_mode_cache(is_dark)
        theme_key = "dark" if is_dark else "light"
        hc = is_system_high_contrast(allow_blocking=False)
        full_key = f"{theme_key}_hc" if hc else theme_key
        state = getattr(self, "_theme_state", None)
        if (
            state is not None
            and not state.applying
            and full_key != getattr(state, "applied_key", None)
        ):
            self._queue_system_theme_update(full_key)

    def _query_portal_setting_async(self, key: str, method: str = "ReadOne") -> None:
        """Dispatch a non-blocking ``ReadOne``/``Read`` call for one portal key."""
        try:
            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                if key == "color-scheme":
                    self._trigger_gsettings_fallback_if_available()
                return
            async_call = getattr(bus, "asyncCall", None)
            if not callable(async_call):
                if key == "color-scheme":
                    self._trigger_gsettings_fallback_if_available()
                return
            message = QDBusMessage.createMethodCall(
                PORTAL_SERVICE,
                PORTAL_PATH,
                PORTAL_INTERFACE,
                method,
            )
            message.setArguments(["org.freedesktop.appearance", key])
            pending = async_call(message, timeout=PORTAL_SETTINGS_TIMEOUT_MS)
            parent = self if isinstance(self, QObject) else None
            watcher = QDBusPendingCallWatcher(pending, parent)
            watchers = getattr(self, "_portal_watchers", None)
            if isinstance(watchers, set):
                watchers.add(watcher)
            watcher.finished.connect(
                lambda *_args, w=watcher, k=key, m=method: self._on_async_portal_reply(
                    w, k, m
                )
            )
        except (RuntimeError, TypeError):
            LOGGER.debug(
                "Could not start async portal query for %r.", key, exc_info=True
            )
            if key == "color-scheme":
                self._trigger_gsettings_fallback_if_available()

    def _on_async_portal_reply(
        self, watcher: QDBusPendingCallWatcher, key: str, method: str
    ) -> None:
        """Apply a completed async portal settings reply without blocking."""
        watchers = getattr(self, "_portal_watchers", None)
        if isinstance(watchers, set):
            watchers.discard(watcher)
        try:
            reply_msg = QDBusPendingReply(watcher).reply()
        except (RuntimeError, TypeError):
            reply_msg = None
        finally:
            delete_later = getattr(watcher, "deleteLater", None)
            if callable(delete_later):
                delete_later()

        if reply_msg is None:
            if key == "color-scheme":
                self._trigger_gsettings_fallback_if_available()
            return
        if reply_msg.type() == QDBusMessage.MessageType.ErrorMessage:
            if method == "ReadOne":
                self._query_portal_setting_async(key, method="Read")
                return
            if key == "color-scheme":
                self._trigger_gsettings_fallback_if_available()
            return
        if (
            reply_msg.type() != QDBusMessage.MessageType.ReplyMessage
            or not reply_msg.arguments()
        ):
            if key == "color-scheme":
                self._trigger_gsettings_fallback_if_available()
            return

        self._apply_async_portal_value(key, reply_msg.arguments()[0])

    def _apply_async_portal_value(self, key: str, raw_value: object) -> None:
        """Update theme or motion state when an async portal startup read finishes."""
        if key == "color-scheme":
            theme_key = portal_color_scheme_to_theme_key(raw_value)
            if theme_key is None:
                self._trigger_gsettings_fallback_if_available()
                return
            cancel_fallback = getattr(self, "_cancel_async_gsettings_fallback", None)
            if callable(cancel_fallback):
                cancel_fallback()
            _set_portal_color_scheme_cache(theme_key == "dark")
            _set_gsettings_dark_mode_cache(None)
            if self.current_theme == "auto" and not self._theme_state.applying:
                hc = is_system_high_contrast(allow_blocking=False)
                full_key = f"{theme_key}_hc" if hc else theme_key
                if full_key != getattr(self._theme_state, "applied_key", None):
                    self._queue_system_theme_update(full_key)
            return

        if key == "contrast":
            hc = portal_contrast_to_bool(raw_value)
            _set_portal_contrast_cache(False if hc is None else hc)
            if hc is not None and not self._theme_state.applying:
                applied_key = getattr(self._theme_state, "applied_key", None)
                current_hc = (
                    applied_key.endswith("_hc")
                    if isinstance(applied_key, str)
                    else None
                )
                if current_hc is None or hc != current_hc:
                    target_key = get_resolved_theme_key(
                        self.current_theme, high_contrast=hc, allow_blocking=False
                    )
                    queue_contrast = getattr(self, "_queue_contrast_theme_update", None)
                    if callable(queue_contrast):
                        queue_contrast(target_key)
                    else:
                        self._queue_system_theme_update(target_key)
            return

        if key == "reduced-motion":
            rm = portal_reduced_motion_to_bool(raw_value)
            _set_portal_reduced_motion_cache(False if rm is None else rm)
            if rm is not None:
                sync_motion = getattr(self, "_sync_motion_preference", None)
                if callable(sync_motion):
                    sync_motion(reduced_motion=rm)

    def _subscribe_to_portal_color_scheme_changes(self) -> None:
        """Subscribe to the portal with a typed Qt slot, not a plain callable."""
        from dendro.ui.styles import _debug_log

        try:
            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                _debug_log(
                    "D-Bus session bus is unavailable; portal theme updates are disabled."
                )
                return

            # QtDBus requires a QObject slot decorated with ``@pyqtSlot``.
            # Passing a regular Python bound method raises TypeError, leaving
            # Qt's less reliable colorSchemeChanged signal as the only path.
            connected = bus.connect(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Settings",
                "SettingChanged",
                self._on_portal_settings_message,
            )
            if connected:
                _debug_log("Subscribed to D-Bus portal color-scheme changes.")
                return

            error = bus.lastError()
            detail = error.message() or "D-Bus refused the signal subscription."
            _debug_log(f"Could not subscribe to portal color-scheme changes: {detail}")
            LOGGER.debug(
                "Could not subscribe to portal color-scheme changes: %s", detail
            )
        except (RuntimeError, TypeError):
            # Appearance portal support is optional. It cannot affect package
            # management, but a failed subscription should remain diagnosable.
            LOGGER.debug(
                "Could not subscribe to portal color-scheme changes.", exc_info=True
            )

    @pyqtSlot(QDBusMessage)
    def _on_portal_settings_message(self, message: QDBusMessage) -> None:
        """Forward one correctly addressed Settings signal to the theme handler."""
        if (
            message.path() != "/org/freedesktop/portal/desktop"
            or message.interface() != "org.freedesktop.portal.Settings"
            or message.member() != "SettingChanged"
        ):
            return

        arguments = message.arguments()
        if len(arguments) != 3:
            LOGGER.debug("Ignoring malformed portal SettingChanged signal.")
            return

        namespace, key, value = arguments
        if not isinstance(namespace, str) or not isinstance(key, str):
            LOGGER.debug("Ignoring portal SettingChanged signal with non-string names.")
            return
        self._on_portal_setting_changed(namespace, key, value)

    def _on_portal_setting_changed(self, namespace: str, key: str, value: object):
        """Queue one update from the exact new portal value, when available."""
        if namespace != "org.freedesktop.appearance":
            return

        from dendro.ui.styles import _debug_log

        if key == "color-scheme":
            theme_key = portal_color_scheme_to_theme_key(value)
            if theme_key:
                cancel_fallback = getattr(
                    self, "_cancel_async_gsettings_fallback", None
                )
                if callable(cancel_fallback):
                    cancel_fallback()
                _set_portal_color_scheme_cache(theme_key == "dark")
                _set_gsettings_dark_mode_cache(None)
            if theme_key and is_system_high_contrast(allow_blocking=False):
                theme_key = f"{theme_key}_hc"
            _debug_log(
                f"D-Bus Portal SettingChanged event received: {namespace}.{key} -> {theme_key or 'unknown'}"
            )
            if self.current_theme == "auto" and not self._theme_state.applying:
                self._queue_system_theme_update(theme_key)
            return

        if key == "contrast":
            hc = portal_contrast_to_bool(value)
            _set_portal_contrast_cache(hc)
            _debug_log(
                f"D-Bus Portal SettingChanged event received: {namespace}.{key} -> {hc}"
            )
            if hc is not None and not self._theme_state.applying:
                target_key = get_resolved_theme_key(
                    self.current_theme, high_contrast=hc, allow_blocking=False
                )
                queue_contrast = getattr(self, "_queue_contrast_theme_update", None)
                if callable(queue_contrast):
                    queue_contrast(target_key)
                else:
                    self._queue_system_theme_update(target_key)
            return

        if key == "reduced-motion":
            rm = portal_reduced_motion_to_bool(value)
            _set_portal_reduced_motion_cache(rm)
            _debug_log(
                f"D-Bus Portal SettingChanged event received: {namespace}.{key} -> {rm}"
            )
            if rm is not None:
                sync_motion = getattr(self, "_sync_motion_preference", None)
                if callable(sync_motion):
                    sync_motion(reduced_motion=rm)

    def _queue_contrast_theme_update(self, theme_key: str | None = None) -> None:
        """Queue a contrast preference refresh regardless of auto/dark/light choice."""
        if self._theme_state.applying:
            return
        self._theme_state.force_contrast = True
        if self._theme_state.queued:
            self._theme_state.queue(theme_key)
            return
        self._theme_state.queue(theme_key)
        QTimer.singleShot(0, self._apply_queued_system_theme)

    def _queue_system_theme_update(self, theme_key: str | None = None) -> None:
        """Apply a desktop-theme event after Qt has finished its own update."""
        if self.current_theme != "auto" or self._theme_state.applying:
            return
        if self._theme_state.queued:
            self._theme_state.queue(theme_key)
            return

        self._theme_state.queue(theme_key)
        QTimer.singleShot(0, self._apply_queued_system_theme)

    def _apply_queued_system_theme(self) -> None:
        """Coalesce portal and Qt notifications into one stylesheet update."""
        self._theme_state.queued = False
        theme_key = self._theme_state.pending_key
        self._theme_state.pending_key = None
        force_contrast = self._theme_state.force_contrast
        self._theme_state.force_contrast = False
        if (
            self.current_theme == "auto" or force_contrast
        ) and not self._theme_state.applying:
            self._apply_theme(self.current_theme, resolved_key=theme_key)

    def _sync_native_color_scheme(self, theme_choice: str, resolved_key: str) -> None:
        """Tell Qt which scheme to use for its native window decorations.

        A stylesheet changes Dendro's widgets but not necessarily a Wayland/X11
        client-side title bar. Qt 6.8 introduced this public process-local hint;
        older PyQt6 releases simply retain their existing platform behaviour.
        """
        app = QGuiApplication.instance()
        if app is None:
            return

        style_hints = app.styleHints()
        set_color_scheme = getattr(style_hints, "setColorScheme", None)
        if not callable(set_color_scheme):
            return

        choice = (theme_choice or "auto").lower()
        if choice == "auto":
            # Qt follows the system by default.  In Auto mode, clear only
            # an explicit Dendro override; a fresh automatic application does
            # not need a native color-scheme write.
            if self._theme_state.native_override is None:
                return
            target_scheme = Qt.ColorScheme.Unknown
        else:
            target_scheme = (
                Qt.ColorScheme.Dark
                if resolved_key.startswith("dark")
                else Qt.ColorScheme.Light
            )

        if self._theme_state.native_override == target_scheme:
            return

        try:
            set_color_scheme(target_scheme)
            self._theme_state.native_override = (
                None if target_scheme == Qt.ColorScheme.Unknown else target_scheme
            )
        except (RuntimeError, TypeError):
            LOGGER.debug(
                "Could not synchronize the native window color scheme.", exc_info=True
            )

    def _apply_theme(self, theme_choice: str, *, resolved_key: str | None = None):
        if self._theme_state.applying:
            return

        self._theme_state.applying = True
        try:
            if resolved_key not in RESOLVED_THEME_KEYS:
                resolved_key = get_resolved_theme_key(
                    theme_choice, allow_blocking=False
                )
            self._theme_state.applied_key = resolved_key
            self._sync_native_color_scheme(theme_choice, resolved_key)

            stylesheet = get_theme_stylesheet(resolved_key)
            app = QApplication.instance()
            if app:
                app.setStyleSheet(stylesheet)
            else:
                self.setStyleSheet(stylesheet)

            theme_cfg = THEMES_CONFIG.get(resolved_key, {})
            is_dark = theme_cfg.get("is_dark", "true") == "true"

            if app and theme_cfg:
                pal = QPalette()
                window_col = QColor(theme_cfg["bg_surface"])
                text_col = QColor(theme_cfg["text_primary"])
                base_col = QColor(theme_cfg["bg_input"])
                accent_col = QColor(theme_cfg["accent"])
                accent_txt = QColor(theme_cfg["accent_text"])

                pal.setColor(QPalette.ColorRole.Window, window_col)
                pal.setColor(QPalette.ColorRole.WindowText, text_col)
                pal.setColor(QPalette.ColorRole.Base, base_col)
                pal.setColor(QPalette.ColorRole.Text, text_col)
                pal.setColor(QPalette.ColorRole.Button, window_col)
                pal.setColor(QPalette.ColorRole.ButtonText, text_col)
                pal.setColor(QPalette.ColorRole.Highlight, accent_col)
                pal.setColor(QPalette.ColorRole.HighlightedText, accent_txt)
                # Item views with alternating rows take this role directly, and
                # Qt's default is a fixed light grey in every scheme.
                pal.setColor(QPalette.ColorRole.AlternateBase, base_col)
                app.setPalette(pal)

            matched_icon_theme = select_matching_icon_theme(
                QIcon.themeName(), is_dark=is_dark
            )
            if matched_icon_theme:
                QIcon.setThemeName(matched_icon_theme)

            pal_colors = get_delegate_palette(resolved_key)

            self.tree_view.set_chevron_color(pal_colors["accent"])
            self.tree_view.set_empty_state_colors(
                pal_colors["text_secondary"], pal_colors["text_dim"]
            )

            tree_pal = self.tree_view.palette()
            tree_pal.setColor(QPalette.ColorRole.Base, pal_colors["bg_base"])
            tree_pal.setColor(QPalette.ColorRole.Window, pal_colors["bg_base"])
            tree_pal.setColor(QPalette.ColorRole.Text, pal_colors["text_main"])
            # The view owns an explicit palette, so the alternate-row colour
            # must be themed here as well; the inherited default is a fixed
            # light grey that Qt never derives from the active palette.
            tree_pal.setColor(QPalette.ColorRole.AlternateBase, pal_colors["bg_card"])
            self.tree_view.setPalette(tree_pal)
            self.tree_view.viewport().setPalette(tree_pal)

            self.tree_delegate.set_theme(resolved_key)
            self.sidebar.set_theme(resolved_key)
            self.inspector_panel.set_theme(resolved_key)

            self.tree_view.viewport().update()
            self.sidebar.viewport().update()
            self.settings.setValue("theme", theme_choice)
        finally:
            self._theme_state.applying = False

    def _set_theme_choice(self, theme_choice: str) -> None:
        """Apply one supported persistent appearance preference."""
        if theme_choice not in {"auto", "dark", "light"}:
            return
        cancel_fallback = getattr(self, "_cancel_async_gsettings_fallback", None)
        if callable(cancel_fallback):
            cancel_fallback()
        if theme_choice == "auto":
            _set_gsettings_dark_mode_cache(None)
        self.current_theme = theme_choice
        self._apply_theme(theme_choice)
        if theme_choice == "auto":
            start_async = getattr(self, "_start_async_portal_sync", None)
            if callable(start_async):
                start_async()

    def _sync_motion_preference(
        self,
        *,
        reduced_motion: bool | None = None,
        allow_blocking: bool = True,
    ) -> None:
        """Apply system reduced-motion preference to tree expand/collapse animation."""
        if reduced_motion is None:
            reduced_motion = is_system_reduced_motion(allow_blocking=allow_blocking)
        state = getattr(self, "_theme_state", None)
        if state is not None:
            state.reduced_motion = bool(reduced_motion)
        tree_view = getattr(self, "tree_view", None)
        set_animated = getattr(tree_view, "setAnimated", None)
        if callable(set_animated):
            set_animated(not reduced_motion)

    def _on_system_motion_changed(self, *args) -> None:
        """Update tree animation when Qt reports a motion-preference change."""
        from dendro.ui.styles import _debug_log

        _set_portal_reduced_motion_cache(None)
        pref = args[0] if args else None
        _debug_log(f"Qt motionPreferenceChanged signal received: {pref}")
        reduced_enum = getattr(
            getattr(Qt, "MotionPreference", None),
            "ReducedMotion",
            None,
        )
        no_pref_enum = getattr(
            getattr(Qt, "MotionPreference", None),
            "NoPreference",
            None,
        )
        if reduced_enum is not None and pref == reduced_enum:
            self._sync_motion_preference(reduced_motion=True)
        elif no_pref_enum is not None and pref == no_pref_enum:
            self._sync_motion_preference(reduced_motion=False)
        else:
            self._sync_motion_preference(allow_blocking=False)

    def _on_system_contrast_changed(self, *args) -> None:
        """Queue a theme refresh when the system high-contrast preference flips."""
        from dendro.ui.styles import _debug_log

        _set_portal_contrast_cache(None)
        pref = args[0] if args else None
        _debug_log(f"Qt contrastPreferenceChanged signal received: {pref}")
        self._queue_contrast_theme_update()

    def _on_system_color_scheme_changed(self, *args):
        """Queue an automatic-theme refresh after Qt applies its new palette."""
        from dendro.ui.styles import _debug_log

        _set_portal_color_scheme_cache(None)
        _set_gsettings_dark_mode_cache(None)
        cancel_fallback = getattr(self, "_cancel_async_gsettings_fallback", None)
        if callable(cancel_fallback):
            cancel_fallback()
        scheme = args[0] if args else Qt.ColorScheme.Unknown
        _debug_log(f"Qt colorSchemeChanged signal received: {scheme}")
        # The portal's SettingChanged value is authoritative when it arrives.
        # This fallback deliberately re-resolves after the event loop rather
        # than letting a potentially stale Qt signal overwrite that value.
        self._queue_system_theme_update()
