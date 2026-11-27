"""Safe, focused package metadata readers for Dendro taxonomy.

The module owns only metadata acquisition and normalization: compressed XML,
Fedora Comps, AppStream, RPM manifest anatomy, and installed desktop entries.
It deliberately does not decide a package category; that belongs to
``core.package_classifier``.
"""

from __future__ import annotations

import bz2
import glob
import gzip
import logging
import lzma
import os
import re
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

try:
    import yaml as _pyyaml  # type: ignore[import-not-found, import-untyped]
except ImportError:
    _pyyaml = None

from dendro.core.ecosystems import (
    ecosystem_ids_for_capability,
    ecosystem_ids_for_directory,
)

LOGGER = logging.getLogger(__name__)

_YAML_ERROR_TYPES = tuple(
    error for error in (getattr(_pyyaml, "YAMLError", None),) if isinstance(error, type)
)

try:
    # Python 3.14+ supplies a streaming Zstandard reader in the standard library.
    from compression import zstd as _stdlib_zstd
except ImportError:
    _stdlib_zstd = None

try:
    # Keep Python 3.12/3.13 installations capable when the optional package is
    # available, without making a native extension mandatory for every install.
    import zstandard as _third_party_zstd  # type: ignore[import-not-found]
except ImportError:
    _third_party_zstd = None

_ZSTD_ERROR_TYPES = tuple(
    error
    for codec in (_stdlib_zstd, _third_party_zstd)
    if isinstance((error := getattr(codec, "ZstdError", None)), type)
)

MEDIA_PLUGIN_DIR_KEYWORDS: Final[tuple[str, ...]] = (
    "/vlc/plugins",
    "/gstreamer-1.0",
    "/xine/plugins",
    "/ladspa",
    "/lv2",
)
GUI_TOOLKIT_DIR_KEYWORDS: Final[tuple[str, ...]] = (
    "/qt5/plugins",
    "/qt6/plugins",
    "/gtk-3.0",
    "/gtk-4.0",
)
DESKTOP_SESSION_DIRS: Final[tuple[str, ...]] = (
    "/usr/share/plasma/shells",
    "/usr/share/gnome-shell",
    "/usr/share/cinnamon",
    "/usr/share/wayland-sessions",
    "/usr/share/xsessions",
)
# These directories describe data roles, not arbitrary /usr/share content. They
# deliberately use narrow, stable FHS locations so an unrelated documentation
# or data package is never promoted simply because it owns a generic directory.
REPOSITORY_CONFIGURATION_DIRS: Final[tuple[str, ...]] = (
    "/etc/yum.repos.d",
    "/etc/distro.repos.d",
    "/etc/dnf",
    "/etc/dnf/repos.d",
    "/etc/dnf/repos.override.d",
    "/etc/pki/rpm-gpg",
    "/usr/share/dnf5/repos.d",
    "/usr/share/dnf5/repos.override.d",
    "/usr/share/pki/rpm-gpg",
    "/usr/share/distribution-gpg-keys",
)


def _matches_directory_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    """Return True when ``path`` equals or descends from one of ``prefixes``."""
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


METAINFO_DATA_DIRS: Final[tuple[str, ...]] = (
    "/usr/share/metainfo",
    "/usr/share/appdata",
)
SYSTEM_DATA_DIRS: Final[tuple[str, ...]] = (
    "/usr/share/app-info",
    "/usr/share/swcatalog",
    "/usr/share/zoneinfo",
    "/usr/share/hwdata",
    "/usr/share/kbd",
    "/usr/share/x11/xkb",
    "/usr/share/libwacom",
    "/usr/share/publicsuffix",
    "/usr/share/mime",
    "/usr/share/color",
    "/usr/lib/udev/hwdb.d",
    "/etc/udev/hwdb.d",
)
REFERENCE_DATA_DIRS: Final[tuple[str, ...]] = (
    "/usr/share/xml",
    "/usr/share/sgml",
    "/etc/xml",
    "/etc/sgml",
    "/usr/share/docbook",
    "/usr/share/adobe",
    "/usr/share/poppler",
    "/usr/share/tesseract",
    "/usr/share/pcre2",
)
DEVELOPMENT_DATA_DIRS: Final[tuple[str, ...]] = (
    "/usr/share/cmake",
    "/usr/lib/cmake",
    "/usr/lib64/cmake",
    "/usr/share/aclocal",
)
XORG_DRIVER_DIRS: Final[tuple[str, ...]] = (
    "/usr/lib/xorg/modules/drivers",
    "/usr/lib64/xorg/modules/drivers",
)

_COMPRESSION_SUFFIXES: Final[tuple[str, ...]] = (
    ".zst",
    ".zstd",
    ".xz",
    ".bz2",
    ".gz",
)
_MAX_DEP11_YAML_BYTES: Final[int] = 64 * 1024 * 1024
_BLOCK_SCALAR_RE: Final[re.Pattern[str]] = re.compile(r"^([|>])([+-]?)([1-9]?)$")


class UnsupportedMetadataCompression(OSError):
    """Raised when an optional local repository-metadata codec is absent."""


@contextmanager
def open_compressed_metadata(file_path: str) -> Iterator[Any]:
    """Yield a binary stream for a supported local XML or YAML metadata file.

    Unsupported compression formats fail explicitly rather than producing a
    misleading parser error.
    """
    if file_path.endswith(".gz"):
        with gzip.open(file_path, "rb") as stream:
            yield stream
        return
    if file_path.endswith(".bz2"):
        with bz2.open(file_path, "rb") as stream:
            yield stream
        return
    if file_path.endswith(".xz"):
        with lzma.open(file_path, "rb") as stream:
            yield stream
        return
    if file_path.endswith((".zst", ".zstd")):
        if _stdlib_zstd is not None:
            with _stdlib_zstd.open(file_path, "rb") as stream:
                yield stream
            return
        if _third_party_zstd is not None:
            with open(file_path, "rb") as compressed_stream:
                decompressor = _third_party_zstd.ZstdDecompressor()
                with decompressor.stream_reader(compressed_stream) as stream:
                    yield stream
            return
        raise UnsupportedMetadataCompression(
            "Zstandard-compressed repository metadata needs Python 3.14+ "
            "or the optional zstandard package."
        )
    if file_path.endswith(".zck"):
        raise UnsupportedMetadataCompression(
            "Zchunk-compressed repository metadata is not supported."
        )

    with open(file_path, "rb") as stream:
        yield stream


@contextmanager
def open_compressed_xml_metadata(file_path: str) -> Iterator[Any]:
    """Yield a binary XML stream for a supported local metadata file."""
    with open_compressed_metadata(file_path) as stream:
        yield stream


def _local_name(tag: object) -> str:
    """Return an XML local name for both namespaced and legacy metadata."""
    text = str(tag)
    return text.rsplit("}", 1)[-1]


def _children(element: Any, name: str) -> list[Any]:
    return [child for child in element if _local_name(child.tag) == name]


def _first_child(element: Any, name: str) -> Any | None:
    return next(iter(_children(element, name)), None)


def _strip_yaml_comment(line: str) -> str:
    """Strip an unquoted ``#`` YAML comment while preserving quoted ``#``."""
    in_single = False
    in_double = False
    escaped = False
    for index, ch in enumerate(line):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            continue
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line.rstrip()


def _split_yaml_key_value(content: str) -> tuple[str, str] | None:
    """Split a ``key: value`` mapping line outside quotes and flow brackets."""
    in_single = False
    in_double = False
    escaped = False
    flow_depth = 0
    for index, ch in enumerate(content):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            continue
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch in "[{":
            flow_depth += 1
        elif ch in "]}":
            flow_depth = max(0, flow_depth - 1)
        elif ch == ":" and flow_depth == 0:
            next_is_sep = index + 1 == len(content) or content[index + 1].isspace()
            if next_is_sep:
                return content[:index].strip(), content[index + 1 :].lstrip()
    return None


def _split_yaml_flow_items(inner: str) -> list[str]:
    """Split top-level comma-separated items inside a YAML flow literal."""
    items: list[str] = []
    start = 0
    in_single = False
    in_double = False
    escaped = False
    depth = 0
    for index, ch in enumerate(inner):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            continue
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            part = inner[start:index].strip()
            if part:
                items.append(part)
            start = index + 1
    tail = inner[start:].strip()
    if tail:
        items.append(tail)
    return items


def _unquote_yaml_scalar(raw: str) -> str:
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        inner = raw[1:-1]
        return (
            inner.replace("\\\\", "\x00")
            .replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\r", "\r")
            .replace("\x00", "\\")
        )
    return raw


def _parse_yaml_scalar(raw: str, *, depth: int = 0) -> Any:
    """Parse a YAML scalar or bounded flow collection safely."""
    value = raw.strip()
    if not value:
        return None
    if value.startswith(("'", '"')) and len(value) >= 2 and value[-1] == value[0]:
        return _unquote_yaml_scalar(value)
    if depth < 16 and value.startswith("[") and value.endswith("]"):
        return [
            _parse_yaml_scalar(item, depth=depth + 1)
            for item in _split_yaml_flow_items(value[1:-1])
        ]
    if depth < 16 and value.startswith("{") and value.endswith("}"):
        mapping: dict[str, Any] = {}
        for item in _split_yaml_flow_items(value[1:-1]):
            pair = _split_yaml_key_value(item)
            if pair is not None:
                key_raw, val_raw = pair
                key = str(_unquote_yaml_scalar(key_raw))
                mapping[key] = _parse_yaml_scalar(val_raw, depth=depth + 1)
        return mapping
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.startswith("!"):
        # Reject local/custom YAML tags rather than constructing arbitrary types.
        return None
    return value


