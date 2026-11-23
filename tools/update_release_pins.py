#!/usr/bin/env python3
"""Update hash-verified release runtime pins while preserving ABI compatibility."""

from __future__ import annotations

import argparse
import json
import os
import re
import tomllib
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MANYLINUX_VERSION_RE = re.compile(r"manylinux_(\d+)_(\d+)_x86_64\b")
_MAX_APPIMAGE_GLIBC = (2, 35)  # Ubuntu 22.04 (Jammy) runner and AppImage baseline
_REQUEST_TIMEOUT_SECONDS = 25

_GITHUB_HOSTS = ("https://api.github.com/",)
_SKIPPED_GITHUB_TAGS = frozenset({"continuous", "old", "latest"})


@dataclass(frozen=True, slots=True)
class PinChange:
    """One updated dependency or artifact pin."""

    component: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    """One resolved release artifact with exact version, URL, and SHA-256."""

    version: str
    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ResolvedPins:
    """Complete verified set of release build pins."""

    python_runtime: ResolvedArtifact
    appimagetool: ResolvedArtifact
    appimage_runtime: ResolvedArtifact
    pyqt6: ResolvedArtifact
    pyqt6_qt6: ResolvedArtifact
    pyqt6_sip: ResolvedArtifact
    defusedxml: ResolvedArtifact
    build_version: str
    setuptools_version: str
    wheel_version: str


FetchJson = Callable[[str], Any]


def _default_fetch_json(url: str) -> Any:
    if not url.startswith("https://"):
        raise ValueError(f"Refusing non-HTTPS metadata request: {url}")

    headers = {
        "User-Agent": "Dendro-Release-Pin-Updater/1.0",
        "Accept": (
            "application/vnd.github.raw"
            if url.endswith("/contents/versions-manifest.json")
            else "application/json"
        ),
    }
    if url.startswith(_GITHUB_HOSTS):
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_stable_version(raw_version: str) -> tuple[int, ...] | None:
    """Parse a stable ``X.Y[.Z...]`` version; return ``None`` for prereleases."""
    candidate = raw_version.strip()
    if not candidate:
        return None

    # Ignore PEP 440 post-releases only after extracting base numeric version,
    # while rejecting any alpha/beta/rc/dev suffix.
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:\.post(\d+))?", candidate)
    if match is None:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    if match.group(2) is not None:
        parts.append(int(match.group(2)))
    return tuple(parts)


def _compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    if padded_left < padded_right:
        return -1
    if padded_left > padded_right:
        return 1
    return 0


def _satisfies_specifier_set(version: str, specifiers: str) -> bool:
    """Evaluate comma-separated PEP 440 comparison clauses against ``version``."""
    parsed = _parse_stable_version(version)
    if parsed is None:
        return False

    for raw_clause in specifiers.split(","):
        clause = raw_clause.strip()
        if not clause:
            continue
        match = re.fullmatch(r"(==|!=|>=|<=|>|<|~=)\s*([^\s;]+)", clause)
        if match is None:
            return False
        op, target_raw = match.groups()
        if target_raw.endswith(".*"):
            prefix = target_raw[:-2]
            target_parts = _parse_stable_version(prefix)
            if target_parts is None:
                return False
            prefix_width = len(prefix.split("."))
            ver_prefix = parsed[:prefix_width]
            if op == "==":
                if ver_prefix != tuple(int(p) for p in prefix.split(".")):
                    return False
                continue
            if op == "!=":
                if ver_prefix == tuple(int(p) for p in prefix.split(".")):
                    return False
                continue
            return False

        target = _parse_stable_version(target_raw)
        if target is None:
            return False
        cmp = _compare_versions(parsed, target)
        if op == "==" and cmp != 0:
            return False
        if op == "!=" and cmp == 0:
            return False
        if op == ">=" and cmp < 0:
            return False
        if op == "<=" and cmp > 0:
            return False
        if op == ">" and cmp <= 0:
            return False
        if op == "<" and cmp >= 0:
            return False
        if op == "~=":
            upper_parts = list(target[: max(1, len(target_raw.split(".")) - 1)])
            upper_parts[-1] += 1
            if cmp < 0 or _compare_versions(parsed, tuple(upper_parts)) >= 0:
                return False
    return True


