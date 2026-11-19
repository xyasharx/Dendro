"""Security advisories and plan size accounting.

Both read facts the DNF5 daemon already sends: ``Advisory.list`` compares each
advisory against installed packages itself, and ``Goal.resolve`` items carry
``install_size`` and ``download_size``.  These tests pin the parsing rules and
the summaries derived from them, including the failure shapes a malformed reply
can take.
"""

from __future__ import annotations

import pytest

from core.dnf5daemon import (
    SecurityAdvisory,
    TransactionItem,
    TransactionPlan,
    TransactionRequest,
    _parse_advisories,
    _signed_size,
    _size_attribute,
    format_byte_size,
)


def _item(
    action: str,
    name: str,
    *,
    install_size: object = 0,
    download_size: object = 0,
    object_type: str = "Package",
) -> TransactionItem:
    return TransactionItem(
        object_type=object_type,
        action=action,
        reason="user",
        attributes={},
        package={
            "name": name,
            "version": "1.0",
            "release": "1.fc44",
            "arch": "x86_64",
            "install_size": install_size,
            "download_size": download_size,
        },
    )


def _resolved_plan(client) -> TransactionPlan:
    """Resolve a real plan through the client; plans are single-use capabilities."""
    return client.resolve(TransactionRequest.from_values(installs=["gimp"]), set())


def _plan(*items: TransactionItem) -> TransactionPlan:
    return TransactionPlan(
        token="token",
        session_path="/org/rpm/dnf/v0/session/1",
        request=TransactionRequest.from_values(installs=["x"]),
        items=items,
        resolver_result=0,
        warnings=(),
        critical_removals=(),
    )


# ---------------------------------------------------------------------------
# Size accounting
# ---------------------------------------------------------------------------


def test_plan_totals_downloads_and_the_net_disk_change() -> None:
    plan = _plan(
        _item("install", "gimp", install_size=100_000_000, download_size=40_000_000),
        _item("upgrade", "glibc", install_size=10_000_000, download_size=2_000_000),
        _item("remove", "old-plugin", install_size=30_000_000),
    )

    assert plan.download_size_total_bytes == 42_000_000
    # Removals free space, so the net effect is what fits on the disk.
    assert plan.install_size_delta_bytes == 80_000_000


def test_removal_only_plan_reports_a_negative_change() -> None:
    plan = _plan(_item("remove", "orphan", install_size=5_000_000))

    assert plan.install_size_delta_bytes == -5_000_000
    assert _signed_size(plan.install_size_delta_bytes).startswith("-")


def test_skipped_items_contribute_no_size() -> None:
    plan = _plan(
        _item("install", "a", install_size=1000, download_size=500),
        _item(
            "Skipped",
            "b",
            install_size=999_999,
            download_size=999_999,
            object_type="Skipped",
        ),
    )

    assert plan.download_size_total_bytes == 500
    assert plan.install_size_delta_bytes == 1000


@pytest.mark.parametrize("value", [None, True, False, -5, "nonsense", {}])
def test_unusable_size_fields_count_as_unknown(value: object) -> None:
    """A malformed size must never crash a plan or count as negative space."""
    assert _size_attribute(value) == 0


def test_unpackaged_items_report_no_size() -> None:
    """Groups and skipped objects have no size attributes at all."""
    item = TransactionItem(
        object_type="Group",
        action="install",
        reason="user",
        attributes={},
        package={"groupid": "core"},
    )

    assert item.install_size_bytes == 0
    assert item.download_size_bytes == 0


def test_plan_preview_reports_both_totals() -> None:
    text = _plan(
        _item("install", "gimp", install_size=100_000_000, download_size=40_000_000)
    ).preview_text()

    assert "Download:" in text
    assert "Disk change: +" in text


def test_no_change_plan_says_so_instead_of_printing_zero_bytes() -> None:
    plan = _plan(_item("install", "tiny"))
    assert "Disk change: no change" in plan.preview_text()


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0.0 B"),
        (512, "512.0 B"),
        (2048, "2.0 KB"),
        (5 * 1024**2, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
    ],
)
def test_byte_formatting_stays_in_one_decade_per_unit(size, expected) -> None:
    assert format_byte_size(size) == expected