def _consume_yaml_block_scalar(
    lines: list[str],
    start_index: int,
    parent_indent: int,
    spec_text: str,
) -> tuple[str, int]:
    """Read a YAML literal (``|``) or folded (``>``) block scalar."""
    match = _BLOCK_SCALAR_RE.match(spec_text)
    if match is None:
        return "", start_index
    style, chomp, indent_digit = match.groups()
    explicit_indent = parent_indent + int(indent_digit) if indent_digit else None
    block_indent: int | None = explicit_indent
    collected: list[str] = []
    idx = start_index
    while idx < len(lines):
        raw_line = lines[idx]
        if raw_line.lstrip() == "":
            collected.append("")
            idx += 1
            continue
        if (
            idx > 0
            and raw_line.startswith(("---", "..."))
            and (len(raw_line) == 3 or raw_line[3].isspace())
        ):
            break
        line_indent = len(raw_line) - len(raw_line.lstrip(" "))
        if block_indent is None:
            if line_indent <= parent_indent:
                break
            block_indent = line_indent
        elif line_indent < block_indent:
            break
        collected.append(raw_line[block_indent:])
        idx += 1

    while collected and collected[-1] == "":
        collected.pop()
    if style == "|":
        text = "\n".join(collected)
    else:
        paragraphs: list[str] = []
        current: list[str] = []
        for entry in collected:
            if entry == "":
                if current:
                    paragraphs.append(" ".join(current))
                    current = []
                else:
                    paragraphs.append("")
            else:
                current.append(entry)
        if current:
            paragraphs.append(" ".join(current))
        text = "\n\n".join(paragraphs)
    if chomp == "+":
        return f"{text}\n" if text else "", idx
    if chomp == "-":
        return text.rstrip("\n"), idx
    return text, idx


def _pop_yaml_stack(
    stack: list[tuple[Any, ...]],
    target_indent: int,
    *,
    pop_equal_seq: bool,
) -> None:
    """Unwind active YAML block frames above ``target_indent``."""
    while len(stack) > 1:
        top = stack[-1]
        kind = top[0]
        frame_indent = top[1]
        if kind == "pending":
            should_pop = (
                frame_indent >= target_indent
                if pop_equal_seq
                else frame_indent > target_indent
            )
            if not should_pop:
                break
            top[2][top[3]] = None
            stack.pop()
            continue
        if kind == "seq":
            should_pop = (
                frame_indent >= target_indent
                if pop_equal_seq
                else frame_indent > target_indent
            )
            if not should_pop:
                break
            stack.pop()
            continue
        if frame_indent > target_indent:
            stack.pop()
            continue
        break


def _materialize_pending_container(
    stack: list[tuple[Any, ...]],
    child_indent: int,
    *,
    expect_sequence: bool,
) -> list[Any] | dict[str, Any] | None:
    """Turn a pending ``key:`` slot on top of ``stack`` into a list or dict."""
    top = stack[-1]
    if top[0] != "pending":
        return None
    pending_indent, parent_map, pending_key = top[1], top[2], top[3]
    if expect_sequence:
        if pending_indent > child_indent:
            return None
        new_seq: list[Any] = []
        parent_map[pending_key] = new_seq
        stack[-1] = ("seq", child_indent, new_seq)
        return new_seq
    if pending_indent >= child_indent:
        return None
    new_map: dict[str, Any] = {}
    parent_map[pending_key] = new_map
    stack[-1] = ("map", child_indent, new_map)
    return new_map


def _parse_dep11_yaml_stream(stream: Any) -> list[dict[str, Any]]:
    """Parse DEP-11 multi-document YAML safely using PyYAML or pure Python."""
    raw_bytes = stream.read(_MAX_DEP11_YAML_BYTES + 1)
    if len(raw_bytes) > _MAX_DEP11_YAML_BYTES:
        raise ValueError("DEP-11 YAML file exceeds maximum supported size.")
    text = raw_bytes.decode("utf-8")
    if _pyyaml is not None:
        return [doc for doc in _pyyaml.safe_load_all(text) if isinstance(doc, Mapping)]

    lines = text.splitlines()
    documents: list[dict[str, Any]] = []
    current_doc: dict[str, Any] = {}
    stack: list[tuple[Any, ...]] = [("map", 0, current_doc)]
    has_content = False

    def _flush_doc() -> None:
        nonlocal current_doc, stack, has_content
        _pop_yaml_stack(stack, -1, pop_equal_seq=True)
        if has_content and current_doc:
            documents.append(current_doc)
        current_doc = {}
        stack = [("map", 0, current_doc)]
        has_content = False

    idx = 0
    line_count = len(lines)
    while idx < line_count:
        raw_line = lines[idx]
        idx += 1
        stripped_leading = raw_line.lstrip(" ")
        if not stripped_leading or stripped_leading.startswith("#"):
            continue
        indent = len(raw_line) - len(stripped_leading)
        if indent == 0 and (
            stripped_leading in {"---", "..."}
            or stripped_leading.startswith(("--- ", "... "))
        ):
            _flush_doc()
            continue

        cleaned = _strip_yaml_comment(stripped_leading)
        if not cleaned:
            continue

        is_seq_line = cleaned == "-" or cleaned.startswith("- ")
        if is_seq_line:
            _pop_yaml_stack(stack, indent, pop_equal_seq=False)
            seq_obj = _materialize_pending_container(
                stack, indent, expect_sequence=True
            )
            if seq_obj is None and stack[-1][0] == "seq" and stack[-1][1] == indent:
                seq_obj = stack[-1][2]
            if not isinstance(seq_obj, list):
                continue
            has_content = True
            item_text = cleaned[1:].lstrip(" ")
            if not item_text:
                seq_obj.append(None)
                continue
            block_match = _BLOCK_SCALAR_RE.match(item_text)
            if block_match is not None:
                scalar_text, idx = _consume_yaml_block_scalar(
                    lines, idx, indent, item_text
                )
                seq_obj.append(scalar_text)
                continue
            pair = _split_yaml_key_value(item_text)
            if pair is not None:
                key_raw, val_raw = pair
                key = str(_unquote_yaml_scalar(key_raw))
                item_map: dict[str, Any] = {}
                seq_obj.append(item_map)
                key_col = indent + 2
                stack.append(("map", key_col, item_map))
                if not val_raw:
                    stack.append(("pending", key_col, item_map, key))
                elif _BLOCK_SCALAR_RE.match(val_raw) is not None:
                    scalar_text, idx = _consume_yaml_block_scalar(
                        lines, idx, key_col, val_raw
                    )
                    item_map[key] = scalar_text
                else:
                    item_map[key] = _parse_yaml_scalar(val_raw)
                continue
            seq_obj.append(_parse_yaml_scalar(item_text))
            continue

        pair = _split_yaml_key_value(cleaned)
        if pair is None:
            continue
        _pop_yaml_stack(stack, indent, pop_equal_seq=True)
        _materialize_pending_container(stack, indent, expect_sequence=False)
        if stack[-1][0] != "map":
            continue
        target_map = stack[-1][2]
        has_content = True
        key_raw, val_raw = pair
        key = str(_unquote_yaml_scalar(key_raw))
        if not val_raw:
            stack.append(("pending", indent, target_map, key))
        elif _BLOCK_SCALAR_RE.match(val_raw) is not None:
            scalar_text, idx = _consume_yaml_block_scalar(lines, idx, indent, val_raw)
            target_map[key] = scalar_text
        else:
            target_map[key] = _parse_yaml_scalar(val_raw)

    _flush_doc()
    return documents


