# dendro/core/dnf5daemon_parsers.py
"""Response parsers for DNF5 daemon D-Bus method calls and signals."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from dendro.core.dnf5daemon_transport import _unwrap_dbus_value
from dendro.core.dnf5daemon_types import (
    TRANSACTION_ERROR,
    Dnf5DaemonCallError,
    Dnf5DaemonUnsupportedError,
    DnfCompsGroup,
    DnfCompsPackage,
    DnfHistoryTransaction,
    DnfPackageRecord,
    RepositoryKeyPrompt,
    SecurityAdvisory,
    TransactionItem,
    TransactionRequest,
    _as_text,
    _require_sequence,
)

_HISTORY_ACTION_KEYS: tuple[tuple[str, str], ...] = (
    ("installed", "Install"),
    ("removed", "Remove"),
    ("upgraded", "Upgrade"),
    ("downgraded", "Downgrade"),
    ("reinstalled", "Reinstall"),
)


def _key_import_prompt(arguments: tuple[Any, ...]) -> RepositoryKeyPrompt:
    """Decode ``(session_path, key_id, user_ids, fingerprint, url, timestamp)``."""
    if len(arguments) < 5:
        raise ValueError(
            "repo_key_import_request carried "
            f"{len(arguments)} argument(s), expected at least five."
        )
    session_path = arguments[0]
    if not isinstance(session_path, str) or not session_path:
        raise ValueError("repo_key_import_request has no session object path.")
    raw_user_ids = arguments[2]
    if isinstance(raw_user_ids, str):
        user_ids: tuple[str, ...] = (raw_user_ids,) if raw_user_ids else ()
    else:
        try:
            user_ids = tuple(str(value) for value in raw_user_ids if value)
        except TypeError:
            user_ids = ()
    return RepositoryKeyPrompt(
        session_path=session_path,
        key_id=str(arguments[1] or ""),
        fingerprint=str(arguments[3] or ""),
        key_url=str(arguments[4] or ""),
        user_ids=user_ids,
    )


def _key_import_arguments(arguments: tuple[Any, ...]) -> tuple[str, str, str]:
    """Compact ``(session_path, key_id, fingerprint)`` view of a key prompt."""
    prompt = _key_import_prompt(arguments)
    return prompt.session_path, prompt.key_id, prompt.fingerprint or prompt.key_url


def _parse_transaction_item(raw: Any) -> TransactionItem:
    values = _require_sequence(raw, "transaction item")
    if len(values) != 5:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid data",
            "A transaction item does not have five fields.",
        )
    object_type, action, reason, attributes, package = values
    if not isinstance(attributes, Mapping) or not isinstance(package, Mapping):
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid data",
            "A transaction item has invalid attributes or package metadata.",
        )
    if not all(isinstance(value, str) for value in (object_type, action, reason)):
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid data",
            "A transaction item has a missing object type, action, or reason.",
        )
    if not object_type.strip():
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid data",
            "A transaction item has a missing object type, action, or reason.",
        )
    attribute_data = {
        str(key): _unwrap_dbus_value(value) for key, value in attributes.items()
    }
    if object_type.strip().casefold() == "skipped":
        normalized_action = action.strip() or "Skipped"
        raw_skipped_reason = attribute_data.get("reason_skipped")
        skipped_reason = (
            raw_skipped_reason.strip().replace("_", " ")
            if isinstance(raw_skipped_reason, str) and raw_skipped_reason.strip()
            else "skipped by resolver"
        )
        normalized_reason = reason.strip() or skipped_reason
    else:
        if not action.strip() or not reason.strip():
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid data",
                "A transaction item has a missing object type, action, or reason.",
            )
        normalized_action = action
        normalized_reason = reason
    package_data = {
        str(key): _unwrap_dbus_value(value) for key, value in package.items()
    }
    identity = next(
        (
            package_data.get(field)
            for field in ("name", "groupid", "id")
            if package_data.get(field)
        ),
        None,
    )
    if not isinstance(identity, str) or not identity.strip():
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid data",
            "A transaction item is missing its package or group identity.",
        )
    return TransactionItem(
        object_type=object_type,
        action=normalized_action,
        reason=normalized_reason,
        attributes=attribute_data,
        package=package_data,
    )


def _advisory_affected_packages(raw: Any) -> tuple[str, ...]:
    """Return NEVRAs from this advisory that are installed and outdated."""
    affected: list[str] = []
    for collection in _require_sequence(raw, "advisory collections"):
        if not isinstance(collection, Mapping):
            continue
        for package in _require_sequence(
            collection.get("packages", ()), "advisory packages"
        ):
            if not isinstance(package, Mapping):
                continue
            applicability = _as_text(package.get("applicability", "")).casefold()
            if applicability != "available":
                continue
            nevra = _as_text(package.get("nevra", "")).strip()
            if nevra:
                affected.append(nevra)
    return tuple(affected)


def _advisory_references(raw: Any) -> tuple[tuple[str, str], tuple[str, str]]:
    """Split advisory references into CVE ids and Bugzilla ids."""
    cves: list[str] = []
    bugzillas: list[str] = []
    for reference in _require_sequence(raw, "advisory references"):
        if not isinstance(reference, Mapping):
            continue
        identifier = _as_text(reference.get("id", "")).strip()
        kind = _as_text(reference.get("type", "")).strip().casefold()
        if not identifier:
            continue
        if kind == "cve":
            cves.append(identifier)
        elif kind == "bugzilla":
            bugzillas.append(identifier)
    return tuple(cves), tuple(bugzillas)


def _parse_advisories(response: tuple[Any, ...]) -> tuple[SecurityAdvisory, ...]:
    """Validate and convert the ``Advisory.list`` reply into advisory records."""
    if len(response) != 1:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid advisory data",
            "Advisory.list did not return exactly one array.",
        )
    advisories: list[SecurityAdvisory] = []
    for raw in _require_sequence(response[0], "advisories"):
        if not isinstance(raw, Mapping):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid advisory data",
                "Advisory.list returned a non-map item.",
            )
        advisory_id = _as_text(raw.get("advisoryid", "")).strip()
        if not advisory_id:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid advisory data",
                "An advisory is missing its identifier.",
            )
        affected = _advisory_affected_packages(raw.get("collections", ()))
        cves, bugzillas = _advisory_references(raw.get("references", ()))
        advisories.append(
            SecurityAdvisory(
                advisory_id=advisory_id,
                title=_as_text(raw.get("title", "")).strip(),
                advisory_type=_as_text(raw.get("type", "")).strip(),
                severity=_as_text(raw.get("severity", "")).strip(),
                affected_packages=affected,
                cve_ids=cves,
                bugzilla_ids=bugzillas,
                description=_as_text(raw.get("description", "")).strip(),
            )
        )
    actionable = [item for item in advisories if item.affects_installed_packages]
    actionable.sort(key=lambda item: (item.severity_key, item.advisory_id))
    return tuple(actionable)


def _extract_repository_urls(raw: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Extract display URL and all declared source URLs from ``Repo.list``."""
    collected: list[str] = []
    raw_baseurl = raw.get("baseurl")
    if isinstance(raw_baseurl, str):
        candidate = raw_baseurl.strip()
        if candidate:
            collected.append(candidate)
    elif isinstance(raw_baseurl, Sequence) and not isinstance(
        raw_baseurl, (bytes, bytearray)
    ):
        for entry in raw_baseurl:
            if not isinstance(entry, str):
                raise Dnf5DaemonCallError(
                    "DNF5 daemon returned invalid repositories",
                    "Repository.list returned a baseurl entry that is not text.",
                )
            candidate = entry.strip()
            if candidate:
                collected.append(candidate)
    elif raw_baseurl is not None:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid repositories",
            "Repository.list returned a baseurl value that is neither text nor a string array.",
        )

    for key in ("metalink", "mirrorlist"):
        raw_value = raw.get(key)
        if raw_value is None:
            continue
        if not isinstance(raw_value, str):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid repositories",
                f"Repository.list returned a {key} value that is not text.",
            )
        candidate = raw_value.strip()
        if candidate:
            collected.append(candidate)

    deduplicated = tuple(dict.fromkeys(collected))
    primary = deduplicated[0] if deduplicated else ""
    return primary, deduplicated


