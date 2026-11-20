"""Apply Dendro's locale at runtime and retranslate live widgets.

Every message in Dendro goes through :func:`core.i18n.translate`, imported as
``tr``.  A module-level function was chosen over Qt's ``self.tr`` because the
majority of Dendro's vocabulary does not live in a widget method: category
labels, sidebar sections, and classification evidence are module-level tables,
and status text is assembled with f-strings.  Qt's per-object ``tr`` cannot see
any of those, so one function-based lookup keeps a single mechanism instead of
two parallel ones.

The cost of that choice is that Qt does not notify widgets when the language
changes, so :class:`LocaleManager` drives retranslation explicitly.  Widgets opt
in by exposing a ``retranslate`` method that re-runs the code which set their
text.
"""

from __future__ import annotations

from PyQt6.QtCore import QCoreApplication, QEvent

from core.i18n import (
    active_locale as _source_locale,
)
from core.i18n import (
    clear_cache,
    get_translator,
    set_active_locale,
)


class LocaleManager:
    """Owns the active locale and repaints widgets when it changes."""

    def __init__(self) -> None:
        self._watchers: list[object] = []

    @property
    def locale(self) -> str:
        """The active locale.

        ``core.i18n`` owns the value, because ``translate`` reads it on every
        call. Keeping a second copy here meant two sources of truth that could
        drift whenever code called ``set_active_locale`` directly.
        """

        return _source_locale()

    def register(self, widget: object) -> None:
        """Add ``widget`` to the set retranslated on a language change."""

        if widget not in self._watchers:
            self._watchers.append(widget)

    def apply(self, locale: str) -> None:
        """Switch to ``locale`` and retranslate everything registered.

        Widgets are retranslated in registration order, which is construction
        order: parents register before their children, so a parent that rebuilds
        text also refreshes the parts it owns.
        """

        if locale == _source_locale():
            return
        # Drop cached catalogs so a language switched in Preferences is read
        # from disk rather than served from the previous catalog.
        clear_cache()
        set_active_locale(locale)
        self.retranslate_all()

    def retranslate_all(self) -> None:
        """Re-run every registered widget's translation pass."""

        for widget in list(self._watchers):
            hook = getattr(widget, "retranslate", None)
            if callable(hook):
                try:
                    hook()
                except RuntimeError:
                    # A widget destroyed since registration is not an error;
                    # the C++ object is simply gone.
                    continue

    def send_language_change(self, widget: object) -> None:
        """Ask Qt to deliver a LanguageChange event to ``widget``.

        Qt widgets built from ``.ui`` files or using ``self.tr`` rely on this
        event rather than a Python hook, so sending it keeps the two mechanisms
        consistent for any widget that mixes both.
        """

        target = getattr(widget, "changeEvent", None)
        if callable(target) and QCoreApplication.instance() is not None:
            event = QEvent(QEvent.Type.LanguageChange)
            QCoreApplication.sendEvent(widget, event)  # type: ignore[arg-type]


_manager = LocaleManager()


def locale_manager() -> LocaleManager:
    """Return the process-wide :class:`LocaleManager`."""

    return _manager


def install(locale: str) -> None:
    """Activate ``locale`` for the current process."""

    _manager.apply(locale)


def active_locale() -> str:
    """Return the locale currently in use."""

    return _manager.locale


def plural_boundary(locale: str) -> int:
    """Smallest count that takes the plural form in ``locale``."""

    return get_translator(locale).plural_boundary