class FedoraCompsCatalog:
    """Thread-safe, auxiliary index of local Fedora Comps group membership."""

    _instance: FedoraCompsCatalog | None = None
    _lock = threading.Lock()

    COMPS_GROUP_MAP: Final[Mapping[str, str]] = {
        "core": "fedora_core",
        "base": "fedora_core",
        "standard": "fedora_core",
        "development-tools": "devel",
        "development-libs": "c_libs",
        "c-development": "devel",
        "system-tools": "cli_tools",
        "security-lab": "security_pkgs",
    }

    def __init__(self, *, load_comps: bool = True) -> None:
        self.package_to_comps: dict[str, str] = {}
        self._loaded = False
        if load_comps:
            self._load_comps()

    @classmethod
    def get_instance(cls) -> FedoraCompsCatalog:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _load_comps(self) -> None:
        if self._loaded:
            return

        patterns = (
            "/var/cache/libdnf5/*/repodata/*comps*.xml*",
            "/var/cache/dnf/*/repodata/*comps*.xml*",
            "/var/cache/rpm-ostree/repomd/*/*comps*.xml*",
            os.path.expanduser("~/.cache/libdnf5/*/repodata/*comps*.xml*"),
        )
        files = sorted(
            {
                path
                for pattern in patterns
                for path in glob.glob(pattern, recursive=False)
            }
        )
        reported_unsupported_codecs: set[str] = set()

        for file_path in files:
            try:
                with open_compressed_xml_metadata(file_path) as stream:
                    self._index_comps_stream(stream)
            except UnsupportedMetadataCompression as exc:
                reason = str(exc)
                if reason not in reported_unsupported_codecs:
                    LOGGER.info(
                        "Skipping Fedora Comps metadata %s: %s", file_path, reason
                    )
                    reported_unsupported_codecs.add(reason)
            except (
                OSError,
                EOFError,
                ET.ParseError,
                DefusedXmlException,
                UnicodeError,
                *_ZSTD_ERROR_TYPES,
            ):
                LOGGER.warning(
                    "Could not parse Fedora Comps metadata %s; skipping that file.",
                    file_path,
                    exc_info=True,
                )

        self._loaded = True

    def _index_comps_stream(self, stream: Any) -> None:
        current_group_id = ""
        element_stack: list[str] = []
        for event, element in ET.iterparse(stream, events=("start", "end")):
            tag = _local_name(element.tag)
            if event == "start":
                element_stack.append(tag)
                continue

            parent_tag = element_stack[-2] if len(element_stack) > 1 else ""
            # Only a direct <group><id> identifies group membership. This
            # prevents a nested, unrelated <id> from accidentally changing the
            # active domain while streaming a third-party catalog.
            if tag == "id" and parent_tag == "group" and element.text:
                current_group_id = element.text.strip().lower()
            elif tag == "packagereq" and element.text and current_group_id:
                package_name = element.text.strip().lower()
                domain = self.COMPS_GROUP_MAP.get(current_group_id)
                existing = self.package_to_comps.get(package_name)
                # A package can belong to several groups. System Tools wins
                # over Security Lab for general command-line utilities.
                if domain and (
                    existing is None
                    or (domain == "cli_tools" and existing == "security_pkgs")
                ):
                    self.package_to_comps[package_name] = domain
            elif tag == "group":
                current_group_id = ""
                element.clear()
            element_stack.pop()

    def get_domain_for_package(self, package_name: str) -> str | None:
        return self.package_to_comps.get(package_name.lower())


def _xdg_data_home() -> str:
    """Resolve ``XDG_DATA_HOME`` dynamically so runtime/test env changes apply."""
    configured = os.environ.get("XDG_DATA_HOME", "").strip()
    if configured:
        return os.path.expanduser(configured)
    return os.path.expanduser("~/.local/share")


def _strip_compression_suffix(filename: str) -> tuple[str, str]:
    """Return ``(base_filename, compression_suffix)`` for a metadata filename."""
    for suffix in _COMPRESSION_SUFFIXES:
        if filename.endswith(suffix):
            return filename[: -len(suffix)], suffix
    return filename, ""


def _zstd_available() -> bool:
    return _stdlib_zstd is not None or _third_party_zstd is not None


def _select_deduplicated_catalog_files(
    directory: str,
    base_suffixes: tuple[str, ...],
) -> list[str]:
    """Return catalog file paths in ``directory`` skipping duplicate siblings."""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        LOGGER.warning(
            "Could not enumerate AppStream metadata directory %s; skipping it.",
            directory,
            exc_info=True,
        )
        return []

    zstd_supported = _zstd_available()
    rank = {
        ".zst": 0 if zstd_supported else 99,
        ".zstd": 0 if zstd_supported else 99,
        ".xz": 1,
        ".bz2": 2,
        ".gz": 3,
        "": 4,
    }
    chosen: dict[str, tuple[int, str]] = {}
    for filename in entries:
        base_name, comp_suffix = _strip_compression_suffix(filename)
        if not base_name.endswith(base_suffixes):
            continue
        file_rank = rank.get(comp_suffix, 50)
        previous = chosen.get(base_name)
        if previous is None or file_rank < previous[0]:
            chosen[base_name] = (file_rank, os.path.join(directory, filename))
    return [chosen[key][1] for key in sorted(chosen)]


@dataclass(frozen=True, slots=True)
class AppStreamComponent:
    """Normalized AppStream 1.0 component record from catalog or MetaInfo."""

    component_id: str
    component_type: str
    pkgnames: tuple[str, ...] = ()
    source_pkgname: str = ""
    categories: tuple[str, ...] = ()
    launchable_desktop_ids: tuple[str, ...] = ()
    compulsory_for_desktops: tuple[str, ...] = ()
    provided_dbus_system: tuple[str, ...] = ()
    provided_dbus_user: tuple[str, ...] = ()
    provided_firmware_runtime: tuple[str, ...] = ()
    provided_firmware_flashed: tuple[str, ...] = ()
    provided_modaliases: tuple[str, ...] = ()
    provided_mediatypes: tuple[str, ...] = ()
    provided_fonts: tuple[str, ...] = ()
    provided_binaries: tuple[str, ...] = ()
    provided_libraries: tuple[str, ...] = ()
    provided_python3: tuple[str, ...] = ()
    provided_ids: tuple[str, ...] = ()
    metainfo_filename: str = ""


def _unique_clean_strings(
    values: Iterable[object], *, lower: bool = False
) -> tuple[str, ...]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for raw in values:
        if raw is None:
            continue
        text = str(raw).strip()
        if lower:
            text = text.lower()
        if text and text not in seen_set:
            seen_set.add(text)
            seen.append(text)
    return tuple(seen)


