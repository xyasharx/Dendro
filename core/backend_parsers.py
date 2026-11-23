# dendro/core/backend_parsers.py
"""Pure parsing and normalization helpers for RPM/DNF CLI and daemon records."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from core.dnf5daemon import DnfHistoryTransaction
from core.package_classifier import ProductionTaxonomyEngine
from core.package_metadata import PackagePhysicalAnatomy
from core.package_types import (
    HistoryEntry,
    PackageInfo,
    PackageState,
    _normalise_rpm_epoch,
)
from core.taxonomy import ClassificationDecision, TaxonomyInputScope

LOGGER = logging.getLogger(__name__)

# Precompiled regular expression used by changelog inspection.
CVE_REGEX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(CVE-\d{4}-\d{4,7})\b", re.IGNORECASE
)
# Validate RPM and DNF package names before external query output changes model state.
RPM_PACKAGE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+~-]*"
)
# RPM stores imported signing keys as ``gpg-pubkey`` pseudo-package records.
RPM_DATABASE_PSEUDO_PACKAGE_NAMES: Final[frozenset[str]] = frozenset({"gpg-pubkey"})

# DNF5's query formatter preserves literal control characters and expands list
# values such as ``%{provides}`` with embedded newlines.
REMOTE_QUERY_FIELD_SEPARATOR: Final[str] = "\x1f"
REMOTE_QUERY_RECORD_SEPARATOR: Final[str] = "\x1e"

RPM_VERIFY_MISSING_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^missing\s+(?:(?P<markers>[a-z]+)\s+)?(?P<path>/.*)$"
)
RPM_VERIFY_CHANGED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<flags>[.SM5DLUGTP?]{8,9})\s+(?:(?P<markers>[a-z]+)\s+)?(?P<path>/.*)$"
)

# `PackageQuery::filter_userinstalled` in libdnf5 keeps installed packages whose
# transaction reason is neither DEPENDENCY nor WEAK_DEPENDENCY.
_NON_USERINSTALLED_REASONS: Final[frozenset[str]] = frozenset(
    {"dependency", "weak dependency"}
)


def classification_package_fields(decision: ClassificationDecision) -> dict[str, Any]:
    """Adapt immutable taxonomy output to legacy :class:`PackageInfo` fields."""
    return {
        "parent_pillar": decision.parent_pillar,
        "sub_category": decision.sub_category,
        "primary_category": decision.primary_category,
        "classification_confidence": decision.confidence,
        "classification_evidence": decision.evidence.value,
        "classification_input_scope": decision.input_scope.value,
        "classification_rationale": list(decision.rationale),
        "secondary_tags": list(decision.secondary_tags),
        **dict(decision.flags),
    }


def _classify_local_rpm_manifest(
    name: str,
    summary: str,
    description: str,
    directories: list[str],
    provides: list[str],
    basenames: list[str],
) -> ClassificationDecision | None:
    """Classify a local RPM without letting taxonomy failure hide its header."""
    anatomy = PackagePhysicalAnatomy.from_manifest_data(
        directories,
        provides,
        basenames,
    )
    try:
        return ProductionTaxonomyEngine.classify(
            name=name,
            summary=summary,
            description=description,
            anatomy=anatomy,
            provides=provides,
            input_scope=TaxonomyInputScope.LOCAL_RPM_MANIFEST,
        )
    except Exception:
        LOGGER.warning(
            "Local RPM taxonomy classification failed for %r; retaining header inspection.",
            name,
            exc_info=True,
        )
        return None


@dataclass(frozen=True, slots=True)
class _RemotePackageRecord:
    """One normalized remote-query result, including optional RPM facts."""

    name: str
    version: str
    release: str
    arch: str
    repository: str
    size_bytes: int
    summary: str
    provides: tuple[str, ...] = ()


def _is_dnf5_binary(dnf_binary: str) -> bool:
    """Return whether the preferred executable is the DNF5 CLI contract."""
    return os.path.basename(dnf_binary).startswith("dnf5") or os.path.basename(
        os.path.realpath(dnf_binary)
    ).startswith("dnf5")


def _parse_dnf_package_identity_output(output: str, query_name: str) -> set[str]:
    """Parse exact installed RPM identities from a fixed DNF query format."""
    package_nevras: set[str] = set()
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        fields = raw_line.split("\x1f")
        if len(fields) != 5:
            raise RuntimeError(
                f"{query_name} returned an invalid package-identity record: {raw_line}"
            )
        name, raw_epoch, version, release, arch = (field.strip() for field in fields)
        if name in RPM_DATABASE_PSEUDO_PACKAGE_NAMES:
            continue
        if (
            RPM_PACKAGE_NAME_PATTERN.fullmatch(name) is None
            or not version
            or not release
            or not arch
        ):
            raise RuntimeError(
                f"{query_name} returned an invalid package-identity record: {raw_line}"
            )
        package_nevras.add(
            PackageInfo(
                name=name,
                version=version,
                release=release,
                arch=arch,
                epoch=_normalise_rpm_epoch(raw_epoch),
                state=PackageState.INSTALLED,
            ).full_nevra
        )
    return package_nevras


def _parse_userinstalled_fd_records(records: Sequence[Mapping[str, Any]]) -> set[str]:
    """Extract user-installed NEVRAs from ``Rpm.list_fd`` installed JSON records."""
    user_pkg_nevras: set[str] = set()
    for raw in records:
        if not isinstance(raw, Mapping):
            raise RuntimeError("DNF5 daemon returned a non-map package record.")
        name = str(raw.get("name") or "").strip()
        if name in RPM_DATABASE_PSEUDO_PACKAGE_NAMES:
            continue
        version = str(raw.get("version") or "").strip()
        release = str(raw.get("release") or "").strip()
        arch = str(raw.get("arch") or "").strip()
        if (
            RPM_PACKAGE_NAME_PATTERN.fullmatch(name) is None
            or not version
            or not release
            or not arch
        ):
            raise RuntimeError(
                f"DNF5 daemon returned an invalid installed package record: {raw!r}"
            )
        reason = str(raw.get("reason") or "").strip().casefold()
        if reason in _NON_USERINSTALLED_REASONS:
            continue
        user_pkg_nevras.add(
            PackageInfo(
                name=name,
                version=version,
                release=release,
                arch=arch,
                epoch=_normalise_rpm_epoch(raw.get("epoch", "")),
                state=PackageState.INSTALLED,
            ).full_nevra
        )
    return user_pkg_nevras


def _parse_remote_package_records(
    output: str, *, includes_provides: bool
) -> tuple[_RemotePackageRecord, ...]:
    """Parse fixed DNF query output without guessing list-valued boundaries."""
    raw_records = (
        output.split(REMOTE_QUERY_RECORD_SEPARATOR)
        if includes_provides
        else output.splitlines()
    )
    records: list[_RemotePackageRecord] = []
    expected_fields = 8 if includes_provides else 7
    for raw_record in raw_records:
        if not raw_record:
            continue
        fields = raw_record.split(
            REMOTE_QUERY_FIELD_SEPARATOR,
            expected_fields - 1,
        )
        if len(fields) != expected_fields:
            raise RuntimeError("DNF repoquery returned a malformed package record.")
        name, version, release, arch, repository, raw_size, summary = fields[:7]
        name, version, release, arch = (
            field.strip() for field in (name, version, release, arch)
        )
        if not all((name, version, release, arch)):
            raise RuntimeError(
                "DNF repoquery returned a package record with missing identity fields."
            )
        try:
            size_bytes = int(raw_size.strip())
        except ValueError as exc:
            raise RuntimeError(
                "DNF repoquery returned a package record with an invalid size."
            ) from exc
        if size_bytes < 0:
            raise RuntimeError(
                "DNF repoquery returned a package record with a negative size."
            )

        provides: tuple[str, ...] = ()
        if includes_provides:
            raw_provides = fields[7]
            if REMOTE_QUERY_FIELD_SEPARATOR in raw_provides:
                raise RuntimeError(
                    "DNF repoquery returned an unsafe capability-list record."
                )
            provides = tuple(
                line.strip() for line in raw_provides.split("\n") if line.strip()
            )

        records.append(
            _RemotePackageRecord(
                name=name,
                version=version,
                release=release,
                arch=arch,
                repository=repository.strip(),
                size_bytes=size_bytes,
                summary=summary.strip(),
                provides=provides,
            )
        )
    return tuple(records)


def _parse_remote_fd_records(
    raw_records: Sequence[Mapping[str, Any]],
) -> tuple[_RemotePackageRecord, ...]:
    """Validate and convert ``Rpm.list_fd`` JSON records into remote candidates."""
    records: list[_RemotePackageRecord] = []
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise RuntimeError("DNF5 daemon returned a malformed package record.")
        name = str(raw.get("name") or "").strip()
        version = str(raw.get("version") or "").strip()
        release = str(raw.get("release") or "").strip()
        arch = str(raw.get("arch") or "").strip()
        if not all((name, version, release, arch)):
            raise RuntimeError(
                "DNF5 daemon returned a package record with missing identity fields."
            )
        raw_size = raw.get("install_size", 0)
        if isinstance(raw_size, bool):
            raise RuntimeError(
                "DNF5 daemon returned a package record with an invalid size."
            )
        try:
            size_bytes = int(raw_size or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "DNF5 daemon returned a package record with an invalid size."
            ) from exc
        if size_bytes < 0:
            raise RuntimeError(
                "DNF5 daemon returned a package record with a negative size."
            )

        raw_provides = raw.get("provides", ())
        provides_list: list[str] = []
        if isinstance(raw_provides, str):
            provides_list = [
                line.strip() for line in raw_provides.splitlines() if line.strip()
            ]
        elif isinstance(raw_provides, Sequence) and not isinstance(
            raw_provides, (bytes, bytearray)
        ):
            provides_list = [
                str(item).strip() for item in raw_provides if str(item).strip()
            ]
        elif raw_provides is not None:
            raise RuntimeError(
                "DNF5 daemon returned a package record with invalid provides."
            )

        records.append(
            _RemotePackageRecord(
                name=name,
                version=version,
                release=release,
                arch=arch,
                repository=str(
                    raw.get("repo_id", raw.get("repository", "")) or ""
                ).strip(),
                size_bytes=size_bytes,
                summary=str(raw.get("summary") or "").strip(),
                provides=tuple(provides_list),
            )
        )
    return tuple(records)


def _history_entry_from_daemon_transaction(
    tx: DnfHistoryTransaction,
) -> HistoryEntry:
    """Adapt one D-Bus ``History.list`` transaction to a UI ``HistoryEntry``."""
    try:
        date_time = datetime.fromtimestamp(tx.start_time).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError) as exc:
        raise RuntimeError(
            f"DNF5 daemon returned an unsupported start time for transaction {tx.transaction_id}."
        ) from exc

    status_text = tx.status.strip()
    actions_text = ", ".join(tx.actions)
    if actions_text and status_text.casefold() in {"", "ok"}:
        action = actions_text
    elif actions_text:
        action = f"{actions_text} ({status_text})"
    else:
        action = status_text or "Recorded"

    return HistoryEntry(
        id=tx.transaction_id,
        command_line=tx.command_line,
        date_time=date_time,
        action=action,
        altered_count=tx.altered_count,
        return_code=0,
    )


def _parse_dnf5_history_json(output: str) -> list[HistoryEntry]:
    """Parse DNF5's documented machine-readable history-list response."""
    try:
        records = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"dnf5 history returned invalid JSON: {exc.msg}.") from exc

    if not isinstance(records, list):
        raise RuntimeError(
            "dnf5 history returned a JSON document that is not a transaction list."
        )

    entries: list[HistoryEntry] = []
    seen_ids: set[int] = set()
    for position, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise RuntimeError(
                f"dnf5 history returned an invalid JSON record at position {position}."
            )

        transaction_id = record.get("id")
        start_time = record.get("start_time")
        altered_count = record.get("altered_count", 0)
        command_line = record.get("command_line", "")
        status = record.get("status", "")

        if (
            isinstance(transaction_id, bool)
            or not isinstance(transaction_id, int)
            or transaction_id < 1
            or transaction_id in seen_ids
        ):
            raise RuntimeError(
                f"dnf5 history returned an invalid transaction ID at position {position}."
            )
        if (
            isinstance(start_time, bool)
            or not isinstance(start_time, int)
            or start_time < 0
        ):
            raise RuntimeError(
                f"dnf5 history returned an invalid start time for transaction {transaction_id}."
            )
        if (
            isinstance(altered_count, bool)
            or not isinstance(altered_count, int)
            or altered_count < 0
        ):
            raise RuntimeError(
                f"dnf5 history returned an invalid altered-package count for transaction {transaction_id}."
            )
        if command_line is None:
            command_line = ""
        if status is None:
            status = ""
        if not isinstance(command_line, str) or not isinstance(status, str):
            raise RuntimeError(
                f"dnf5 history returned invalid text fields for transaction {transaction_id}."
            )

        try:
            date_time = datetime.fromtimestamp(start_time).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError) as exc:
            raise RuntimeError(
                f"dnf5 history returned an unsupported start time for transaction {transaction_id}."
            ) from exc

        seen_ids.add(transaction_id)
        entries.append(
            HistoryEntry(
                id=transaction_id,
                command_line=command_line,
                date_time=date_time,
                action=status.strip() or "Recorded",
                altered_count=altered_count,
                return_code=0,
            )
        )
    return entries


