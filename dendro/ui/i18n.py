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

import os
import sys

from PyQt6 import sip
from PyQt6.QtCore import QLocale, QTimer
from PyQt6.QtWidgets import QApplication, QWidget

from dendro.core.i18n import (
    active_locale as _source_locale,
)
from dendro.core.i18n import (
    clear_cache,
    get_translator,
    qt_locale,
    set_active_locale,
)

_TRACE_PATH = os.environ.get("DENDRO_I18N_TRACE")


def _note_failure(widget: QWidget, error: BaseException) -> None:
    """Record a failing hook where a crash log or a test can find it."""

    line = f"error {type(widget).__module__}.{type(widget).__name__}: {error}"
    sys.stderr.write(f"dendro.i18n: retranslate failed for {line}\n")
    _trace_line(line)


def _trace(widget: QWidget, stage: str) -> None:
    """Append ``stage widget`` to the trace file, unbuffered, when asked.

    A fault inside a hook takes the process with it, so the only record of
    which widget did it is something written before the call and flushed. Off
    unless ``DENDRO_I18N_TRACE`` names a file, so this costs nothing in normal
    use. Used by CI to turn an otherwise anonymous exit 139 into a class name.
    """

    if not _TRACE_PATH:
        return
    _trace_line(f"{stage} {type(widget).__module__}.{type(widget).__name__}")


def _trace_line(line: str) -> None:
    """Append one line to the trace file and close it immediately.

    Closed on every write so a fault cannot lose the last line to a buffer;
    that is the whole point of the file.
    """

    if not _TRACE_PATH:
        return
    try:
        fd = os.open(_TRACE_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except OSError:
        pass


class LocaleManager:
    """Owns the active locale and repaints every live widget when it changes."""

    #: Identity of widgets whose ``retranslate`` is currently running, so a
    #: hook that triggers another language change cannot re-enter itself.
    _busy: set[int]
    #: Whether a sweep is already waiting for the next event-loop turn.
    _sweep_queued: bool
    #: The last ``(widget class, exception)`` a hook raised, or ``None``. Tests
    #: assert this stays ``None``; nothing else reads it.
    last_failure: tuple[str, BaseException] | None

    def __init__(self) -> None:
        self._busy = set()
        self._sweep_queued = False
        self.last_failure = None

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

        QLocale.setDefault(qt_locale(locale))
        if locale == _source_locale():
            return
        # Drop cached catalogs so a language switched in Preferences is read
        # from disk rather than served from the previous catalog.
        clear_cache()
        set_active_locale(locale)
        self._schedule_sweep()

    def _schedule_sweep(self) -> None:
        """Run the sweep on the next event-loop turn, once, and never inline.

        ``apply`` is normally reached from a widget's own signal handler - the
        Preferences combo box emits ``currentIndexChanged``, which the window
        turns into a language change. Retranslating the whole widget tree from
        inside that handler means every hook runs while Qt is still delivering
        a signal, and some of those widgets are the ones the signal came from.
        That is a re-entrancy problem no amount of per-hook guarding fixes: it
        crashed on two different Fedora Qt builds, on different tests, and never
        on the third. Deferring costs one event-loop turn and removes the whole
        class of failure.

        The timer is static and the callback is the manager's, not a widget's.
        The manager is a module-level singleton that outlives every widget, so
        unlike a widget-owned callback this cannot be invoked on a dead object.
        """

        if self._sweep_queued:
            return
        app = QApplication.instance()
        if app is None or app.closingDown():
            return
        self._sweep_queued = True
        QTimer.singleShot(0, self._run_queued_sweep)

    def _run_queued_sweep(self) -> None:
        self._sweep_queued = False
        app = QApplication.instance()
        if app is None or app.closingDown():
            return
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
            _trace(widget, "enter")
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
            except Exception as error:  # one bad hook, not the whole app
                # A hook that raises must not stop the rest of the tree from
                # being translated, and it must not surface as an exception
                # inside a Qt slot, where it is reported against whatever the
                # event loop was doing instead of against this widget. Record it
                # and carry on: a half-translated window is worse than a logged
                # widget name, and the name is what makes it fixable.
                # ``repr`` of the bound method, not just the class: the same
                # class is used all over the UI, and the method that raised is
                # what identifies the widget's owner.
                self.last_failure = (
                    f"{type(widget).__name__}.{getattr(hook, '__qualname__', '?')}",
                    error,
                )
                _note_failure(widget, error)
                continue
            finally:
                self._busy.discard(id(widget))
                _trace(widget, "leave")

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


def install(locale: str) -> None:
    """Activate ``locale`` for the current process."""

    _manager.apply(locale)


def active_locale() -> str:
    """Return the locale currently in use."""

    return _manager.locale


def plural_boundary(locale: str) -> int:
    """Smallest count that takes the plural form in ``locale``."""

    return get_translator(locale).plural_boundary
