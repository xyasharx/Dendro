"""CLI startup tests that do not require a Qt runtime."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

from core.build_provenance import (
    BuildProvenance,
    format_version_identity,
    stamp_build_info,
)
import main as dendro_main


PROJECT_ROOT = Path(__file__).parents[1]
_OBJECT_ID = "a" * 40
_TREE_ID = "b" * 40


def _project_version() -> str:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def _run_without_site_packages(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the source entry point as a minimal packaging environment would."""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-S", "main.py", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_version_does_not_import_the_gui_runtime() -> None:
    result = _run_without_site_packages("--version")

    assert result.returncode == 0
    pattern = (
        rf"dendro {re.escape(_project_version())} "
        r"\(Git commit [0-9a-f]{40}(?:[0-9a-f]{24})?; "
        r"Git tree [0-9a-f]{40}(?:[0-9a-f]{24})?(?:; dirty)?\)\n"
    )
    assert re.fullmatch(pattern, result.stdout)
    assert result.stderr == ""


def test_gui_launch_reports_a_missing_qt_runtime_explicitly() -> None:
    result = _run_without_site_packages()

    assert result.returncode == 1
    assert "PyQt6 GUI runtime is unavailable" in result.stderr


def test_source_bundle_version_path_uses_stamped_build_info(tmp_path: Path) -> None:
    """AppImage and Flatpak copy the package plus canonical project metadata."""
    bundle = tmp_path / "dendro"
    bundle.mkdir()
    shutil.copy2(PROJECT_ROOT / "main.py", bundle / "main.py")
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", bundle / "pyproject.toml")
    shutil.copytree(PROJECT_ROOT / "core", bundle / "core")
    provenance = BuildProvenance(
        commit=_OBJECT_ID,
        tree=_TREE_ID,
        source="test",
    )
    stamp_build_info(bundle / "core" / "_build_info.py", provenance)

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-S", str(bundle / "main.py"), "--version"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert (
        result.stdout
        == f"dendro {format_version_identity(_project_version(), provenance)}\n"
    )
    assert result.stderr == ""


def test_version_identity_marks_a_dirty_source_checkout() -> None:
    provenance = BuildProvenance(
        commit=_OBJECT_ID,
        tree=_TREE_ID,
        dirty=True,
        source="git",
    )

    assert format_version_identity("2.14.0", provenance) == (
        f"2.14.0 (Git commit {_OBJECT_ID}; Git tree {_TREE_ID}; dirty)"
    )


def test_release_version_is_consistent_across_python_rpm_and_appstream_metadata() -> (
    None
):
    import xml.etree.ElementTree as ET

    project_version = _project_version()

    spec_source = (PROJECT_ROOT / "dendro.spec").read_text(encoding="utf-8")
    spec_match = re.search(r"^Version:\s+(.+)$", spec_source, flags=re.MULTILINE)
    assert spec_match is not None
    assert spec_match.group(1).strip() == project_version

    releases = ET.parse(
        PROJECT_ROOT / "data" / "io.github.xyasharx.Dendro.metainfo.xml"
    ).findall("./releases/release")
    assert releases
    assert releases[0].attrib["version"] == project_version

    appimage_builder = (PROJECT_ROOT / "build_appimage.sh").read_text(encoding="utf-8")
    assert 'cp -a core ui main.py pyproject.toml "$APPDIR/usr/app/"' in appimage_builder
    assert "python3 -m core.build_provenance" in appimage_builder

    flatpak_manifest = (PROJECT_ROOT / "io.github.xyasharx.Dendro.yml").read_text(
        encoding="utf-8"
    )
    assert "cp -a core ui main.py pyproject.toml /app/share/dendro/" in flatpak_manifest
    assert "python3 -m core.build_provenance" in flatpak_manifest

    attributes = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "core/_build_info.py export-subst" in attributes


def test_argument_parser_options_disable_color_only_when_supported(monkeypatch) -> None:
    monkeypatch.setattr(dendro_main.sys, "version_info", (3, 13, 0))
    assert "color" not in dendro_main._argument_parser_options()

    monkeypatch.setattr(dendro_main.sys, "version_info", (3, 14, 0))
    assert dendro_main._argument_parser_options()["color"] is False
