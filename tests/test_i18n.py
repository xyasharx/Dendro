"""Translation catalog integrity and locale-resolution behaviour.

These tests guard the two failure modes that are easy to ship by accident:
a catalog that has drifted behind the code, and a language switch that silently
stops applying. Both are invisible in English, so they are asserted here rather
than left to manual review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.i18n import (
    LOCALE_ENDONYMS,
    SUPPORTED_LOCALES,
    SYSTEM_LOCALE,
    CatalogError,
    Translator,
    active_locale,
    catalog_directory,
    clear_cache,
    iter_shipped_locales,
    load_catalog,
    messages_for,
    normalise_locale,
    resolve_locale,
    set_active_locale,
    translate,
    translate_plural,
)

PROJECT_ROOT = Path(__file__).parents[1]
LOCALE_DIR = PROJECT_ROOT / "data" / "locales"

#: The languages this feature is required to ship, with regional variants.
REQUIRED_LOCALES = ("es", "pt_BR", "pt_PT", "fr", "hi", "de", "ru")


@pytest.fixture(autouse=True)
def _reset_locale():
    """Restore the default locale after each test that switches it."""

    yield
    clear_cache()
    set_active_locale("en")


def _source_strings() -> set[str]:
    """Collect the translatable strings the extractor finds in the code."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dendro_i18n_tool", PROJECT_ROOT / "tools" / "dendro-i18n.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return set(module.collect())


def test_every_required_language_ships_a_catalog() -> None:
    for locale in REQUIRED_LOCALES:
        assert locale in SUPPORTED_LOCALES, f"{locale} is not advertised"
        assert (LOCALE_DIR / f"{locale}.json").is_file(), f"{locale}.json is missing"


def test_both_portuguese_variants_are_distinct_catalogs() -> None:
    """Brazilian and European Portuguese must not be one aliased catalog."""

    brazilian = messages_for("pt_BR")
    european = messages_for("pt_PT")
    assert brazilian and european
    differences = [key for key in brazilian if brazilian[key] != european.get(key)]
    # The variants genuinely differ across most of the interface; a low count
    # would mean one catalog was cloned from the other.
    assert len(differences) > len(brazilian) // 3, (
        f"pt_BR and pt_PT share {len(brazilian) - len(differences)} of "
        f"{len(brazilian)} strings; they should be distinct translations"
    )


@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_catalog_covers_every_source_string(locale: str) -> None:
    """No shipped catalog may fall behind the code."""

    source = _source_strings()
    messages = messages_for(locale)
    missing = sorted(source - set(messages))
    assert not missing, f"{locale} is missing {len(missing)}: {missing[:5]}"


@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_catalog_has_no_stale_or_empty_entries(locale: str) -> None:
    source = _source_strings()
    messages = messages_for(locale)
    extra = sorted(set(messages) - source)
    empty = sorted(key for key, value in messages.items() if not value.strip())
    assert not extra, f"{locale} has stale keys: {extra[:5]}"
    assert not empty, f"{locale} has untranslated keys: {empty[:5]}"


@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_no_translation_merely_repeats_the_english_source(locale: str) -> None:
    """A value identical to its key is a missed translation, not a translation."""

    source = _source_strings()
    messages = messages_for(locale)
    # Proper nouns and format tokens are legitimately unchanged across
    # languages, so they are excluded rather than asserted against.
    # Entries that are correctly identical in the listed language, so a
    # matching value is precision rather than an omission:
    #   product/project names   Flatpak, Tcl/Tk, .NET & Mono, R & CRAN, TeX
    #   true cognates           French "Classification"/"Description"/
    #                           "Architecture"/"Type", German "Details"/
    #                           "Installation"/"Repository"/"Status",
    #                           "Scripts" in fr/es/pt, "Arch:" in de/es/pt,
    #                           "Repo"/"Repos" in de/es/pt technical usage
    invariant = {
        ".NET & Mono",
        "Arch: -",
        "Architecture",
        "Classification",
        "Description",
        "Details",
        "Flatpak",
        "Installation",
        "R & CRAN",
        "Repo: -",
        "Repository",
        "Repos",
        "Scripts",
        "Status",
        "Tcl/Tk",
        "Tcl/tk",
        "TeX & LaTeX",
        "Type",
    }
    untranslated = sorted(
        key for key in source if key not in invariant and messages.get(key) == key
    )
    assert not untranslated, f"{locale} left these in English: {untranslated[:5]}"