class AppStreamCatalog:
    """Thread-safe AppStream 1.0 catalog and MetaInfo index.

    Uses ``gi.repository.AppStream`` (libappstream's ``AppStream.Pool``) on
    default OS metadata paths when available, and falls back to ``defusedxml``
    plus bounded DEP-11 YAML parsing otherwise.  Upstream MetaInfo files
    without ``<pkgname>`` remain queryable via MetaInfo filename and
    ``<launchable type="desktop-id">`` joins.
    """

    _instance: AppStreamCatalog | None = None
    _lock = threading.Lock()

    _CANONICAL_TYPES: Final[Mapping[str, str]] = {
        "": "generic",
        "generic": "generic",
        "desktop": "desktop-application",
        "desktop-application": "desktop-application",
        "application": "desktop-application",
        "webapp": "web-application",
        "web-application": "web-application",
        "console": "console-application",
        "console-application": "console-application",
        "inputmethod": "inputmethod",
        "input-method": "inputmethod",
        "addon": "addon",
        "shell-extension": "addon",
        "codec": "codec",
        "font": "font",
        "firmware": "firmware",
        "driver": "driver",
        "service": "service",
        "localization": "localization",
        "icon-theme": "icon-theme",
        "theme": "icon-theme",
        "runtime": "runtime",
        "repository": "repository",
        "operating-system": "operating-system",
    }

    # Component types listed here are indexed faithfully. ``runtime`` and
    # ``generic`` are retained for metadata consumers and diagnostics, and are
    # deliberately not promoted into desktop/CLI roles by type alone.
    _TYPE_TO_ATTRIBUTE: Final[Mapping[str, str]] = {
        "desktop": "desktop_packages",
        "desktop-application": "desktop_packages",
        "application": "desktop_packages",
        "webapp": "desktop_packages",
        "web-application": "desktop_packages",
        "console": "console_packages",
        "console-application": "console_packages",
        "inputmethod": "inputmethod_packages",
        "input-method": "inputmethod_packages",
        "addon": "addon_packages",
        "shell-extension": "addon_packages",
        "codec": "codec_packages",
        "font": "font_packages",
        "firmware": "firmware_packages",
        "driver": "driver_packages",
        "service": "service_packages",
        "localization": "localization_packages",
        "icon-theme": "icon_theme_packages",
        "theme": "icon_theme_packages",
        "runtime": "runtime_packages",
        "repository": "repository_packages",
        "operating-system": "operating_system_packages",
        "generic": "generic_packages",
    }

    _ALL_PACKAGE_TYPE_ATTRS: Final[tuple[str, ...]] = (
        "desktop_packages",
        "web_app_packages",
        "console_packages",
        "inputmethod_packages",
        "addon_packages",
        "codec_packages",
        "font_packages",
        "firmware_packages",
        "driver_packages",
        "service_packages",
        "localization_packages",
        "icon_theme_packages",
        "runtime_packages",
        "repository_packages",
        "operating_system_packages",
        "generic_packages",
    )

    def __init__(
        self,
        *,
        load_catalog: bool = True,
        catalog_dirs: Sequence[str] | None = None,
        metainfo_dirs: Sequence[str] | None = None,
        use_native_pool: bool | None = None,
    ) -> None:
        self.desktop_packages: set[str] = set()
        self.web_app_packages: set[str] = set()
        self.console_packages: set[str] = set()
        self.inputmethod_packages: set[str] = set()
        self.addon_packages: set[str] = set()
        self.codec_packages: set[str] = set()
        self.font_packages: set[str] = set()
        self.firmware_packages: set[str] = set()
        self.driver_packages: set[str] = set()
        self.service_packages: set[str] = set()
        self.localization_packages: set[str] = set()
        self.icon_theme_packages: set[str] = set()
        # Preserved for callers that inspect AppStream runtime/generic records.
        # They are intentionally not classifier signals by component type alone.
        self.runtime_packages: set[str] = set()
        self.repository_packages: set[str] = set()
        self.operating_system_packages: set[str] = set()
        self.generic_packages: set[str] = set()
        self.pkg_xdg_categories: dict[str, set[str]] = {}
        self.pkg_compulsory_desktops: dict[str, set[str]] = {}
        self.pkg_source_packages: dict[str, str] = {}
        self.pkg_components: dict[str, list[AppStreamComponent]] = {}
        self.metainfo_components: dict[str, AppStreamComponent] = {}
        self.component_id_components: dict[str, list[AppStreamComponent]] = {}
        self.desktop_id_components: dict[str, list[AppStreamComponent]] = {}
        self.source_pkg_components: dict[str, list[AppStreamComponent]] = {}
        self.backend_used = "none"
        self._catalog_dirs = tuple(catalog_dirs) if catalog_dirs is not None else None
        self._metainfo_dirs = (
            tuple(metainfo_dirs) if metainfo_dirs is not None else None
        )
        self._use_native_pool = use_native_pool
        self._loaded = False
        if load_catalog:
            self._load_catalog()

    @classmethod
    def get_instance(cls) -> AppStreamCatalog:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def invalidate_cache(cls) -> None:
        """Drop the cached singleton so subsequent reads reload AppStream state."""
        with cls._lock:
            cls._instance = None

    @classmethod
    def _canonical_type(cls, raw_type: str) -> str:
        normalized = raw_type.strip().lower()
        return cls._CANONICAL_TYPES.get(normalized, normalized or "generic")

    def _default_catalog_dirs(self) -> tuple[str, ...]:
        xdg_data = _xdg_data_home()
        return (
            "/usr/share/swcatalog/xml",
            "/usr/share/swcatalog/yaml",
            "/var/lib/swcatalog/xml",
            "/var/lib/swcatalog/yaml",
            "/var/cache/swcatalog/xml",
            "/var/cache/swcatalog/yaml",
            os.path.join(xdg_data, "swcatalog", "xml"),
            os.path.join(xdg_data, "swcatalog", "yaml"),
            "/usr/share/app-info/xmls",
            "/usr/share/app-info/yaml",
        )

    def _default_metainfo_dirs(self) -> tuple[str, ...]:
        return (
            "/usr/share/metainfo",
            "/usr/share/appdata",
            os.path.join(_xdg_data_home(), "metainfo"),
        )

    def _load_catalog(self) -> None:
        if self._loaded:
            return

        use_default_paths = self._catalog_dirs is None and self._metainfo_dirs is None
        should_try_native = self._use_native_pool is True or (
            self._use_native_pool is None and use_default_paths
        )
        if should_try_native and self._load_via_gi_appstream():
            self._load_supplemental_os_dirs()
            self.backend_used = "gi-appstream"
            self._loaded = True
            return

        self._load_python_catalogs()
        self.backend_used = "python"
        self._loaded = True

    def _load_via_gi_appstream(self) -> bool:
        """Load OS catalogs and MetaInfo via ``gi.repository.AppStream``."""
        try:
            import gi  # type: ignore[import-not-found, import-untyped]

            gi.require_version("AppStream", "1.0")
            from gi.repository import AppStream  # type: ignore[import-not-found]
        except Exception:
            return False

        try:
            pool = AppStream.Pool()
            flags = (
                AppStream.PoolFlags.LOAD_OS_CATALOG
                | AppStream.PoolFlags.LOAD_OS_METAINFO
            )
            pool.set_flags(flags)
            user_swcatalog = os.path.join(_xdg_data_home(), "swcatalog")
            if os.path.isdir(user_swcatalog):
                pool.add_extra_data_location(
                    user_swcatalog, AppStream.FormatStyle.CATALOG
                )
        except Exception:
            LOGGER.debug(
                "Could not configure AppStream.Pool; falling back to Python parser.",
                exc_info=True,
            )
            return False

        try:
            pool.load(None)
        except Exception:
            # AppStream.Pool.load() can raise GLib.Error on one malformed file
            # while still populating valid components in the pool.
            LOGGER.debug("AppStream.Pool.load finished with warnings.", exc_info=True)

        try:
            # Keep component_box alive across the loop: AsComponentBox.as_array()
            # returns (transfer none) internal pointers and can segfault on
            # temporary boxes in PyGObject (Bug#1091493).
            component_box = pool.get_components()
            size = int(component_box.get_size())
            for index in range(size):
                raw_component = component_box.index_safe(index)
                if raw_component is not None:
                    self._index_gi_component(AppStream, raw_component)
        except Exception:
            LOGGER.warning(
                "Failed extracting AppStream.Pool components; falling back to Python.",
                exc_info=True,
            )
            return False
        return True

    def _load_supplemental_os_dirs(self) -> None:
        """Load legacy/user locations not covered by ``AsPool`` OS flags."""
        supplemental_catalogs = (
            "/usr/share/app-info/xmls",
            "/usr/share/app-info/yaml",
        )
        supplemental_metainfo = (
            "/usr/share/appdata",
            os.path.join(_xdg_data_home(), "metainfo"),
        )
        self._scan_catalog_dirs(supplemental_catalogs)
        self._scan_metainfo_dirs(supplemental_metainfo)

    def _load_python_catalogs(self) -> None:
        catalog_dirs = (
            self._catalog_dirs
            if self._catalog_dirs is not None
            else self._default_catalog_dirs()
        )
        metainfo_dirs = (
            self._metainfo_dirs
            if self._metainfo_dirs is not None
            else self._default_metainfo_dirs()
        )
        self._scan_catalog_dirs(catalog_dirs)
        self._scan_metainfo_dirs(metainfo_dirs)

    def _scan_catalog_dirs(self, catalog_dirs: Sequence[str]) -> None:
        supported_base_suffixes = (".xml", ".yml", ".yaml")
        for catalog_dir in catalog_dirs:
            if not os.path.isdir(catalog_dir):
                continue
            for file_path in _select_deduplicated_catalog_files(
                catalog_dir, supported_base_suffixes
            ):
                self._parse_appstream_file(file_path)

    def _scan_metainfo_dirs(self, metainfo_dirs: Sequence[str]) -> None:
        for metainfo_dir in metainfo_dirs:
            if not os.path.isdir(metainfo_dir):
                continue
            self._scan_single_metainfo_dir(metainfo_dir)

    def _scan_single_metainfo_dir(self, metainfo_dir: str) -> None:
        try:
            filenames = sorted(os.listdir(metainfo_dir))
        except OSError:
            LOGGER.warning(
                "Could not enumerate AppStream metadata directory %s; skipping it.",
                metainfo_dir,
                exc_info=True,
            )
            return
        for filename in filenames:
            if filename.lower().endswith((".metainfo.xml", ".appdata.xml", ".xml")):
                self._parse_appstream_file(
                    os.path.join(metainfo_dir, filename),
                    metainfo_filename=filename,
                )

    def _index_gi_component(self, appstream_mod: Any, cpt: Any) -> None:
        try:
            kind_enum = cpt.get_kind()
            raw_kind = appstream_mod.component_kind_to_string(kind_enum) or getattr(
                kind_enum, "value_nick", ""
            )
            component_type = self._canonical_type(str(raw_kind or ""))
            component_id = str(cpt.get_id() or "").strip()
            pkgnames = _unique_clean_strings(cpt.get_pkgnames() or (), lower=True)
            if not pkgnames:
                pkgnames = self._gi_package_bundle_names(appstream_mod, cpt)
            source_pkgname = str(cpt.get_source_pkgname() or "").strip().lower()
            categories = _unique_clean_strings(cpt.get_categories() or (), lower=True)
            compulsory = _unique_clean_strings(cpt.get_compulsory_for_desktops() or ())
            desktop_ids = self._gi_launchable_desktop_ids(appstream_mod, cpt)
            provided = self._gi_provided_values(appstream_mod, cpt)
            record = AppStreamComponent(
                component_id=component_id,
                component_type=component_type,
                pkgnames=pkgnames,
                source_pkgname=source_pkgname,
                categories=categories,
                launchable_desktop_ids=desktop_ids,
                compulsory_for_desktops=compulsory,
                **provided,
            )
            self._register_component(record)
        except Exception:
            LOGGER.debug("Skipping malformed GI AppStream component.", exc_info=True)

    @staticmethod
    def _gi_package_bundle_names(appstream_mod: Any, cpt: Any) -> tuple[str, ...]:
        package_kind = getattr(
            getattr(appstream_mod, "BundleKind", None), "PACKAGE", None
        )
        names: list[str] = []
        for bundle in cpt.get_bundles() or ():
            if package_kind is not None and bundle.get_kind() != package_kind:
                continue
            bundle_id = str(bundle.get_id() or "").strip().lower()
            if bundle_id:
                names.append(bundle_id)
        return _unique_clean_strings(names, lower=True)

    @staticmethod
    def _gi_launchable_desktop_ids(appstream_mod: Any, cpt: Any) -> tuple[str, ...]:
        desktop_kind = getattr(
            getattr(appstream_mod, "LaunchableKind", None), "DESKTOP_ID", None
        )
        desktop_ids: list[str] = []
        for launchable in cpt.get_launchables() or ():
            if desktop_kind is not None and launchable.get_kind() != desktop_kind:
                continue
            desktop_ids.extend(launchable.get_entries() or ())
        return _unique_clean_strings(desktop_ids)

    @staticmethod
    def _gi_provided_values(appstream_mod: Any, cpt: Any) -> dict[str, tuple[str, ...]]:
        provided_kind = getattr(appstream_mod, "ProvidedKind", None)
        attr_by_kind = {
            getattr(provided_kind, "DBUS_SYSTEM", None): "provided_dbus_system",
            getattr(provided_kind, "DBUS_USER", None): "provided_dbus_user",
            getattr(
                provided_kind, "FIRMWARE_RUNTIME", None
            ): "provided_firmware_runtime",
            getattr(
                provided_kind, "FIRMWARE_FLASHED", None
            ): "provided_firmware_flashed",
            getattr(provided_kind, "MODALIAS", None): "provided_modaliases",
            getattr(provided_kind, "MEDIATYPE", None): "provided_mediatypes",
            getattr(provided_kind, "FONT", None): "provided_fonts",
            getattr(provided_kind, "BINARY", None): "provided_binaries",
            getattr(provided_kind, "LIBRARY", None): "provided_libraries",
            getattr(provided_kind, "PYTHON", None): "provided_python3",
            getattr(provided_kind, "ID", None): "provided_ids",
        }
        buckets: dict[str, list[str]] = {}
        for prov in cpt.get_provided() or ():
            field_name = attr_by_kind.get(prov.get_kind())
            if field_name is not None:
                buckets.setdefault(field_name, []).extend(prov.get_items() or ())
        return {key: _unique_clean_strings(values) for key, values in buckets.items()}

    def _parse_appstream_file(
        self, file_path: str, *, metainfo_filename: str = ""
    ) -> None:
        base_name, _comp_suffix = _strip_compression_suffix(file_path.lower())
        if base_name.endswith((".yml", ".yaml")):
            self._parse_dep11_yaml_file(file_path)
            return
        resolved_metainfo = metainfo_filename
        if not resolved_metainfo and base_name.endswith(
            (".metainfo.xml", ".appdata.xml")
        ):
            resolved_metainfo = os.path.basename(file_path)
        self._parse_appstream_xml_file(file_path, metainfo_filename=resolved_metainfo)

    def _parse_appstream_xml_file(
        self, file_path: str, *, metainfo_filename: str = ""
    ) -> None:
        try:
            with open_compressed_xml_metadata(file_path) as stream:
                context = ET.iterparse(stream, events=("start", "end"))
                _event, root = next(context)
                root_tag = _local_name(root.tag)
                is_single_metainfo = root_tag in {"component", "application"}
                for event, element in context:
                    if event != "end" or _local_name(element.tag) not in {
                        "component",
                        "application",
                    }:
                        continue
                    if is_single_metainfo and element is not root:
                        continue
                    self._index_xml_component(
                        element,
                        metainfo_filename=metainfo_filename
                        if is_single_metainfo
                        else "",
                    )
                    element.clear()
                    if not is_single_metainfo:
                        root.clear()
        except (
            OSError,
            EOFError,
            ET.ParseError,
            DefusedXmlException,
            StopIteration,
            UnicodeError,
            *_ZSTD_ERROR_TYPES,
        ):
            LOGGER.warning(
                "Could not parse AppStream metadata %s; skipping that file.",
                file_path,
                exc_info=True,
            )

    def _parse_dep11_yaml_file(self, file_path: str) -> None:
        try:
            with open_compressed_metadata(file_path) as stream:
                documents = _parse_dep11_yaml_stream(stream)
        except (
            OSError,
            EOFError,
            UnicodeError,
            ValueError,
            *_ZSTD_ERROR_TYPES,
            *_YAML_ERROR_TYPES,
        ):
            LOGGER.warning(
                "Could not parse AppStream metadata %s; skipping that file.",
                file_path,
                exc_info=True,
            )
            return
        if not documents:
            return
        header = documents[0]
        if str(header.get("File") or "").strip().lower() != "dep-11":
            LOGGER.debug("Skipping non-DEP-11 YAML file %s.", file_path)
            return
        for doc in documents[1:]:
            self._index_yaml_component(doc)

    def _index_xml_component(
        self, component: Any, *, metainfo_filename: str = ""
    ) -> None:
        raw_type = (component.get("type") or "").strip().lower()
        component_type = self._canonical_type(raw_type)
        merge_kind = (component.get("merge") or "").strip().lower()

        id_element = _first_child(component, "id")
        component_id = (
            id_element.text.strip()
            if id_element is not None and id_element.text
            else ""
        )
        package_names = _unique_clean_strings(
            (child.text for child in _children(component, "pkgname")), lower=True
        )
        if not package_names:
            package_names = _unique_clean_strings(
                (
                    child.text
                    for child in _children(component, "bundle")
                    if child.get("type") == "package"
                ),
                lower=True,
            )
        source_element = _first_child(component, "source_pkgname")
        source_pkgname = (
            source_element.text.strip().lower()
            if source_element is not None and source_element.text
            else ""
        )
        categories_element = _first_child(component, "categories")
        categories = (
            _unique_clean_strings(
                (child.text for child in _children(categories_element, "category")),
                lower=True,
            )
            if categories_element is not None
            else ()
        )
        compulsory = _unique_clean_strings(
            child.text for child in _children(component, "compulsory_for_desktop")
        )
        launchable_desktop_ids = _unique_clean_strings(
            child.text
            for child in _children(component, "launchable")
            if (child.get("type") or "").strip().lower() == "desktop-id"
        )
        provided = self._xml_provided_values(_first_child(component, "provides"))
        record = AppStreamComponent(
            component_id=component_id,
            component_type=component_type,
            pkgnames=package_names,
            source_pkgname=source_pkgname,
            categories=categories,
            launchable_desktop_ids=launchable_desktop_ids,
            compulsory_for_desktops=compulsory,
            metainfo_filename=metainfo_filename,
            **provided,
        )
        self._register_component(record, merge=merge_kind)

    @staticmethod
    def _xml_provided_values(
        provides_element: Any | None,
    ) -> dict[str, tuple[str, ...]]:
        if provides_element is None:
            return {}
        buckets: dict[str, list[str]] = {}
        for child in provides_element:
            text = (child.text or "").strip()
            if not text:
                continue
            tag = _local_name(child.tag)
            if tag == "dbus":
                dbus_type = (child.get("type") or "user").strip().lower()
                key = (
                    "provided_dbus_system"
                    if dbus_type == "system"
                    else "provided_dbus_user"
                )
                buckets.setdefault(key, []).append(text)
            elif tag == "firmware":
                fw_type = (child.get("type") or "runtime").strip().lower()
                key = (
                    "provided_firmware_flashed"
                    if fw_type == "flashed"
                    else "provided_firmware_runtime"
                )
                buckets.setdefault(key, []).append(text)
            else:
                direct_map = {
                    "modalias": "provided_modaliases",
                    "mediatype": "provided_mediatypes",
                    "mimetype": "provided_mediatypes",
                    "font": "provided_fonts",
                    "binary": "provided_binaries",
                    "library": "provided_libraries",
                    "python3": "provided_python3",
                    "id": "provided_ids",
                }
                field_name = direct_map.get(tag)
                if field_name is not None:
                    buckets.setdefault(field_name, []).append(text)
        return {key: _unique_clean_strings(values) for key, values in buckets.items()}

    def _index_yaml_component(self, doc: Mapping[str, Any]) -> None:
        raw_type = str(doc.get("Type") or "").strip().lower()
        component_type = self._canonical_type(raw_type)
        merge_kind = str(doc.get("Merge") or "").strip().lower()
        component_id = str(doc.get("ID") or "").strip()

        raw_package = doc.get("Package")
        raw_packages = doc.get("Packages")
        package_candidates: list[object] = []
        if isinstance(raw_package, str):
            package_candidates.append(raw_package)
        elif isinstance(raw_package, Sequence):
            package_candidates.extend(raw_package)
        if isinstance(raw_packages, Sequence) and not isinstance(
            raw_packages, (str, bytes)
        ):
            package_candidates.extend(raw_packages)
        package_names = _unique_clean_strings(package_candidates, lower=True)
        if not package_names:
            raw_bundles = doc.get("Bundles")
            if isinstance(raw_bundles, Sequence) and not isinstance(
                raw_bundles, (str, bytes)
            ):
                package_names = _unique_clean_strings(
                    (
                        bundle.get("id")
                        for bundle in raw_bundles
                        if isinstance(bundle, Mapping)
                        and str(bundle.get("type") or "").strip().lower() == "package"
                    ),
                    lower=True,
                )

        source_pkgname = str(doc.get("SourcePackage") or "").strip().lower()
        raw_categories = doc.get("Categories")
        categories = (
            _unique_clean_strings(raw_categories, lower=True)
            if isinstance(raw_categories, Sequence)
            and not isinstance(raw_categories, (str, bytes))
            else ()
        )
        raw_compulsory = doc.get("CompulsoryForDesktop")
        if isinstance(raw_compulsory, str):
            compulsory = _unique_clean_strings((raw_compulsory,))
        elif isinstance(raw_compulsory, Sequence) and not isinstance(
            raw_compulsory, (str, bytes)
        ):
            compulsory = _unique_clean_strings(raw_compulsory)
        else:
            compulsory = ()

        launchable_desktop_ids: tuple[str, ...] = ()
        raw_launchable = doc.get("Launchable")
        if isinstance(raw_launchable, Mapping):
            raw_desktop_ids = raw_launchable.get("desktop-id")
            if isinstance(raw_desktop_ids, Sequence) and not isinstance(
                raw_desktop_ids, (str, bytes)
            ):
                launchable_desktop_ids = _unique_clean_strings(raw_desktop_ids)
            elif isinstance(raw_desktop_ids, str):
                launchable_desktop_ids = _unique_clean_strings((raw_desktop_ids,))

        provided = self._yaml_provided_values(doc.get("Provides"))
        record = AppStreamComponent(
            component_id=component_id,
            component_type=component_type,
            pkgnames=package_names,
            source_pkgname=source_pkgname,
            categories=categories,
            launchable_desktop_ids=launchable_desktop_ids,
            compulsory_for_desktops=compulsory,
            **provided,
        )
        self._register_component(record, merge=merge_kind)

    @staticmethod
    def _yaml_provided_values(raw_provides: Any) -> dict[str, tuple[str, ...]]:
        if not isinstance(raw_provides, Mapping):
            return {}
        buckets: dict[str, list[str]] = {}
        simple_lists = {
            "binaries": "provided_binaries",
            "libraries": "provided_libraries",
            "mediatypes": "provided_mediatypes",
            "mimetypes": "provided_mediatypes",
            "modaliases": "provided_modaliases",
            "python3": "provided_python3",
            "ids": "provided_ids",
        }
        for key, field_name in simple_lists.items():
            values = raw_provides.get(key)
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                buckets.setdefault(field_name, []).extend(
                    str(item) for item in values if item is not None
                )

        raw_fonts = raw_provides.get("fonts")
        if isinstance(raw_fonts, Sequence) and not isinstance(raw_fonts, (str, bytes)):
            for entry in raw_fonts:
                if isinstance(entry, Mapping) and entry.get("name"):
                    buckets.setdefault("provided_fonts", []).append(str(entry["name"]))
                elif isinstance(entry, str):
                    buckets.setdefault("provided_fonts", []).append(entry)

        raw_dbus = raw_provides.get("dbus")
        if isinstance(raw_dbus, Sequence) and not isinstance(raw_dbus, (str, bytes)):
            for entry in raw_dbus:
                if not isinstance(entry, Mapping):
                    continue
                service = str(entry.get("service") or "").strip()
                if not service:
                    continue
                dbus_type = str(entry.get("type") or "user").strip().lower()
                field_name = (
                    "provided_dbus_system"
                    if dbus_type == "system"
                    else "provided_dbus_user"
                )
                buckets.setdefault(field_name, []).append(service)

        raw_firmware = raw_provides.get("firmware")
        if isinstance(raw_firmware, Sequence) and not isinstance(
            raw_firmware, (str, bytes)
        ):
            for entry in raw_firmware:
                if not isinstance(entry, Mapping):
                    continue
                fw_type = str(entry.get("type") or "runtime").strip().lower()
                if fw_type == "flashed":
                    value = str(entry.get("guid") or "").strip()
                    if value:
                        buckets.setdefault("provided_firmware_flashed", []).append(
                            value
                        )
                else:
                    value = str(entry.get("file") or "").strip()
                    if value:
                        buckets.setdefault("provided_firmware_runtime", []).append(
                            value
                        )

        return {key: _unique_clean_strings(values) for key, values in buckets.items()}

    def _register_component(
        self, component: AppStreamComponent, *, merge: str = ""
    ) -> None:
        if merge == "remove-component":
            self._remove_component_by_id(component.component_id)
            return
        if merge == "replace" and component.component_id:
            self._remove_component_by_id(component.component_id)

        cid = component.component_id.lower()
        if cid:
            self.component_id_components.setdefault(cid, []).append(component)
        if component.metainfo_filename:
            self.metainfo_components[component.metainfo_filename.lower()] = component
        if component.source_pkgname:
            self.source_pkg_components.setdefault(
                component.source_pkgname.lower(), []
            ).append(component)

        desktop_keys = list(component.launchable_desktop_ids)
        if (
            not desktop_keys
            and component.component_type in {"desktop-application", "web-application"}
            and cid.endswith(".desktop")
        ):
            desktop_keys.append(component.component_id)
        for desktop_id in desktop_keys:
            normalized_id = desktop_id.strip().lower()
            if normalized_id:
                self.desktop_id_components.setdefault(normalized_id, []).append(
                    component
                )

        type_attribute = self._TYPE_TO_ATTRIBUTE.get(component.component_type)
        for package_name in component.pkgnames:
            self.pkg_components.setdefault(package_name, []).append(component)
            if type_attribute is not None:
                getattr(self, type_attribute).add(package_name)
            if component.component_type == "web-application":
                self.web_app_packages.add(package_name)
            if component.categories:
                self.pkg_xdg_categories.setdefault(package_name, set()).update(
                    component.categories
                )
            if component.compulsory_for_desktops:
                self.pkg_compulsory_desktops.setdefault(package_name, set()).update(
                    component.compulsory_for_desktops
                )
            if component.source_pkgname:
                self.pkg_source_packages[package_name] = component.source_pkgname

    def _remove_component_by_id(self, component_id: str) -> None:
        target_id = component_id.strip().lower()
        if not target_id:
            return
        removed = self.component_id_components.pop(target_id, [])
        if not removed:
            return
        removed_ids = {id(item) for item in removed}
        affected_packages: set[str] = set()
        for item in removed:
            affected_packages.update(item.pkgnames)
            if item.metainfo_filename:
                existing = self.metainfo_components.get(item.metainfo_filename.lower())
                if existing is not None and id(existing) in removed_ids:
                    self.metainfo_components.pop(item.metainfo_filename.lower(), None)

        for desktop_key in list(self.desktop_id_components):
            kept = [
                item
                for item in self.desktop_id_components[desktop_key]
                if id(item) not in removed_ids
            ]
            if kept:
                self.desktop_id_components[desktop_key] = kept
            else:
                self.desktop_id_components.pop(desktop_key, None)

        for source_key in list(self.source_pkg_components):
            kept = [
                item
                for item in self.source_pkg_components[source_key]
                if id(item) not in removed_ids
            ]
            if kept:
                self.source_pkg_components[source_key] = kept
            else:
                self.source_pkg_components.pop(source_key, None)

        for pkg in affected_packages:
            kept = [
                item
                for item in self.pkg_components.get(pkg, ())
                if id(item) not in removed_ids
            ]
            if kept:
                self.pkg_components[pkg] = kept
            else:
                self.pkg_components.pop(pkg, None)
            self._recompute_package_indexes(pkg, kept)

    def _recompute_package_indexes(
        self, package_name: str, components: Sequence[AppStreamComponent]
    ) -> None:
        for attr in self._ALL_PACKAGE_TYPE_ATTRS:
            getattr(self, attr).discard(package_name)
        self.pkg_xdg_categories.pop(package_name, None)
        self.pkg_compulsory_desktops.pop(package_name, None)
        self.pkg_source_packages.pop(package_name, None)
        for item in components:
            type_attribute = self._TYPE_TO_ATTRIBUTE.get(item.component_type)
            if type_attribute is not None:
                getattr(self, type_attribute).add(package_name)
            if item.component_type == "web-application":
                self.web_app_packages.add(package_name)
            if item.categories:
                self.pkg_xdg_categories.setdefault(package_name, set()).update(
                    item.categories
                )
            if item.compulsory_for_desktops:
                self.pkg_compulsory_desktops.setdefault(package_name, set()).update(
                    item.compulsory_for_desktops
                )
            if item.source_pkgname:
                self.pkg_source_packages[package_name] = item.source_pkgname

    def component_for_metainfo(
        self, metainfo_filename: str
    ) -> AppStreamComponent | None:
        """Return a MetaInfo component matched by filename or derived ID."""
        basename = os.path.basename(metainfo_filename.strip()).lower()
        if not basename:
            return None
        direct = self.metainfo_components.get(basename)
        if direct is not None:
            return direct
        candidate_id = basename
        for suffix in (".metainfo.xml", ".appdata.xml"):
            if candidate_id.endswith(suffix):
                candidate_id = candidate_id[: -len(suffix)]
                break
        matches = self.component_id_components.get(candidate_id)
        if matches:
            return matches[0]
        if not candidate_id.endswith(".desktop"):
            desktop_matches = self.component_id_components.get(
                f"{candidate_id}.desktop"
            )
            if desktop_matches:
                return desktop_matches[0]
        return None

    def components_for_package(
        self,
        package_name: str,
        *,
        metainfo_files: Iterable[str] = (),
        desktop_ids: Iterable[str] = (),
    ) -> tuple[AppStreamComponent, ...]:
        """Resolve all AppStream components owned by ``package_name``.

        Explicit catalog ``pkgnames`` joins match directly.  MetaInfo filename
        and ``<launchable type="desktop-id">`` joins additionally enforce
        ``not comp.pkgnames or pkg in comp.pkgnames`` so an AppStream catalog
        record belonging to another package cannot attach by ID overlap alone.
        """
        pkg = package_name.strip().lower()
        if not pkg:
            return ()
        resolved: list[AppStreamComponent] = []
        seen: set[int] = set()

        def _add_if_new(comp: AppStreamComponent) -> None:
            obj_id = id(comp)
            if obj_id not in seen:
                seen.add(obj_id)
                resolved.append(comp)

        for comp in self.pkg_components.get(pkg, ()):
            _add_if_new(comp)

        for raw_metainfo in metainfo_files:
            comp = self.component_for_metainfo(raw_metainfo)
            if comp is not None and (not comp.pkgnames or pkg in comp.pkgnames):
                _add_if_new(comp)

        for raw_desktop_id in desktop_ids:
            did = raw_desktop_id.strip().lower()
            if not did:
                continue
            candidates = list(self.desktop_id_components.get(did, ()))
            candidates.extend(self.component_id_components.get(did, ()))
            if did.endswith(".desktop"):
                candidates.extend(
                    self.component_id_components.get(did.removesuffix(".desktop"), ())
                )
            for comp in candidates:
                if not comp.pkgnames or pkg in comp.pkgnames:
                    _add_if_new(comp)

        return tuple(resolved)

    def compulsory_desktops_for_package(
        self,
        package_name: str,
        *,
        metainfo_files: Iterable[str] = (),
        desktop_ids: Iterable[str] = (),
    ) -> tuple[str, ...]:
        """Return sorted desktop environments marking this package compulsory."""
        pkg = package_name.strip().lower()
        desktops = set(self.pkg_compulsory_desktops.get(pkg, set()))
        for comp in self.components_for_package(
            pkg, metainfo_files=metainfo_files, desktop_ids=desktop_ids
        ):
            desktops.update(comp.compulsory_for_desktops)
        return tuple(sorted(desktops))

    def _index_component(self, component: Any) -> None:
        """Backward-compatible helper delegating to ``_index_xml_component``."""
        self._index_xml_component(component)


