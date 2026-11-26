"""Which words crossing the DNF5 daemon boundary are Dendro's to translate.

Most of what the daemon returns is *data*: package names, versions,
repository ids, CVE numbers, advisory ids.  Translating any of it would be
wrong -- a translated package name is a package that does not exist.

Two things it returns are not data:

*Severity levels.*  ``Advisory.list`` returns one of a closed set of English
words (``critical``, ``important``, ``moderate``, ``low``, ``none``, plus the
non-security ``bugfix``/``enhancement``/``newpackage``).  They are enumerators,
not prose: they choose a colour, they sort the list, and they appear inside
sentences Dendro builds.  Left raw, a German summary reads "3 Hinweise (1
important, 1 low)".

*Error messages.*  These are prose, and the daemon can emit them in the user's
language -- but only if the session asked for one.  ``SessionManager.open_session``
takes a ``locale`` option, and ``dnf5daemon-server`` runs each method under
``newlocale(LC_ALL_MASK, locale, 0)`` so libdnf5's own translatable strings come
back translated.  Two details make that safe to pass rather than risky:

* ``locale`` must be a glibc locale name (``de_DE.UTF-8``), not a BCP-47 tag.
  ``newlocale`` rejects anything else.
* A rejected name does not fail the call.  ``ThreadsManager::handle_method``
  catches the failure and runs the method under the locale already in force,
  so a wrong guess degrades to today's English instead of breaking the
  operation.

Dendro's own catalog ids and the daemon's glibc locale names are therefore kept
apart on purpose: the two vocabularies are close enough to be confused and
different enough that the confusion is a silent bug.  That is why
``DAEMON_LOCALES`` is a plain mapping between them rather than a loop that adds
a suffix.

One limit is honest rather than fixable here: whether a given host has the
matching glibc locale generated is outside Dendro's control, and a host without
it gets the daemon's fallback.  What this module guarantees is that the session
asks for the right locale in the right format, that the value tracks a live
language switch, and that the names it sends are ones ``newlocale`` can accept.
"""

from __future__ import annotations

from typing import Final

from dendro.core.i18n import SUPPORTED_LOCALES, active_locale, tr

#: Severity enumerators the daemon returns, keyed by their casefold form.
#:
#: Every value here is an interface message: it is looked up in the catalogues
#: and shown to the user.  A severity the daemon adds later falls through
#: unchanged rather than disappearing, which is the honest failure -- an
#: unrecognised word is still readable, an empty cell is not.
SEVERITY_MESSAGES: Final[dict[str, str]] = {
    "critical": "Critical",
    "important": "Important",
    "high": "High",
    "moderate": "Moderate",
    "medium": "Medium",
    "low": "Low",
    "none": "None",
    "bugfix": "Bugfix",
    "enhancement": "Enhancement",
    "newpackage": "New package",
}

#: The glibc locale name to ask the daemon for, per Dendro catalog.
#:
#: Only locales that have a translation domain of their own are listed.  A
#: glibc locale that does not exist on the host would be rejected by
#: ``newlocale`` and silently ignored by the daemon, so passing it would be
#: noise rather than a benefit.
DAEMON_LOCALES: Final[dict[str, str]] = {
    "de": "de_DE.UTF-8",
    "es": "es_ES.UTF-8",
    "fr": "fr_FR.UTF-8",
    "hi": "hi_IN.UTF-8",
    "pt_BR": "pt_BR.UTF-8",
    "pt_PT": "pt_PT.UTF-8",
    "ru": "ru_RU.UTF-8",
}

#: What ``open_session`` gets when Dendro has no catalog for the active locale.
#:
#: ``C`` is always present and always English, so the session still requests a
#: valid locale instead of leaving the daemon to inherit the server's own.
_NO_CATALOG_LOCALE: Final[str] = "C.UTF-8"


def daemon_locale(catalog_locale: str | None = None) -> str:
    """Return the glibc locale name to request for ``catalog_locale``.

    ``catalog_locale`` defaults to the locale Dendro is currently showing, so a
    session opened after a language switch asks for the new language.  Unknown
    or unsupported values fall back to ``C.UTF-8`` rather than to a guess: the
    daemon would ignore a bad name, but the session should not be sending one.
    """

    locale = catalog_locale if catalog_locale is not None else active_locale()
    if locale in DAEMON_LOCALES:
        return DAEMON_LOCALES[locale]
    # A language tag with no catalog of its own ("en_GB", "C", "POSIX") still
    # names a language the daemon may translate into; only the region is lost.
    language = locale.split("_", 1)[0].split(".", 1)[0]
    if language == "en":
        return "C.UTF-8"
    return DAEMON_LOCALES.get(language, _NO_CATALOG_LOCALE)


def severity_message(severity: str) -> str:
    """Return the translated label for a daemon severity.

    The daemon's word is an enumerator, so it is looked up here rather than
    displayed raw. A severity Dendro has not seen yet is returned exactly as the
    daemon sent it: an unknown word is still readable, and dropping it would lose
    information the user can act on.

    The label lives in the interface catalogues, and both the literal ids and
    the unknown-word fall-through are written where the extractor can see them.
    """

    label = SEVERITY_MESSAGES.get(severity.strip().casefold())
    return tr(label) if label else severity.strip() or tr("Unspecified")


def is_translated_locale(locale: str) -> bool:
    """True when Dendro ships a catalog for ``locale``."""

    return locale in SUPPORTED_LOCALES