def _parse_package_records(
    response: tuple[Any, ...], label: str
) -> tuple[DnfPackageRecord, ...]:
    if len(response) != 1:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid package data",
            f"Rpm.list did not return exactly one array for {label}.",
        )

    records: list[DnfPackageRecord] = []
    for raw in _require_sequence(response[0], label):
        if not isinstance(raw, Mapping):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                f"Rpm.list returned a non-map item for {label}.",
            )
        name = _as_text(raw.get("name", "")).strip()
        epoch = _as_text(raw.get("epoch", "")).strip()
        if epoch in {"", "(none)"}:
            epoch = "0"
        version = _as_text(raw.get("version", "")).strip()
        release = _as_text(raw.get("release", "")).strip()
        arch = _as_text(raw.get("arch", "")).strip()
        if not name or not version or not release or not arch:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                f"An {label} item is missing its name, version, release, or architecture.",
            )
        raw_size = raw.get("install_size", raw.get("download_size", 0))
        if isinstance(raw_size, bool):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                f"An {label} item has an invalid package size.",
            )
        try:
            size_bytes = int(raw_size or 0)
        except (TypeError, ValueError) as exc:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                f"An {label} item has an invalid package size.",
            ) from exc
        if size_bytes < 0:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid package data",
                f"An {label} item has a negative package size.",
            )
        records.append(
            DnfPackageRecord(
                name=name,
                version=version,
                release=release,
                arch=arch,
                repository=_as_text(
                    raw.get("repo_id", raw.get("repository", ""))
                ).strip(),
                epoch=epoch,
                summary=_as_text(raw.get("summary", "")).strip(),
                size_bytes=size_bytes,
            )
        )
    return tuple(records)


