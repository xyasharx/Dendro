#!/usr/bin/env python3
"""Audit Dendro taxonomy coverage from normalized RPM package facts.

Examples:
  tools/dendro-taxonomy-audit.py --installed-rpm-database --output report.json
  tools/dendro-taxonomy-audit.py --input corpus.jsonl --top 40

JSONL rows use the ``PackageClassificationRecord`` schema.  The tool never
executes package payloads; it reads package metadata only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Final

# ruff: noqa: E402
# This developer tool is intentionally runnable straight from a source checkout;
# Python otherwise places only its ``tools`` directory on sys.path.
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from core.classification_audit import (
    ClassificationAuditInputError,
    PackageClassificationRecord,
    audit_taxonomy_records,
)
from core.package_metadata import AppStreamCatalog, FedoraCompsCatalog
from core.taxonomy import TaxonomyInputScope

# Unit Separator is safe between fixed fields. Bell is deliberately used inside
# repeated RPM tag lists because Python's ``str.splitlines`` treats the nearby
# ASCII record/group separators as line boundaries.
_FIELD_SEPARATOR: Final = "\x1f"
_LIST_SEPARATOR: Final = "\x07"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Dendro taxonomy coverage from saved RPM facts or the local "
            "installed RPM database."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--input",
        type=Path,
        metavar="JSONL",
        help="Normalized package facts in newline-delimited JSON; use - for stdin.",
    )
    source.add_argument(
        "--installed-rpm-database",
        action="store_true",
        help="Read installed package metadata through the local rpm command.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="JSON",
        help="Write the JSON report atomically to this path instead of stdout.",
    )
    parser.add_argument(
        "--top",
        type=_positive_int,
        default=25,
        help="Number of unresolved clusters to retain per signal type (default: 25).",
    )
    parser.add_argument(
        "--examples",
        type=_positive_int,
        default=5,
        help="Maximum package examples retained per unresolved cluster (default: 5).",
    )
    parser.add_argument(
        "--with-system-catalogs",
        action="store_true",
        help=(
            "Include local AppStream and Fedora Comps catalogs. Omit this for a "
            "portable metadata-only corpus comparison."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records = (
            _iter_installed_rpm_database()
            if args.installed_rpm_database
            else _iter_jsonl_records(args.input)
        )
        if args.with_system_catalogs:
            appstream = AppStreamCatalog.get_instance()
            comps = FedoraCompsCatalog.get_instance()
        else:
            appstream = AppStreamCatalog(load_catalog=False)
            comps = FedoraCompsCatalog(load_comps=False)
        report = audit_taxonomy_records(
            records,
            appstream=appstream,
            comps=comps,
            cluster_limit=args.top,
            examples_per_cluster=args.examples,
        )
    except (ClassificationAuditInputError, OSError, RuntimeError, ValueError) as exc:
        print(f"dendro-taxonomy-audit: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(report.to_json_mapping(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(payload)
    else:
        _write_atomically(args.output, payload)
    return 0


def _iter_jsonl_records(input_path: Path) -> Iterator[PackageClassificationRecord]:
    if str(input_path) == "-":
        yield from _parse_jsonl_rows(sys.stdin, "stdin")
        return
    try:
        with input_path.open(encoding="utf-8") as stream:
            yield from _parse_jsonl_rows(stream, str(input_path))
    except OSError as exc:
        raise RuntimeError(f"could not read JSONL input {input_path}: {exc}") from exc


def _parse_jsonl_rows(
    lines: Iterable[str], source_name: str
) -> Iterator[PackageClassificationRecord]:
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ClassificationAuditInputError(
                f"{source_name}:{line_number}: invalid JSON: {exc.msg}"
            ) from exc
        try:
            yield PackageClassificationRecord.from_json_mapping(raw)
        except ClassificationAuditInputError as exc:
            raise ClassificationAuditInputError(
                f"{source_name}:{line_number}: {exc}"
            ) from exc


def _iter_installed_rpm_database() -> Iterator[PackageClassificationRecord]:
    rpm_binary = shutil.which("rpm")
    if rpm_binary is None:
        raise RuntimeError(
            "--installed-rpm-database requires an rpm command on this host"
        )

    query_format = (
        f"%{{NAME}}{_FIELD_SEPARATOR}%{{ARCH}}{_FIELD_SEPARATOR}%{{SOURCERPM}}"
        f"{_FIELD_SEPARATOR}%{{SUMMARY}}{_FIELD_SEPARATOR}%{{VENDOR}}"
        f"{_FIELD_SEPARATOR}%{{PACKAGER}}{_FIELD_SEPARATOR}"
        f"[%{{DIRNAMES}}{_LIST_SEPARATOR}]{_FIELD_SEPARATOR}"
        f"[%{{BASENAMES}}{_LIST_SEPARATOR}]{_FIELD_SEPARATOR}"
        f"[%{{PROVIDENAME}}{_LIST_SEPARATOR}]{_FIELD_SEPARATOR}"
        f"[%{{REQUIRENAME}}{_LIST_SEPARATOR}]\n"
    )
    try:
        completed = subprocess.run(
            [rpm_binary, "-qa", "--queryformat", query_format],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=_clean_rpm_environment(),
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"could not query the installed RPM database: {type(exc).__name__}: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip() or f"rpm exited with status {completed.returncode}"
        )
        raise RuntimeError(f"could not query the installed RPM database: {detail}")

    for line_number, line in enumerate(completed.stdout.splitlines(), start=1):
        if not line:
            continue
        fields = line.split(_FIELD_SEPARATOR)
        if len(fields) != 10:
            raise RuntimeError(
                "rpm returned a malformed installed-package record at "
                f"line {line_number}: expected 10 fields, found {len(fields)}"
            )
        (
            name,
            architecture,
            source_rpm,
            summary,
            vendor,
            packager,
            directories,
            basenames,
            provides,
            requires,
        ) = fields
        yield PackageClassificationRecord(
            name=name,
            summary=summary,
            directories=_split_rpm_list(directories),
            basenames=_split_rpm_list(basenames),
            provides=_split_rpm_list(provides),
            requires=_split_rpm_list(requires),
            architecture=architecture,
            source_rpm=source_rpm,
            vendor=vendor,
            packager=packager,
            input_scope=TaxonomyInputScope.INSTALLED_MANIFEST,
        )


def _split_rpm_list(value: str) -> tuple[str, ...]:
    return tuple(item for item in value.split(_LIST_SEPARATOR) if item)


def _clean_rpm_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for unsafe_name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME"):
        environment.pop(unsafe_name, None)
    return environment


def _write_atomically(output_path: Path, payload: str) -> None:
    output_path = output_path.expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, output_path)
    except OSError as exc:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"could not write audit report {output_path}: {exc}"
        ) from exc


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
