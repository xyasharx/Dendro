"""Tests for source commit/tree identity in development and release builds."""

from __future__ import annotations

import ast
from pathlib import Path

from core import build_provenance
from core.build_provenance import BuildProvenance

_COMMIT = "a" * 40
_TREE = "b" * 40


def _write_build_info(root: Path, commit: object, tree: object) -> None:
    destination = root / "core" / "_build_info.py"
    destination.parent.mkdir()
    destination.write_text(
        f"COMMIT = {commit!r}\nTREE = {tree!r}\nDIRTY = False\nSOURCE = 'archive'\n",
        encoding="utf-8",
    )


def _clear_build_environment(monkeypatch) -> None:
    for name in ("DENDRO_BUILD_COMMIT", "DENDRO_BUILD_TREE", "DENDRO_BUILD_DIRTY"):
        monkeypatch.delenv(name, raising=False)


def test_archive_embedded_identity_works_without_a_git_checkout(
    tmp_path, monkeypatch
) -> None:
    _clear_build_environment(monkeypatch)
    _write_build_info(tmp_path, _COMMIT, _TREE)

    provenance = build_provenance.discover_build_provenance(tmp_path)

    assert provenance == BuildProvenance(
        commit=_COMMIT,
        tree=_TREE,
        dirty=False,
        source="archive",
    )


def test_explicit_build_environment_takes_priority(tmp_path, monkeypatch) -> None:
    _write_build_info(tmp_path, "c" * 40, "d" * 40)
    monkeypatch.setenv("DENDRO_BUILD_COMMIT", _COMMIT)
    monkeypatch.setenv("DENDRO_BUILD_TREE", _TREE)
    monkeypatch.setenv("DENDRO_BUILD_DIRTY", "true")

    provenance = build_provenance.discover_build_provenance(tmp_path)

    assert provenance == BuildProvenance(
        commit=_COMMIT,
        tree=_TREE,
        dirty=True,
        source="environment",
    )


def test_unexpanded_archive_template_is_not_reported_as_a_real_identity(
    tmp_path,
    monkeypatch,
) -> None:
    _clear_build_environment(monkeypatch)
    _write_build_info(tmp_path, "$Format:%H$", "$Format:%T$")

    provenance = build_provenance.discover_build_provenance(tmp_path)

    assert not provenance.is_available
    assert provenance.source == "unavailable"


def test_git_identity_marks_tracked_source_changes(monkeypatch, tmp_path) -> None:
    _clear_build_environment(monkeypatch)
    (tmp_path / ".git").mkdir()

    def fake_git(_root: Path, *arguments: str) -> str | None:
        values = {
            ("show", "-s", "--format=%H%n%T", "HEAD"): f"{_COMMIT}\n{_TREE}",
            ("status", "--porcelain", "--untracked-files=no"): " M main.py",
        }
        return values.get(arguments)

    monkeypatch.setattr(build_provenance, "_run_git", fake_git)

    provenance = build_provenance.discover_build_provenance(tmp_path)

    assert provenance == BuildProvenance(
        commit=_COMMIT,
        tree=_TREE,
        dirty=True,
        source="git",
    )


def test_stamping_writes_a_self_contained_python_module(tmp_path) -> None:
    destination = tmp_path / "staged" / "core" / "_build_info.py"
    provenance = BuildProvenance(commit=_COMMIT, tree=_TREE, source="git")

    build_provenance.stamp_build_info(destination, provenance)

    module = ast.parse(destination.read_text(encoding="utf-8"))
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert assignments == {
        "COMMIT": _COMMIT,
        "TREE": _TREE,
        "DIRTY": False,
        "SOURCE": "git",
    }