def _parse_fd_package_json_line(line: str) -> dict[str, Any]:
    """Decode one NDJSON package record emitted by ``Rpm.list_fd``."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid package JSON line from DNF5 daemon: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("DNF5 daemon package stream item is not a JSON object.")
    return payload


def _parse_int_field(raw_value: Any, *, field_name: str, minimum: int = 0) -> int:
    if isinstance(raw_value, bool) or raw_value is None:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid history data",
            f"History.list returned an invalid {field_name}.",
        )
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid history data",
            f"History.list returned a non-integer {field_name}.",
        ) from exc
    if parsed < minimum:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid history data",
            f"History.list returned an out-of-range {field_name}.",
        )
    return parsed


def _parse_history_transactions(
    response: tuple[Any, ...],
) -> tuple[DnfHistoryTransaction, ...]:
    """Validate and convert ``History.list`` output into typed history transactions."""
    if len(response) != 1:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid history data",
            "History.list did not return exactly one array.",
        )

    transactions: list[DnfHistoryTransaction] = []
    for raw in _require_sequence(response[0], "history transactions"):
        if not isinstance(raw, Mapping):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid history data",
                "History.list returned a non-map item.",
            )
        tx_id = _parse_int_field(raw.get("id"), field_name="transaction id", minimum=1)
        start_time = _parse_int_field(
            raw.get("start", 0), field_name="start timestamp", minimum=0
        )
        end_time = _parse_int_field(
            raw.get("end", start_time), field_name="end timestamp", minimum=0
        )
        command_line = _as_text(raw.get("description", "")).strip() or "System Action"
        status = _as_text(raw.get("status", "")).strip()

        altered_count = 0
        actions: list[str] = []
        has_package_lists = False
        for key, label in _HISTORY_ACTION_KEYS:
            bucket = raw.get(key)
            if bucket is None:
                continue
            has_package_lists = True
            items = _require_sequence(bucket, f"history {key}")
            if items:
                altered_count += len(items)
                actions.append(label)

        if not has_package_lists and "altered" in raw:
            altered_count = _parse_int_field(
                raw.get("altered"), field_name="altered count", minimum=0
            )

        transactions.append(
            DnfHistoryTransaction(
                transaction_id=tx_id,
                command_line=command_line,
                start_time=start_time,
                end_time=end_time,
                status=status,
                altered_count=altered_count,
                actions=tuple(actions),
            )
        )

    transactions.sort(key=lambda tx: tx.transaction_id, reverse=True)
    return tuple(transactions)


def _parse_comps_groups(response: tuple[Any, ...]) -> tuple[DnfCompsGroup, ...]:
    """Decode ``org.rpm.dnf.v0.comps.Group.list`` output into typed groups."""
    if len(response) != 1:
        raise Dnf5DaemonCallError(
            "DNF5 daemon returned invalid group data",
            "comps.Group.list did not return exactly one array.",
        )

    groups: list[DnfCompsGroup] = []
    for raw in _require_sequence(response[0], "comps groups"):
        if not isinstance(raw, Mapping):
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid group data",
                "comps.Group.list returned a non-map item.",
            )
        groupid = _as_text(_unwrap_dbus_value(raw.get("groupid", ""))).strip()
        if not groupid:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned invalid group data",
                "A comps group entry is missing its groupid.",
            )
        name = _as_text(_unwrap_dbus_value(raw.get("name", ""))).strip()
        installed = bool(_unwrap_dbus_value(raw.get("installed", False)))
        uservisible = bool(_unwrap_dbus_value(raw.get("uservisible", False)))

        raw_repos = _unwrap_dbus_value(raw.get("repos", ()))
        repos: list[str] = []
        if raw_repos is not None:
            for repo_entry in _require_sequence(raw_repos, "comps group repos"):
                repo_id = _as_text(_unwrap_dbus_value(repo_entry)).strip()
                if repo_id:
                    repos.append(repo_id)

        raw_packages = _unwrap_dbus_value(raw.get("packages", ()))
        packages: list[DnfCompsPackage] = []
        if raw_packages is not None:
            for pkg_entry in _require_sequence(raw_packages, "comps group packages"):
                if isinstance(pkg_entry, str):
                    pkg_name = pkg_entry.strip()
                    if pkg_name:
                        packages.append(DnfCompsPackage(name=pkg_name))
                    continue
                if not isinstance(pkg_entry, Mapping):
                    raise Dnf5DaemonCallError(
                        "DNF5 daemon returned invalid group data",
                        "A comps group package entry is not a map.",
                    )
                pkg_name = _as_text(
                    _unwrap_dbus_value(pkg_entry.get("name", ""))
                ).strip()
                if not pkg_name:
                    continue
                raw_type = _unwrap_dbus_value(pkg_entry.get("type", 0))
                if isinstance(raw_type, bool):
                    package_type = 0
                else:
                    try:
                        package_type = int(raw_type or 0)
                    except (TypeError, ValueError):
                        package_type = 0
                condition = _as_text(
                    _unwrap_dbus_value(pkg_entry.get("condition", ""))
                ).strip()
                packages.append(
                    DnfCompsPackage(
                        name=pkg_name,
                        package_type=package_type,
                        condition=condition,
                    )
                )

        groups.append(
            DnfCompsGroup(
                groupid=groupid,
                name=name,
                installed=installed,
                uservisible=uservisible,
                repos=tuple(repos),
                packages=tuple(packages),
            )
        )
    return tuple(groups)


_OFFLINE_TEST_CODE_RE = re.compile(
    r"offline\s+rpm\s+transaction\s+test\s+failed\s+with\s+code\s+(-?\d+)\.?",
    re.IGNORECASE,
)

# Maps ``libdnf5::base::Transaction::TransactionRunResult`` integer values to
# clear explanations when ``dnf5daemon-server`` throws ``ERROR_TRANSACTION``
# without detailed RPM test problem lines (see rpm-software-management/dnf5#2416).
_TRANSACTION_RUN_RESULT_DETAILS: dict[int, tuple[str, str]] = {
    1: (
        "This transaction has already been run (code 1: ERROR_RERUN).",
        "Resolve a fresh transaction plan and try staging again.",
    ),
    2: (
        "Dependency resolution problems remained in the transaction (code 2: ERROR_RESOLVE).",
        "Discard this plan, refresh package metadata, and resolve again before staging.",
    ),
    3: (
        "Failed to obtain the RPM transaction lock because another package operation is in progress (code 3: ERROR_LOCK).",
        "Wait for other package managers or background updates to finish, then resolve and stage this transaction again.",
    ),
    4: (
        "The RPM pre-transaction check failed (code 4: ERROR_CHECK).",
        "",
    ),
    5: (
        "The RPM transaction test failed (code 5: ERROR_RPM_RUN).",
        "",
    ),
    6: (
        "Package OpenPGP signature verification failed (code 6: ERROR_GPG_CHECK).",
        "Verify that the required repository signing keys are imported and trusted, or inspect the local RPM package signature before staging again.",
    ),
    7: (
        "Installed system packages changed after this transaction plan was resolved (code 7: ERROR_SYSTEM_CHANGED).",
        "Refresh package metadata in Dendro and resolve a new transaction plan before staging again.",
    ),
}


def _offline_rpm_diagnostic_guidance(request: TransactionRequest | None) -> str:
    if request is not None and request.upgrade_all:
        terminal_hint = (
            "Check available disk space with 'df -h', or run 'sudo dnf5 upgrade --offline' "
            "in a terminal to inspect the exact RPM test error."
        )
    else:
        terminal_hint = (
            "Check available disk space with 'df -h', or test the equivalent operation with "
            "'dnf5' ('--offline') in a terminal to inspect the exact RPM test error."
        )
    return (
        "Fedora's DNF5 daemon does not currently report individual RPM test failures "
        "over D-Bus. Common causes include insufficient free disk space on '/' or '/boot', "
        "file conflicts between packages, or RPM-level dependency errors.\n\n"
        f"{terminal_hint}"
    )


def _format_offline_transaction_error(
    error: Dnf5DaemonCallError,
    request: TransactionRequest | None = None,
) -> Dnf5DaemonCallError:
    """Enrich opaque ``Goal.do_transaction(offline=True)`` RPM test errors."""
    if isinstance(error, Dnf5DaemonUnsupportedError):
        return error

    raw_detail = (error.detail or "").strip()
    code_match = _OFFLINE_TEST_CODE_RE.search(raw_detail)
    is_transaction_error = error.dbus_name == TRANSACTION_ERROR
    if not is_transaction_error and code_match is None:
        return error

    non_empty_lines = [line for line in raw_detail.splitlines() if line.strip()]
    if len(non_empty_lines) > 1:
        return Dnf5DaemonCallError(
            "Offline transaction staging failed",
            "The daemon's offline RPM transaction test failed. "
            "No transaction was scheduled for the next boot.\n\n"
            f"{raw_detail}",
            dbus_name=error.dbus_name,
        )

    if code_match is not None:
        code = int(code_match.group(1))
        summary, specific_guidance = _TRANSACTION_RUN_RESULT_DETAILS.get(
            code,
            (
                f"The daemon's offline RPM transaction test failed (code {code}).",
                "",
            ),
        )
        guidance = specific_guidance or _offline_rpm_diagnostic_guidance(request)
    else:
        lowered = raw_detail.casefold()
        if (
            "transaction lock" in lowered
            or "another transaction is in progress" in lowered
        ):
            summary = "Failed to obtain the RPM transaction lock because another package operation is in progress."
            guidance = (
                "Wait for other package managers or background updates to finish, "
                "then resolve and stage this transaction again."
            )
        elif "signature verification failed" in lowered or "openpgp" in lowered:
            summary = "Package OpenPGP signature verification failed."
            guidance = (
                "Verify that the required repository signing keys are imported and trusted, "
                "or inspect the local RPM package signature before staging again."
            )
        elif "system repository has changed" in lowered:
            summary = "Installed system packages changed after this transaction plan was resolved."
            guidance = (
                "Refresh package metadata in Dendro and resolve a new transaction plan "
                "before staging again."
            )
        else:
            summary = "The RPM transaction test failed during offline staging."
            guidance = _offline_rpm_diagnostic_guidance(request)

    paragraphs = [
        f"{summary} No transaction was scheduled for the next boot.",
        guidance,
    ]
    if raw_detail:
        paragraphs.append(f"Daemon message: {raw_detail}")

    return Dnf5DaemonCallError(
        "Offline transaction staging failed",
        "\n\n".join(paragraphs),
        dbus_name=error.dbus_name,
    )
