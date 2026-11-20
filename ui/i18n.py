"""Apply Dendro's locale at runtime and retranslate live widgets.

Every message in Dendro goes through :func:`core.i18n.translate`, imported as
``tr``.  A module-level function was chosen over Qt's ``self.tr`` because the
majority of Dendro's vocabulary does not live in a widget method: category
labels, sidebar sections, and classification evidence are module-level tables,
and status text is assembled with f-strings.  Qt's per-object ``tr`` cannot see
any of those, so one function-based lookup keeps a single mechanism instead of
two parallel ones.

The cost of that choice is that Qt cannot retranslate any of it: ``tr`` is an
ordinary function, so Qt has no record of which widget took which string.  A
language change therefore has to repaint explicitly, and the interesting
question is *which* widgets get repainted.

The answer is: every live one.  An opt-in registry of objects that happened to
exist at startup is the obvious design and the wrong one -- a dialog created
after the switch, or a widget whose author forgot to register it, silently
stays in the old language.  Instead :meth:`LocaleManager.apply` walks
``QApplication.allWidgets()``, finds objects that expose a ``retranslate``
hook, and calls it.  Membership is decided by the widget being alive rather
than by an earlier registration, so there is no registration to forget.
"""

from __future__ import annotations

from PyQt6 import sip
from PyQt6.QtWidgets import QApplication, QWidget

from core.i18n import (
    active_locale as _source_locale,
)
from core.i18n import (
    clear_cache,
    get_translator,
    set_active_locale,
)


class LocaleManager:
    """Owns the active locale and repaints every live widget when it changes."""

    #: Identity of widgets whose ``retranslate`` is currently running, so a
    #: hook that triggers another language change cannot re-enter itself.
    _busy: set[int]

    def __init__(self) -> None:
        self._busy = set()

    @property
    def locale(self) -> str:
        """The active locale.

        ``core.i18n`` owns the value, because ``translate`` reads it on every
        call. Keeping a second copy here meant two sources of truth that could
        drift whenever code called ``set_active_locale`` directly.
        """

        return _source_locale()

    def apply(self, locale: str) -> None:
        """Switch to ``locale`` and retranslate every live widget.

        Ordering is Qt's own widget order, which is creation order: parents are
        created before their children. A parent that rebuilds text therefore
        gets its say before the children it owns, and a child that recomputes
        from the same source ends up consistent either way.
        """

        if locale == _source_locale():
            return
        # Drop cached catalogs so a language switched in Preferences is read
        # from disk rather than served from the previous catalog.
        clear_cache()
        set_active_locale(locale)
        self.retranslate_all()

    def retranslate_all(self) -> None:
        """Re-run the translation pass on every live widget that has one.

        Two guards matter here, both learned from a crash rather than guessed:

        A hook may change the language again (the Preferences dialog sits on
        the code path that started the sweep), so a widget already being
        retranslated is skipped. Without that, a hook that ends up calling
        ``install`` re-enters itself and recurses until the stack runs out.

        ``allWidgets`` also lists widgets that have had ``deleteLater`` called
        and whose C++ object is already gone; calling a bound method on one
        raises ``RuntimeError`` at best and faults at worst, depending on the Qt
        build. ``sip.isdeleted`` is the supported way to ask.
        """

        for widget in self.live_widgets():
            if sip.isdeleted(widget) or id(widget) in self._busy:
                continue
            self._busy.add(id(widget))
            try:
                # ``getattr`` is inside the guard on purpose: on a wrapper whose
                # C++ object is gone, even looking the attribute up raises
                # RuntimeError, and that happens before ``callable`` is reached.
                hook = getattr(widget, "retranslate", None)
                if callable(hook):
                    hook()
            except RuntimeError:
                # Torn down between the guard and the call.
                continue
            finally:
                self._busy.discard(id(widget))

    def live_widgets(self) -> list[QWidget]:
        """Return the application's widgets, in Qt's own (creation) order.

        The list is *not* filtered: Qt documents that ``allWidgets`` may still
        hold a widget whose C++ object is gone, so the caller checks each entry
        rather than trusting the list.
        """

        application = QApplication.instance()
        if application is None:
            return []
        return list(application.allWidgets())


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