def test_german_uses_real_umlauts_not_asii_transliteration() -> None:
    """``ae``/``oe``/``ue``/``ss`` are a transliteration, not German spelling.

    A catalogue written with ASCII stand-ins reads as broken to a native
    speaker and defeats the point of shipping the language, so the shipped
    strings must use the real characters.  Word stems that genuinely contain
    those letter pairs (``Quelle``, ``Adresse``, ``aktuell``) are unaffected
    because they are matched as whole words.
    """

    #: German words whose stems genuinely contain one of those letter pairs.
    legitimate = {
        "Aktuell", "Aktuelle", "Aktueller", "Aktuellen",
        "Daemon", "Daemons",
        "Paketbetreuer",
        "Quelle", "Quellen", "Quellenherkunft",
        "Systemsteuerungen",
        "TrueType",
        "Vertrauen", "vertrauen", "vertrauenswürdig",
        "Visuelle", "visuelle",
        "aktuell", "aktuelle", "aktueller", "aktuellen",
        "dauerhafte",
        "neue", "neuen", "neuer", "neuere", "neuesten",
    }
    transliterations = ("ae", "oe", "ue")
    offenders = []
    for key, value in messages_for("de").items():
        # Include umlauts in the class, or "vertrauenswürdig" is split at the
        # umlaut and only its first half is examined.
        for word in re.findall(r"[A-Za-zÄÖÜäöüß]+", value):
            if word in legitimate:
                continue
            if any(pair in word.lower() for pair in transliterations):
                offenders.append((key, word))
    assert not offenders, f"transliterated rather than spelled: {offenders[:5]}"


def test_shipped_scripts_are_written_in_their_own_alphabet() -> None:
    """Each locale must actually be set in its script, not romanised."""

    #: Locale -> (Unicode block description, predicate over a character).
    scripts = {
        "ru": ("Cyrillic", lambda ch: "\u0400" <= ch <= "\u04ff"),
        "hi": ("Devanagari", lambda ch: "\u0900" <= ch <= "\u097f"),
    }
    for locale, (name, in_script) in scripts.items():
        messages = messages_for(locale)
        covered = sum(
            1 for value in messages.values() if any(in_script(ch) for ch in value)
        )
        assert covered >= len(messages) * 0.9, (
            f"{locale} only writes {covered}/{len(messages)} messages in {name}"
        )


def test_spanish_uses_inverted_opening_punctuation() -> None:
    """``¿`` and ``¡`` open Spanish questions and exclamations."""

    for key, value in messages_for("es").items():
        if key.rstrip().endswith("?"):
            assert "¿" in value, f"es: {key[:40]!r} lost its opening ¿"
        if key.rstrip().endswith("!"):
            assert "¡" in value, f"es: {key[:40]!r} lost its opening ¡"


def test_catalog_files_declare_their_locale_and_plural_rule() -> None:
    for locale in REQUIRED_LOCALES:
        payload = load_catalog(locale)
        assert payload["locale"], f"{locale} does not declare its locale tag"
        assert isinstance(payload["plural_boundary"], int)
        assert payload["plural_boundary"] >= 1


def test_english_needs_no_catalog() -> None:
    assert messages_for("en") == {}


def test_translation_falls_back_to_the_source_string() -> None:
    assert Translator("de").gettext("This string is not in any catalog") == (
        "This string is not in any catalog"
    )


def test_translation_of_a_known_string_is_localised() -> None:
    assert Translator("de").gettext("System Up to Date") == "System ist aktuell"
    assert Translator("ru").gettext("System Up to Date") == "Система обновлена"


