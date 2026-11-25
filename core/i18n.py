"""Locale resolution and message-catalog lookup for Dendro.

Dendro ships its own message catalogs instead of relying on Qt Linguist ``.qm``
files.  Three constraints drove that decision:

* ``.qm`` needs ``lrelease``, which is a build-only tool in the Qt linguist
  package.  Requiring it would add a toolchain dependency to the RPM build and
  to every contributor's machine.
* English must stay the source of truth in the code.  A catalog layout that
  keeps the English text at the call site stays readable, greppable, and needs
  no extraction step to keep working.
* The catalog must be testable without a GUI, so this module deliberately has
  no Qt import.  :mod:`ui.i18n` layers Qt's translator hook on top of it.

A catalog is a plain ``.json`` file mapping the English source text to a
translation.  Keys are matched exactly, including punctuation, so a translator
can see precisely which string they are changing.

Lookup order for :func:`translate`: the requested locale, then its base
language, then English.  A missing key falls back to the source string rather
than raising, so an incomplete catalog degrades rather than breaking the UI.

Plural selection is split out of the message text on purpose.  Languages in the
supported set need different plural boundaries, and English's "one vs many" rule
is wrong for some of them, so :func:`plural_form` reads the boundary from the
catalog rather than hard-coding English grammar.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final

#: Locales Dendro ships a catalog for.  Order is presentation order in
#: Preferences, so the most widely spoken locales come first.
SUPPORTED_LOCALES: Final[tuple[str, ...]] = (
    "en",
    "es",
    "pt_BR",
    "pt_PT",
    "fr",
    "de",
    "hi",
    "ru",
)

#: Sentinel meaning "follow the desktop's locale" rather than pinning one.
SYSTEM_LOCALE: Final[str] = "system"

#: Endonym for each locale, used in the Preferences selector so a speaker can
#: find their language without reading English.  Accents and native scripts are
#: intentional: an endonym spelled in ASCII is the wrong word.
LOCALE_ENDONYMS: Final[dict[str, str]] = {
    "en": "English",
    "es": "Español",
    "pt_BR": "Português (Brasil)",
    "pt_PT": "Português (Portugal)",
    "fr": "Français",
    "de": "Deutsch",
    "hi": "हिन्दी",
    "ru": "Русский",
}

#: Human-readable English name, used where a locale must be described in the
#: surrounding (translated) sentence rather than listed.
LOCALE_NAMES: Final[dict[str, str]] = {
    "en": "English",
    "es": "Spanish",
    "pt_BR": "Brazilian Portuguese",
    "pt_PT": "European Portuguese",
    "fr": "French",
    "de": "German",
    "hi": "Hindi",
    "ru": "Russian",
}

_CATALOG_ENV_VAR: Final[str] = "DENDRO_LOCALE_DIR"


class CatalogError(RuntimeError):
    """A shipped catalog is malformed or missing a required key."""


@dataclass(frozen=True, slots=True)
class LocaleResolution:
    """The result of mapping a free-form locale name onto a shipped catalog."""

    requested: str
    #: Locale actually used for lookup, or ``"en"`` when nothing matched.
    locale: str
    #: True when the request could not be honoured and English was used.
    fell_back: bool


def catalog_directory() -> Path:
    """Return the directory holding the shipped ``.json`` catalogs.

    Resolution order lets tests and packagers relocate the catalogs without
    touching code: the ``DENDRO_LOCALE_DIR`` environment variable, then the
    copy next to the installed package, then the source checkout.
    """

    override = os.environ.get(_CATALOG_ENV_VAR)
    if override:
        return Path(override)

    package_root = Path(__file__).resolve().parent
    # Each distribution form keeps the catalogs somewhere different:
    #   source checkout : <repo>/data/locales
    #   Flatpak         : /app/share/dendro/locales  (beside the copied package)
    #   AppImage        : <AppDir>/usr/app/locales    (beside the copied package)
    #   RPM             : /usr/share/dendro/locales
    # Checking the sibling directory before the absolute path keeps the
    # self-contained bundles from reading the host's system-wide catalogs.
    # ``data-files`` in pyproject.toml are installed relative to the
    # interpreter's prefix, which is where a ``pip install`` puts them.
    prefix_share = Path(sys.prefix) / "share" / "dendro" / "locales"
    candidates = (
        # Bundle layouts: catalogs sit directly beside or in the package dir.
        package_root / "locales",
        package_root.parent / "locales",
        # Source checkout keeps them under the repository's data directory.
        package_root.parent / "data" / "locales",
        package_root / "data" / "locales",
        # pip install into a virtualenv or --prefix.
        prefix_share,
        # System installation.
        Path("/usr/share/dendro/locales"),
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    # Return the source-checkout path so error messages name a real location.
    return candidates[0]


def normalise_locale(raw: str) -> str:
    """Reduce an arbitrary locale tag to a comparable ``ll`` or ``ll_CC`` form.

    Handles the shapes that reach us in practice: ``POSIX`` and ``C`` for an
    unset locale, ``de_DE.UTF-8@euro`` from ``LANG``, and ``pt-BR`` from
    command-line tools that prefer a hyphen.
    """

    value = raw.strip()
    if not value or value.upper() in {"C", "POSIX"}:
        return ""
    # Strip encoding and modifier suffixes, then normalise the separator.
    value = value.split(".", 1)[0].split("@", 1)[0].replace("-", "_")
    parts = [part for part in value.split("_") if part]
    if not parts:
        return ""
    language = parts[0].lower()
    if len(parts) == 1:
        return language
    return f"{language}_{parts[1].upper()}"


def resolve_locale(requested: str, system_locale: str = "") -> LocaleResolution:
    """Map a user preference onto a shipped catalog.

    ``requested`` may be :data:`SYSTEM_LOCALE`, an explicit shipped locale, or
    an arbitrary tag.  An unmatched tag falls back to English through the base
    language, so ``es_MX`` resolves to the ``es`` catalog.
    """

    if not requested or requested == SYSTEM_LOCALE:
        candidate = normalise_locale(system_locale)
        if candidate in SUPPORTED_LOCALES:
            return LocaleResolution(SYSTEM_LOCALE, candidate, False)
        if candidate and candidate.split("_", 1)[0] in SUPPORTED_LOCALES:
            return LocaleResolution(SYSTEM_LOCALE, candidate.split("_", 1)[0], False)
        return LocaleResolution(SYSTEM_LOCALE, "en", bool(candidate))

    candidate = normalise_locale(requested)
    if candidate in SUPPORTED_LOCALES:
        return LocaleResolution(requested, candidate, False)
    # A specific form such as es_MX is served by its base language.
    base = candidate.split("_", 1)[0]
    if base in SUPPORTED_LOCALES:
        return LocaleResolution(requested, base, False)
    return LocaleResolution(requested, "en", True)


@cache
def _load_catalog(locale: str, directory: str) -> dict[str, Any]:
    path = Path(directory) / f"{locale}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if locale == "en":
            # English is the source language and needs no catalog.
            return {}
        raise CatalogError(f"missing translation catalog: {path}") from None
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid JSON in {path}: {exc}") from None

    if not isinstance(payload, dict):
        raise CatalogError(f"{path} must contain a JSON object")
    messages = payload.get("messages")
    if not isinstance(messages, dict):
        raise CatalogError(f"{path} must contain a 'messages' object")
    return payload


def load_catalog(locale: str) -> dict[str, Any]:
    """Return the parsed catalog for ``locale`` (cached per process)."""

    return _load_catalog(locale, str(catalog_directory()))


def catalog_metadata(locale: str) -> dict[str, Any]:
    """Return catalog metadata such as translator notes, without the messages."""

    payload = load_catalog(locale)
    return {key: value for key, value in payload.items() if key != "messages"}


def messages_for(locale: str) -> dict[str, str]:
    """Return the flat message map for ``locale``, empty for English."""

    payload = load_catalog(locale)
    messages = payload.get("messages", {})
    return {key: value for key, value in messages.items() if isinstance(value, str)}


def cldr_plural_category(locale: str, count: int) -> str:
    """Return the CLDR cardinal plural category for ``locale`` and ``count``."""
    n = abs(int(count))
    normalized = normalise_locale(locale)
    lang = normalized.split("_", 1)[0]
    if lang == "ru":
        mod10, mod100 = n % 10, n % 100
        if mod10 == 1 and mod100 != 11:
            return "one"
        if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
            return "few"
        if mod10 == 0 or (5 <= mod10 <= 9) or (11 <= mod100 <= 14):
            return "many"
        return "other"
    if lang in {"fr", "hi"} or normalized == "pt_BR":
        return "one" if n in (0, 1) else "other"
    return "one" if n == 1 else "other"


class Translator:
    """Resolve English source text to a translated string.

    Instances are intentionally cheap; :func:`get_translator` caches one per
    process.  Callers use the module-level :func:`translate` in application
    code, and instantiate this directly only when they need a specific locale
    (tests, or rendering a preview of another language).
    """

    __slots__ = (
        "_locale",
        "_messages",
        "_plural_boundary",
        "_plurals",
        "_reverse_messages",
    )

    def __init__(self, locale: str) -> None:
        self._locale = locale
        self._messages = messages_for(locale)
        meta = catalog_metadata(locale)
        raw_boundary = meta.get("plural_boundary", 1)
        self._plural_boundary = raw_boundary if isinstance(raw_boundary, int) else 1
        raw_plurals = meta.get("plurals")
        self._plurals: dict[str, dict[str, str]] = {}
        if isinstance(raw_plurals, dict):
            for key, forms in raw_plurals.items():
                if isinstance(key, str) and isinstance(forms, dict):
                    self._plurals[key] = {
                        form: text
                        for form, text in forms.items()
                        if isinstance(form, str) and isinstance(text, str)
                    }
        self._reverse_messages: dict[str, str] | None = None

    @property
    def locale(self) -> str:
        return self._locale

    @property
    def plural_boundary(self) -> int:
        """Smallest count that takes the plural form in this language."""

        return self._plural_boundary

    def gettext(self, source: str) -> str:
        """Return the translation of ``source``, or ``source`` itself."""

        try:
            return self._messages.get(source, source)
        except TypeError:
            # Unhashable input cannot be a catalog key; never fail a paint pass.
            return source

    def source_for(self, translated: str) -> str:
        """Recover the English catalog key for ``translated`` when uniquely mapped."""
        if not translated or self._locale == "en":
            return translated
        if self._reverse_messages is None:
            reverse: dict[str, str] = {}
            ambiguous: set[str] = set()
            for source_key, target_text in self._messages.items():
                if target_text in ambiguous:
                    continue
                if target_text in reverse and reverse[target_text] != source_key:
                    reverse.pop(target_text, None)
                    ambiguous.add(target_text)
                    continue
                reverse[target_text] = source_key
            self._reverse_messages = reverse
        try:
            return self._reverse_messages.get(translated, translated)
        except TypeError:
            return translated

    def ngettext(self, source: str, plural: str, count: int) -> str:
        """Return the correctly inflected form for ``count``.

        ``source`` is the English singular and ``plural`` the English plural;
        both are catalog keys.  Languages with multi-form CLDR rules (such as
        Russian one/few/many) supply a ``plurals`` entry in their catalog;
        single-boundary languages pick between ``source`` and ``plural``.
        """
        forms = self._plurals.get(source) or self._plurals.get(plural)
        if forms:
            category = cldr_plural_category(self._locale, count)
            translated = forms.get(category) or forms.get("other") or forms.get("one")
            if translated is not None:
                if "{count}" in translated:
                    try:
                        return translated.replace("{count}", str(count))
                    except (ValueError, IndexError):
                        return translated
                return translated

        key = source if count < self._plural_boundary else plural
        try:
            translated = self._messages.get(key)
        except TypeError:
            return key
        if translated is None:
            return key
        # Catalogs may embed a ``{count}`` placeholder; format defensively so a
        # stray brace in a translation cannot raise during a paint pass.
        if "{count}" in translated:
            try:
                return translated.replace("{count}", str(count))
            except (ValueError, IndexError):
                return translated
        return translated


_translators: dict[str, Translator] = {}
_active_locale = "en"


def get_translator(locale: str) -> Translator:
    """Return the cached :class:`Translator` for ``locale``."""

    translator = _translators.get(locale)
    if translator is None:
        translator = Translator(locale)
        _translators[locale] = translator
    return translator


def set_active_locale(locale: str) -> None:
    """Record the locale used by the module-level convenience functions."""

    global _active_locale
    _active_locale = locale


def active_locale() -> str:
    """Return the locale currently driving :func:`translate`."""

    return _active_locale


def translate(source: str) -> str:
    """Translate ``source`` using the active locale."""

    if _active_locale == "en":
        return source
    return get_translator(_active_locale).gettext(source)


def reverse_translate(text: str, locale: str | None = None) -> str:
    """Recover the English source key for ``text`` in ``locale`` (or active)."""
    target_locale = _active_locale if locale is None else locale
    if not text or target_locale == "en":
        return text
    return get_translator(target_locale).source_for(text)


def translate_plural(source: str, plural: str, count: int) -> str:
    """Translate a count-dependent message using the active locale."""

    if _active_locale == "en":
        return source if count == 1 else plural
    return get_translator(_active_locale).ngettext(source, plural, count)


#: Short name used at call sites: ``from core.i18n import tr``.
tr = translate

#: Plural counterpart, used for count-dependent messages.
tr_plural = translate_plural


def clear_cache() -> None:
    """Drop cached catalogs and translators.

    Tests and the Preferences language switch both need this: without it a
    changed catalog file would stay stale for the life of the process.
    """

    global _translators
    _load_catalog.cache_clear()
    _translators = {}


def iter_shipped_locales() -> tuple[str, ...]:
    """Return the locales whose catalog files are actually present."""

    directory = catalog_directory()
    return tuple(
        locale
        for locale in SUPPORTED_LOCALES
        if locale == "en" or (directory / f"{locale}.json").is_file()
    )
