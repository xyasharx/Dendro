"""Tests for the reusable release-tag validation entry point."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT = PROJECT_ROOT / "tools" / "verify_release_tag.py"


def _project_version() -> str:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_tag_validator_accepts_the_canonical_version() -> None:
    version = _project_version()

    result = _run("--tag", f"v{version}")

    assert result.returncode == 0
    assert result.stdout == f"Release tag matches project metadata: v{version}\n"
    assert result.stderr == ""


def test_release_tag_validator_reports_a_mismatch(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nversion = '1.2.3'\n", encoding="utf-8")

    result = _run("--tag", "v1.2.4", "--pyproject", str(pyproject))

    assert result.returncode == 1
    assert result.stdout == ""
    assert "got 'v1.2.4'" in result.stderr
    assert "expected 'v1.2.3'" in result.stderr


def test_release_tag_validator_reports_invalid_project_metadata(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nversion = 2120\n", encoding="utf-8")

    result = _run("--tag", "v1.2.3", "--pyproject", str(pyproject))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "[project].version must be a non-empty string" in result.stderr