def test_unhashable_input_does_not_raise() -> None:
    """Lookup must never fail a paint pass."""

    assert Translator("de").gettext(["not", "hashable"]) == ["not", "hashable"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("de_DE.UTF-8", "de_DE"),
        ("de_DE.UTF-8@euro", "de_DE"),
        ("pt-BR", "pt_BR"),
        ("pt_PT", "pt_PT"),
        ("hi_IN", "hi_IN"),
        ("", ""),
        ("C", ""),
        ("POSIX", ""),
    ],
)
def test_locale_tags_are_normalised(raw: str, expected: str) -> None:
    assert normalise_locale(raw) == expected


def test_regional_variant_resolves_to_its_base_language() -> None:
    resolution = resolve_locale("es_MX")
    assert resolution.locale == "es"
    assert not resolution.fell_back


def test_system_locale_preference_uses_the_desktop_language() -> None:
    assert resolve_locale(SYSTEM_LOCALE, "de_DE.UTF-8").locale == "de"
    assert resolve_locale(SYSTEM_LOCALE, "pt_BR.UTF-8").locale == "pt_BR"


def test_unsupported_system_locale_falls_back_to_english() -> None:
    resolution = resolve_locale(SYSTEM_LOCALE, "ko_KR.UTF-8")
    assert resolution.locale == "en"
    assert resolution.fell_back


def test_unknown_explicit_locale_falls_back_to_english() -> None:
    resolution = resolve_locale("ja_JP")
    assert resolution.locale == "en"
    assert resolution.fell_back


def test_endonyms_are_present_for_every_shipped_language() -> None:
    for locale in SUPPORTED_LOCALES:
        assert LOCALE_ENDONYMS.get(locale), f"{locale} has no endonym"
    # Spot-check that the endonyms are genuinely native spellings, not ASCII
    # transliterations, which would look wrong to a speaker.
    assert LOCALE_ENDONYMS["de"] == "Deutsch"
    assert LOCALE_ENDONYMS["hi"] == "हिन्दी"
    assert LOCALE_ENDONYMS["ru"] == "Русский"


def test_shipped_locales_reflect_the_files_on_disk() -> None:
    shipped = set(iter_shipped_locales())
    assert set(REQUIRED_LOCALES) <= shipped
    assert "en" in shipped


def test_plural_selection_uses_the_language_boundary() -> None:
    german = Translator("de")
    # German uses the plural form for every count except one.
    assert german.ngettext("{count} file", "{count} files", 1).startswith("{count}")
    assert german.plural_boundary == 2


def test_active_locale_drives_module_level_translate() -> None:
    set_active_locale("fr")
    assert active_locale() == "fr"
    assert translate("System Up to Date") == "Système à jour"
    set_active_locale("en")
    assert translate("System Up to Date") == "System Up to Date"


def test_module_level_plural_helper_uses_english_grammar_by_default() -> None:
    set_active_locale("en")
    assert translate_plural("one item", "many items", 1) == "one item"
    assert translate_plural("one item", "many items", 5) == "many items"


def test_catalog_directory_points_at_real_catalogs() -> None:
    directory = catalog_directory()
    assert directory.is_dir()
    assert (directory / "de.json").is_file()


def test_missing_catalog_raises_a_clear_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DENDRO_LOCALE_DIR", str(tmp_path))
    clear_cache()
    with pytest.raises(CatalogError, match="missing translation catalog"):
        load_catalog("de")