def _dnf5_json_option_is_unsupported(
    result: subprocess.CompletedProcess[str],
) -> bool:
    """Recognize only an old CLI rejecting the machine-readable option."""
    diagnostic = f"{result.stdout}\n{result.stderr}".casefold()
    return "--json" in diagnostic and any(
        marker in diagnostic
        for marker in ("unknown argument", "unknown option", "unrecognized option")
    )


__all__ = (
    "CVE_REGEX_PATTERN",
    "REMOTE_QUERY_FIELD_SEPARATOR",
    "REMOTE_QUERY_RECORD_SEPARATOR",
    "RPM_DATABASE_PSEUDO_PACKAGE_NAMES",
    "RPM_PACKAGE_NAME_PATTERN",
    "RPM_VERIFY_CHANGED_PATTERN",
    "RPM_VERIFY_MISSING_PATTERN",
    "_RemotePackageRecord",
    "_classify_local_rpm_manifest",
    "_dnf5_json_option_is_unsupported",
    "_history_entry_from_daemon_transaction",
    "_is_dnf5_binary",
    "_parse_dnf5_history_json",
    "_parse_dnf_package_identity_output",
    "_parse_remote_fd_records",
    "_parse_remote_package_records",
    "_parse_userinstalled_fd_records",
    "classification_package_fields",
)
