"""The security advisory dialog and the daemon call that feeds it."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtWidgets import QLabel

from core.dnf5daemon import (
    ADVISORY_INTERFACE,
    Dnf5DaemonClient,
    SecurityAdvisory,
    _parse_advisories,
)
from tests.test_dnf5daemon import RecordingTransport
from ui.advisory_dialog import SecurityAdvisoryDialog, _cve_browser_url


def _advisory(**overrides: Any) -> SecurityAdvisory:
    values: dict[str, Any] = {
        "advisory_id": "FEDORA-2026-a1b2c3",
        "title": "Critical: openssl security update",
        "advisory_type": "security",
        "severity": "Critical",
        "affected_packages": ("openssl-3.2.2-1.fc44.x86_64",),
        "cve_ids": ("CVE-2026-33871",),
        "bugzilla_ids": ("2311223",),
        "description": "A use-after-free was fixed.",
    }
    values.update(overrides)
    return SecurityAdvisory(**values)


def test_dialog_lists_severity_title_and_package(qtbot) -> None:
    dialog = SecurityAdvisoryDialog((_advisory(),))
    qtbot.addWidget(dialog)

    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 0).text() == "Critical"
    assert dialog.table.item(0, 1).text() == "Critical: openssl security update"
    assert dialog.table.item(0, 2).text() == "openssl-3.2.2-1.fc44.x86_64"
    assert "CVE-2026-33871" in dialog.table.item(0, 3).text()


def test_selecting_an_advisory_shows_its_details(qtbot) -> None:
    dialog = SecurityAdvisoryDialog((_advisory(),))
    qtbot.addWidget(dialog)

    text = dialog.detail.toPlainText()
    assert "FEDORA-2026-a1b2c3" in text
    assert "openssl-3.2.2-1.fc44.x86_64" in text
    assert "CVE-2026-33871" in text
    assert "A use-after-free was fixed." in text
    # The first advisory is selected on open, so the actions are live at once
    # rather than requiring a selection the user has no reason to expect.
    assert dialog.open_reference_button.isEnabled() is True
    assert dialog.open_reference_button.text() == "Open CVE-2026-33871"


def test_a_bugzilla_reference_is_reachable_without_a_cve(qtbot) -> None:
    """A Bugzilla-only advisory used to have no reachable page at all.

    The action was gated on ``cve_ids``, so an advisory that publishes only a
    bug number disabled the button and left the reference visible but unusable.
    """
    dialog = SecurityAdvisoryDialog((_advisory(cve_ids=(), bugzilla_ids=("2311223",)),))
    qtbot.addWidget(dialog)

    assert dialog.open_reference_button.isEnabled() is True
    # A bare bug number does not read as a page, so the caption names the kind.
    assert dialog.open_reference_button.text() == "Open Bug 2311223"
    assert "2311223" in dialog.detail.toPlainText()

    opened: list[str] = []
    import ui.advisory_dialog as module

    original = module.open_external_web_url
    module.open_external_web_url = lambda _parent, url, link_label="": (
        opened.append(url) or True
    )
    try:
        dialog.open_reference_button.click()
    finally:
        module.open_external_web_url = original
    assert opened == ["https://bugzilla.redhat.com/show_bug.cgi?id=2311223"]


def test_empty_result_explains_itself_instead_of_showing_a_blank_table(qtbot) -> None:
    dialog = SecurityAdvisoryDialog(())
    qtbot.addWidget(dialog)

    assert dialog.table.rowCount() == 0
    labels = [label.text() for label in dialog.findChildren(QLabel)]
    assert any("No published security advisory" in text for text in labels)


def test_summary_names_every_severity_present(qtbot) -> None:
    dialog = SecurityAdvisoryDialog(
        (
            _advisory(advisory_id="A", severity="Critical"),
            _advisory(advisory_id="B", severity="Critical"),
            _advisory(advisory_id="C", severity="Moderate"),
        )
    )
    qtbot.addWidget(dialog)

    labels = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "3 advisories" in labels
    # Counted by severity word, not by an adjective spliced onto a noun: the
    # latter has to agree in gender, number and case, which the languages
    # Dendro ships disagree about.
    assert "critical: 2" in labels
    assert "moderate: 1" in labels


def test_window_title_and_reference_url_are_sane(qtbot) -> None:
    assert _cve_browser_url("CVE-2026-1") == (
        "https://access.redhat.com/security/cve/CVE-2026-1"
    )
    dialog = SecurityAdvisoryDialog((_advisory(),))
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == "Security Advisories"


class _AdvisoryTransport(RecordingTransport):
    """Returns a canned advisory reply and records the options it was sent."""

    def __init__(self, advisories: list[dict]) -> None:
        super().__init__()
        self._advisories = advisories
        self.advisory_options: dict[str, Any] | None = None

    def call(
        self,
        path: str,
        interface: str,
        method: str,
        signatures: tuple[str, ...],
        arguments: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        if interface == ADVISORY_INTERFACE and method == "list":
            self.advisory_options = arguments[0]
            return (self._advisories,)
        return super().call(path, interface, method, signatures, arguments)


def test_client_asks_the_daemon_for_actionable_advisories() -> None:
    transport = _AdvisoryTransport(
        [
            {
                "advisoryid": "FEDORA-1",
                "title": "Critical: openssl",
                "type": "security",
                "severity": "Critical",
                "collections": [
                    {
                        "packages": [
                            {
                                "nevra": "openssl-3.2.2-1.fc44.x86_64",
                                "applicability": "available",
                            }
                        ]
                    }
                ],
            }
        ]
    )
    client = Dnf5DaemonClient(transport=transport)

    advisories = client.list_security_advisories()

    assert [item.advisory_id for item in advisories] == ["FEDORA-1"]
    assert transport.advisory_options is not None
    # Requesting only the attributes that are displayed keeps the reply small.
    assert "severity" in transport.advisory_options["advisory_attrs"]
    assert transport.advisory_options["interactive"] is False


def test_advisory_request_projects_every_attribute_the_parser_reads() -> None:
    """``advisory_attrs`` is a projection: unlisted keys are absent from the reply.

    The daemon returns *only* the attributes named in this option, so a parser
    that reads an attribute the request never asked for fails on every real
    reply while still passing against a hand-written fixture. ``advisoryid`` is
    the identity field the parser requires, and leaving it out made the Security
    panel reject every advisory as "missing its identifier".
    """
    from core.dnf5daemon import _parse_advisories

    transport = _AdvisoryTransport(
        [
            {
                "advisoryid": "FEDORA-1",
                "title": "Critical: openssl",
                "type": "security",
                "severity": "Critical",
                "description": "Fixed.",
                "references": [],
                "collections": [
                    {
                        "packages": [
                            {
                                "nevra": "openssl-3.2.2-1.fc44.x86_64",
                                "applicability": "available",
                            }
                        ]
                    }
                ],
            }
        ]
    )
    Dnf5DaemonClient(transport=transport).list_security_advisories()
    requested = set(transport.advisory_options["advisory_attrs"])

    # Every key the parser dereferences must be part of the projection.
    for attribute in (
        "advisoryid",
        "title",
        "type",
        "severity",
        "description",
        "references",
        "collections",
    ):
        assert attribute in requested, f"{attribute} is read but never requested"

    # The reply is filtered server-side to advisories with a pending fix.
    assert transport.advisory_options["availability"] == "available"

    # And the projection is self-sufficient: parsing the reply the daemon would
    # actually return (only the requested keys) must not raise.
    projected = {
        key: value for key, value in _ADVISORY_FIXTURE.items() if key in requested
    }
    parsed = _parse_advisories(([projected],))
    assert [item.advisory_id for item in parsed] == ["FEDORA-1"]


_ADVISORY_FIXTURE = {
    "advisoryid": "FEDORA-1",
    "title": "Critical: openssl",
    "type": "security",
    "severity": "Critical",
    "description": "Fixed.",
    "references": [],
    "collections": [
        {
            "packages": [
                {"nevra": "openssl-3.2.2-1.fc44.x86_64", "applicability": "available"}
            ]
        }
    ],
}


def test_client_returns_nothing_when_no_advisory_is_actionable() -> None:
    transport = _AdvisoryTransport(
        [
            {
                "advisoryid": "FEDORA-1",
                "severity": "Low",
                "collections": [
                    {
                        "packages": [
                            {"nevra": "x-1-1.fc44.x86_64", "applicability": "installed"}
                        ]
                    }
                ],
            }
        ]
    )

    assert Dnf5DaemonClient(transport=transport).list_security_advisories() == ()


def test_parse_rejects_a_reply_without_the_single_array() -> None:
    from core.dnf5daemon import Dnf5DaemonCallError

    with pytest.raises(Dnf5DaemonCallError):
        _parse_advisories(([], []))


def test_every_reference_is_listed_and_openable(qtbot) -> None:
    """All of an advisory's references are offered, not only the first.

    The panel used to open ``cve_ids[0]`` and nothing else, so a second CVE in
    the same advisory was displayed in the row and in the detail pane but had
    no way to be reached.
    """
    from ui.advisory_dialog import advisory_references

    advisory = _advisory(
        cve_ids=("CVE-2026-1", "CVE-2026-2"), bugzilla_ids=("2311223",)
    )
    assert advisory_references(advisory) == (
        ("cve", "CVE-2026-1"),
        ("cve", "CVE-2026-2"),
        ("bugzilla", "2311223"),
    )

    dialog = SecurityAdvisoryDialog((advisory,))
    qtbot.addWidget(dialog)
    # The button opens the first; the detail pane carries the rest as links.
    assert dialog.open_reference_button.text() == "Open CVE-2026-1"
    hrefs = [
        fragment.charFormat().anchorHref()
        for fragment in _anchor_fragments(dialog.detail.document())
    ]
    assert hrefs == [
        "https://access.redhat.com/security/cve/CVE-2026-1",
        "https://access.redhat.com/security/cve/CVE-2026-2",
        "https://bugzilla.redhat.com/show_bug.cgi?id=2311223",
    ]


def _anchor_fragments(document):
    """Yield every text fragment of ``document`` that is a link."""
    block = document.begin()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.isValid() and fragment.charFormat().isAnchor():
                yield fragment
            iterator += 1
        block = block.next()


def test_reference_links_are_coloured_from_the_theme(qtbot) -> None:
    """Qt paints anchors in ``QPalette.link()``, which is #0000ff by default.

    That colour is 1.71:1 against the dark card -- effectively invisible on the
    one surface this pane is drawn on. The colour has to come from the theme,
    and it has to be underlined so the affordance is not colour alone.

    It is set through the document's stylesheet rather than run-level formats,
    which also keeps this class from owning a ``QTextDocument``: ``setDocument``
    does not take ownership, and a document created per selection is a lifetime
    with no owner.
    """
    from core.settings import get_app_settings
    from ui.styles import get_delegate_palette

    for theme in ("dark", "light"):
        get_app_settings().setValue("theme", theme)
        dialog = SecurityAdvisoryDialog((_advisory(),))
        qtbot.addWidget(dialog)

        sheet = dialog.detail.document().defaultStyleSheet()
        accent = get_delegate_palette(theme)["accent"].name()
        assert accent in sheet
        assert "text-decoration: underline" in sheet
        # And the anchors themselves are real links to the right pages.
        hrefs = [
            fragment.charFormat().anchorHref()
            for fragment in _anchor_fragments(dialog.detail.document())
        ]
        # The default fixture carries one CVE and one Bugzilla reference.
        assert hrefs == [
            "https://access.redhat.com/security/cve/CVE-2026-33871",
            "https://bugzilla.redhat.com/show_bug.cgi?id=2311223",
        ]


def test_detail_text_is_escaped_rather_than_rendered_as_markup(qtbot) -> None:
    """Advisory text comes from repository metadata and is untrusted."""
    payload = "<b>bold</b> & <img src='http://example.invalid/x.png'>"
    dialog = SecurityAdvisoryDialog((_advisory(description=payload),))
    qtbot.addWidget(dialog)

    text = dialog.detail.toPlainText()
    assert "<img" in text
    assert "<b>bold</b>" in text
    document = dialog.detail.document()
    assert "<img" not in document.toHtml()


def test_an_empty_panel_hides_actions_that_cannot_do_anything(qtbot) -> None:
    """Nothing to select means nothing to open, copy, or read."""
    dialog = SecurityAdvisoryDialog(())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.table.isVisible() is False
    assert dialog.detail.isVisible() is False
    assert dialog.open_reference_button.isVisible() is False
    assert dialog.copy_button.isVisible() is False
    assert dialog.empty_hint.isVisible() is True


def test_the_panel_speaks_the_active_language(qtbot) -> None:
    """The column headers and the actions are translated, not English only.

    ``setHorizontalHeaderLabels`` given a literal list is invisible to the
    extraction tool, so the headers never reached a catalog and shipped in
    English in all seven languages. ``COLUMN_HEADERS`` exists so the ids are
    extractable; this asserts they are actually applied.
    """
    from core.i18n import set_active_locale

    try:
        set_active_locale("de")
        dialog = SecurityAdvisoryDialog((_advisory(),))
        qtbot.addWidget(dialog)
        headers = [
            dialog.table.horizontalHeaderItem(column).text()
            for column in range(dialog.table.columnCount())
        ]
        assert headers == ["Schweregrad", "Hinweis", "Betroffenes Paket", "Verweise"]
        assert dialog.open_reference_button.text() == "CVE-2026-33871 öffnen"
    finally:
        set_active_locale("en")


def test_the_detail_pane_starts_at_the_top_for_each_advisory(qtbot) -> None:
    """Switching rows must not leave the previous description's scroll offset."""
    # Long enough to overflow the pane is the whole requirement; a much longer
    # description only makes the document layout heavier than the test needs.
    long_description = "\n".join(f"line {index}" for index in range(60))
    dialog = SecurityAdvisoryDialog(
        (
            _advisory(advisory_id="A", description=long_description),
            _advisory(advisory_id="B", description=long_description),
        )
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    scrollbar = dialog.detail.verticalScrollBar()
    assert scrollbar.maximum() > 0, "the description did not overflow the pane"
    scrollbar.setValue(scrollbar.maximum())
    assert scrollbar.value() > 0

    dialog.table.selectRow(1)
    assert scrollbar.value() == 0