def _extract_requires_dist_specifier(
    requires_dist: Sequence[str] | None, dependency_name: str
) -> str | None:
    if not requires_dist:
        return None
    normalized_target = dependency_name.lower().replace("_", "-")
    for raw_req in requires_dist:
        parts = raw_req.split(";", 1)
        if len(parts) == 2 and "extra" in parts[1]:
            continue
        requirement = parts[0].strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*)$", requirement)
        if match is None:
            continue
        pkg_name = match.group(1).lower().replace("_", "-")
        if pkg_name == normalized_target:
            return match.group(2).strip("() ")
    return None


def _wheel_glibc_compatible(filename: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    if "-none-any.whl" in filename:
        return True
    if "musllinux" in filename or "x86_64" not in filename:
        return False
    matches = _MANYLINUX_VERSION_RE.findall(filename)
    if matches:
        return any(
            (int(major), int(minor)) <= _MAX_APPIMAGE_GLIBC for major, minor in matches
        )
    return any(
        legacy in filename
        for legacy in (
            "manylinux1_x86_64",
            "manylinux2010_x86_64",
            "manylinux2014_x86_64",
        )
    )


def _wheel_matches_python(filename: str, python_major_minor: tuple[int, int]) -> bool:
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return False
    py_tag, abi_tag = parts[-3], parts[-2]
    target_cp = f"cp{python_major_minor[0]}{python_major_minor[1]}"
    if abi_tag == "none" and py_tag in {"py3", "py2.py3"}:
        return True
    if py_tag == target_cp and abi_tag == target_cp:
        return True
    if abi_tag == "abi3":
        abi_match = re.fullmatch(r"cp3(\d+)", py_tag)
        if abi_match is not None:
            min_minor = int(abi_match.group(1))
            return python_major_minor[0] == 3 and python_major_minor[1] >= min_minor
    return False


def _select_pypi_wheel(
    metadata: Mapping[str, Any],
    *,
    python_major_minor: tuple[int, int],
    specifier: str = "",
    require_exact_cp_abi: bool = False,
) -> ResolvedArtifact:
    releases = metadata.get("releases")
    if not isinstance(releases, Mapping):
        raise ValueError("PyPI metadata is missing releases map")

    target_cp = f"cp{python_major_minor[0]}{python_major_minor[1]}"
    candidates: list[tuple[tuple[int, ...], ResolvedArtifact]] = []

    for version, files in releases.items():
        parsed = _parse_stable_version(version)
        if parsed is None:
            continue
        if specifier and not _satisfies_specifier_set(version, specifier):
            continue
        if not isinstance(files, list):
            continue

        for file_info in files:
            if not isinstance(file_info, Mapping) or file_info.get("yanked"):
                continue
            filename = str(file_info.get("filename", ""))
            url = str(file_info.get("url", ""))
            digests = file_info.get("digests")
            sha256 = (
                str(digests.get("sha256", "")) if isinstance(digests, Mapping) else ""
            )
            if not url.startswith("https://files.pythonhosted.org/"):
                continue
            if not _SHA256_RE.fullmatch(sha256):
                continue
            if not _wheel_glibc_compatible(filename):
                continue
            if require_exact_cp_abi:
                if f"-{target_cp}-{target_cp}-" not in filename:
                    continue
            elif not _wheel_matches_python(filename, python_major_minor):
                continue
            candidates.append(
                (parsed, ResolvedArtifact(version=version, url=url, sha256=sha256))
            )
            break

    if not candidates:
        raise RuntimeError(
            f"No compatible wheel found for Python {python_major_minor[0]}.{python_major_minor[1]} "
            f"(specifier={specifier!r})"
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _latest_pypi_version(
    metadata: Mapping[str, Any], *, min_version: tuple[int, ...] = (0, 0, 0)
) -> str:
    releases = metadata.get("releases")
    if not isinstance(releases, Mapping):
        raise ValueError("PyPI metadata is missing releases map")
    candidates: list[tuple[tuple[int, ...], str]] = []
    for version, files in releases.items():
        parsed = _parse_stable_version(version)
        if parsed is None or _compare_versions(parsed, min_version) < 0:
            continue
        if not isinstance(files, list) or not files:
            continue
        if all(isinstance(item, Mapping) and item.get("yanked") for item in files):
            continue
        candidates.append((parsed, version))
    if not candidates:
        raise RuntimeError("No stable non-yanked PyPI version found")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _available_setup_python_versions(manifest: Any) -> set[str]:
    """Return stable CPython versions published for both Ubuntu 22.04 and 24.04."""
    if not isinstance(manifest, list):
        raise ValueError("actions/python-versions manifest must be a JSON list")

    available: set[str] = set()
    for entry in manifest:
        if not isinstance(entry, Mapping) or not entry.get("stable"):
            continue
        version = str(entry.get("version", ""))
        if _parse_stable_version(version) is None:
            continue
        files = entry.get("files")
        if not isinstance(files, list):
            continue
        platforms = {
            (
                str(item.get("platform", "")),
                str(item.get("platform_version", "")),
                str(item.get("arch", "")),
            )
            for item in files
            if isinstance(item, Mapping)
        }
        if {("linux", "22.04", "x64"), ("linux", "24.04", "x64")}.issubset(platforms):
            available.add(version)
    return available


def _supported_sip_minor_versions(
    sip_metadata: Mapping[str, Any],
) -> set[tuple[int, int]]:
    latest_sip_version = _latest_pypi_version(sip_metadata)
    files = sip_metadata.get("releases", {}).get(latest_sip_version, [])
    supported: set[tuple[int, int]] = set()
    for file_info in files:
        if not isinstance(file_info, Mapping) or file_info.get("yanked"):
            continue
        filename = str(file_info.get("filename", ""))
        if not _wheel_glibc_compatible(filename):
            continue
        match = re.search(r"-cp3(\d+)-cp3\1-", filename)
        if match is not None:
            supported.add((3, int(match.group(1))))
    return supported


def _resolve_python_runtime(
    pbs_releases: Sequence[Mapping[str, Any]],
    setup_python_versions: set[str],
    supported_sip_minors: set[tuple[int, int]],
    min_python: tuple[int, ...],
) -> ResolvedArtifact:
    candidates: list[tuple[tuple[int, ...], str, ResolvedArtifact]] = []

    for release in pbs_releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name", ""))
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if not isinstance(asset, Mapping):
                continue
            name = str(asset.get("name", ""))
            match = re.fullmatch(
                r"cpython-(\d+\.\d+\.\d+)\+(\d+)-x86_64-unknown-linux-gnu-install_only\.tar\.gz",
                name,
            )
            if match is None:
                continue
            version_str, build_tag = match.groups()
            parsed = _parse_stable_version(version_str)
            if parsed is None or _compare_versions(parsed, min_python) < 0:
                continue
            if version_str not in setup_python_versions:
                continue
            if (parsed[0], parsed[1]) not in supported_sip_minors:
                continue
            digest = str(asset.get("digest", ""))
            if not digest.startswith("sha256:"):
                continue
            sha256 = digest.removeprefix("sha256:")
            if not _SHA256_RE.fullmatch(sha256):
                continue
            url = str(asset.get("browser_download_url", ""))
            if not url.startswith(
                f"https://github.com/astral-sh/python-build-standalone/releases/download/{tag}/"
            ):
                continue
            candidates.append(
                (
                    parsed,
                    build_tag,
                    ResolvedArtifact(version=version_str, url=url, sha256=sha256),
                )
            )

    if not candidates:
        raise RuntimeError(
            "No compatible CPython release matched python-build-standalone, "
            "actions/python-versions, and PyQt6-sip wheels"
        )
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _resolve_github_release_asset(
    releases: Sequence[Mapping[str, Any]],
    *,
    repo_prefix: str,
    asset_name: str,
) -> ResolvedArtifact:
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name", ""))
        if not tag or tag.lower() in _SKIPPED_GITHUB_TAGS:
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if not isinstance(asset, Mapping) or asset.get("name") != asset_name:
                continue
            digest = str(asset.get("digest", ""))
            if not digest.startswith("sha256:"):
                continue
            sha256 = digest.removeprefix("sha256:")
            if not _SHA256_RE.fullmatch(sha256):
                continue
            url = str(asset.get("browser_download_url", ""))
            if not url.startswith(
                f"https://github.com/{repo_prefix}/releases/download/{tag}/"
            ):
                continue
            return ResolvedArtifact(version=tag, url=url, sha256=sha256)

    raise RuntimeError(
        f"No immutable GitHub release asset {asset_name!r} with SHA-256 digest found in {repo_prefix}"
    )


def resolve_latest_pins(
    *,
    pyproject_path: Path,
    fetch_json: FetchJson = _default_fetch_json,
) -> ResolvedPins:
    """Resolve the newest mutually-compatible, SHA-256-verified release pins."""
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    requires_python = str(project["project"].get("requires-python", ">=3.12"))
    min_python_match = re.search(r">=\s*(\d+(?:\.\d+)*)", requires_python)
    min_python = (
        _parse_stable_version(min_python_match.group(1))
        if min_python_match is not None
        else (3, 12, 0)
    ) or (3, 12, 0)

    build_requires = project.get("build-system", {}).get("requires", [])
    setuptools_floor = (77, 0, 0)
    for req in build_requires:
        if isinstance(req, str) and req.startswith("setuptools"):
            floor_match = re.search(r">=\s*(\d+(?:\.\d+)*)", req)
            if floor_match is not None:
                setuptools_floor = (
                    _parse_stable_version(floor_match.group(1)) or setuptools_floor
                )

    pbs_releases = fetch_json(
        "https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=10"
    )
    setup_python_manifest = fetch_json(
        "https://api.github.com/repos/actions/python-versions/contents/versions-manifest.json"
    )
    appimagetool_releases = fetch_json(
        "https://api.github.com/repos/AppImage/appimagetool/releases?per_page=10"
    )
    runtime_releases = fetch_json(
        "https://api.github.com/repos/AppImage/type2-runtime/releases?per_page=10"
    )

    pyqt6_meta = fetch_json("https://pypi.org/pypi/PyQt6/json")
    pyqt6_qt6_meta = fetch_json("https://pypi.org/pypi/PyQt6-Qt6/json")
    pyqt6_sip_meta = fetch_json("https://pypi.org/pypi/PyQt6-sip/json")
    defusedxml_meta = fetch_json("https://pypi.org/pypi/defusedxml/json")
    build_meta = fetch_json("https://pypi.org/pypi/build/json")
    setuptools_meta = fetch_json("https://pypi.org/pypi/setuptools/json")
    wheel_meta = fetch_json("https://pypi.org/pypi/wheel/json")

    setup_python_versions = _available_setup_python_versions(setup_python_manifest)
    supported_sip_minors = _supported_sip_minor_versions(pyqt6_sip_meta)
    python_runtime = _resolve_python_runtime(
        pbs_releases, setup_python_versions, supported_sip_minors, min_python
    )

    parsed_py = _parse_stable_version(python_runtime.version)
    assert parsed_py is not None
    py_major_minor = (parsed_py[0], parsed_py[1])

    # Pick newest PyQt6 whose requires_dist constraints on PyQt6-Qt6 and PyQt6-sip
    # can both be satisfied by compatible manylinux/cp wheels.
    pyqt6_releases = sorted(
        (
            (_parse_stable_version(ver), ver)
            for ver in pyqt6_meta.get("releases", {})
            if _parse_stable_version(ver) is not None
        ),
        reverse=True,
    )
    pyqt6_art: ResolvedArtifact | None = None
    pyqt6_qt6_art: ResolvedArtifact | None = None
    pyqt6_sip_art: ResolvedArtifact | None = None

    for _parsed_ver, pyqt6_ver in pyqt6_releases:
        release_files = pyqt6_meta.get("releases", {}).get(pyqt6_ver, [])
        try:
            _select_pypi_wheel(
                {"releases": {pyqt6_ver: release_files}},
                python_major_minor=py_major_minor,
            )
        except RuntimeError:
            continue

        ver_meta = fetch_json(f"https://pypi.org/pypi/PyQt6/{pyqt6_ver}/json")
        try:
            candidate_pyqt6 = _select_pypi_wheel(
                {"releases": {pyqt6_ver: ver_meta.get("urls", [])}},
                python_major_minor=py_major_minor,
            )
            requires_dist = ver_meta.get("info", {}).get("requires_dist")
            qt6_spec = (
                _extract_requires_dist_specifier(requires_dist, "PyQt6-Qt6") or ""
            )
            sip_spec = (
                _extract_requires_dist_specifier(requires_dist, "PyQt6-sip") or ""
            )
            candidate_qt6 = _select_pypi_wheel(
                pyqt6_qt6_meta,
                python_major_minor=py_major_minor,
                specifier=qt6_spec,
            )
            candidate_sip = _select_pypi_wheel(
                pyqt6_sip_meta,
                python_major_minor=py_major_minor,
                specifier=sip_spec,
                require_exact_cp_abi=True,
            )
        except RuntimeError:
            continue
        pyqt6_art = candidate_pyqt6
        pyqt6_qt6_art = candidate_qt6
        pyqt6_sip_art = candidate_sip
        break

    if pyqt6_art is None or pyqt6_qt6_art is None or pyqt6_sip_art is None:
        raise RuntimeError("Could not find mutually compatible PyQt6/Qt6/sip wheels")

    defusedxml_art = _select_pypi_wheel(
        defusedxml_meta,
        python_major_minor=py_major_minor,
    )

    return ResolvedPins(
        python_runtime=python_runtime,
        appimagetool=_resolve_github_release_asset(
            appimagetool_releases,
            repo_prefix="AppImage/appimagetool",
            asset_name="appimagetool-x86_64.AppImage",
        ),
        appimage_runtime=_resolve_github_release_asset(
            runtime_releases,
            repo_prefix="AppImage/type2-runtime",
            asset_name="runtime-x86_64",
        ),
        pyqt6=pyqt6_art,
        pyqt6_qt6=pyqt6_qt6_art,
        pyqt6_sip=pyqt6_sip_art,
        defusedxml=defusedxml_art,
        build_version=_latest_pypi_version(build_meta),
        setuptools_version=_latest_pypi_version(
            setuptools_meta, min_version=setuptools_floor
        ),
        wheel_version=_latest_pypi_version(wheel_meta),
    )


def _replace_readonly_pin(
    content: str,
    prefix: str,
    artifact: ResolvedArtifact,
    component: str,
    changes: list[PinChange],
) -> str:
    pattern = re.compile(
        rf"^(readonly {prefix}_URL=')([^']+)('\nreadonly {prefix}_SHA256=')([0-9a-f]{{64}})(')$",
        re.MULTILINE,
    )
    match = pattern.search(content)
    if match is None:
        raise ValueError(
            f"Could not locate {prefix}_URL / {prefix}_SHA256 in build_appimage.sh"
        )
    old_url, old_sha = match.group(2), match.group(4)
    if old_url != artifact.url or old_sha != artifact.sha256:
        old_label = old_url.rsplit("/", 1)[-1]
        new_label = artifact.url.rsplit("/", 1)[-1]
        changes.append(PinChange(component, old_label, new_label))
        return (
            content[: match.start()]
            + f"{match.group(1)}{artifact.url}{match.group(3)}{artifact.sha256}{match.group(5)}"
            + content[match.end() :]
        )
    return content


def _update_build_appimage(
    text: str, pins: ResolvedPins, changes: list[PinChange]
) -> str:
    updated = text
    for prefix, artifact, label in (
        ("PYTHON", pins.python_runtime, "CPython (AppImage)"),
        ("APPIMAGETOOL", pins.appimagetool, "appimagetool"),
        ("APPIMAGE_RUNTIME", pins.appimage_runtime, "AppImage type2-runtime"),
        ("PYQT6", pins.pyqt6, "PyQt6"),
        ("PYQT6_QT6", pins.pyqt6_qt6, "PyQt6-Qt6"),
        ("PYQT6_SIP", pins.pyqt6_sip, "PyQt6-sip"),
        ("DEFUSEDXML", pins.defusedxml, "defusedxml"),
    ):
        updated = _replace_readonly_pin(updated, prefix, artifact, label, changes)
    return updated


def _update_release_workflow(
    text: str, pins: ResolvedPins, changes: list[PinChange]
) -> str:
    updated = text
    py_pattern = re.compile(r"^(\s*python-version:\s*')([^']+)(')$", re.MULTILINE)
    old_py_versions = [m.group(2) for m in py_pattern.finditer(updated)]
    if not old_py_versions:
        raise ValueError("No python-version pins found in release.yml")
    if any(ver != pins.python_runtime.version for ver in old_py_versions):
        changes.append(
            PinChange(
                "setup-python (release.yml)",
                ", ".join(sorted(set(old_py_versions))),
                pins.python_runtime.version,
            )
        )
        updated = py_pattern.sub(rf"\g<1>{pins.python_runtime.version}\g<3>", updated)

    for pkg, new_ver in (
        ("build", pins.build_version),
        ("setuptools", pins.setuptools_version),
        ("wheel", pins.wheel_version),
    ):
        pkg_pattern = re.compile(
            rf"^(\s*'{re.escape(pkg)}==)([^']+)('.*)$", re.MULTILINE
        )
        match = pkg_pattern.search(updated)
        if match is None:
            raise ValueError(f"Missing {pkg}== pin in release.yml")
        old_ver = match.group(2)
        if old_ver != new_ver:
            changes.append(PinChange(f"{pkg} (release.yml)", old_ver, new_ver))
            updated = pkg_pattern.sub(rf"\g<1>{new_ver}\g<3>", updated)

    return updated


def _update_flatpak_manifest(
    text: str, pins: ResolvedPins, changes: list[PinChange]
) -> str:
    wheel_name = pins.defusedxml.url.rsplit("/", 1)[-1]
    updated = text

    pip_pattern = re.compile(
        r"^(\s*- python3 -m pip install --no-index --no-deps --prefix=/app )(defusedxml-[^\s]+\.whl)$",
        re.MULTILINE,
    )
    url_pattern = re.compile(
        r"^(\s*url:\s*)(https://files\.pythonhosted\.org/packages/[^\s]+/defusedxml-[^\s]+\.whl)$",
        re.MULTILINE,
    )
    sha_pattern = re.compile(
        r"^(\s*sha256:\s*)([0-9a-f]{64})$",
        re.MULTILINE,
    )

    pip_match = pip_pattern.search(updated)
    url_match = url_pattern.search(updated)
    sha_match = sha_pattern.search(updated)
    if pip_match is None or url_match is None or sha_match is None:
        raise ValueError("Could not locate defusedxml block in Flatpak manifest")

    if (
        pip_match.group(2) != wheel_name
        or url_match.group(2) != pins.defusedxml.url
        or sha_match.group(2) != pins.defusedxml.sha256
    ):
        changes.append(
            PinChange("defusedxml (Flatpak)", pip_match.group(2), wheel_name)
        )
        updated = pip_pattern.sub(rf"\g<1>{wheel_name}", updated, count=1)
        updated = url_pattern.sub(rf"\g<1>{pins.defusedxml.url}", updated, count=1)
        updated = sha_pattern.sub(rf"\g<1>{pins.defusedxml.sha256}", updated, count=1)

    return updated


def apply_updates(
    repo_root: Path,
    pins: ResolvedPins,
    *,
    write: bool = False,
) -> list[PinChange]:
    """Compute and optionally write updated pins across release/packaging files."""
    changes: list[PinChange] = []

    targets: list[tuple[Path, Callable[[str, ResolvedPins, list[PinChange]], str]]] = [
        (repo_root / "build_appimage.sh", _update_build_appimage),
        (repo_root / ".github" / "workflows" / "release.yml", _update_release_workflow),
        (repo_root / "io.github.xyasharx.Dendro.yml", _update_flatpak_manifest),
    ]

    for path, updater in targets:
        original = path.read_text(encoding="utf-8")
        updated = updater(original, pins, changes)
        if write and updated != original:
            path.write_text(updated, encoding="utf-8")

    return changes


def format_markdown_summary(changes: Sequence[PinChange]) -> str:
    """Format a Markdown table summarizing pin updates for PR descriptions."""
    if not changes:
        return "All release runtime pins are already up to date.\n"

    lines = [
        "### Updated release runtime pins",
        "",
        "| Component | Previous | Updated |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {change.component} | `{change.old}` | `{change.new}` |"
        for change in changes
    )
    lines.append("")
    return "\n".join(lines)


def _parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check or update hash-verified release runtime pins."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repository root containing build_appimage.sh and .github/workflows/release.yml.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write updated pins back to repository files.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 1 if any pin has an available update.",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        default=None,
        help="Optional Markdown file path to write the update summary.",
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parse_arguments(arguments)
    repo_root = options.repo_root.resolve()

    pins = resolve_latest_pins(pyproject_path=repo_root / "pyproject.toml")
    changes = apply_updates(repo_root, pins, write=options.write)
    summary = format_markdown_summary(changes)

    if options.summary_file is not None:
        options.summary_file.parent.mkdir(parents=True, exist_ok=True)
        options.summary_file.write_text(summary, encoding="utf-8")

    print(summary, end="")
    if options.check and changes:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
