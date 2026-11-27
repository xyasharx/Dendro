"""Regression coverage for the reproducible taxonomy-audit boundary."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from dendro.core.classification_audit import (
    ClassificationAuditInputError,
    PackageClassificationRecord,
    audit_taxonomy_records,
)
from dendro.core.package_metadata import AppStreamCatalog, FedoraCompsCatalog
from dendro.core.taxonomy import TaxonomyInputScope


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
                "Full Perl module description",
                "Fedora Project",
                "Example Packager",
                f"/usr/share/perl5/vendor_perl{tool._LIST_SEPARATOR}",
                f"Module.pm{tool._LIST_SEPARATOR}",
                f"perl(Example::Module){tool._LIST_SEPARATOR}",
                f"perl(:MODULE_COMPAT_5.40.0){tool._LIST_SEPARATOR}",
                f"/usr/share/perl5/vendor_perl/Module.pm{tool._LIST_SEPARATOR}",
            )
        )
        + tool._RECORD_SEPARATOR
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
    assert record.description == "Full Perl module description"
    assert record.directories == ("/usr/share/perl5/vendor_perl",)
    assert record.file_paths == ("/usr/share/perl5/vendor_perl/Module.pm",)
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


def test_audit_forwards_source_rpm_requires_and_remote_desktop_signals() -> None:
    appstream, comps = _empty_catalogs()
    appstream.desktop_packages.add("blender")

    blender_data = PackageClassificationRecord(
        name="blender-data",
        source_rpm="blender-4.2.0-1.fc43.src.rpm",
        vendor="Fedora COPR - example/blender",
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    ).classify(appstream=appstream, comps=comps)
    assert blender_data.primary_category == "desktop_addons"
    assert any("COPR" in line for line in blender_data.rationale)

    x_driver = PackageClassificationRecord(
        name="xorg-x11-drv-custom",
        requires=("xserver-abi(videodrv-25) >= 0",),
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    ).classify(appstream=appstream, comps=comps)
    assert x_driver.primary_category == "graphics_drivers"

    selinux_pkg = PackageClassificationRecord(
        name="container-selinux",
        requires=("selinux-policy >= 41.0",),
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    ).classify(appstream=appstream, comps=comps)
    assert selinux_pkg.primary_category == "security_pkgs"

    gnome_ext = PackageClassificationRecord(
        name="custom-gnome-extension",
        requires=("gnome-shell >= 47",),
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    ).classify(appstream=appstream, comps=comps)
    assert gnome_ext.primary_category == "desktop_addons"

    py_abi_pkg = PackageClassificationRecord(
        name="custom-py-helper",
        requires=("python(abi) = 3.13",),
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    ).classify(appstream=appstream, comps=comps)
    assert py_abi_pkg.primary_category == "python_pkgs"

    remote_gui = PackageClassificationRecord(
        name="cool-gui-app",
        provides=(
            "application(org.example.Cool.desktop)",
            "mimehandler(application/pdf)",
        ),
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    ).classify(appstream=appstream, comps=comps)
    assert remote_gui.primary_category == "user_apps"
    assert remote_gui.sub_category == "desktop_office"

    for opaque_name in ("tree", "opaque-package"):
        opaque = PackageClassificationRecord(
            name=opaque_name,
            summary="Opaque helper"
            if opaque_name == "opaque-package"
            else "Display directory tree",
            input_scope=TaxonomyInputScope.REMOTE_METADATA,
        ).classify(appstream=appstream, comps=comps)
        assert opaque.primary_category == "unclassified"


def test_audit_with_system_catalogs_enables_system_desktop_index_parity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dendro.core.package_metadata import SystemDesktopIndex

    xdg_share = tmp_path / "share"
    apps_kde4 = xdg_share / "applications" / "kde4"
    apps_kde4.mkdir(parents=True)
    (apps_kde4 / "showcase.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Showcase\n"
        "Exec=showcase\n"
        "Categories=Graphics;\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_DIRS", str(xdg_share))
    SystemDesktopIndex.reset_instance()
    try:
        record = PackageClassificationRecord(
            name="showcase-pkg",
            summary="Interactive showcase",
            directories=("/usr/bin/", "/usr/share/applications/kde4/"),
            basenames=("showcase", "showcase.desktop"),
            file_paths=(
                "/usr/bin/showcase",
                "/usr/share/applications/kde4/showcase.desktop",
            ),
            input_scope=TaxonomyInputScope.INSTALLED_MANIFEST,
        )
        portable = record.classify(
            appstream=AppStreamCatalog(load_catalog=False),
            comps=FedoraCompsCatalog(load_comps=False),
            use_system_desktop_index=False,
        )
        with_desktop = record.classify(
            appstream=AppStreamCatalog(load_catalog=False),
            comps=FedoraCompsCatalog(load_comps=False),
            use_system_desktop_index=True,
        )
        assert with_desktop.primary_category == "user_apps"
        assert with_desktop.sub_category == "desktop_graphics"
        assert portable.sub_category != "desktop_graphics"
        assert portable.sub_category == "desktop_utilities"

        tool = _load_audit_tool()
        corpus = tmp_path / "corpus.jsonl"
        corpus.write_text(
            json.dumps(record.to_json_mapping()) + "\n",
            encoding="utf-8",
        )
        out = tmp_path / "report.json"
        monkeypatch.setattr(
            tool.AppStreamCatalog,
            "get_instance",
            classmethod(lambda cls: AppStreamCatalog(load_catalog=False)),
        )
        monkeypatch.setattr(
            tool.FedoraCompsCatalog,
            "get_instance",
            classmethod(lambda cls: FedoraCompsCatalog(load_comps=False)),
        )
        exit_code = tool.main(
            ["--input", str(corpus), "--with-system-catalogs", "--output", str(out)]
        )
        assert exit_code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["by_primary_category"] == {"user_apps": 1}
    finally:
        SystemDesktopIndex.reset_instance()
