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
    assert dialog.open_cve_button.isEnabled() is False

    dialog.table.selectRow(0)

    text = dialog.detail.toPlainText()
    assert "FEDORA-2026-a1b2c3" in text
    assert "openssl-3.2.2-1.fc44.x86_64" in text
    assert "CVE-2026-33871" in text
    assert "A use-after-free was fixed." in text
    assert dialog.open_cve_button.isEnabled() is True


def test_an_advisory_without_cves_cannot_open_a_cve_page(qtbot) -> None:
    dialog = SecurityAdvisoryDialog((_advisory(cve_ids=(), bugzilla_ids=("2311223",)),))
    qtbot.addWidget(dialog)
    dialog.table.selectRow(0)

    assert dialog.open_cve_button.isEnabled() is False
    assert "2311223" in dialog.detail.toPlainText()


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
    assert "2 critical" in labels
    assert "1 moderate" in labels


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
