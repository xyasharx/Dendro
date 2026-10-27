"""Build-time and runtime source-provenance support for Dendro.

Release artifacts do not normally contain a ``.git`` directory.  This module
therefore records the exact source commit and tree in a small generated module
at build time, while retaining a Git fallback for developers running directly
from a checkout.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_OBJECT_ID_PATTERN: Final = re.compile(
    r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$", re.IGNORECASE
)
_BUILD_INFO_RELATIVE_PATH: Final = Path("core") / "_build_info.py"


@dataclass(frozen=True, slots=True)
class BuildProvenance:
    """The source identity embedded in, or discovered for, one Dendro build."""

    commit: str | None = None
    tree: str | None = None
    dirty: bool = False
    source: str = "unavailable"

    @property
    def is_available(self) -> bool:
        """Return whether both Git object IDs are available and valid."""
        return self.commit is not None and self.tree is not None


def _normalise_object_id(value: object) -> str | None:
    """Return a normalized Git SHA-1/SHA-256 object ID, if valid."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _OBJECT_ID_PATTERN.fullmatch(candidate):
        return None
    return candidate.lower()


def _run_git(source_root: Path, *arguments: str) -> str | None:
    """Run one bounded Git query without letting Git availability affect Dendro."""
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _provenance_from_git(source_root: Path) -> BuildProvenance | None:
    """Read the current checkout identity without invoking a shell."""
    # Installed artifacts and Git archives have no repository metadata. Avoid
    # even starting Git in those common cases.
    if not (source_root / ".git").exists():
        return None

    identity = _run_git(source_root, "show", "-s", "--format=%H%n%T", "HEAD")
    if identity is None:
        return None

    fields = identity.splitlines()
    if len(fields) != 2:
        return None
    commit = _normalise_object_id(fields[0])
    tree = _normalise_object_id(fields[1])
    if commit is None or tree is None:
        return None

    status = _run_git(source_root, "status", "--porcelain", "--untracked-files=no")
    return BuildProvenance(
        commit=commit,
        tree=tree,
        dirty=bool(status),
        source="git",
    )


def _literal_assignments(path: Path) -> dict[str, object]:
    """Read literal constants from a build-info module without importing it."""
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return {}

    values: dict[str, object] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            values[target.id] = ast.literal_eval(node.value)
        except ValueError:
            continue
    return values


def _provenance_from_embedded_file(path: Path) -> BuildProvenance | None:
    """Read archive-substituted or build-stamped identity from ``path``."""
    values = _literal_assignments(path)
    commit = _normalise_object_id(values.get("COMMIT"))
    tree = _normalise_object_id(values.get("TREE"))
    if commit is None or tree is None:
        return None
    return BuildProvenance(
        commit=commit,
        tree=tree,
        dirty=bool(values.get("DIRTY", False)),
        source=str(values.get("SOURCE") or "embedded"),
    )


def _provenance_from_environment() -> BuildProvenance | None:
    """Use an explicitly supplied release-build identity when present."""
    raw_commit = os.environ.get("DENDRO_BUILD_COMMIT")
    raw_tree = os.environ.get("DENDRO_BUILD_TREE")
    if raw_commit is None and raw_tree is None:
        return None

    commit = _normalise_object_id(raw_commit)
    tree = _normalise_object_id(raw_tree)
    if commit is None or tree is None:
        return None

    dirty = os.environ.get("DENDRO_BUILD_DIRTY", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    return BuildProvenance(commit=commit, tree=tree, dirty=dirty, source="environment")


def discover_build_provenance(source_root: Path) -> BuildProvenance:
    """Discover identity from explicit build data, Git, or an embedded module.

    The order is intentional: a release pipeline may supply an authoritative
    environment value; a development checkout should report its current Git
    state; an archive or installed artifact then uses its stamped constants.
    """
    root = source_root.resolve()
    return (
        _provenance_from_environment()
        or _provenance_from_git(root)
        or _provenance_from_embedded_file(root / _BUILD_INFO_RELATIVE_PATH)
        or BuildProvenance()
    )


def format_version_identity(version: str, provenance: BuildProvenance) -> str:
    """Return the user-visible version plus exact source identity."""
    if not provenance.is_available:
        return f"{version} (source revision unavailable)"

    details = [
        f"Git commit {provenance.commit}",
        f"Git tree {provenance.tree}",
    ]
    if provenance.dirty:
        details.append("dirty")
    return f"{version} ({'; '.join(details)})"


def stamp_build_info(output_path: Path, provenance: BuildProvenance) -> None:
    """Write a self-contained build-info module into a staging directory."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        '"""Generated Dendro build provenance. Do not edit manually."""\n\n'
        f"COMMIT = {provenance.commit!r}\n"
        f"TREE = {provenance.tree!r}\n"
        f"DIRTY = {provenance.dirty!r}\n"
        f"SOURCE = {provenance.source!r}\n",
        encoding="utf-8",
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stamp Dendro source provenance into a staged build module."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path.cwd(),
        help="Dendro source root used to discover provenance.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Staged core/_build_info.py path to write.",
    )
    return parser


def main() -> int:
    """Stamp a staged build-info module for an artifact build."""
    arguments = _argument_parser().parse_args()
    provenance = discover_build_provenance(arguments.source_root)
    stamp_build_info(arguments.output, provenance)
    if provenance.is_available:
        print(
            "Stamped Dendro provenance "
            f"from {provenance.source}: {provenance.commit} / {provenance.tree}"
        )
    else:
        print("Stamped Dendro provenance: source revision unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
