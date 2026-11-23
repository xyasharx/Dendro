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
        r"^BuildRequires:\s*python3-setuptools\s*(>=\s*[\d.]+)", spec, re.MULTILINE
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
        re.MULTILINE,
    ), "dendro.spec must declare qt6-qtwayland-adwaita-decoration"


def test_appimage_python_matches_sip_abi_and_release_workflow() -> None:
    """Keep the AppImage CPython runtime, PyQt6-sip wheel, and release jobs aligned."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    requires_python = _version_floor(project["requires-python"])

    appimage_script = (ROOT / "build_appimage.sh").read_text(encoding="utf-8")
    python_match = re.search(
        r"^readonly PYTHON_URL='[^']*cpython-(\d+\.\d+\.\d+)(?:%2B|\+)[^']*'$",
        appimage_script,
        re.MULTILINE,
    )
    assert python_match is not None, (
        "build_appimage.sh must declare a pinned cpython PYTHON_URL"
    )
    appimage_python = python_match.group(1)
    major_str, minor_str, patch_str = appimage_python.split(".")
    assert (int(major_str), int(minor_str), int(patch_str)) >= requires_python

    sip_match = re.search(
        r"^readonly PYQT6_SIP_URL='[^']*pyqt6_sip-[^'-]+-cp(\d+)-cp(\d+)-[^']*'$",
        appimage_script,
        re.MULTILINE,
    )
    assert sip_match is not None, (
        "build_appimage.sh must pin a CPython-ABI PyQt6-sip wheel"
    )
    expected_cp_tag = f"{major_str}{minor_str}"
    assert sip_match.groups() == (expected_cp_tag, expected_cp_tag)

    release_workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    workflow_pythons = re.findall(
        r"^\s*python-version:\s*'(\d+\.\d+\.\d+)'$",
        release_workflow,
        re.MULTILINE,
    )
    assert len(workflow_pythons) >= 2
    assert set(workflow_pythons) == {appimage_python}


def test_appimage_pinned_downloads_carry_valid_sha256_digests() -> None:
    """Every pinned artifact and XCB library in build_appimage.sh needs a SHA-256."""
    appimage_script = (ROOT / "build_appimage.sh").read_text(encoding="utf-8")
    digest_pins = dict(
        re.findall(
            r"^readonly ([A-Z0-9_]+_SHA256)='([^']+)'$",
            appimage_script,
            re.MULTILINE,
        )
    )
    expected_vars = {
        "PYTHON_SHA256",
        "APPIMAGETOOL_SHA256",
        "APPIMAGE_RUNTIME_SHA256",
        "PYQT6_SHA256",
        "PYQT6_QT6_SHA256",
        "PYQT6_SIP_SHA256",
        "DEFUSEDXML_SHA256",
    }
    assert expected_vars.issubset(digest_pins)
    for var_name, digest in digest_pins.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest), (
            f"{var_name} is not a 64-character SHA-256 hex digest: {digest!r}"
        )

    xcb_bundles = re.findall(
        r"^\s*'([^|]+)\|https://[^|]+\|([0-9a-f]+)'$",
        appimage_script,
        re.MULTILINE,
    )
    assert len(xcb_bundles) >= 9
    for soname, digest in xcb_bundles:
        assert re.fullmatch(r"[0-9a-f]{64}", digest), (
            f"XCB bundle {soname} has an invalid SHA-256 digest: {digest!r}"
        )
