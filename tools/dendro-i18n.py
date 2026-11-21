"""Extract and verify Dendro's translatable strings.

Dendro keeps English at the call site, so the source of truth for what needs
translating is the code itself.  This tool walks the syntax tree instead of
grepping, which means it sees strings passed through ``tr()``, collected into
category tables, and built with f-strings, and it never mistakes a CSS block or
a docstring for a user-facing message.

Commands
--------
``list``    Print every translatable source string, one per line.
``check``   Verify each shipped catalog covers the current strings, and report
            missing, extra, and untranslated entries.  Exit status is non-zero
            when a catalog is incomplete, so CI fails on drift.
``stats``   Print per-locale coverage counts.

The checker is deliberately strict about *coverage* (every string must have an
entry) and lenient about *content* (it does not judge translation quality).
Coverage is the failure mode that silently ships English to users; quality is
caught in review.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = PROJECT_ROOT / "data" / "locales"

#: Call targets whose first positional argument is shown to the user.
_TRANSLATED_CALLS = frozenset(
    {
        # Dendro's own lookup, imported as ``tr``.
        "tr",
        "translate",
        # Plural form used for count-dependent text.
        "tr_plural",
    }
)

#: Methods whose first positional argument is visible text but which are not
#: constructors. ``setWindowTitle`` was missing, so every title written through
#: it was invisible to the extractor and shipped in English in all seven
#: languages.
_TEXT_SETTERS = frozenset(
    {
        "setWindowTitle",
        "setPlaceholderText",
        "setToolTip",
        "setStatusTip",
        "setAccessibleName",
        "setAccessibleDescription",
    }
)

#: Widget constructors whose first positional argument is visible text.
_TEXT_CONSTRUCTORS = frozenset(
    {
        "QAction",
        "QCheckBox",
        "QDialog",
        "QGroupBox",
        "QLabel",
        "QPushButton",
        "QRadioButton",
        "QToolButton",
    }
)

#: Attribute names on taxonomy/dataclass definitions that hold display text.
_DISPLAY_KEYWORDS = frozenset({"label", "tooltip", "description"})

#: Fragments that mark a literal as code rather than prose.
_NON_PROSE_MARKERS = (
    "image: url(",
    "px;",
    "rgba(",
    "<span",
    "<div",
    "<b>",
    "<svg",
    "<?xml",
    "<!DOCTYPE",
    "<html",
    "://",
    "QSS",
)


#: Strings that are data rather than interface copy: proper names, author
#: credits, and identifiers that must read identically in every language.
_NOT_TRANSLATABLE = frozenset(
    {
        "Yashar &lt;yashar@duck.com&gt;",
        "GNU General Public License v3.0 or later (GPL-3.0-or-later)",
    }
)


def _is_translatable(value: str) -> bool:
    """Return True when ``value`` is prose worth translating."""

    text = value.strip()
    if text in _NOT_TRANSLATABLE:
        return False
    if len(text) < 2:
        return False
    if "\n" in text:
        # Multi-line blocks are stylesheets, markup documents, or docstrings.
        return False
    if any(marker in text for marker in _NON_PROSE_MARKERS):
        return False
    if not any(char.isalpha() for char in text):
        return False
    # Identifiers and dotted paths are internal keys, not messages.
    return not (text.startswith(("core.", "ui.", "data/")) or text.endswith("_"))


def _keyword_display_strings(node: ast.Call) -> list[str]:
    """Return display text passed by keyword to a spec constructor."""

    return [
        keyword.value.value
        for keyword in node.keywords
        if keyword.arg in _DISPLAY_KEYWORDS
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
        and _is_translatable(keyword.value.value)
    ]


def _positional_text(argument: ast.expr) -> list[str]:
    """Return ``argument`` itself when it is a translatable literal."""

    if (
        isinstance(argument, ast.Constant)
        and isinstance(argument.value, str)
        and _is_translatable(argument.value)
    ):
        return [argument.value]
    return []


def _positional_display(node: ast.Call) -> list[str]:
    """Return positional display text from a table-entry constructor.

    Taxonomy rows are built positionally (``SidebarSection("apps", "1. Desktop
    Applications", 1)``), so the extractor needs to know which argument slots
    hold text.  The mapping is explicit rather than heuristic: guessing would
    either miss strings or capture identifiers.
    """

    func = node.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else func.id
        if isinstance(func, ast.Name)
        else ""
    )
    # Argument indices that hold user-facing text, per constructor.
    indexes = _POSITIONAL_TEXT_INDEXES.get(name)
    if not indexes:
        return []
    found = []
    for index in indexes:
        if index < len(node.args):
            argument = node.args[index]
            if (
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and _is_translatable(argument.value)
            ):
                found.append(argument.value)
    return found


#: Constructor name -> argument indices holding display text.  Kept in one
#: place so a new taxonomy row shape is a one-line change here.
_POSITIONAL_TEXT_INDEXES = {
    "SidebarSection": (1,),
    "PillarSpec": (1,),
    "_category": (1, 2),
    "CategorySpec": (1, 2),
    # QMessageBox helpers take (parent, title, text).
    "information": (1, 2),
    "warning": (1, 2),
    "critical": (1, 2),
    "question": (1, 2),
}


#: Module-level ``[(key, label), ...]`` tables whose labels are shown in the
#: interface, mapped to the tuple index holding the visible text.
#: ``{table name: column index}`` for tables whose display text is one cell of a
#: row. ``SEARCH_FILTER_ROWS`` is ``(message, shell, has_values)``, so the message
#: is column 0.
_DISPLAY_TABLES = {
    "THEME_DISPLAY_OPTIONS": 1,
    "SEARCH_FILTER_ROWS": 0,
}

#: Display tables whose *values* are interface text, keyed by table name and the
#: index of the string inside each row.  A dict's values are read too: they are
#: how a mapping from a fixed vocabulary (a daemon enumerator, a status token)
#: onto a message id is written, and the message ids are easy to forget.
_DISPLAY_DICT_TABLES = frozenset({"SEVERITY_MESSAGES"})

#: A flat ``(message, message, ...)`` sequence of interface text.  Empty ids are
#: skipped so a table can align its entries with something else if it needs to.
_TEXT_SEQUENCES = frozenset(
    {
        "_ABOUT_MESSAGES",
        "_PREFERENCES_MESSAGES",
        "_STATS_CARD_MESSAGES",
        "COLUMN_HEADERS",
    }
)


def _table_display_strings(node: ast.AST) -> list[str]:
    """Return display text stored in a module-level label table."""

    target = None
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        target = node.target
    if not isinstance(target, ast.Name):
        return []
    if target.id in _TEXT_SEQUENCES:
        if not isinstance(node.value, ast.Tuple | ast.List):
            return []
        return [
            element.value
            for element in node.value.elts
            if isinstance(element, ast.Constant)
            and isinstance(element.value, str)
            and _is_translatable(element.value)
        ]
    if target.id in _DISPLAY_DICT_TABLES:
        if not isinstance(node.value, ast.Dict):
            return []
        return [
            value.value
            for value in node.value.values
            if isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and _is_translatable(value.value)
        ]
    if target.id not in _DISPLAY_TABLES:
        return []
    index = _DISPLAY_TABLES[target.id]
    if not isinstance(node.value, ast.List | ast.Tuple):
        return []
    found: list[str] = []
    for row in node.value.elts:
        if not isinstance(row, ast.Tuple) or len(row.elts) <= index:
            continue
        cell = row.elts[index]
        if (
            isinstance(cell, ast.Constant)
            and isinstance(cell.value, str)
            and _is_translatable(cell.value)
        ):
            found.append(cell.value)
    return found


def extract_strings(source: Path) -> set[str]:
    """Return every translatable literal in ``source``."""

    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

    # Docstrings are documentation, not interface text.
    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    strings: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name in _TEXT_SETTERS:
                # A literal argument is a message. A variable is not: the
                # ``tr()`` at the call site or a declared message table owns it.
                positional = _positional_text(node.args[0]) if node.args else []
                strings.update(positional)
            if name in _TEXT_CONSTRUCTORS:
                # Constructors take their visible label positionally; this is
                # how most dialog copy reaches the screen.
                positional = _positional_text(node.args[0]) if node.args else []
                strings.update(positional)
            if name in _TRANSLATED_CALLS:
                for argument in node.args[:1]:
                    if (
                        isinstance(argument, ast.Constant)
                        and isinstance(argument.value, str)
                        and _is_translatable(argument.value)
                    ):
                        strings.add(argument.value)
                # ``tr_plural(singular, plural, n)`` carries two messages.
                if name == "tr_plural":
                    for argument in node.args[:2]:
                        if (
                            isinstance(argument, ast.Constant)
                            and isinstance(argument.value, str)
                            and _is_translatable(argument.value)
                        ):
                            strings.add(argument.value)
            strings.update(_keyword_display_strings(node))
            strings.update(_positional_display(node))

        # Module-level tables (categories, sections, evidence labels) hold the
        # bulk of the vocabulary; collect their label/tooltip values.
        elif isinstance(node, ast.keyword) and (
            node.arg in _DISPLAY_KEYWORDS
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and _is_translatable(node.value.value)
        ):
            strings.add(node.value.value)

        strings.update(_table_display_strings(node))

    strings -= {value for value in strings if value in docstrings}
    return strings


def source_files() -> list[Path]:
    """Return the Python files that contain user-facing text."""

    files = [PROJECT_ROOT / "main.py"]
    for directory in ("ui", "core"):
        files.extend(sorted((PROJECT_ROOT / directory).glob("*.py")))
    return [path for path in files if path.is_file()]


def collect() -> list[str]:
    """Return all translatable strings in the project, sorted."""

    found: set[str] = set()
    for path in source_files():
        found |= extract_strings(path)
    return sorted(found)


def load_catalog(locale: str) -> dict:
    path = CATALOG_DIR / f"{locale}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def shipped_locales() -> list[str]:
    return sorted(path.stem for path in CATALOG_DIR.glob("*.json") if path.stem != "en")


def cmd_list(args: argparse.Namespace) -> int:
    for text in collect():
        print(text)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    strings = set(collect())
    for locale in shipped_locales():
        messages = load_catalog(locale).get("messages", {})
        covered = len(strings & set(messages))
        percent = (covered / len(strings) * 100) if strings else 0.0
        print(f"{locale:8s} {covered:4d}/{len(strings):4d}  {percent:5.1f}%")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    strings = set(collect())
    failures = 0
    print(f"source strings: {len(strings)}")

    for locale in shipped_locales():
        payload = load_catalog(locale)
        messages = payload.get("messages", {})
        missing = sorted(strings - set(messages))
        extra = sorted(set(messages) - strings)
        empty = sorted(key for key, value in messages.items() if not str(value).strip())

        status = "ok" if not (missing or extra or empty) else "FAIL"
        print(
            f"{locale:8s} {len(messages):4d} entries  "
            f"missing={len(missing):3d} extra={len(extra):3d} empty={len(empty):3d}  {status}"
        )

        if missing or extra or empty:
            failures += 1
            if not args.quiet:
                for text in missing[: args.max_report]:
                    print(f"    missing: {text[:100]!r}")
                for text in extra[: args.max_report]:
                    print(f"    extra  : {text[:100]!r}")
                for text in empty[: args.max_report]:
                    print(f"    empty  : {text[:100]!r}")

    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="print translatable source strings")
    list_parser.set_defaults(func=cmd_list)

    check_parser = sub.add_parser("check", help="verify catalog coverage")
    check_parser.add_argument("--quiet", action="store_true")
    check_parser.add_argument("--max-report", type=int, default=10)
    check_parser.set_defaults(func=cmd_check)

    stats_parser = sub.add_parser("stats", help="print per-locale coverage")
    stats_parser.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
