"""Offline unit tests for tools/update_release_pins.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
MODULE_PATH = PROJECT_ROOT / "tools" / "update_release_pins.py"
SPEC = importlib.util.spec_from_file_location("update_release_pins", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
updater = importlib.util.module_from_spec(SPEC)
sys.modules["update_release_pins"] = updater
SPEC.loader.exec_module(updater)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
SHA_1 = "1" * 64


def _fake_catalog() -> dict[str, object]:
    return {
        "https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=10": [
            {
                "tag_name": "20261201",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "cpython-3.16.0+20261201-x86_64-unknown-linux-gnu-install_only.tar.gz",
                        "digest": f"sha256:{SHA_1}",
                        "browser_download_url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261201/cpython-3.16.0%2B20261201-x86_64-unknown-linux-gnu-install_only.tar.gz",
                    },
                    {
                        "name": "cpython-3.15.3+20261201-x86_64-unknown-linux-gnu-install_only.tar.gz",
                        "digest": f"sha256:{SHA_A}",
                        "browser_download_url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261201/cpython-3.15.3%2B20261201-x86_64-unknown-linux-gnu-install_only.tar.gz",
                    },
                ],
            }
        ],
        "https://api.github.com/repos/actions/python-versions/contents/versions-manifest.json": [
            {
                "version": "3.16.0",
                "stable": True,
                "files": [
                    {"platform": "linux", "platform_version": "22.04", "arch": "x64"},
                    {"platform": "linux", "platform_version": "24.04", "arch": "x64"},
                ],
            },
            {
                "version": "3.15.3",
                "stable": True,
                "files": [
                    {"platform": "linux", "platform_version": "22.04", "arch": "x64"},
                    {"platform": "linux", "platform_version": "24.04", "arch": "x64"},
                ],
            },
        ],
        "https://api.github.com/repos/AppImage/appimagetool/releases?per_page=10": [
            {
                "tag_name": "continuous",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "appimagetool-x86_64.AppImage",
                        "digest": f"sha256:{SHA_1}",
                        "browser_download_url": "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage",
                    }
                ],
            },
            {
                "tag_name": "1.9.2",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "appimagetool-x86_64.AppImage",
                        "digest": f"sha256:{SHA_B}",
                        "browser_download_url": "https://github.com/AppImage/appimagetool/releases/download/1.9.2/appimagetool-x86_64.AppImage",
                    }
                ],
            },
        ],
        "https://api.github.com/repos/AppImage/type2-runtime/releases?per_page=10": [
            {
                "tag_name": "continuous",
                "draft": False,
                "prerelease": False,
                "assets": [],
            },
            {
                "tag_name": "20260101",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "runtime-x86_64",
                        "digest": f"sha256:{SHA_C}",
                        "browser_download_url": "https://github.com/AppImage/type2-runtime/releases/download/20260101/runtime-x86_64",
                    }
                ],
            },
        ],
        "https://pypi.org/pypi/PyQt6/json": {
            "releases": {
                "6.13.0a1": [],
                "6.13.0": [
                    {
                        "filename": "pyqt6-6.13.0-cp310-abi3-manylinux_2_39_x86_64.whl",
                        "url": "https://files.pythonhosted.org/packages/xx/pyqt6-6.13.0-cp310-abi3-manylinux_2_39_x86_64.whl",
                        "digests": {"sha256": SHA_1},
                    }
                ],
                "6.12.2": [
                    {
                        "filename": "pyqt6-6.12.2-cp310-abi3-manylinux_2_34_x86_64.whl",
                        "url": "https://files.pythonhosted.org/packages/xx/pyqt6-6.12.2-cp310-abi3-manylinux_2_34_x86_64.whl",
                        "digests": {"sha256": SHA_D},
                    }
                ],
            }
        },
        "https://pypi.org/pypi/PyQt6/6.12.2/json": {
            "info": {
                "requires_dist": [
                    "PyQt6-sip (<14,>=13.13)",
                    "PyQt6-Qt6 (<6.13.0,>=6.12.0)",
                ]
            },
            "urls": [
                {
                    "filename": "pyqt6-6.12.2-cp310-abi3-manylinux_2_34_x86_64.whl",
                    "url": "https://files.pythonhosted.org/packages/xx/pyqt6-6.12.2-cp310-abi3-manylinux_2_34_x86_64.whl",
                    "digests": {"sha256": SHA_D},
                }
            ],
        },
        "https://pypi.org/pypi/PyQt6-Qt6/json": {
            "releases": {
                "6.13.0": [
                    {
                        "filename": "pyqt6_qt6-6.13.0-py3-none-manylinux_2_34_x86_64.whl",
                        "url": "https://files.pythonhosted.org/packages/xx/pyqt6_qt6-6.13.0-py3-none-manylinux_2_34_x86_64.whl",
                        "digests": {"sha256": SHA_1},
                    }
                ],
                "6.12.1": [
                    {
                        "filename": "pyqt6_qt6-6.12.1-py3-none-manylinux_2_34_x86_64.whl",
                        "url": "https://files.pythonhosted.org/packages/xx/pyqt6_qt6-6.12.1-py3-none-manylinux_2_34_x86_64.whl",
                        "digests": {"sha256": SHA_E},
                    }
                ],
            }
        },
        "https://pypi.org/pypi/PyQt6-sip/json": {
            "releases": {
                "13.13.2": [
                    {
                        "filename": "pyqt6_sip-13.13.2-cp315-cp315-manylinux1_x86_64.manylinux_2_5_x86_64.whl",
                        "url": "https://files.pythonhosted.org/packages/xx/pyqt6_sip-13.13.2-cp315-cp315-manylinux1_x86_64.manylinux_2_5_x86_64.whl",
                        "digests": {"sha256": SHA_F},
                    }
                ]
            }
        },
        "https://pypi.org/pypi/defusedxml/json": {
            "releases": {
                "0.7.1": [
                    {
                        "filename": "defusedxml-0.7.1-py2.py3-none-any.whl",
                        "url": "https://files.pythonhosted.org/packages/07/6c/aa3f2f849e01cb6a001cd8554a88d4c77c5c1a31c95bdf1cf9301e6d9ef4/defusedxml-0.7.1-py2.py3-none-any.whl",
                        "digests": {
                            "sha256": "a352e7e428770286cc899e2542b6cdaedb2b4953ff269a210103ec58f6198a61"
                        },
                    }
                ]
            }
        },
        "https://pypi.org/pypi/build/json": {
            "releases": {"1.7.0": [{"yanked": False}]}
        },
        "https://pypi.org/pypi/setuptools/json": {
            "releases": {"85.0.0": [{"yanked": False}]}
        },
        "https://pypi.org/pypi/wheel/json": {
            "releases": {"0.49.0": [{"yanked": False}]}
        },
    }


def test_resolve_latest_pins_enforces_abi_glibc_and_constraints() -> None:
    catalog = _fake_catalog()

    pins = updater.resolve_latest_pins(
        pyproject_path=PROJECT_ROOT / "pyproject.toml",
        fetch_json=lambda url: catalog[url],
    )

    # Skips CPython 3.16.0 because PyQt6-sip only ships cp315.
    assert pins.python_runtime.version == "3.15.3"
    assert pins.python_runtime.sha256 == SHA_A
    # Skips continuous AppImage releases.
    assert pins.appimagetool.version == "1.9.2"
    assert pins.appimage_runtime.version == "20260101"
    # Skips PyQt6 6.13.0 (glibc 2.39 > Jammy 2.35) and Qt 6.13.0 (<6.13.0).
    assert pins.pyqt6.version == "6.12.2"
    assert pins.pyqt6_qt6.version == "6.12.1"
    assert pins.pyqt6_sip.version == "13.13.2"
    assert pins.build_version == "1.7.0"
    assert pins.setuptools_version == "85.0.0"
    assert pins.wheel_version == "0.49.0"


def test_apply_updates_rewrites_copied_repo_cleanly(tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    for rel in (
        "pyproject.toml",
        "build_appimage.sh",
        ".github/workflows/release.yml",
        "io.github.xyasharx.Dendro.yml",
    ):
        dest = tmp_path / rel
        dest.write_text(
            (PROJECT_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8"
        )

    catalog = _fake_catalog()
    pins = updater.resolve_latest_pins(
        pyproject_path=tmp_path / "pyproject.toml",
        fetch_json=lambda url: catalog[url],
    )
    changes = updater.apply_updates(tmp_path, pins, write=True)

    assert changes
    appimage = (tmp_path / "build_appimage.sh").read_text(encoding="utf-8")
    release_yml = (tmp_path / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "cpython-3.15.3%2B20261201" in appimage
    assert f"readonly PYTHON_SHA256='{SHA_A}'" in appimage
    assert release_yml.count("python-version: '3.15.3'") == 2
    assert "'setuptools==85.0.0'" in release_yml

    # Re-applying the same resolved pins is idempotent.
    assert updater.apply_updates(tmp_path, pins, write=False) == []


def test_default_fetcher_rejects_non_https_urls() -> None:
    with pytest.raises(ValueError, match="Refusing non-HTTPS"):
        updater._default_fetch_json("http://pypi.org/pypi/PyQt6/json")
