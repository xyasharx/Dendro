#!/usr/bin/env python3
"""Validate that a release tag exactly matches project.version in pyproject.toml."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys
import tomllib


def _parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a release tag against project.version in pyproject.toml."
    )
    parser.add_argument(
        "--tag",
        required=True,
        help="Tag being published, for example v2.12.0.",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help="Path to the canonical project metadata file.",
    )
    return parser.parse_args(arguments)


def _project_version(pyproject_path: Path) -> str:
    try:
        with pyproject_path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError as error:
        raise ValueError(f"Project metadata file does not exist: {pyproject_path}") from error
    except OSError as error:
        raise ValueError(f"Could not read project metadata file {pyproject_path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"Invalid TOML in {pyproject_path}: {error}") from error

    project = document.get("project")
    if not isinstance(project, dict):
        raise ValueError(f"{pyproject_path} must contain a [project] table")

    version = project.get("version")
    if not isinstance(version, str) or not version or version != version.strip():
        raise ValueError(
            f"{pyproject_path} [project].version must be a non-empty string without surrounding whitespace"
        )

    return version


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parse_arguments(arguments)

    try:
        version = _project_version(options.pyproject)
    except ValueError as error:
        print(f"Release version validation failed: {error}", file=sys.stderr)
        return 2

    expected_tag = f"v{version}"
    if options.tag != expected_tag:
        print(
            f"Release tag mismatch: got {options.tag!r}; expected {expected_tag!r} "
            f"from {options.pyproject}.",
            file=sys.stderr,
        )
        return 1

    print(f"Release tag matches project metadata: {options.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
