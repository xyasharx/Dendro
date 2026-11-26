"""Where the data/prose line sits at the DNF5 daemon boundary.

Two kinds of value cross it and they need opposite handling:

* **data** -- package names, versions, repository ids, CVE and advisory ids.
  Translating any of it would be wrong, and nothing here does.
* **enumerators and prose** -- severity words, which Dendro renders itself, and
  daemon error messages, which the daemon can render for us if the session asks
  for a language.

The tests below pin both halves, including the two details of the ``locale``
option that make passing it safe: the value must be a glibc locale name, and a
name the daemon rejects is ignored rather than fatal.
"""

from __future__ import annotations

import dendro.core.i18n as core_i18n
from dendro.core.daemon_locale import (
    DAEMON_LOCALES,
    SEVERITY_MESSAGES,
    daemon_locale,
    is_translated_locale,
    severity_message,
)

#: Severity words that are spelled the same in a language as in English, so a
#: matching value is precision rather than a missed translation. French uses the
#: same adjective for "Important" advisories.
COGNATES = frozenset({("fr", "Important")})


def test_every_daemon_locale_is_a_glibc_name() -> None:
    """``newlocale`` rejects a BCP-47 tag, so the mapping cannot drift into one.

    Dendro's catalog ids ("pt_BR") and the daemon's locale names ("pt_BR.UTF-8")
    live in the same corner of the code and are easy to confuse; the daemon
    silently ignores a name it cannot load, which would turn the mistake into an
    invisible regression.
    """

    for catalog, locale in DAEMON_LOCALES.items():
        assert "." in locale, f"{catalog} maps to {locale!r}, which is not a glibc name"
        language, _, suffix = locale.partition(".")
        assert suffix.startswith("UTF-8"), locale
        # The language part of the glibc name must be the catalog's language, so
        # a copy-paste mistake cannot send the wrong language.
        assert language.split("_")[0] == catalog.split("_")[0], (catalog, locale)


def test_every_shipped_catalog_has_a_daemon_locale() -> None:
    """Every language Dendro shows must also be asked for at the daemon."""

    for locale in core_i18n.SUPPORTED_LOCALES:
        if locale == "en":
            continue
        assert locale in DAEMON_LOCALES, f"{locale} has no daemon locale"
        assert is_translated_locale(locale)


def test_daemon_locale_follows_the_active_catalog() -> None:
    original = core_i18n.active_locale()
    try:
        for catalog, expected in DAEMON_LOCALES.items():
            core_i18n.set_active_locale(catalog)
            assert daemon_locale() == expected, catalog
    finally:
        core_i18n.clear_cache()
        core_i18n.set_active_locale(original)


def test_english_and_an_unknown_language_fall_back_to_a_valid_locale() -> None:
    """Never send a name the daemon would reject, and never send a guess."""

    assert daemon_locale("en") == "C.UTF-8"
    # A region with no catalog of its own still names the same language.
    assert daemon_locale("de_AT") == "de_DE.UTF-8"
    # A language with no catalog at all must not be invented.
    assert daemon_locale("ja") == "C.UTF-8"
    assert daemon_locale("") == "C.UTF-8"


def test_severity_words_are_interface_messages() -> None:
    """The daemon's enumerators are Dendro's to translate, not to display raw."""

    assert SEVERITY_MESSAGES["critical"] == "Critical"
    # Every catalog must carry each of them, or a severity ships in English.
    for locale in core_i18n.SUPPORTED_LOCALES:
        if locale == "en":
            continue
        messages = core_i18n.messages_for(locale)
        for message in SEVERITY_MESSAGES.values():
            assert messages.get(message), f"{locale} does not translate {message!r}"
            if (locale, message) in COGNATES:
                continue
            assert messages[message] != message, f"{locale} left {message!r} in English"


def test_an_unrecognised_severity_is_shown_as_sent() -> None:
    """A word the daemon adds later must still be readable."""

    assert severity_message("Critical") == "Critical"
    assert severity_message("  HIGH ") == "High"
    assert severity_message("dire") == "dire"


def test_data_crossing_the_boundary_is_not_translated() -> None:
    """Package names and advisory ids are identifiers, not messages.

    They are asserted here because the tempting "translate everything from the
    daemon" change would break them, and it would look like a translation
    improvement while doing so.
    """

    from dendro.core.dnf5daemon import SecurityAdvisory

    advisory = SecurityAdvisory(
        advisory_id="FEDORA-2026-1a2b3c",
        title="Important: kernel security update",
        advisory_type="security",
        severity="Important",
        affected_packages=("kernel-core-6.12.1-200.fc44.x86_64",),
        cve_ids=("CVE-2026-1234",),
    )
    # Identifiers carry no catalog entry, so a lookup is a no-op; asserting that
    # is what keeps them out of the catalogues by accident.
    assert core_i18n.translate(advisory.advisory_id) == "FEDORA-2026-1a2b3c"
    assert core_i18n.translate(advisory.affected_packages[0]) == (
        "kernel-core-6.12.1-200.fc44.x86_64"
    )
    assert core_i18n.translate(advisory.cve_ids[0]) == "CVE-2026-1234"


def test_the_dialog_summary_translates_severities(qtbot) -> None:
    """End to end: a German summary must not contain English severity words.

    This is the defect in its original form. The summary is a translated
    sentence, and the severity words inside it were the daemon's raw English.
    """

    from dendro.core.dnf5daemon import SecurityAdvisory
    from dendro.ui.advisory_dialog import SecurityAdvisoryDialog

    original = core_i18n.active_locale()
    try:
        core_i18n.set_active_locale("de")
        dialog = SecurityAdvisoryDialog(
            (
                SecurityAdvisory("A", "t", "security", "Important"),
                SecurityAdvisory("B", "t", "security", "Low"),
            )
        )
        qtbot.addWidget(dialog)
        summary = dialog.explanation.text()
        assert "wichtig: 1" in summary, summary
        assert "niedrig: 1" in summary, summary
        for leaked in ("important", "low"):
            assert leaked not in summary, f"{leaked!r} left in English: {summary}"
    finally:
        core_i18n.clear_cache()
        core_i18n.set_active_locale(original)


def test_an_advisory_without_a_severity_reads_as_a_message(qtbot) -> None:
    """A missing severity must resolve to a message, not to a bare token.

    The empty word has no catalogue entry of its own, so the summary used to
    splice whatever sentinel stood in for it straight into the translated
    sentence: German read "(kritisch: 1, unspecified: 1)".
    """

    from dendro.core.dnf5daemon import SecurityAdvisory
    from dendro.ui.advisory_dialog import SecurityAdvisoryDialog

    original = core_i18n.active_locale()
    try:
        core_i18n.set_active_locale("de")
        dialog = SecurityAdvisoryDialog(
            (
                SecurityAdvisory("A", "t", "security", ""),
                SecurityAdvisory("B", "t", "security", "Critical"),
            )
        )
        qtbot.addWidget(dialog)
        summary = dialog.explanation.text()
        cells = [
            dialog.table.item(row, 0).text() for row in range(dialog.table.rowCount())
        ]
        assert "nicht festgelegt" in summary, summary
        assert "unspecified" not in summary.lower(), summary
        # The table and the summary must agree about the same advisory.
        assert "Nicht festgelegt" in cells, cells
        assert "Kritisch" in cells, cells
    finally:
        core_i18n.clear_cache()
        core_i18n.set_active_locale(original)
