"""CLI startup tests that do not require a Qt runtime."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib


PROJECT_ROOT = Path(__file__).parents[1]


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
    assert result.stdout == f"dendro {_project_version()}\n"
    assert result.stderr == ""


def test_gui_launch_reports_a_missing_qt_runtime_explicitly() -> None:
    result = _run_without_site_packages()

    assert result.returncode == 1
    assert "PyQt6 GUI runtime is unavailable" in result.stderr


def test_source_bundle_version_path_uses_copied_project_metadata(tmp_path: Path) -> None:
    """AppImage and Flatpak copy these files without installing wheel metadata."""
    bundle = tmp_path / "dendro"
    bundle.mkdir()
    shutil.copy2(PROJECT_ROOT / "main.py", bundle / "main.py")
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", bundle / "pyproject.toml")

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
    assert result.stdout == f"dendro {_project_version()}\n"
    assert result.stderr == ""


def test_release_version_is_consistent_across_python_rpm_and_appstream_metadata() -> None:
    import re
    import xml.etree.ElementTree as ET

    project_version = _project_version()

    spec_source = (PROJECT_ROOT / "dendro.spec").read_text(encoding="utf-8")
    spec_match = re.search(r"^Version:\s+(.+)$", spec_source, flags=re.MULTILINE)
    assert spec_match is not None
    assert spec_match.group(1).strip() == project_version

    releases = ET.parse(PROJECT_ROOT / "data" / "io.github.xyasharx.Dendro.metainfo.xml").findall("./releases/release")
    assert releases
    assert releases[0].attrib["version"] == project_version

    appimage_builder = (PROJECT_ROOT / "build_appimage.sh").read_text(encoding="utf-8")
    assert 'cp -a core ui main.py pyproject.toml "$APPDIR/usr/app/"' in appimage_builder

    flatpak_manifest = (PROJECT_ROOT / "io.github.xyasharx.Dendro.yml").read_text(encoding="utf-8")
    assert "cp -a core ui main.py pyproject.toml /app/share/dendro/" in flatpak_manifest