@dataclass(slots=True)
class PackagePhysicalAnatomy:
    """Normalized package-manifest capabilities used by classification rules."""

    has_binaries: bool = False
    has_user_bin: bool = False
    has_admin_sbin: bool = False
    has_libexec: bool = False
    has_desktop_file: bool = False
    has_systemd_system: bool = False
    has_systemd_user: bool = False
    has_c_headers: bool = False
    has_fonts_dir: bool = False
    has_themes_dir: bool = False
    has_docs_dir: bool = False
    has_firmware_dir: bool = False
    has_kernel_modules_dir: bool = False
    has_dri_dir: bool = False
    has_xorg_driver_dir: bool = False
    has_media_plugins_dir: bool = False
    has_gui_toolkit_dir: bool = False
    has_kio_dir: bool = False
    has_shell_or_session_dir: bool = False
    has_repository_config_dir: bool = False
    has_metainfo_dir: bool = False
    has_system_data_dir: bool = False
    has_reference_data_dir: bool = False
    has_development_data_dir: bool = False
    has_jvm_dir: bool = False
    has_jar_files: bool = False
    has_locales_dir: bool = False
    has_shared_libs_dir: bool = False
    has_man1: bool = False
    has_man8: bool = False
    has_man3: bool = False
    has_python_runtime: bool = False

    # Declarative language/platform evidence.  These IDs retain every positive
    # source so the classifier can use one as a primary role and expose the
    # others as searchable facets without reparsing the RPM payload.
    ecosystem_capability_ids: set[str] = field(default_factory=set)
    ecosystem_path_ids: set[str] = field(default_factory=set)

    exported_sonames: list[str] = field(default_factory=list)
    provides_pkgconfig: bool = False
    provides_font: bool = False
    provides_kmod: bool = False
    provides_appstream: bool = False
    provides_wm: bool = False
    provides_gstreamer: bool = False
    provides_python_dist: bool = False
    provides_rust_crate: bool = False
    provides_jvm_artifact: bool = False
    provides_nodejs_pkg: bool = False
    provided_desktop_ids: set[str] = field(default_factory=set)
    provided_metainfo_files: set[str] = field(default_factory=set)

    @property
    def has_plugins_dir(self) -> bool:
        return self.has_media_plugins_dir or self.has_gui_toolkit_dir

    @property
    def ecosystem_ids(self) -> frozenset[str]:
        """Return every ecosystem affirmatively seen in manifest metadata."""
        return frozenset(self.ecosystem_capability_ids | self.ecosystem_path_ids)

    @classmethod
    def from_manifest_data(
        cls,
        dirnames: Sequence[str],
        provides: Sequence[str],
        basenames: Sequence[str] | None = None,
    ) -> PackagePhysicalAnatomy:
        anatomy = cls()
        for directory in dirnames:
            anatomy._inspect_directory(directory)
        anatomy._inspect_basenames(basenames or ())
        anatomy._inspect_provides(provides)
        return anatomy

    def _inspect_directory(self, directory: str) -> None:
        path = directory.strip().rstrip("/")
        if not path:
            return
        self.ecosystem_path_ids.update(ecosystem_ids_for_directory(path))
        for handler in (
            self._inspect_executable_directory,
            self._inspect_system_directory,
            self._inspect_asset_directory,
            self._inspect_hardware_or_plugin_directory,
            self._inspect_runtime_directory,
        ):
            if handler(path):
                return

    def _inspect_executable_directory(self, path: str) -> bool:
        if path in {"/usr/bin", "/bin"}:
            self.has_binaries = True
            self.has_user_bin = True
            return True
        if path in {"/usr/sbin", "/sbin"}:
            self.has_binaries = True
            self.has_admin_sbin = True
            return True
        if path == "/usr/libexec" or path.startswith("/usr/libexec/"):
            self.has_libexec = True
            return True
        return False

    def _inspect_system_directory(self, path: str) -> bool:
        if path.startswith(
            ("/usr/share/applications", "/usr/local/share/applications")
        ):
            self.has_desktop_file = True
            return True
        if path.startswith(("/usr/lib/systemd/system", "/lib/systemd/system")):
            self.has_systemd_system = True
            return True
        if path.startswith("/usr/lib/systemd/user"):
            self.has_systemd_user = True
            return True
        if path == "/usr/include" or path.startswith("/usr/include/"):
            self.has_c_headers = True
            return True
        return False

    def _inspect_asset_directory(self, path: str) -> bool:
        normalized = path.casefold()
        if normalized == "/usr/share/fonts" or normalized.startswith(
            ("/usr/share/fonts/", "/usr/share/x11/fonts/")
        ):
            self.has_fonts_dir = True
            return True
        if normalized.startswith(
            (
                "/usr/share/themes",
                "/usr/share/backgrounds",
                "/usr/share/wallpapers",
                "/usr/share/plasma/desktoptheme",
                "/usr/share/plasma/look-and-feel",
            )
        ):
            self.has_themes_dir = True
            return True
        if path.startswith("/usr/share/icons/"):
            # GUI applications commonly install hicolor icons; only independent
            # icon themes qualify as a theme package.
            pieces = path.split("/")
            if len(pieces) >= 5 and pieces[4] != "hicolor":
                self.has_themes_dir = True
            return True
        if normalized.startswith(REFERENCE_DATA_DIRS):
            self.has_reference_data_dir = True
            return True
        if _matches_directory_prefix(normalized, REPOSITORY_CONFIGURATION_DIRS):
            self.has_repository_config_dir = True
            return True
        if _matches_directory_prefix(normalized, METAINFO_DATA_DIRS):
            self.has_metainfo_dir = True
            return True
        if normalized.startswith(SYSTEM_DATA_DIRS):
            self.has_system_data_dir = True
            return True
        if path == "/usr/share/doc" or path.startswith(
            ("/usr/share/doc/", "/usr/share/help/")
        ):
            self.has_docs_dir = True
            return True
        # RPM metadata may retain the legacy /lib path even on UsrMerge
        # systems where it resolves to /usr/lib. Both forms identify firmware.
        if path in {"/usr/lib/firmware", "/lib/firmware"} or path.startswith(
            ("/usr/lib/firmware/", "/lib/firmware/")
        ):
            self.has_firmware_dir = True
            return True
        return False

    def _inspect_hardware_or_plugin_directory(self, path: str) -> bool:
        normalized = path.casefold()
        if path.startswith(("/usr/lib/modules/", "/lib/modules/")):
            self.has_kernel_modules_dir = True
            return True
        if normalized.startswith(XORG_DRIVER_DIRS):
            self.has_xorg_driver_dir = True
            return True
        if path.startswith(
            (
                "/usr/lib64/dri",
                "/usr/lib/dri",
                "/usr/share/vulkan/icd.d",
                "/etc/vulkan/icd.d",
            )
        ):
            self.has_dri_dir = True
            return True
        if any(keyword in path for keyword in MEDIA_PLUGIN_DIR_KEYWORDS):
            self.has_media_plugins_dir = True
            return True
        if any(keyword in path for keyword in GUI_TOOLKIT_DIR_KEYWORDS):
            self.has_gui_toolkit_dir = True
            return True
        if "/kio" in path:
            self.has_kio_dir = True
            return True
        if path.startswith(DESKTOP_SESSION_DIRS):
            self.has_shell_or_session_dir = True
            return True
        return False

    def _inspect_runtime_directory(self, path: str) -> bool:
        normalized = path.casefold()
        if normalized.startswith(DEVELOPMENT_DATA_DIRS):
            self.has_development_data_dir = True
            return True
        if path.startswith(("/usr/share/java", "/usr/lib/jvm")):
            self.has_jvm_dir = True
            return True
        if path == "/usr/share/locale" or path.startswith("/usr/share/locale/"):
            self.has_locales_dir = True
            return True
        if path in {"/usr/lib64", "/usr/lib"}:
            self.has_shared_libs_dir = True
            return True
        if path.endswith("/man/man1") or "/man/man1/" in path:
            self.has_man1 = True
            return True
        if path.endswith("/man/man8") or "/man/man8/" in path:
            self.has_man8 = True
            return True
        if path.endswith("/man/man3") or "/man/man3/" in path:
            self.has_man3 = True
            return True
        if "/python3" in path and "site-packages" in path:
            self.has_python_runtime = True
            return True
        return False

    def _inspect_basenames(self, basenames: Iterable[str]) -> None:
        has_jar = False
        for name in basenames:
            if name.endswith(".jar"):
                has_jar = True
            elif self.has_metainfo_dir and name.lower().endswith(
                (".metainfo.xml", ".appdata.xml")
            ):
                self.provided_metainfo_files.add(name.lower())
        self.has_jar_files = has_jar

    def _inspect_provides(self, provides: Iterable[str]) -> None:
        for raw_provide in provides:
            provide = raw_provide.strip()
            if not provide:
                continue
            self.ecosystem_capability_ids.update(ecosystem_ids_for_capability(provide))
            if ".so" in provide and "(" in provide:
                self.exported_sonames.append(provide)
            elif provide.startswith("pkgconfig("):
                self.provides_pkgconfig = True
            elif provide.startswith("font("):
                self.provides_font = True
            elif provide.startswith("kmod("):
                self.provides_kmod = True
            elif provide.startswith(("appdata(", "metainfo(")):
                self.provides_appstream = True
                if provide.endswith(")"):
                    inner = provide.split("(", 1)[1][:-1].strip()
                    if inner:
                        self.provided_metainfo_files.add(
                            os.path.basename(inner).lower()
                        )
            elif "windowmanager" in provide.lower():
                self.provides_wm = True
            elif provide.startswith("gstreamer1("):
                self.provides_gstreamer = True
            elif provide.startswith("python3dist("):
                self.provides_python_dist = True
            elif provide.startswith("crate("):
                self.provides_rust_crate = True
            elif provide.startswith(("mvn(", "osgi(")):
                self.provides_jvm_artifact = True
            elif provide.startswith("npm("):
                self.provides_nodejs_pkg = True
            elif provide.startswith("application(") and provide.endswith(")"):
                desktop_id = provide[12:-1].strip()
                if desktop_id:
                    self.provided_desktop_ids.add(desktop_id)
                    self.has_desktop_file = True

    @classmethod
    def from_rpm_header(cls, header: Any) -> PackagePhysicalAnatomy:
        """Build anatomy from an RPM header without coupling to backend state."""
        if header is None:
            return cls()
        try:
            import rpm  # type: ignore[import-not-found, import-untyped]
        except ImportError:
            return cls()
        try:

            def decode(value: Any) -> str:
                return (
                    value.decode("utf-8", errors="replace")
                    if isinstance(value, bytes)
                    else str(value or "")
                )

            raw_dirs = [decode(value) for value in (header[rpm.RPMTAG_DIRNAMES] or [])]
            raw_provides = [
                decode(value) for value in (header[rpm.RPMTAG_PROVIDENAME] or [])
            ]
            raw_basenames = [
                decode(value) for value in (header[rpm.RPMTAG_BASENAMES] or [])
            ]
            anatomy = cls.from_manifest_data(raw_dirs, raw_provides, raw_basenames)
            directory_indexes = header[rpm.RPMTAG_DIRINDEXES] or []
            real_desktop_entry = False
            if directory_indexes and len(directory_indexes) == len(raw_basenames):
                exact_metainfo = {
                    os.path.basename(p.strip().split("(", 1)[1][:-1].strip()).lower()
                    for p in raw_provides
                    if p.strip().startswith(("appdata(", "metainfo("))
                    and p.strip().endswith(")")
                    and p.strip().split("(", 1)[1][:-1].strip()
                }
                for basename, directory_index in zip(
                    raw_basenames, directory_indexes, strict=True
                ):
                    if not 0 <= directory_index < len(raw_dirs):
                        continue
                    directory = raw_dirs[directory_index].rstrip("/")
                    if basename.endswith(".desktop") and directory.startswith(
                        ("/usr/share/applications", "/usr/local/share/applications")
                    ):
                        real_desktop_entry = True
                        anatomy.provided_desktop_ids.add(basename)
                    elif basename.lower().endswith(
                        (".metainfo.xml", ".appdata.xml")
                    ) and _matches_directory_prefix(
                        directory.casefold(), METAINFO_DATA_DIRS
                    ):
                        exact_metainfo.add(basename.lower())
                anatomy.provided_metainfo_files = exact_metainfo
            else:
                real_desktop_entry = any(
                    name.endswith(".desktop") for name in raw_basenames
                )
            if not real_desktop_entry and not anatomy.provided_desktop_ids:
                anatomy.has_desktop_file = False
            return anatomy
        except Exception:
            LOGGER.warning(
                "Could not inspect an RPM header's file manifest.", exc_info=True
            )
            return cls()


