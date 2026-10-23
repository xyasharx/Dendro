"""Setuptools hooks that stamp source provenance into distributable artifacts."""

from __future__ import annotations

import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist

PROJECT_ROOT = Path(__file__).resolve().parent
# PEP 517 executes setup.py through an in-process hook whose import path does
# not necessarily contain the project root.
sys.path.insert(0, str(PROJECT_ROOT))

from core.build_provenance import (  # noqa: E402
    discover_build_provenance,
    stamp_build_info,
)


class BuildPyWithProvenance(_build_py):
    """Stamp the copied package module without modifying the source checkout."""

    def run(self) -> None:
        super().run()
        stamp_build_info(
            Path(self.build_lib) / "core" / "_build_info.py",
            discover_build_provenance(PROJECT_ROOT),
        )


class SdistWithProvenance(_sdist):
    """Ensure an sdist remains identifiable after its ``.git`` directory is gone."""

    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        super().make_release_tree(base_dir, files)
        stamp_build_info(
            Path(base_dir) / "core" / "_build_info.py",
            discover_build_provenance(PROJECT_ROOT),
        )


setup(
    cmdclass={
        "build_py": BuildPyWithProvenance,
        "sdist": SdistWithProvenance,
    }
)
