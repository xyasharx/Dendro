"""Regression coverage for the reproducible taxonomy-audit boundary."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from core.classification_audit import (
    ClassificationAuditInputError,
    PackageClassificationRecord,
    audit_taxonomy_records,
)
from core.package_metadata import AppStreamCatalog, FedoraCompsCatalog
from core.taxonomy import TaxonomyInputScope


def _empty_catalogs() -> tuple[AppStreamCatalog, FedoraCompsCatalog]:
    return AppStreamCatalog(load_catalog=False), FedoraCompsCatalog(load_comps=False)


def _load_audit_tool() -> ModuleType:
    path = Path(__file__).parents[1] / "tools" / "dendro-taxonomy-audit.py"
    spec = importlib.util.spec_from_file_location("dendro_taxonomy_audit_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_reports_perl_evidence_and_retains_truly_opaque_records() -> None:
    appstream, comps = _empty_catalogs()
    report = audit_taxonomy_records(
        (
            PackageClassificationRecord(
                name="perl-Example-Module",
                directories=("/usr/share/perl5/vendor_perl/Example",),
                provides=("perl(Example::Module)",),
                input_scope=TaxonomyInputScope.REMOTE_METADATA,
            ),
            PackageClassificationRecord(
                name="python3-example",
                provides=("python3dist(example)",),
                input_scope=TaxonomyInputScope.REMOTE_METADATA,
            ),
            PackageClassificationRecord(
                name="opaque-package",
                provides=("opaque-runtime(Component)",),
                input_scope=TaxonomyInputScope.REMOTE_METADATA,
            ),
        ),
        appstream=appstream,
        comps=comps,
        cluster_limit=10,
        examples_per_cluster=2,
    )

    assert report.total_records == 3
    assert report.automatically_classified == 2
    assert report.unresolved == 1
    assert dict(report.by_primary_category) == {
        "perl_pkgs": 1,
        "python_pkgs": 1,
        "unclassified": 1,
    }
    assert dict(report.by_scope) == {"remote_metadata": 3}
    assert len(report.unresolved_capability_clusters) == 1
    opaque_cluster = report.unresolved_capability_clusters[0]
    assert opaque_cluster.key == "opaque-runtime"
    assert opaque_cluster.count == 1
    assert opaque_cluster.examples == ("opaque-package",)
    assert report.unresolved_directory_clusters == ()
    assert report.unresolved_name_clusters[0].key == "opaque"
    assert report.to_json_mapping()["schema_version"] == 1


def test_audit_record_validates_json_schema_without_silently_dropping_facts() -> None:
    record = PackageClassificationRecord.from_json_mapping(
        {
            "name": "ruby-example",
            "provides": ["rubygem(example)"],
            "requires": [],
            "input_scope": "remote_metadata",
        }
    )

    assert record.name == "ruby-example"
    assert record.provides == ("rubygem(example)",)
    assert record.input_scope == TaxonomyInputScope.REMOTE_METADATA
    assert record.to_json_mapping()["input_scope"] == "remote_metadata"

    with pytest.raises(ClassificationAuditInputError, match="unknown audit field"):
        PackageClassificationRecord.from_json_mapping(
            {"name": "example", "untracked_fact": "must not disappear"}
        )
    with pytest.raises(ClassificationAuditInputError, match="array of strings"):
        PackageClassificationRecord.from_json_mapping(
            {"name": "example", "provides": "perl(Example)"}
        )


def test_audit_rejects_invalid_limits_and_wrong_record_types() -> None:
    with pytest.raises(ValueError, match="cluster_limit"):
        audit_taxonomy_records((), cluster_limit=0)
    with pytest.raises(TypeError, match="PackageClassificationRecord"):
        audit_taxonomy_records((object(),))  # type: ignore[arg-type]


def test_audit_tool_reads_installed_rpm_records_without_executing_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_audit_tool()
    captured: dict[str, object] = {}
    row = (
        tool._FIELD_SEPARATOR.join(
            (
                "perl-Example-Module",
                "noarch",
                "perl-Example-Module-1-1.src.rpm",
                "Example Perl module",
                "Fedora Project",
                "Example Packager",
                f"/usr/share/perl5/vendor_perl{tool._LIST_SEPARATOR}",
                f"Module.pm{tool._LIST_SEPARATOR}",
                f"perl(Example::Module){tool._LIST_SEPARATOR}",
                f"perl(:MODULE_COMPAT_5.40.0){tool._LIST_SEPARATOR}",
            )
        )
        + "\n"
    )

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=row, stderr="")

    monkeypatch.setattr(tool.shutil, "which", lambda _name: "/usr/bin/rpm")
    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    monkeypatch.setenv("LD_PRELOAD", "/untrusted.so")

    records = list(tool._iter_installed_rpm_database())

    assert len(records) == 1
    record = records[0]
    assert record.name == "perl-Example-Module"
    assert record.directories == ("/usr/share/perl5/vendor_perl",)
    assert record.provides == ("perl(Example::Module)",)
    assert record.requires == ("perl(:MODULE_COMPAT_5.40.0)",)
    assert record.input_scope == TaxonomyInputScope.INSTALLED_MANIFEST
    assert captured["command"][:2] == ["/usr/bin/rpm", "-qa"]
    assert "LD_PRELOAD" not in captured["kwargs"]["env"]  # type: ignore[index]


def test_audit_tool_reads_jsonl_and_writes_a_stable_report(tmp_path: Path) -> None:
    input_path = tmp_path / "corpus.jsonl"
    output_path = tmp_path / "report.json"
    input_path.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "name": "perl-Example-Module",
                        "directories": ["/usr/share/perl5/vendor_perl/Example"],
                        "provides": ["perl(Example::Module)"],
                        "input_scope": "remote_metadata",
                    }
                ),
                json.dumps(
                    {
                        "name": "python3-example",
                        "provides": ["python3dist(example)"],
                        "input_scope": "remote_metadata",
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    tool_path = Path(__file__).parents[1] / "tools" / "dendro-taxonomy-audit.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(tool_path),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--top",
            "3",
            "--examples",
            "2",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["total_records"] == 2
    assert report["unresolved"] == 0
    assert report["by_primary_category"] == {
        "perl_pkgs": 1,
        "python_pkgs": 1,
    }
    assert report["unresolved_clusters"]["capability"] == []
