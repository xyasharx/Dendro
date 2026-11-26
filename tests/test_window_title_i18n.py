"""The window keeps one identity across a language switch, and the local-RPM
dialog's own copy reaches the catalogues.

Both defects were invisible in English and invisible to the catalog check:

* the window title was built from two different message ids, so a language
  switch did not translate the title, it *replaced* the long descriptive name
  with a short product name;
* the dialog's captions were written with ``setWindowTitle``, which the
  extractor did not read, and its field names were wrapped in ``<b>`` markup,
  which the extractor discards as non-prose. Every catalogue therefore looked
  complete while those strings shipped in English.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dendro.core.i18n as core_i18n
from dendro.core.host_environment import HostEnvironment, HostEnvironmentKind

PROJECT_ROOT = Path(__file__).parents[1]
LOCALE_DIR = PROJECT_ROOT / "data" / "locales"

#: What the title says in each language. One id, translated, never replaced.
EXPECTED_TITLES = {
    "en": "Fedora Package Tree & Dependency Inspector (Dendro)",
    "de": "Fedora-Paketbaum und Abhängigkeitsinspektor (Dendro)",
    "fr": "Arborescence des paquets et inspecteur de dépendances Fedora (Dendro)",
}


@pytest.fixture(autouse=True)
def _restore_locale():
    core_i18n.clear_cache()
    core_i18n.set_active_locale("en")
    yield
    core_i18n.clear_cache()
    core_i18n.set_active_locale("en")


def _main_window(qtbot):
    from dendro.ui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    return window


def test_window_title_translates_without_changing_identity(qtbot) -> None:
    """A switch may rewrite the title, but must not swap it for another one."""

    window = _main_window(qtbot)
    assert window.windowTitle() == EXPECTED_TITLES["en"]

    for locale in ("de", "fr"):
        window._apply_language_choice(locale)
        assert window.windowTitle() == EXPECTED_TITLES[locale]

    # Back to English: the identity the window started with, character for
    # character. The old code returned "Dendro Package Manager" here.
    window._apply_language_choice("en")
    assert window.windowTitle() == EXPECTED_TITLES["en"]


def test_window_title_is_one_message_id() -> None:
    """The two halves of the title used to be two different ids.

    A regression that reintroduces a second id is a regression that reintroduces
    the identity swap, so the source is checked rather than only the behaviour.
    """

    import ast

    source = (PROJECT_ROOT / "dendro" / "ui" / "main_window.py").read_text(
        encoding="utf-8"
    )
    titles = [
        node.args[0].args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setWindowTitle"
        and node.args
        and isinstance(node.args[0], ast.Call)
        and isinstance(node.args[0].args[0], ast.Constant)
    ]
    # Every literal window title in the module is the one id. Before the fix
    # there were two, and a language switch chose the second.
    assert titles == ["Fedora Package Tree & Dependency Inspector (Dendro)"], titles

    # And no caption is written as a bare literal anywhere in the module. The
    # defect that survived a first pass of this fix was a leftover
    # ``title_prefix`` built from bare strings whose setWindowTitle ran *before*
    # the translated one, so the later call hid it: the window looked right and
    # the dead code stayed. A caption outside a tr() call is that signature.
    for caption in ("Install Package", "Inspect Package", "Dendro Package Manager"):
        for number, line in enumerate(source.splitlines(), start=1):
            if caption not in line:
                continue
            assert "tr(" in line, (
                f"line {number} writes the caption {caption!r} without tr(): {line.strip()!r}"
            )


def _local_rpm():
    from dendro.core.backend import LocalRpmInspectionResult

    return LocalRpmInspectionResult(
        file_path="/tmp/example-1.0-1.fc44.x86_64.rpm",
        name="example",
        version="1.0",
        release="1.fc44",
        arch="x86_64",
        summary="An example package",
        description="An example package.",
        url="https://example.invalid",
        license="",
        size_bytes=123456,
        requires=[],
    )


@pytest.mark.parametrize(
    ("environment", "expected_en", "expected_de"),
    [
        (HostEnvironment.traditional(), "Install Package", "Paket installieren"),
        (
            HostEnvironment(kind=HostEnvironmentKind.ATOMIC_OSTREE),
            "Inspect Package",
            "Paket untersuchen",
        ),
    ],
)
def test_local_rpm_caption_is_translated(
    qtbot, environment, expected_en, expected_de
) -> None:
    from dendro.ui.main_window import LocalRpmInstallDialog

    core_i18n.set_active_locale("en")
    dialog = LocalRpmInstallDialog(_local_rpm(), host_environment=environment)
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == f"{expected_en}: example"

    dialog.deleteLater()
    core_i18n.set_active_locale("de")
    german = LocalRpmInstallDialog(_local_rpm(), host_environment=environment)
    qtbot.addWidget(german)
    assert german.windowTitle() == f"{expected_de}: example"


def test_local_rpm_field_names_are_plain_messages(qtbot) -> None:
    """Field names must be messages, not markup, so they can be translated."""

    from PyQt6.QtWidgets import QLabel

    from dendro.ui.main_window import LocalRpmInstallDialog

    core_i18n.set_active_locale("de")
    dialog = LocalRpmInstallDialog(_local_rpm())
    qtbot.addWidget(dialog)

    field_names = [
        label
        for label in dialog.findChildren(QLabel)
        if label.objectName() == "LocalRpmFieldName"
    ]
    assert field_names, "field names lost their style id"
    for label in field_names:
        assert "<" not in label.text(), (
            f"markup leaked into a message: {label.text()!r}"
        )
    texts = {label.text() for label in field_names}
    # Translated, and phrased as a message the catalogue owns.
    assert "Architektur:" in texts, texts
    assert "Dateipfad:" in texts, texts
    assert not any("Architecture:" in text for text in texts), texts


def test_every_locale_translates_the_local_rpm_fields() -> None:
    """The shipped catalogues must carry the dialog's copy, not fall back."""

    for path in sorted(LOCALE_DIR.glob("*.json")):
        if path.stem == "en":
            continue
        messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
        for message in ("Status:", "Architecture:", "File Path:", "Package Size:"):
            value = messages.get(message)
            assert value, f"{path.stem} does not translate {message!r}"
            assert "<" not in value, f"{path.stem} put markup in {message!r}: {value!r}"


def test_extractor_sees_setters_and_markup_free_messages() -> None:
    """The blind spots that hid these strings from the catalog check."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dendro_i18n_tool", PROJECT_ROOT / "tools" / "dendro-i18n.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    # ``setWindowTitle("Some Caption")`` is interface text.
    assert "setWindowTitle" in module._TEXT_SETTERS

    # And a literal title is found, so a new one cannot go unextracted.
    found = module.extract_strings(PROJECT_ROOT / "dendro" / "ui" / "repo_dialog.py")
    assert "System Software Repositories" in found

    # Markup is still not prose: this is what makes ``<b>`` wrapping a trap.
    assert module._is_translatable("<b>Architecture:</b>") is False
    assert module._is_translatable("Architecture:") is True

    # Nothing in the UI may hand the extractor markup, because it will drop the
    # message silently. One such label survived a first pass of this fix in a
    # branch that spelled the same field name at different indentation.
    offenders = [
        f"{path.name}:{line}"
        for path in sorted((PROJECT_ROOT / "dendro" / "ui").glob("*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if 'tr("<b>' in line or "tr('<b>" in line
    ]
    assert not offenders, f"markup-wrapped messages: {offenders}"