def test_malformed_catalog_is_reported_not_ignored(tmp_path, monkeypatch) -> None:
    (tmp_path / "de.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("DENDRO_LOCALE_DIR", str(tmp_path))
    clear_cache()
    with pytest.raises(CatalogError, match="invalid JSON"):
        load_catalog("de")


def test_catalog_without_messages_object_is_rejected(tmp_path, monkeypatch) -> None:
    (tmp_path / "de.json").write_text(json.dumps({"locale": "de"}), encoding="utf-8")
    monkeypatch.setenv("DENDRO_LOCALE_DIR", str(tmp_path))
    clear_cache()
    with pytest.raises(CatalogError, match="'messages' object"):
        load_catalog("de")


def test_catalogs_are_utf8_and_keep_their_native_script() -> None:
    """Hindi and Russian must ship real script, not escaped ASCII."""

    for locale, needle in (("hi", "सिस्टम"), ("ru", "Система")):
        raw = (LOCALE_DIR / f"{locale}.json").read_text(encoding="utf-8")
        assert needle in raw
        # ensure_ascii=False keeps the file reviewable in a diff.
        assert "\\u" not in raw


@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_retranslate_rewrites_an_open_preferences_dialog(locale: str) -> None:
    """Choosing a language must repaint the dialog doing the choosing.

    The Preferences dialog is the one place the user changes language from, so
    it is on screen while the rest of the window repaints around it. If it does
    not re-translate itself the user watches the app switch language underneath
    an English dialog.
    """

    from PyQt6.QtWidgets import QApplication

    from ui.settings_dialog import DendroSettingsDialog

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    dialog = DendroSettingsDialog()
    english = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
    try:
        set_active_locale(locale)
        dialog.retranslate()
        translated = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
        assert len(translated) == len(english)
        assert translated != english, f"{locale} left the dialog in English"
        assert all(translated), "a retranslated tab lost its title"
        # Rebuilding must not leave duplicate layouts or stale pages behind.
        assert dialog.layout() is not None
        assert dialog.tabs.count() == len(english)
    finally:
        dialog.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_retranslate_rewrites_the_inspector_chrome(locale: str) -> None:
    """The inspector's tab bar and metadata card must follow the language.

    Its gutter is a fixed pixel width measured from the painted field names, so
    a translation wider than the English original has to widen it rather than be
    clipped.
    """

    from PyQt6.QtWidgets import QApplication, QLabel

    from ui.inspector_panel import PackageInspectorPanel

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    panel = PackageInspectorPanel()
    english_tabs = [panel.tabs.tabText(index) for index in range(panel.tabs.count())]
    try:
        set_active_locale(locale)
        panel.retranslate()
        tabs = [panel.tabs.tabText(index) for index in range(panel.tabs.count())]
        assert tabs != english_tabs, f"{locale} left the inspector tabs in English"
        names = {label.text() for label in panel._stat_names.values()}
        assert "-" not in names and all(names), "field names did not translate"
        headings = {
            label.text()
            for label in panel._stats_card.findChildren(QLabel, "StatGroupTitle")
        }
        assert headings, "the metadata card lost its group headings"
        # Every field name still fits its gutter. The width is measured from
        # the painted text, so it legitimately shrinks in a language whose field
        # names are shorter than the English originals -- what must hold is that
        # it was re-measured rather than left at the English value.
        metrics_width = panel._measure_stat_name_width()
        assert panel._stat_name_width == metrics_width
        assert panel._stat_name_width > 0
    finally:
        panel.deleteLater()
        application.processEvents()


def test_packaging_declares_every_catalog() -> None:
    """A new language must reach the wheel, not just the source checkout.

    ``pyproject.toml`` enumerates the catalogs for ``data-files`` because
    setuptools cannot glob there. Adding a locale without touching that list
    ships a language that silently falls back to English once installed.
    """

    import tomllib

    project_root = Path(__file__).resolve().parent.parent
    declared = tomllib.loads((project_root / "pyproject.toml").read_text())
    data_files = declared["tool"]["setuptools"]["data-files"]
    packaged = {
        Path(entry).name
        for entries in data_files.values()
        for entry in entries
        if entry.endswith(".json")
    }
    on_disk = {path.name for path in LOCALE_DIR.glob("*.json")}
    assert packaged == on_disk, (
        f"pyproject.toml is out of step: unpackaged={sorted(on_disk - packaged)}, "
        f"missing on disk={sorted(packaged - on_disk)}"
    )


def test_missing_catalog_falls_back_to_the_english_source() -> None:
    """An untranslated message renders its English id rather than crashing.

    A catalogue can legitimately lag the source between releases. Falling back
    keeps the interface usable; the ``check`` command is what reports the gap.
    """

    set_active_locale("de")
    clear_cache()
    # A string that exists in the sources but not in the German catalogue.
    unknown = "This sentence is deliberately not translated anywhere at all."
    assert translate(unknown) == unknown


def test_a_setting_the_locale_unknown_to_us_resolves_to_english() -> None:
    """An unsupported desktop locale must not break startup."""

    resolution = resolve_locale("ja_JP.UTF-8", "ja_JP.UTF-8")
    assert resolution.locale == "en"