class DesktopEntryMetadata:
    """Minimal, defensive parser for installed XDG desktop-entry metadata."""

    __slots__ = (
        "categories",
        "desktop_id",
        "exec_cmd",
        "is_auxiliary",
        "is_im",
        "is_nodisplay",
        "is_settings",
        "is_terminal",
        "is_wm",
        "name",
    )

    def __init__(self, file_path: str) -> None:
        self.desktop_id = os.path.basename(file_path)
        self.name = ""
        self.exec_cmd = ""
        self.categories: set[str] = set()
        self.is_nodisplay = False
        self.is_terminal = False
        self.is_wm = False
        self.is_im = False
        self.is_settings = False
        self.is_auxiliary = False
        self._parse(file_path)

    def _parse(self, file_path: str) -> None:
        normalized_path = file_path.replace("\\", "/")
        if any(
            segment in normalized_path
            for segment in ("/autostart/", "/kservices/", "/akonadi/")
        ):
            self.is_auxiliary = True
        try:
            with open(file_path, encoding="utf-8", errors="replace") as stream:
                in_entry = False
                for raw_line in stream:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("["):
                        in_entry = line == "[Desktop Entry]"
                        continue
                    if in_entry and "=" in line:
                        key, value = (part.strip() for part in line.split("=", 1))
                        self._consume_field(key, value)
            self._finalize_categories()
        except (OSError, UnicodeError, ValueError):
            self.is_nodisplay = True
            LOGGER.warning(
                "Could not parse desktop entry %s; treating it as non-displayable.",
                file_path,
                exc_info=True,
            )

    def _consume_field(self, key: str, value: str) -> None:
        if (key == "Type" and value != "Application") or (
            key in {"NoDisplay", "Hidden"} and value.lower() == "true"
        ):
            self.is_nodisplay = True
        elif key == "Terminal" and value.lower() == "true":
            self.is_terminal = True
        elif key == "Exec" and not self.exec_cmd:
            command = value.split(maxsplit=1)[0] if value.split() else ""
            self.exec_cmd = os.path.basename(command.strip("\"' ")).lower()
            if not self.exec_cmd:
                self.is_nodisplay = True
        elif key == "Categories":
            self.categories = {
                value.strip().lower() for value in value.split(";") if value.strip()
            }
        elif key in {"X-GNOME-Provides", "X-GNOME-Autostart-Phase"}:
            normalized = value.lower()
            self.is_wm = self.is_wm or "windowmanager" in normalized
            self.is_im = self.is_im or "inputmethod" in normalized

    def _finalize_categories(self) -> None:
        self.is_wm = self.is_wm or bool(
            self.categories.intersection({"windowmanager", "windowmaker"})
        )
        self.is_im = self.is_im or bool(
            self.categories.intersection({"inputmethod", "x-inputmethod"})
        )
        self.is_settings = bool(
            self.categories.intersection(
                {
                    "settings",
                    "desktopsettings",
                    "hardwaresettings",
                    "packagemanager",
                    "preferences",
                    "x-kde-settings",
                    "x-gnome-settings-panel",
                    "x-unity-settings-panel",
                }
            )
        )
        self.is_auxiliary = self.is_auxiliary or bool(
            self.categories.intersection(
                {"screensaver", "trayicon", "applet", "statusicon", "shell"}
            )
        )


class SystemDesktopIndex:
    """Lazy map of installed system desktop entries by case-insensitive ID."""

    _instance: SystemDesktopIndex | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.desktop_map: dict[str, DesktopEntryMetadata] = {}
        self._load_all_desktop_files()

    @classmethod
    def get_instance(cls) -> SystemDesktopIndex:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _load_all_desktop_files(self) -> None:
        for search_dir in ("/usr/share/applications", "/usr/local/share/applications"):
            if not os.path.isdir(search_dir):
                continue
            for root, _directories, filenames in os.walk(search_dir):
                for filename in filenames:
                    if filename.endswith(".desktop"):
                        entry = DesktopEntryMetadata(os.path.join(root, filename))
                        self.desktop_map[filename] = entry
                        self.desktop_map[filename.lower()] = entry
