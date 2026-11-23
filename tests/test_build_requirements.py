"""Build-tool floors must match the PEP 639 license metadata in pyproject.toml."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
MINIMUM_SETUPTOOLS_FOR_PEP_639 = (77, 0, 0)


def _version_floor(requirement: str) -> tuple[int, ...]:
    match = re.search(r">=\s*(\d+(?:\.\d+)*)", requirement)
    assert match is not None, f"no lower bound in {requirement!r}"
    parts = [int(part) for part in match.group(1).split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def test_pyproject_uses_pep_639_license_metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    # A string license expression and license-files require PEP 639 support.
    assert isinstance(project["license"], str)
    assert "license-files" in project


def test_build_system_requires_setuptools_that_understands_pep_639() -> None:
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "build-system"
    ]["requires"]
    setuptools_requirement = next(
        requirement for requirement in requires if requirement.startswith("setuptools")
    )

    assert _version_floor(setuptools_requirement) >= MINIMUM_SETUPTOOLS_FOR_PEP_639


def test_rpm_build_requires_the_same_setuptools_floor() -> None:
    spec = (ROOT / "dendro.spec").read_text(encoding="utf-8")
    match = re.search(
        r"^BuildRequires:\s*python3-setuptools\s*(>=\s*[\d.]+)", spec, re.M
    )

    assert match is not None
    assert _version_floor(match.group(1)) >= MINIMUM_SETUPTOOLS_FOR_PEP_639


def test_rpm_recommends_adwaita_wayland_decoration() -> None:
    """Containers without gnome-shell miss qt6-qtwayland-adwaita-decoration.

    Fedora's qt6-qtwayland-adwaita-decoration only supplements (qt6-qtbase and
    gnome-shell), while python3-pyqt6-base pulls qt6-qtbase-gui with Qt's
    fallback bradient decoration whose title bar ignores double-clicks.
    """
    spec = (ROOT / "dendro.spec").read_text(encoding="utf-8")
    assert re.search(
        r"^(?:Recommends|Requires):\s*qt6-qtwayland-adwaita-decoration\b",
        spec,
        re.M,
    ), "dendro.spec must declare qt6-qtwayland-adwaita-decoration"
