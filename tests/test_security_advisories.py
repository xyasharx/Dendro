"""Security advisories and plan size accounting.

Both read facts the DNF5 daemon already sends: ``Advisory.list`` compares each
advisory against installed packages itself, and ``Goal.resolve`` items carry
``install_size`` and ``download_size``.  These tests pin the parsing rules and
the summaries derived from them, including the failure shapes a malformed reply
can take.
"""

from __future__ import annotations

import pytest

from dendro.core.dnf5daemon import (
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
        (0, "0 bytes"),
        (512, "512 bytes"),
        (2048, "2.0 kB"),
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
    from dendro.core.dnf5daemon import Dnf5DaemonCallError

    with pytest.raises(Dnf5DaemonCallError):
        _parse_advisories((["not a map"],))


def test_an_advisory_without_an_identifier_is_rejected() -> None:
    from dendro.core.dnf5daemon import Dnf5DaemonCallError

    with pytest.raises(Dnf5DaemonCallError):
        _parse_advisories(([{"title": "no id"}],))


def test_advisory_list_reply_must_be_a_single_array() -> None:
    from dendro.core.dnf5daemon import Dnf5DaemonCallError

    with pytest.raises(Dnf5DaemonCallError):
        _parse_advisories(())


# ---------------------------------------------------------------------------
# Offline (next-boot) staging
# ---------------------------------------------------------------------------


def test_offline_execution_requests_the_offline_daemon_option() -> None:
    """Staging must ask the daemon to store the transaction with Polkit interaction."""
    from dendro.core.dnf5daemon import Dnf5DaemonClient
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
    assert options["interactive"] is True
    # Staging must not also run the live transaction.
    assert not any(call[2] == "run" for call in transport.calls)


@pytest.mark.parametrize(
    ("daemon_message", "expected_snippets"),
    [
        (
            "offline rpm transaction test failed with code 5.",
            ("code 5: ERROR_RPM_RUN", "disk space", "file conflicts", "'df -h'"),
        ),
        (
            "offline rpm transaction test failed with code 4.",
            ("code 4: ERROR_CHECK", "disk space", "'dnf5' ('--offline')"),
        ),
        (
            "offline rpm transaction test failed with code 3.",
            ("code 3: ERROR_LOCK", "Wait for other package managers"),
        ),
        (
            "offline rpm transaction test failed with code 6.",
            ("code 6: ERROR_GPG_CHECK", "OpenPGP signature verification"),
        ),
        (
            "offline rpm transaction test failed with code 7.",
            ("code 7: ERROR_SYSTEM_CHANGED", "resolve a new transaction plan"),
        ),
        (
            "offline rpm transaction test failed with code 99.",
            (
                "code 99",
                "Daemon message: offline rpm transaction test failed with code 99.",
            ),
        ),
    ],
)
def test_offline_execution_explains_opaque_rpm_test_code_failures(
    daemon_message: str, expected_snippets: tuple[str, ...]
) -> None:
    """Upstream dnf5daemon-server omits RPM test problems over D-Bus (dnf5#2416)."""
    from dendro.core.dnf5daemon import (
        GOAL_INTERFACE,
        TRANSACTION_ERROR,
        Dnf5DaemonCallError,
        Dnf5DaemonClient,
    )
    from tests.test_dnf5daemon import RecordingTransport

    class _FailingOfflineTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == GOAL_INTERFACE and method == "do_transaction":
                raise Dnf5DaemonCallError(
                    "DNF5 daemon request failed",
                    daemon_message,
                    dbus_name=TRANSACTION_ERROR,
                )
            return super().call(path, interface, method, signatures, arguments)

    client = Dnf5DaemonClient(transport=_FailingOfflineTransport())
    plan = client.resolve(TransactionRequest.from_values(installs=["gimp"]), set())

    with pytest.raises(Dnf5DaemonCallError) as exc_info:
        client.execute_offline(plan)

    err = exc_info.value
    assert err.title == "Offline transaction staging failed"
    assert err.dbus_name == TRANSACTION_ERROR
    assert "No transaction was scheduled for the next boot." in err.detail
    assert f"Daemon message: {daemon_message}" in err.detail
    for snippet in expected_snippets:
        assert snippet in err.detail
    client.discard(plan)


def test_offline_execution_upgrade_all_guidance_mentions_offline_upgrade_check() -> (
    None
):
    from dendro.core.dnf5daemon import (
        GOAL_INTERFACE,
        TRANSACTION_ERROR,
        Dnf5DaemonCallError,
        Dnf5DaemonClient,
    )
    from tests.test_dnf5daemon import RecordingTransport

    class _FailingUpgradeTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == GOAL_INTERFACE and method == "do_transaction":
                raise Dnf5DaemonCallError(
                    "DNF5 daemon request failed",
                    "Transaction failed: Rpm transaction failed..",
                    dbus_name=TRANSACTION_ERROR,
                )
            return super().call(path, interface, method, signatures, arguments)

    client = Dnf5DaemonClient(transport=_FailingUpgradeTransport())
    plan = client.resolve(TransactionRequest.from_values(upgrade_all=True), set())

    with pytest.raises(Dnf5DaemonCallError) as exc_info:
        client.execute_offline(plan)

    err = exc_info.value
    assert err.title == "Offline transaction staging failed"
    assert "'sudo dnf5 upgrade --offline'" in err.detail
    assert "Daemon message: Transaction failed: Rpm transaction failed.." in err.detail
    client.discard(plan)


@pytest.mark.parametrize(
    ("daemon_message", "expected_phrase"),
    [
        (
            "Transaction failed: Failed to obtain rpm transaction lock. Another transaction is in progress..",
            "Wait for other package managers",
        ),
        (
            "Transaction failed: Signature verification failed..",
            "OpenPGP signature verification",
        ),
        (
            "Transaction failed: This transaction is no longer valid because the system repository has changed since it was loaded..",
            "resolve a new transaction plan",
        ),
    ],
)
def test_offline_execution_tailors_guidance_for_transaction_failed_variants(
    daemon_message: str, expected_phrase: str
) -> None:
    from dendro.core.dnf5daemon import (
        GOAL_INTERFACE,
        TRANSACTION_ERROR,
        Dnf5DaemonCallError,
        Dnf5DaemonClient,
    )
    from tests.test_dnf5daemon import RecordingTransport

    class _VariantTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == GOAL_INTERFACE and method == "do_transaction":
                raise Dnf5DaemonCallError(
                    "DNF5 daemon request failed",
                    daemon_message,
                    dbus_name=TRANSACTION_ERROR,
                )
            return super().call(path, interface, method, signatures, arguments)

    client = Dnf5DaemonClient(transport=_VariantTransport())
    plan = _resolved_plan(client)

    with pytest.raises(Dnf5DaemonCallError) as exc_info:
        client.execute_offline(plan)

    assert expected_phrase in exc_info.value.detail
    assert f"Daemon message: {daemon_message}" in exc_info.value.detail
    client.discard(plan)


def test_offline_execution_preserves_multiline_daemon_transaction_errors() -> None:
    from dendro.core.dnf5daemon import (
        GOAL_INTERFACE,
        TRANSACTION_ERROR,
        Dnf5DaemonCallError,
        Dnf5DaemonClient,
    )
    from tests.test_dnf5daemon import RecordingTransport

    multiline = (
        "Transaction failed: Rpm transaction failed.\n"
        "  - installing package firefox needs 102MB more space on the / filesystem"
    )

    class _DetailedTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == GOAL_INTERFACE and method == "do_transaction":
                raise Dnf5DaemonCallError(
                    "DNF5 daemon request failed",
                    multiline,
                    dbus_name=TRANSACTION_ERROR,
                )
            return super().call(path, interface, method, signatures, arguments)

    client = Dnf5DaemonClient(transport=_DetailedTransport())
    plan = _resolved_plan(client)

    with pytest.raises(Dnf5DaemonCallError) as exc_info:
        client.execute_offline(plan)

    err = exc_info.value
    assert err.title == "Offline transaction staging failed"
    assert "needs 102MB more space" in err.detail
    assert "does not currently report individual RPM test failures" not in err.detail
    client.discard(plan)


def test_offline_execution_preserves_polkit_and_unsupported_errors() -> None:
    from dendro.core.dnf5daemon import (
        GOAL_INTERFACE,
        Dnf5DaemonCallError,
        Dnf5DaemonClient,
        Dnf5DaemonUnsupportedError,
    )
    from tests.test_dnf5daemon import RecordingTransport

    class _AuthErrorTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == GOAL_INTERFACE and method == "do_transaction":
                raise Dnf5DaemonCallError(
                    "DNF5 daemon request failed",
                    "Not authorized",
                    dbus_name="org.freedesktop.DBus.Error.Failed",
                )
            return super().call(path, interface, method, signatures, arguments)

    client = Dnf5DaemonClient(transport=_AuthErrorTransport())
    plan = _resolved_plan(client)
    with pytest.raises(Dnf5DaemonCallError) as auth_exc:
        client.execute_offline(plan)
    assert auth_exc.value.title == "DNF5 daemon request failed"
    assert auth_exc.value.detail == "Not authorized"
    client.discard(plan)

    class _UnsupportedTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == GOAL_INTERFACE and method == "do_transaction":
                raise Dnf5DaemonUnsupportedError(
                    "DNF5 daemon interface unsupported",
                    "Unknown method",
                    dbus_name="org.freedesktop.DBus.Error.UnknownMethod",
                )
            return super().call(path, interface, method, signatures, arguments)

    client2 = Dnf5DaemonClient(transport=_UnsupportedTransport())
    plan2 = _resolved_plan(client2)
    with pytest.raises(Dnf5DaemonUnsupportedError, match="Unknown method"):
        client2.execute_offline(plan2)
    client2.discard(plan2)


def test_offline_execution_refuses_an_empty_plan() -> None:
    from dendro.core.dnf5daemon import Dnf5DaemonClient, Dnf5DaemonPlanError
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
    from dendro.core.dnf5daemon import Dnf5DaemonClient
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
    from dendro.core.dnf5daemon import Dnf5DaemonCallError, Dnf5DaemonClient
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
    from dendro.ui.transaction_plan_dialog import TransactionPlanReviewDialog

    plan = _plan(_item("install", "gimp", install_size=1000, download_size=500))
    dialog = TransactionPlanReviewDialog(plan)
    qtbot.addWidget(dialog)

    assert dialog.stage_for_next_boot() is False
    assert "next boot" in dialog.stage_checkbox.text()

    dialog.stage_checkbox.setChecked(True)
    assert dialog.stage_for_next_boot() is True


def test_review_dialog_shows_the_size_summary(qtbot) -> None:
    from PyQt6.QtWidgets import QLabel

    from dendro.ui.transaction_plan_dialog import TransactionPlanReviewDialog

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
