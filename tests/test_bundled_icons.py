"""Bundled Dendro icon set: coverage, SVG validity, lookup safety, fallback."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from core import taxonomy
from ui import icons
from ui.icon_data import ICON_SVGS
from ui.icons import app_icon, bundled_icon

ROOT = Path(__file__).parents[1]
HEADER_SOURCE = ROOT / "ui" / "header.py"
SVG_NS = "{http://www.w3.org/2000/svg}"


def _visible_category_tags() -> list[str]:
    return [str(row.tag) for row in taxonomy.sidebar_rows() if not row.is_header]


def _header_icon_names() -> set[str]:
    source = HEADER_SOURCE.read_text(encoding="utf-8")
    return set(re.findall(r'app_icon\(\s*"(header-[a-z_]+)"', source))


def test_every_visible_category_has_a_bundled_icon() -> None:
    tags = _visible_category_tags()
    assert tags, "sidebar must expose at least one category"
    missing = [tag for tag in tags if f"category-{tag}" not in ICON_SVGS]
    assert missing == []


def test_every_header_icon_used_by_header_has_a_bundled_icon() -> None:
    names = _header_icon_names()
    assert len(names) >= 10, "header.py should reference its bundled icons via app_icon"
    missing = [name for name in sorted(names) if name not in ICON_SVGS]
    assert missing == []


def test_no_unused_header_icons_are_bundled() -> None:
    shipped = {name for name in ICON_SVGS if name.startswith("header-")}
    assert shipped == _header_icon_names()


def test_no_unused_category_icons_are_bundled() -> None:
    shipped = {name for name in ICON_SVGS if name.startswith("category-")}
    expected = {f"category-{tag}" for tag in _visible_category_tags()}
    assert shipped == expected


@pytest.mark.parametrize("name", sorted(ICON_SVGS))
def test_bundled_svg_is_well_formed_and_self_contained(name: str) -> None:
    svg = ICON_SVGS[name]
    root = ET.fromstring(svg)
    assert root.tag == f"{SVG_NS}svg"
    assert root.get("viewBox") == "0 0 48 48"
    assert root.get("width") == "48"
    assert root.get("height") == "48"
    # Icons must not load external resources or run scripts.
    assert "href" not in svg
    assert "<script" not in svg


def test_bundled_icons_are_non_null_qicons(qapp) -> None:
    for name in sorted(ICON_SVGS):
        icon = bundled_icon(name)
        assert not icon.isNull(), name
        assert icon.availableSizes(), name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../icons",
        "header-search/../../x",
        "/etc/passwd",
        "Header-Search",
        "header search",
    ],
)
def test_bundled_icon_refuses_unknown_or_unsafe_names(qapp, name: str) -> None:
    assert bundled_icon(name).isNull()


def test_bundled_icon_missing_name_is_null(qapp) -> None:
    assert bundled_icon("header-does-not-exist").isNull()


def test_app_icon_prefers_bundled_and_skips_theme_lookup(qapp, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def spy(*names: str):
        calls.append(names)
        return icons.QIcon()

    monkeypatch.setattr(icons, "theme_icon", spy)
    assert not app_icon("header-search", "system-search").isNull()
    assert calls == []


def test_app_icon_falls_back_to_theme_when_bundle_is_missing(monkeypatch) -> None:
    received: list[tuple[str, ...]] = []
    sentinel = object()

    def fake_theme_icon(*names: str):
        received.append(names)
        return sentinel

    monkeypatch.setattr(icons, "theme_icon", fake_theme_icon)
    assert app_icon("header-missing", "first", "second") is sentinel
    assert received == [("first", "second")]


def test_bundled_icons_load_without_qtsvg_python_binding(qapp, monkeypatch) -> None:
    """CI's Fedora image ships qt6-qtsvg but not the PyQt6.QtSvg binding."""
    monkeypatch.setitem(sys.modules, "PyQt6.QtSvg", None)
    for name in sorted(ICON_SVGS):
        assert not bundled_icon(name).isNull(), name


def test_bundled_icon_with_broken_svg_is_null(qapp, monkeypatch) -> None:
    monkeypatch.setitem(ICON_SVGS, "header-broken", "<svg this is not valid")
    assert bundled_icon("header-broken").isNull()