# ---------------------------------------------------------------------------
# Advisory parsing
# ---------------------------------------------------------------------------


def _advisory(
    advisory_id: str,
    *,
    severity: str = "Important",
    packages: tuple[tuple[str, str], ...] = (
        ("glibc-2.41-3.fc44.x86_64", "available"),
    ),
    references: tuple[tuple[str, str], ...] = (),
) -> dict:
    return {
        "advisoryid": advisory_id,
        "title": f"Update {advisory_id}",
        "type": "security",
        "severity": severity,
        "description": "A flaw was fixed.",
        "references": [
            {"id": identifier, "type": kind, "title": "", "url": ""}
            for identifier, kind in references
        ],
        "collections": [
            {
                "packages": [
                    {
                        "name": nevra.split("-")[0],
                        "nevra": nevra,
                        "applicability": state,
                    }
                    for nevra, state in packages
                ]
            }
        ],
    }


def test_advisories_keep_only_pending_fixes() -> None:
    """Advisories already installed or for absent packages are not to-dos."""
    reply = (
        [
            _advisory(
                "FEDORA-1", packages=(("glibc-2.41-3.fc44.x86_64", "available"),)
            ),
            _advisory(
                "FEDORA-2", packages=(("glibc-2.41-3.fc44.x86_64", "installed"),)
            ),
            _advisory("FEDORA-3", packages=(("python3-1-1.fc44.x86_64", "unrelated"),)),
        ],
    )

    advisories = _parse_advisories(reply)

    assert [item.advisory_id for item in advisories] == ["FEDORA-1"]
    assert advisories[0].affected_packages == ("glibc-2.41-3.fc44.x86_64",)


def test_advisory_references_split_into_cves_and_bugzillas() -> None:
    reply = (
        [
            _advisory(
                "FEDORA-1",
                references=(
                    ("CVE-2026-1", "cve"),
                    ("CVE-2026-2", "cve"),
                    ("2311223", "bugzilla"),
                ),
            )
        ],
    )

    advisory = _parse_advisories(reply)[0]

    assert advisory.cve_ids == ("CVE-2026-1", "CVE-2026-2")
    assert advisory.bugzilla_ids == ("2311223",)


def test_advisories_are_ordered_by_severity() -> None:
    reply = (
        [
            _advisory("A-low", severity="Low"),
            _advisory("B-critical", severity="Critical"),
            _advisory("C-moderate", severity="Moderate"),
            _advisory("D-unspecified", severity=""),
        ],
    )

    order = [item.advisory_id for item in _parse_advisories(reply)]

    assert order == ["B-critical", "C-moderate", "A-low", "D-unspecified"]


def test_severity_keys_are_comparable_integers() -> None:
    """A string key would sort "10" before "2" and silently mis-order the list."""
    keys = [
        SecurityAdvisory("a", "", "", severity).severity_key
        for severity in ("Critical", "Important", "Low", "unknown")
    ]

    assert all(isinstance(key, int) for key in keys)
    assert keys == sorted(keys)


def test_a_non_map_advisory_is_rejected_rather_than_rendered() -> None:
    from core.dnf5daemon import Dnf5DaemonCallError

    with pytest.raises(Dnf5DaemonCallError):
        _parse_advisories((["not a map"],))


def test_an_advisory_without_an_identifier_is_rejected() -> None:
    from core.dnf5daemon import Dnf5DaemonCallError

    with pytest.raises(Dnf5DaemonCallError):
        _parse_advisories(([{"title": "no id"}],))


def test_advisory_list_reply_must_be_a_single_array() -> None:
    from core.dnf5daemon import Dnf5DaemonCallError

    with pytest.raises(Dnf5DaemonCallError):
        _parse_advisories(())


