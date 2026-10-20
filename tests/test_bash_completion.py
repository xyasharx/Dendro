"""Regression tests for Dendro's Bash completion."""
from __future__ import annotations

from pathlib import Path
import subprocess


PROJECT_ROOT = Path(__file__).parents[1]
COMPLETION = PROJECT_ROOT / "data" / "dendro.bash"
SPEC = PROJECT_ROOT / "dendro.spec"


def _complete(*words: str, cwd: Path = PROJECT_ROOT) -> list[str]:
    script = r'''
set -euo pipefail
source "$1"
shift
COMP_WORDS=("$@")
COMP_CWORD=$((${#COMP_WORDS[@]} - 1))
_dendro_completion
if ((${#COMPREPLY[@]})); then
    printf '%s\n' "${COMPREPLY[@]}"
fi
'''
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(COMPLETION), *words],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_bash_completion_completes_the_version_option() -> None:
    assert _complete("dendro", "--ver") == ["--version"]


def test_bash_completion_offers_rpm_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "package.rpm").touch()
    (tmp_path / "notes.txt").touch()
    (tmp_path / "packages").mkdir()

    candidates = _complete("dendro", "", cwd=tmp_path)

    assert "package.rpm" in candidates
    assert "packages" in candidates
    assert "notes.txt" not in candidates


def test_rpm_spec_installs_the_bash_completion() -> None:
    spec = SPEC.read_text(encoding="utf-8")

    assert "BuildRequires:  bash-completion" in spec
    assert "Recommends:     bash-completion" in spec
    assert "data/dendro.bash" in spec
    assert "%{buildroot}%{bash_completions_dir}/dendro" in spec
    assert "%{bash_completions_dir}/dendro" in spec
