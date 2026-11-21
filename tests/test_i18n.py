"""Translation catalog integrity and locale-resolution behaviour.

These tests guard the two failure modes that are easy to ship by accident:
a catalog that has drifted behind the code, and a language switch that silently
stops applying. Both are invisible in English, so they are asserted here rather
than left to manual review.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

import core.i18n as core_i18n
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
    #   cognate severity        French "Important"
    #   true cognates           French "Classification"/"Description"/
    #                           "Architecture"/"Type", German "Details"/
    #                           "Installation"/"Repository"/"Status",
    #                           "Scripts" in fr/es/pt, "Arch:" in de/es/pt,
    #                           "Repo"/"Repos" in de/es/pt technical usage,
    #                           "Status:" likewise in de
    #   column headers          "ID" is the technical acronym in every shipped
    #                           language, so no catalog translates it; the
    #                           history and repository tables use it as a
    #                           header. German and French call a software
    #                           "Version" a "Version" too.
    invariant = {
        ".NET & Mono",
        "Arch: -",
        "Architecture",
        "Classification",
        "Description",
        "Details",
        "Flatpak",
        "ID",
        # French spells this severity exactly as English does.
        "Important",
        "Installation",
        "R & CRAN",
        "Repo: -",
        "Repository",
        "Repos",
        "Scripts",
        "Status",
        "Status:",
        # German and French keep the Latin "Version" for a software release.
        "Version",
        # German uses "Name" unchanged in a table header.
        "Name",
        # Placeholders only, no prose: the order and the punctuation are what a
        # language would change, and French already does ("{severity} : {count}").
        "{severity}: {count}",
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
        "Aktuell",
        "Aktuelle",
        "Aktueller",
        "Aktuellen",
        "Daemon",
        "Daemons",
        "Paketbetreuer",
        "Quelle",
        "Quellen",
        "Quellenherkunft",
        "Systemsteuerungen",
        "TrueType",
        "Vertrauen",
        "vertrauen",
        "vertrauenswürdig",
        "Visuelle",
        "visuelle",
        "aktuell",
        "aktuelle",
        "aktueller",
        "aktuellen",
        "dauerhafte",
        "neue",
        "genaue",
        "genauen",
        "neuen",
        "neuer",
        "neuere",
        "neuesten",
        # "Neues Paket" is correct German for "New package"; the "ue" spans a
        # syllable boundary rather than standing in for "ü".
        "Neues",
    }
    transliterations = ("ae", "oe", "ue")
    offenders = []
    #: Filter keywords the user types into the search field. They are literal
    #: tokens, so they stay in English in every catalog.
    filter_tokens = {"queued", "orphan", "installed", "value"}

    for key, value in messages_for("de").items():
        # ``{placeholder}`` names are template slots, not German prose: scanning
        # them would flag ``{value}`` as a transliterated word.
        prose = re.sub(r"\{[a-z0-9_]+\}", "", value)
        # Include umlauts in the class, or "vertrauenswürdig" is split at the
        # umlaut and only its first half is examined.
        for word in re.findall(r"[A-Za-zÄÖÜäöüß]+", prose):
            if word in filter_tokens:
                continue
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
    registered = len(dialog._translatable)
    try:
        set_active_locale(locale)
        dialog.retranslate()
        translated = [
            dialog.tabs.tabText(index) for index in range(dialog.tabs.count())
        ]
        assert len(translated) == len(english)
        assert translated != english, f"{locale} left the dialog in English"
        assert all(translated), "a retranslated tab lost its title"
        # Retranslation is in place, so the page set and the registry must be
        # untouched: no duplicated pages, and no growing registration list.
        assert dialog.layout() is not None
        assert dialog.tabs.count() == len(english)
        assert len(dialog._translatable) == registered
        # Running it again must be a no-op rather than a crash or a leak.
        dialog.retranslate()
        assert len(dialog._translatable) == registered
        assert dialog.tabs.count() == len(english)
        # Every registered caption is a real string, not a collapsed tuple or
        # an empty placeholder.
        captions = [widget.text() for widget, _setter, _message in dialog._translatable]
        assert all(isinstance(text, str) and text for text in captions)
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


def test_a_pip_install_finds_its_catalogs_under_the_prefix(
    tmp_path, monkeypatch
) -> None:
    """``data-files`` land in ``<prefix>/share/dendro/locales``, not beside the code.

    A ``pip install`` puts the package in ``site-packages`` and the catalogs
    under the interpreter prefix, so a lookup that only checks sibling
    directories silently falls back to English for every wheel install.
    """

    prefix = tmp_path / "prefix"
    catalogs = prefix / "share" / "dendro" / "locales"
    catalogs.mkdir(parents=True)
    (catalogs / "de.json").write_text(
        json.dumps({"locale": "de", "plural_boundary": 2, "messages": {}}),
        encoding="utf-8",
    )

    monkeypatch.delenv("DENDRO_LOCALE_DIR", raising=False)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    # Pretend the package lives somewhere with no sibling catalogs.
    isolated_package = tmp_path / "site-packages" / "core"
    isolated_package.mkdir(parents=True)
    monkeypatch.setattr(core_i18n, "__file__", str(isolated_package / "i18n.py"))

    assert catalog_directory() == catalogs


@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_preferences_dialog_has_no_untranslated_captions(locale: str) -> None:
    """Every visible caption must change when the language changes.

    Retranslation is driven by an explicit registry, so a label built without
    being registered keeps its English text forever while the rest of the
    dialog changes around it. That happened twice while building this page (the
    combo entries and the whole About tab), so it is asserted rather than
    trusted.

    The comparison is against the dialog's own English render rather than
    against a list of English words: German and Portuguese are Latin-script, so
    "looks English" is not decidable from the characters alone. A caption that
    is byte-identical to its English self, while not being a value that is
    legitimately identical, is an unregistered label.
    """

    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QLabel,
        QPushButton,
        QTabWidget,
    )

    from ui.settings_dialog import DendroSettingsDialog

    #: Correctly the same in every language: product and project names, version
    #: strings, the licence name, and endonyms (which are never translated).
    invariant = {
        "<b>Dendro</b>",
        "Dendro",
        "Español",
        "Português (Brasil)",
        "Português (Portugal)",
        "Français",
        "Deutsch",
        "हिन्दी",
        "Русский",
        "English",
    }
    invariant_fragments = (
        "GNU General Public License",
        "Python ",
        "PyQt6",
        "librpm",
        "libdnf5",
        "Fedora RPM Host",
        "Yashar",
    )

    def captions(dialog) -> set[str]:
        found = set()
        for cls in (QLabel, QPushButton, QCheckBox):
            for widget in dialog.findChildren(cls):
                found.add(widget.text() or "")
        for combo in dialog.findChildren(QComboBox):
            for index in range(combo.count()):
                found.add(combo.itemText(index))
        for tabs in dialog.findChildren(QTabWidget):
            for index in range(tabs.count()):
                found.add(tabs.tabText(index))
        return found

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    dialog = DendroSettingsDialog()
    try:
        english = captions(dialog)
        set_active_locale(locale)
        dialog.retranslate()
        translated = captions(dialog)

        offenders = []
        for text in translated:
            if not text.strip() or text in invariant:
                continue
            if any(fragment in text for fragment in invariant_fragments):
                continue
            if text in english:
                offenders.append(text[:60])

        assert not offenders, f"{locale} left these in English: {offenders[:6]}"
    finally:
        dialog.deleteLater()
        application.processEvents()


def test_retranslation_registers_every_preferences_caption_once() -> None:
    """The registry must be complete and must not grow on repeated passes."""

    from PyQt6.QtWidgets import QApplication

    from ui.settings_dialog import DendroSettingsDialog

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    dialog = DendroSettingsDialog()
    try:
        registered = len(dialog._translatable)
        combos = len(dialog._combo_messages)
        # Nine Preferences captions, eight About captions; five combo entries
        # (three theme names, Follow System, English). Endonyms are excluded.
        assert registered >= 15
        assert combos == 5
        for _ in range(5):
            dialog.retranslate()
        assert len(dialog._translatable) == registered
        assert len(dialog._combo_messages) == combos
    finally:
        dialog.deleteLater()
        application.processEvents()


#: Every runtime topology Dendro distinguishes, built from the injectable probe
#: so the branches that a normal CI box never reports are still exercised.
def _host_environments() -> dict[str, object]:
    from core.host_environment import detect_host_environment
    from tests.test_host_environment import FakeHostProbe

    def probe(**kwargs) -> object:
        return detect_host_environment(FakeHostProbe(**kwargs))

    return {
        "flatpak_sandbox": probe(existing={"/.flatpak-info"}),
        "unbooted_container": probe(
            existing={"/run/.containerenv"},
            files={
                "/etc/os-release": 'PRETTY_NAME="Fedora Linux 45 (Container Image)"\n'
            },
        ),
        "atomic_ostree": probe(
            existing={"/run/ostree-booted"},
            files={"/etc/os-release": 'PRETTY_NAME="Fedora Silverblue 45"\n'},
        ),
        "read_only_usr": probe(
            read_only_paths={"/usr"},
            files={"/etc/os-release": 'PRETTY_NAME="Fedora IoT 45"\n'},
        ),
        "traditional_fedora": probe(
            files={"/etc/os-release": 'PRETTY_NAME="Fedora Linux 45"\n'},
        ),
    }


@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_every_host_mode_label_follows_the_language(locale: str) -> None:
    """All five runtime topologies must translate, not just the common one.

    A normal host only ever reports one of these, so five of the six strings
    below would otherwise ship untested and stay English on the machines that
    actually need them - Fedora Atomic, read-only ``/usr``, and container
    users are exactly the ones who see these banners.
    """

    environments = _host_environments()

    def snapshot() -> dict[str, tuple[str, str, str]]:
        captured = {}
        for name, environment in environments.items():
            banner = environment.mode_banner()
            captured[name] = (
                environment.display_label(),
                banner[0] if banner else "",
                environment.dnf5_daemon_unavailable("install packages")[0],
            )
        return captured

    set_active_locale("en")
    english = snapshot()

    set_active_locale(locale)
    translated = snapshot()

    for name, (label, banner, reason) in translated.items():
        # The distribution's own name is appended verbatim and is not a
        # translation target, so compare the part before it.
        english_label = english[name][0].split(" (")[0]
        translated_label = label.split(" (")[0]
        assert translated_label != english_label, f"{locale}: {name} label unchanged"
        if banner:
            assert banner != english[name][1], f"{locale}: {name} banner unchanged"
        assert reason != english[name][2], f"{locale}: {name} refusal unchanged"


@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_host_mode_messages_keep_their_placeholders(locale: str) -> None:
    """A translated refusal must still name the action the user tried.

    ``{action}`` moves position between languages, so the sentence is
    translated with the placeholder intact and formatted afterwards. A
    translation that dropped it would silently lose the one detail that tells
    the user what Dendro declined to do.
    """

    messages = messages_for(locale)
    carriers = [key for key in messages if "{action}" in key]
    assert carriers, "no message carries an action placeholder"
    for key in carriers:
        assert "{action}" in messages[key], f"{locale}: {key[:50]!r} dropped {{action}}"


def test_translated_refusal_names_the_action_in_the_new_language() -> None:
    """The formatted sentence reads naturally, not as English with a hole."""

    environment = _host_environments()["atomic_ostree"]
    set_active_locale("en")
    english_title, _ = environment.dnf5_daemon_unavailable("install")
    set_active_locale("de")
    german_title, german_detail = environment.dnf5_daemon_unavailable("install")
    assert german_title != english_title
    assert "{action}" not in german_detail
    assert "Dendro" in german_detail


def test_a_page_built_after_the_switch_is_translated_from_the_start() -> None:
    """A widget created later must come out in the current language.

    This is what an opt-in registration list cannot promise: anything built
    after the language change - a dialog opened minutes later, a panel added by
    a deferred import - never ran the registration path and would render the
    language that was active at import time. Building it now is the honest
    regression test for that.
    """

    from PyQt6.QtWidgets import QApplication

    from ui.i18n import install
    from ui.settings_dialog import DendroSettingsDialog

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    install("de")

    dialog = DendroSettingsDialog()
    try:
        tabs = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
        assert tabs == ["Einstellungen", "Info && Rechtliches"], tabs
        assert "Einstellungen" in dialog.windowTitle()
    finally:
        dialog.deleteLater()
        application.processEvents()


def test_language_switch_repaints_widgets_nobody_registered() -> None:
    """The sweep reaches every live widget, not a hand-maintained list.

    ``_apply_language_choice`` used to name the three panels it knew about. A
    widget it did not name kept its text, so the check here is that a switch
    driven through the window reaches a child that appears nowhere in that
    list.
    """

    from PyQt6.QtWidgets import QApplication, QLabel

    from core.i18n import tr
    from ui.main_window import MainWindow

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    window = MainWindow()
    try:
        # A label outside the header/sidebar/inspector trio, added at runtime.
        probe = QLabel(tr("Settings"), window)
        probe.retranslate = lambda: probe.setText(tr("Settings"))
        window.show()
        application.processEvents()

        window._apply_language_choice("de")
        # The sweep runs on the next event-loop turn, not inline inside the
        # signal that asked for the change.
        application.processEvents()
        assert probe.text() == "Einstellungen", probe.text()
    finally:
        window.deleteLater()
        application.processEvents()


def test_retranslating_a_widget_twice_is_idempotent() -> None:
    """Re-running the pass must not stack a second copy of the text.

    Hooks patch text in place, so calling them again is cheap and safe. This
    pins that down: a hook that appended instead of replacing would double the
    label on the second language change, which is the failure mode the automatic
    sweep makes reachable from more places than before.
    """

    from PyQt6.QtWidgets import QApplication, QLabel

    from core.i18n import tr
    from ui.i18n import LocaleManager

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    label = QLabel(tr("Settings"))
    label.retranslate = lambda: label.setText(tr("Settings"))

    manager = LocaleManager()
    try:
        for _ in range(3):
            manager.apply("de")
            application.processEvents()
            assert label.text() == "Einstellungen", label.text()

        manager.apply("en")
        application.processEvents()
        assert label.text() == "Settings", label.text()
    finally:
        label.deleteLater()
        application.processEvents()


def test_a_widget_whose_cxx_object_is_gone_is_skipped(monkeypatch) -> None:
    """The sweep must survive a stale entry rather than fault on it.

    ``QApplication.allWidgets`` is documented to still list a widget whose C++
    side has been destroyed, and PyQt raises ``RuntimeError`` the moment you
    touch it - ``getattr`` included. One distribution's Qt build turns that
    same touch into a crash, which is what the guard is for. The list is
    stubbed here because the window between destruction and removal from
    ``allWidgets`` cannot be held open from Python.
    """

    from PyQt6 import sip
    from PyQt6.QtWidgets import QApplication, QLabel

    from core.i18n import tr
    from ui.i18n import LocaleManager

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    doomed = QLabel(tr("Settings"))
    survivor = QLabel(tr("Settings"))
    survivor.retranslate = lambda: survivor.setText(tr("Settings"))

    manager = LocaleManager()
    monkeypatch.setattr(
        manager, "live_widgets", lambda: [doomed, survivor], raising=False
    )
    sip.delete(doomed)
    assert sip.isdeleted(doomed)

    try:
        manager.apply("de")
        application.processEvents()
        assert survivor.text() == "Einstellungen", survivor.text()
    finally:
        survivor.deleteLater()
        application.processEvents()
        set_active_locale("en")


def test_a_hook_that_starts_another_sweep_does_not_recurse() -> None:
    """A hook may kick off a sweep itself; it must not re-enter its own.

    The Preferences dialog retranslates in place from inside the signal that
    changed the language, so "retranslate while retranslating" is a reachable
    state, not a thought experiment. Without the guard the hook calls itself
    until the stack runs out.
    """

    from PyQt6.QtWidgets import QApplication, QLabel

    from core.i18n import tr
    from ui.i18n import LocaleManager

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    manager = LocaleManager()
    calls: list[str] = []
    recruit = QLabel(tr("Settings"))
    recruit.retranslate = lambda: recruit.setText(tr("Settings"))
    instigator = QLabel(tr("Settings"))

    def recurse() -> None:
        calls.append("instigator")
        instigator.setText(tr("Settings"))
        manager.retranslate_all()

    instigator.retranslate = recurse

    # Only these two are visited, so the recursion is bounded by the guard
    # rather than by how many widgets happen to exist.
    manager.live_widgets = lambda: [instigator, recruit]  # type: ignore[method-assign]
    try:
        manager.apply("de")
        application.processEvents()
        assert calls == ["instigator"], calls
        assert recruit.text() == "Einstellungen", recruit.text()
    finally:
        recruit.deleteLater()
        instigator.deleteLater()
        application.processEvents()
        set_active_locale("en")


def test_the_deferred_refit_is_owned_by_the_dialog() -> None:
    """The re-fit timer must die with the dialog, not outlive it.

    ``QTimer.singleShot(0, ...)`` creates a timer parented to the event
    dispatcher and calls a bound method on the dialog later. If the dialog is
    destroyed in between, the callback runs against a wrapper whose C++ object
    is gone - a race, so it passes locally for months and then faults on one
    distribution's Qt build.

    The dialog is destroyed with ``sip.delete`` rather than a global
    ``DeferredDelete`` sweep, so this only tears down its own widget.
    """

    from PyQt6 import sip
    from PyQt6.QtWidgets import QApplication

    from core.i18n import tr
    from ui.settings_dialog import DendroSettingsDialog

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    dialog = DendroSettingsDialog()
    try:
        timer = dialog._refit_timer
        assert timer.parent() is dialog, timer.parent()
        assert timer.isSingleShot()

        fired: list[int] = []
        timer.timeout.connect(lambda: fired.append(1))

        # Arm the re-fit, then destroy the dialog before the timer can fire.
        dialog.retranslate()
        sip.delete(dialog)
        application.processEvents()
        assert fired == [], fired
    finally:
        set_active_locale("en")
        assert tr("Preferences")  # the catalog still resolves afterwards


def test_no_hook_runs_inside_the_signal_that_changed_the_language() -> None:
    """The sweep must not run while Qt is still delivering a signal.

    A language change normally starts inside a widget's own handler - the
    Preferences combo box emits ``currentIndexChanged`` and the window turns it
    into ``install``. Retranslating the tree from there runs every hook with
    signal delivery still on the stack, including hooks on the widget that
    emitted it. That crashed on two Fedora Qt builds and not a third, so the
    sweep is deferred to the next event-loop turn instead. This asserts the
    deferral directly: nothing repaints before the loop turns, everything has
    by the time it does.
    """

    from PyQt6.QtWidgets import QApplication, QLabel

    from core.i18n import tr
    from ui.i18n import LocaleManager

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    manager = LocaleManager()
    seen: list[str] = []
    label = QLabel(tr("Settings"))
    label.retranslate = lambda: (
        seen.append(label.text()),
        label.setText(tr("Settings")),
    )
    manager.live_widgets = lambda: [label]  # type: ignore[method-assign]
    try:
        manager.apply("de")
        assert seen == [], "the sweep ran before the event loop turned"

        application.processEvents()
        assert seen, "the sweep never ran"
        assert label.text() == "Einstellungen", label.text()

        # A burst of changes collapses into one pending sweep, not one each.
        manager.apply("fr")
        manager.apply("en")
        application.processEvents()
        assert label.text() == "Settings", label.text()
    finally:
        label.deleteLater()
        application.processEvents()
        set_active_locale("en")


# Header labels that are legitimately identical after a language change: a glyph
# with no words in it, a product name that is never translated, and cognates
# where the target language uses the same word.
_HEADER_UNCHANGED_OK = {"?", "Flatpak", "Repos", "Details"}


def _header_snapshot(header):
    """Return ``(widgets, actions, texts, tooltips)`` for the header bar."""

    from PyQt6.QtGui import QAction
    from PyQt6.QtWidgets import QPushButton, QToolButton

    widgets = header.findChildren(QPushButton) + header.findChildren(QToolButton)
    actions = header.findChildren(QAction)
    texts = [(w.text(), w.toolTip()) for w in widgets]
    action_texts = [(a.text(), a.toolTip()) for a in actions]
    return widgets, actions, texts, action_texts


@pytest.mark.parametrize("locale", [loc for loc in SUPPORTED_LOCALES if loc != "en"])
def test_every_header_tooltip_follows_the_language(locale: str) -> None:
    """No header control may keep an English tooltip after a switch.

    The header is built once in ``_init_ui``, so a tooltip written there is
    English forever unless ``HeaderBar.retranslate`` rewrites it. Only seven of
    the eighteen controls were listed when this was first written, which is
    exactly the kind of list that silently goes stale.

    The header is a plain widget, so it is driven directly rather than through
    ``MainWindow``: a window refuses to be destroyed while Qt still has it
    scheduled for deletion, and this test has no use for the rest of the window.
    """

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    from ui.header import HeaderBar
    from ui.i18n import install

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    header = HeaderBar()
    try:
        widgets, actions, before, action_before = _header_snapshot(header)
        install(locale)
        application.processEvents()

        stuck = [
            tooltip
            for widget, (_text, tooltip) in zip(widgets, before, strict=True)
            if tooltip and widget.toolTip() == tooltip
        ]
        stuck += [
            tooltip
            for action, (_text, tooltip) in zip(actions, action_before, strict=True)
            if tooltip and action.toolTip() == tooltip
        ]
        assert stuck == [], f"{len(stuck)} tooltips kept their English text: {stuck}"
    finally:
        # Destroy the header now rather than scheduling it: a pending
        # ``deleteLater`` fires inside a later test's event loop and surfaces as
        # that test's failure, which is what a stray `CALL ERROR` looks like.
        header.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        header.close()
        application.processEvents()
        set_active_locale("en")


@pytest.mark.parametrize("locale", [loc for loc in SUPPORTED_LOCALES if loc != "en"])
def test_every_header_button_label_follows_the_language(locale: str) -> None:
    """No header button may keep its English label after a switch.

    ``retranslate`` used to refresh tooltips only, so every button read English
    inside a translated window. Labels carrying a count are recomputed from the
    stored count, which is why the count has to survive the change as well.
    """

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    from ui.header import HeaderBar
    from ui.i18n import install

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    header = HeaderBar()
    try:
        widgets, _actions, before, _action_before = _header_snapshot(header)
        install(locale)
        application.processEvents()

        stuck = [
            text
            for widget, (text, _tip) in zip(widgets, before, strict=True)
            if text and widget.text() == text and text not in _HEADER_UNCHANGED_OK
        ]
        assert stuck == [], f"{len(stuck)} labels kept their English text: {stuck}"
    finally:
        # Destroyed now, not scheduled: this test asks about the header, and a
        # whole MainWindow left pending a deletion is what turned a stray
        # exception into another test's `CALL ERROR`.
        header.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        header.close()
        application.processEvents()
        set_active_locale("en")


def test_a_counted_header_label_keeps_its_count_across_a_switch() -> None:
    """The number and the language must both survive a language change.

    A label built as ``f"Apply Changes ({count})"`` was not translatable at all;
    a label built once as ``tr(...)`` would have lost the count when the locale
    changed. The count is stored on the header so ``retranslate`` can rebuild
    the label rather than restore a stale string.

    The header is given a mutable host explicitly. CI runs inside a container
    where the DNF5 daemon is unavailable, and a read-only host replaces the
    count in every one of these labels with "Read-Only", which is correct
    behaviour and would hide the thing this test is about.
    """

    from PyQt6.QtWidgets import QApplication

    from core.host_environment import HostEnvironment
    from core.i18n import tr
    from ui.header import HeaderBar
    from ui.i18n import install

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    header = HeaderBar()
    header.configure_host_environment(HostEnvironment.traditional())
    try:
        header.update_queue_badge(7)
        header.update_available_updates_badge(3)
        header.set_orphan_clean_visible(True, 4)
        assert header.apply_btn.text() == tr("Apply Changes ({count})").format(count=7)

        install("de")
        application.processEvents()

        expected = {
            header.apply_btn: tr("Apply Changes ({count})").format(count=7),
            header.updates_btn: tr("Updates ({count})").format(count=3),
            header.clean_orphans_btn: tr("Clean Orphans ({count})").format(count=4),
        }
        for button, label in expected.items():
            assert button.text() == label, (button.text(), label)
        # The count itself, not just the translated frame around it.
        assert "7" in header.apply_btn.text()
        assert "3" in header.updates_btn.text()
        assert "4" in header.clean_orphans_btn.text()
    finally:
        header.deleteLater()
        application.processEvents()
        set_active_locale("en")


def test_the_search_reference_is_translated_and_keeps_its_tokens() -> None:
    """The filter reference must be prose, not one untranslatable markup blob.

    It used to be a module-level constant of English HTML, so it was outside the
    catalogs entirely: an id containing ``<b>`` and ``<code>`` cannot be
    translated accurately. It is now a table of prose rows rendered on read, and
    every ``{...}`` token the user types has to survive verbatim.
    """

    import ui.header as header_module

    set_active_locale("en")
    try:
        english = header_module.search_filter_tooltip()
        assert english, "the reference is empty"
        for _message, shell, has_values in header_module.SEARCH_FILTER_ROWS:
            if has_values:
                # Placeholders are substituted, so none may survive in the output.
                assert "{" not in shell.format("").replace("{}", ""), shell

        for locale in SUPPORTED_LOCALES:
            if locale == "en":
                continue
            set_active_locale(locale)
            translated = header_module.search_filter_tooltip()
            assert translated != english, locale
            # The literal tokens are what the user types: they stay identical.
            for token in (
                "<code>status:</code>",
                "<code>arch:</code>",
                "<code>size:</code>",
                "<code>file:/path/to/file</code>",
            ):
                assert token in translated, (locale, token)
            # The reference must not contain the English prose it replaced.
            assert "Use ordinary words to search package names" not in translated, (
                locale
            )
            assert "Start typing a filter name or value" not in translated, locale
    finally:
        set_active_locale("en")


def test_the_read_only_apply_label_translates_in_every_host_mode() -> None:
    """The apply button has two shapes; both must follow the language.

    On a mutable host it reads "Apply Changes (N)"; on Atomic, read-only `/usr`,
    an unbooted container, and anywhere else without the daemon it reads
    "Read-Only". CI caught the first shape only because the test that covered it
    assumed a mutable host: in a container the label is the other shape, so the
    assertion failed for the right reason and for the wrong host.
    """

    from PyQt6.QtWidgets import QApplication

    from core.host_environment import HostEnvironment, HostEnvironmentKind
    from core.i18n import tr
    from ui.header import HeaderBar
    from ui.i18n import install

    application = QApplication.instance() or QApplication([])
    header = HeaderBar()
    try:
        set_active_locale("en")
        english = {}
        for kind in HostEnvironmentKind:
            header.configure_host_environment(
                HostEnvironment(kind=kind, os_pretty_name="Fedora Linux 45")
            )
            header.update_queue_badge(7)
            english[kind] = header.apply_btn.text()
            assert english[kind] in {
                tr("Apply Changes ({count})").format(count=7),
                tr("Read-Only"),
            }, english[kind]

        # Both shapes must appear, or this test is not covering the case above.
        assert len(set(english.values())) == 2, english

        install("de")
        application.processEvents()
        for kind, before in english.items():
            header.configure_host_environment(
                HostEnvironment(kind=kind, os_pretty_name="Fedora Linux 45")
            )
            header.update_queue_badge(7)
            assert header.apply_btn.text() != before, (kind.name, before)
            assert install("de") is None  # idempotent, never raises
            assert header.apply_btn.text() != before, (kind.name, before)
    finally:
        header.deleteLater()
        application.processEvents()
        set_active_locale("en")


def test_a_hook_that_raises_does_not_stop_the_rest_of_the_sweep() -> None:
    """One broken widget must not leave the whole window half translated.

    More importantly it must not raise *inside a Qt slot*: an exception there is
    reported against whatever the event loop was doing, not against the widget
    that caused it. CI showed this as `CALL ERROR: Exceptions caught in Qt event
    loop` on a test that had nothing to do with the widget at fault.
    """

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QLabel

    from core.i18n import tr
    from ui.i18n import LocaleManager

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    manager = LocaleManager()
    broken = QLabel(tr("Settings"))

    def explode() -> None:
        raise TypeError("() missing 1 required positional argument: 'source'")

    broken.retranslate = explode
    healthy = QLabel(tr("Settings"))
    healthy.retranslate = lambda: healthy.setText(tr("Settings"))
    manager.live_widgets = lambda: [broken, healthy]  # type: ignore[method-assign]

    try:
        manager.apply("de")
        application.processEvents()
        # The healthy widget was still translated, after the failing one.
        assert healthy.text() == "Einstellungen", healthy.text()
        assert manager.last_failure is not None
        widget_name, error = manager.last_failure
        assert widget_name.startswith("QLabel."), widget_name
        assert "explode" in widget_name, widget_name
        assert isinstance(error, TypeError), error
    finally:
        # Destroyed *now*, not scheduled. `WA_DeleteOnClose` only destroys a
        # widget that was shown, and a lingering `deleteLater` would be swept by
        # a later test's language change and fail that test instead - which is
        # exactly how this reproduced.
        for widget in (healthy, broken):
            widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            widget.show()
            widget.close()
        application.processEvents()
        set_active_locale("en")


@pytest.mark.parametrize("locale", [loc for loc in SUPPORTED_LOCALES if loc != "en"])
def test_no_real_widget_fails_to_retranslate(locale: str) -> None:
    """No widget in the real window may raise from its retranslate hook.

    This is the assertion that would have named the Fedora-only `CALL ERROR`
    directly: the sweep records which widget raised instead of letting an
    exception escape into the event loop.
    """

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    from ui.header import HeaderBar
    from ui.i18n import LocaleManager

    application = QApplication.instance() or QApplication([])
    set_active_locale("en")
    manager = LocaleManager()
    header = HeaderBar()
    try:
        manager.apply(locale)
        application.processEvents()
        assert manager.last_failure is None, (
            "a widget failed to retranslate: "
            f"{manager.last_failure} - if this names a hook defined in a test, "
            "that test leaked a widget into the sweep"
        )
    finally:
        header.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        header.close()
        application.processEvents()
        set_active_locale("en")


#: The four tables whose column headers used to be literal lists.
#: ``setHorizontalHeaderLabels`` was not known to the extractor, so every header
#: written that way was invisible to it and shipped in English in all seven
#: languages -- 17 strings across these tables, including the inspector's
#: manifest.
_HEADER_DECLARATIONS = {
    "ui/flatpak_dialog.py",
    "ui/history_dialog.py",
    "ui/inspector_panel.py",
    "ui/repo_dialog.py",
}


def test_column_headers_are_visible_to_the_extractor() -> None:
    """A literal list handed to ``setHorizontalHeaderLabels`` is not a message.

    The extractor reads ``tr()`` call sites and declared message tables. A bare
    list is neither, so the strings inside it never reached a catalog. This
    asserts every header setter is given a declared table (or an explicit
    ``tr()``), which is the shape the extractor can see.
    """

    import ast

    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "ui").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None)
            if name not in {
                "setHorizontalHeaderLabels",
                "setVerticalHeaderLabels",
            }:
                continue
            if not node.args:
                continue
            argument = node.args[0]
            # A literal list is invisible; a comprehension over a declared table
            # or a name is what the extractor can follow.
            if isinstance(argument, ast.List | ast.Tuple) and all(
                isinstance(element, ast.Constant) for element in argument.elts
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "these headers are a literal list, which the extraction tool cannot see, "
        f"so they would ship in English in every language: {offenders}"
    )


def test_the_column_header_tables_are_extracted() -> None:
    """Every declared header table reaches the source strings, in all catalogs."""

    source = _source_strings()
    expected = {
        "ID",
        "Action / Status",
        "Date & Time",
        "Altered",
        "Command Line",
        "Status",
        "Repository ID",
        "Display Name",
        "Channel Origin",
        "File Path",
        "Size",
        "Kind",
        "Name",
        "Application ID",
        "Version",
        "Branch",
        "Arch",
        "Origin",
        "Installed size",
    }
    assert expected <= source, f"headers missing from extraction: {expected - source}"

    for locale in REQUIRED_LOCALES:
        messages = messages_for(locale)
        missing = sorted(expected - set(messages))
        assert not missing, f"{locale} is missing header translations: {missing}"


@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_a_real_table_shows_the_translated_headers(locale: str) -> None:
    """The headers are not just in the catalog: a built table asks for them.

    ``COLUMN_HEADERS`` existing is not enough -- the table has to pass it
    through ``tr()`` when it sets its labels. This builds the history table,
    whose five headers were the most visible English-only case.
    """

    from PyQt6.QtWidgets import QApplication

    from core.i18n import tr
    from ui.history_dialog import COLUMN_HEADERS as HISTORY_COLUMN_HEADERS
    from ui.history_dialog import DnfHistoryDialog

    application = QApplication.instance() or QApplication([])
    try:
        set_active_locale(locale)
        dialog = DnfHistoryDialog()
        try:
            application.processEvents()
            shown = [
                dialog.table.horizontalHeaderItem(column).text()
                for column in range(dialog.table.columnCount())
            ]
            expected = [tr(text) for text in HISTORY_COLUMN_HEADERS]
            assert shown == expected
            if locale != "en":
                assert shown != list(HISTORY_COLUMN_HEADERS), (
                    f"{locale} still shows the English headers: {shown}"
                )
        finally:
            dialog.close()
            application.processEvents()
    finally:
        set_active_locale("en")