# ---------------------------------------------------------------------------
# Offline (next-boot) staging
# ---------------------------------------------------------------------------


def test_offline_execution_requests_the_offline_daemon_option() -> None:
    """Staging must ask the daemon to store the transaction, not run RPM now."""
    from core.dnf5daemon import Dnf5DaemonClient
    from tests.test_dnf5daemon import RecordingTransport

    transport = RecordingTransport()
    client = Dnf5DaemonClient(transport=transport)
    plan = _resolved_plan(client)

    result = client.execute_offline(plan)

    # No live transaction ran, so no completion signal is expected.
    assert result.completed_signal_received is False
    assert result.completed_successfully is True
    do_transaction = [call for call in transport.calls if call[2] == "do_transaction"]
    assert len(do_transaction) == 1
    options = do_transaction[0][4][0]
    assert options["offline"] is True
    assert options["interactive"] is False
    # Staging must not also run the live transaction.
    assert not any(call[2] == "run" for call in transport.calls)


def test_offline_execution_refuses_an_empty_plan() -> None:
    from core.dnf5daemon import Dnf5DaemonClient, Dnf5DaemonPlanError
    from tests.test_dnf5daemon import RecordingTransport

    transport = RecordingTransport()
    client = Dnf5DaemonClient(transport=transport)
    # A resolve that yields only skipped items is a plan with no changes.
    transport.resolve_items = [
        ("Skipped", "", "", {"reason_skipped": "conflict"}, {"name": "x"})
    ]
    plan = client.resolve(TransactionRequest.from_values(installs=["x"]), set())

    with pytest.raises(Dnf5DaemonPlanError, match="nothing was staged"):
        client.execute_offline(plan)


def test_offline_status_reports_the_daemon_flag() -> None:
    from core.dnf5daemon import Dnf5DaemonClient
    from tests.test_dnf5daemon import RecordingTransport

    class _StatusTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if method == "get_status":
                return (True, {"status": "pending"})
            return super().call(path, interface, method, signatures, arguments)

    assert (
        Dnf5DaemonClient(transport=_StatusTransport()).offline_transaction_pending()
        is True
    )


def test_offline_status_rejects_a_malformed_reply() -> None:
    from core.dnf5daemon import Dnf5DaemonCallError, Dnf5DaemonClient
    from tests.test_dnf5daemon import RecordingTransport

    class _BadStatusTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if method == "get_status":
                return ("not a bool", {})
            return super().call(path, interface, method, signatures, arguments)

    with pytest.raises(Dnf5DaemonCallError, match="did not return a pending flag"):
        Dnf5DaemonClient(transport=_BadStatusTransport()).offline_transaction_pending()


def test_review_dialog_offers_next_boot_staging_and_defaults_to_now(qtbot) -> None:
    """Staging is opt-in; the dialog must not silently defer an update."""
    from ui.transaction_plan_dialog import TransactionPlanReviewDialog

    plan = _plan(_item("install", "gimp", install_size=1000, download_size=500))
    dialog = TransactionPlanReviewDialog(plan)
    qtbot.addWidget(dialog)

    assert dialog.stage_for_next_boot() is False
    assert "next boot" in dialog.stage_checkbox.text()

    dialog.stage_checkbox.setChecked(True)
    assert dialog.stage_for_next_boot() is True


def test_review_dialog_shows_the_size_summary(qtbot) -> None:
    from PyQt6.QtWidgets import QLabel

    from ui.transaction_plan_dialog import TransactionPlanReviewDialog

    plan = _plan(
        _item(
            "install", "gimp", install_size=100 * 1024**2, download_size=40 * 1024**2
        ),
        _item("remove", "old", install_size=30 * 1024**2),
    )
    dialog = TransactionPlanReviewDialog(plan)
    qtbot.addWidget(dialog)

    labels = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "2 packages" in labels
    assert "1 / 1" in labels  # one install, one removal
    assert "40.0 MB" in labels
    # 1024-based units: 100 MiB added, 30 MiB freed.
    assert "+70.0 MB" in labels
