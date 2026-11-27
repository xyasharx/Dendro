"""Safe, focused package metadata readers for Dendro taxonomy.

The module owns only metadata acquisition and normalization: compressed XML,
Fedora Comps, AppStream, RPM manifest anatomy, and installed desktop entries.
It deliberately does not decide a package category; that belongs to
``core.package_classifier``.
"""

from __future__ import annotations

import bz2
import configparser
import glob
import gzip
import hashlib
import io
import logging
import lzma
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Final

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

try:
    import yaml as _pyyaml  # type: ignore[import-not-found, import-untyped]
except ImportError:
    _pyyaml = None

from dendro.core.ecosystems import (
    bundled_ecosystem_ids_for_capability,
    ecosystem_ids_for_capability,
    ecosystem_ids_for_directory,
    is_python_dist_capability,
)
from dendro.core.process_utils import is_running_in_flatpak

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
    # Optional PEP 784 backport for Python 3.12/3.13 environments.
    import backports.zstd as _backports_zstd  # type: ignore[import-not-found]
except ImportError:
    _backports_zstd = None

try:
    # Keep Python 3.12/3.13 installations capable when the optional package is
    # available, without making a native extension mandatory for every install.
    import zstandard as _third_party_zstd  # type: ignore[import-not-found]
except ImportError:
    _third_party_zstd = None

_ZSTD_ERROR_TYPES = tuple(
    error
    for codec in (_stdlib_zstd, _backports_zstd, _third_party_zstd)
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
DEFAULT_XDG_DATA_DIRS: Final[tuple[str, ...]] = (
    "/usr/local/share",
    "/usr/share",
)
_SYSTEM_APPLICATIONS_DIRS: Final[tuple[str, ...]] = tuple(
    f"{data_dir}/applications" for data_dir in DEFAULT_XDG_DATA_DIRS
)


def resolve_xdg_data_dirs() -> tuple[str, ...]:
    """Return ordered absolute XDG data directories per XDG Base Directory Spec."""
    raw = os.environ.get("XDG_DATA_DIRS", "")
    resolved: list[str] = []
    seen: set[str] = set()
    for entry in raw.split(os.pathsep):
        cleaned = entry.strip()
        if not cleaned or not posixpath.isabs(cleaned):
            continue
        normalized = posixpath.normpath(cleaned)
        if normalized not in seen:
            seen.add(normalized)
            resolved.append(normalized)
    return tuple(resolved) if resolved else DEFAULT_XDG_DATA_DIRS


def desktop_file_id_from_path(
    file_path: str, data_dirs: Sequence[str] | None = None
) -> str | None:
    """Return the XDG Desktop File ID for ``file_path`` inside ``applications/``.

    Per FreeDesktop Desktop Entry Specification 1.5, a desktop file's ID is its
    path relative to ``<data_dir>/applications/`` with ``/`` replaced by ``-``.
    Files outside any ``applications/`` directory (such as autostart, kservices,
    akonadi, or documentation samples) have no Desktop File ID and return None.
    """
    raw_clean = file_path.strip().replace("\\", "/")
    if not raw_clean:
        return None
    clean_path = posixpath.normpath(raw_clean)
    if not clean_path.endswith(".desktop"):
        return None

    if data_dirs is None:
        candidate_dirs: list[str] = list(resolve_xdg_data_dirs())
        for default_dir in DEFAULT_XDG_DATA_DIRS:
            if default_dir not in candidate_dirs:
                candidate_dirs.append(default_dir)
    else:
        candidate_dirs = [
            posixpath.normpath(d.strip().replace("\\", "/"))
            for d in data_dirs
            if d and d.strip()
        ]

    for data_dir in candidate_dirs:
        apps_root = "/applications" if data_dir == "/" else f"{data_dir}/applications"
        prefix = f"{apps_root}/"
        if not clean_path.startswith(prefix):
            continue
        relative = clean_path[len(prefix) :]
        if not relative or relative == ".desktop" or relative.endswith("/.desktop"):
            return None
        return relative.replace("/", "-")
    return None


def _unescape_desktop_string(raw: str) -> str:
    """Decode Desktop Entry ``string`` escapes (\\s, \\n, \\t, \\r, \\\\)."""
    if "\\" not in raw:
        return raw
    result: list[str] = []
    escape_map = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if char == "\\" and index + 1 < length:
            nxt = raw[index + 1]
            if nxt in escape_map:
                result.append(escape_map[nxt])
            else:
                result.append("\\" + nxt)
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _parse_desktop_string_list(raw: str) -> tuple[str, ...]:
    """Split a semicolon-delimited Desktop Entry list preserving ``\\;`` escapes."""
    if not raw:
        return ()
    items: list[str] = []
    current: list[str] = []
    escape_map = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\", ";": ";"}
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if char == "\\" and index + 1 < length:
            nxt = raw[index + 1]
            if nxt in escape_map:
                current.append(escape_map[nxt])
            else:
                current.append("\\" + nxt)
            index += 2
            continue
        if char == ";":
            token = "".join(current).strip()
            if token:
                items.append(token)
            current.clear()
            index += 1
            continue
        current.append(char)
        index += 1
    trailing = "".join(current).strip()
    if trailing:
        items.append(trailing)
    return tuple(items)


def _is_try_exec_available(try_exec: str) -> bool:
    """Return True if ``TryExec`` is empty or resolves to an executable file."""
    candidate = try_exec.strip()
    if not candidate:
        return True
    resolved = shutil.which(candidate)
    if resolved is None:
        return False
    try:
        file_stat = os.stat(resolved)
    except OSError:
        return False
    return stat.S_ISREG(file_stat.st_mode) and bool(file_stat.st_mode & 0o111)


def _is_shown_in_desktop(
    only_show_in: Sequence[str],
    not_show_in: Sequence[str],
    current_desktop: str | Sequence[str] | None = None,
) -> bool:
    """Evaluate ``OnlyShowIn`` / ``NotShowIn`` against ``$XDG_CURRENT_DESKTOP``."""
    if current_desktop is None:
        raw_desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
        desktops = tuple(
            item.strip() for item in raw_desktop.split(":") if item.strip()
        )
    elif isinstance(current_desktop, str):
        desktops = tuple(
            item.strip() for item in current_desktop.split(":") if item.strip()
        )
    else:
        desktops = tuple(
            item.strip() for item in current_desktop if item and item.strip()
        )

    only_set = set(only_show_in)
    not_set = set(not_show_in)
    for desktop in desktops:
        if desktop in not_set:
            return False
        if desktop in only_set:
            return True
    return not bool(only_set)


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
    "/usr/lib/rpm/macros.d",
    "/usr/lib/rpm/lua",
)
_NON_ICON_THEME_DIRS: Final[tuple[str, ...]] = (
    "/usr/share/themes",
    "/usr/share/backgrounds",
    "/usr/share/wallpapers",
    "/usr/share/plasma/desktoptheme",
    "/usr/share/plasma/look-and-feel",
)
_SHARED_ICON_THEME_NAMES: Final[frozenset[str]] = frozenset(
    {"hicolor", "breeze", "breeze-dark", "adwaita", "oxygen", "gnome", "locolor"}
)
_PYTHON_RUNTIME_DIR_RE: Final[re.Pattern[str]] = re.compile(
    r"^/(?:usr/)?(?:lib|lib64)/python\d+(?:\.\d+)*/(?:site|dist)-packages(?:/|$)",
    re.IGNORECASE,
)
_ELF_SONAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:lib|ld(?:64)?[.-])[A-Za-z0-9_+.-]*\.so(?:\.\d[0-9A-Za-z_+.-]*)?(?:\([^)]*\))?(?:\((?:64|32)bit\))?$"
)
XORG_DRIVER_DIRS: Final[tuple[str, ...]] = (
    "/usr/lib/xorg/modules/drivers",
    "/usr/lib64/xorg/modules/drivers",
)

_COMPRESSION_SUFFIXES: Final[tuple[str, ...]] = (
    ".zck",
    ".zst",
    ".zstd",
    ".xz",
    ".bz2",
    ".gz",
)
_MAX_DEP11_YAML_BYTES: Final[int] = 64 * 1024 * 1024
_MAX_DECOMPRESSED_METADATA_BYTES: Final[int] = 256 * 1024 * 1024
_MAX_ZCK_FILE_SIZE: Final[int] = 128 * 1024 * 1024
_MAX_ZCK_CHUNKS: Final[int] = 200_000
_ZSTD_FORMATTED_DICT_MAGIC: Final[bytes] = b"\x37\xa4\x30\xec"
_ZCK_MAGIC: Final[bytes] = b"\x00ZCK1"
_ZCK_DETACHED_MAGIC: Final[bytes] = b"\x00ZHR1"
_ZCK_COMP_NONE: Final[int] = 0
_ZCK_COMP_ZSTD: Final[int] = 2
_ZCK_HASH_SPECS: Final[Mapping[int, tuple[str, int]]] = {
    0: ("sha1", 20),
    1: ("sha256", 32),
    2: ("sha512", 64),
    3: ("sha512", 16),
}
_BLOCK_SCALAR_RE: Final[re.Pattern[str]] = re.compile(r"^([|>])([+-]?)([1-9]?)$")


class UnsupportedMetadataCompression(OSError):
    """Raised when an optional local repository-metadata codec is absent."""


@dataclass(frozen=True, slots=True)
class _ZckIndexEntry:
    digest: bytes
    digest_uncompressed: bytes
    comp_length: int
    uncomp_length: int


def _zstd_cli_binary() -> str:
    return shutil.which("zstd") or ""


def _python_zstd_available() -> bool:
    return any((_stdlib_zstd, _backports_zstd, _third_party_zstd))


def _decompress_zstd_cli_file(file_path: str) -> bytes:
    zstd_bin = _zstd_cli_binary()
    if not zstd_bin:
        raise UnsupportedMetadataCompression(
            "Zstandard-compressed repository metadata needs Python 3.14+, "
            "backports.zstd, zstandard, or the zstd CLI."
        )
    proc = subprocess.Popen(
        [zstd_bin, "-d", "-q", "-c", "--", file_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    chunks: list[bytes] = []
    total = 0
    try:
        assert proc.stdout is not None
        while True:
            block = proc.stdout.read(65536)
            if not block:
                break
            total += len(block)
            if total > _MAX_DECOMPRESSED_METADATA_BYTES:
                proc.kill()
                raise ValueError(
                    f"Decompressed zstd metadata {file_path} exceeds size limit"
                )
            chunks.append(block)
        stderr_data = proc.stderr.read(4096) if proc.stderr is not None else b""
        returncode = proc.wait(timeout=30)
    except Exception:
        proc.kill()
        proc.wait()
        raise
    if returncode != 0:
        detail = stderr_data.decode("utf-8", errors="replace").strip()
        raise OSError(
            f"zstd failed to decompress {file_path} (exit {returncode}): {detail}"
        )
    return b"".join(chunks)


def _decompress_zstd_bytes(
    data: bytes,
    *,
    zstd_dict: bytes | None = None,
    max_output_size: int = _MAX_DECOMPRESSED_METADATA_BYTES,
) -> bytes:
    pep784_mod = _stdlib_zstd if _stdlib_zstd is not None else _backports_zstd
    if pep784_mod is not None:
        if zstd_dict is not None:
            is_raw = not zstd_dict.startswith(_ZSTD_FORMATTED_DICT_MAGIC)
            zd = pep784_mod.ZstdDict(zstd_dict, is_raw=is_raw)
            out = pep784_mod.decompress(data, zstd_dict=zd)
        else:
            out = pep784_mod.decompress(data)
        if len(out) > max_output_size:
            raise ValueError("Decompressed zstd chunk exceeds expected limit")
        return out

    if _third_party_zstd is not None:
        if zstd_dict is not None:
            if zstd_dict.startswith(_ZSTD_FORMATTED_DICT_MAGIC):
                dict_type = getattr(_third_party_zstd, "DICT_TYPE_FULLDICT", 0)
            else:
                dict_type = getattr(_third_party_zstd, "DICT_TYPE_RAWCONTENT", 0)
            try:
                dict_obj = _third_party_zstd.ZstdCompressionDict(
                    zstd_dict, dict_type=dict_type
                )
            except TypeError:
                dict_obj = _third_party_zstd.ZstdCompressionDict(zstd_dict)
            decomp = _third_party_zstd.ZstdDecompressor(dict_data=dict_obj)
        else:
            decomp = _third_party_zstd.ZstdDecompressor()

        chunks: list[bytes] = []
        total = 0
        with decomp.stream_reader(io.BytesIO(data)) as reader:
            while True:
                block = reader.read(65536)
                if not block:
                    break
                total += len(block)
                if total > max_output_size:
                    raise ValueError("Decompressed zstd chunk exceeds expected limit")
                chunks.append(block)
        return b"".join(chunks)

    raise UnsupportedMetadataCompression(
        "Zstandard chunk decompression needs Python 3.14+, backports.zstd, "
        "or the optional zstandard package."
    )


def _read_zck_compint(buf: memoryview, offset: int) -> tuple[int, int]:
    """Decode zchunk's 7-bit LEB128 integer where MSB >= 0x80 marks the last byte."""
    val = 0
    pos = offset
    for shift in range(0, 64, 7):
        if pos >= len(buf):
            raise EOFError("Unexpected EOF while reading zchunk compressed integer")
        byte = buf[pos]
        pos += 1
        if byte >= 0x80:
            val += (byte & 0x7F) << shift
            return val, pos
        val += byte << shift
    raise ValueError("Overflow while reading zchunk compressed integer")


def _zck_digest(hash_type: int, data: bytes | memoryview) -> bytes:
    spec = _ZCK_HASH_SPECS.get(hash_type)
    if spec is None:
        raise UnsupportedMetadataCompression(
            f"Unsupported zchunk checksum algorithm {hash_type}"
        )
    algorithm, digest_len = spec
    return hashlib.new(algorithm, data).digest()[:digest_len]


def _decompress_zck_bytes(payload: bytes) -> bytes:
    """Validate and decompress a complete ``.zck`` (zchunk v1) byte payload."""
    if len(payload) > _MAX_ZCK_FILE_SIZE:
        raise ValueError("Zchunk metadata file exceeds maximum allowed size")
    if len(payload) < 5:
        raise EOFError("Truncated zchunk file")
    magic = payload[:5]
    if magic == _ZCK_DETACHED_MAGIC:
        raise UnsupportedMetadataCompression(
            "Detached zchunk headers (.zhr) do not contain repository metadata."
        )
    if magic != _ZCK_MAGIC:
        raise ValueError("Invalid zchunk lead magic bytes")

    view = memoryview(payload)
    pos = 5
    lead_hash_type, pos = _read_zck_compint(view, pos)
    if lead_hash_type not in {0, 1}:
        raise UnsupportedMetadataCompression(
            f"Unsupported zchunk lead hash type {lead_hash_type}"
        )
    lead_digest_len = _ZCK_HASH_SPECS[lead_hash_type][1]
    header_length, pos = _read_zck_compint(view, pos)
    if header_length <= 0 or header_length > _MAX_ZCK_FILE_SIZE:
        raise ValueError(f"Invalid zchunk header length {header_length}")
    hdr_digest_loc = pos
    if pos + lead_digest_len + header_length > len(payload):
        raise EOFError("Truncated zchunk header")
    expected_hdr_digest = payload[pos : pos + lead_digest_len]
    lead_size = pos + lead_digest_len

    hdr_hasher = hashlib.new(_ZCK_HASH_SPECS[lead_hash_type][0])
    hdr_hasher.update(view[:hdr_digest_loc])
    hdr_hasher.update(view[lead_size : lead_size + header_length])
    if hdr_hasher.digest()[:lead_digest_len] != expected_hdr_digest:
        raise ValueError("Zchunk header checksum verification failed")

    header_view = view[lead_size : lead_size + header_length]
    hpos = 0
    if hpos + lead_digest_len > header_length:
        raise EOFError("Truncated zchunk preface digest")
    expected_data_digest = bytes(header_view[hpos : hpos + lead_digest_len])
    hpos += lead_digest_len

    flags, hpos = _read_zck_compint(header_view, hpos)
    if flags & 0x01:
        raise UnsupportedMetadataCompression("Zchunk stream mode is not supported.")
    has_optional_elems = bool(flags & 0x02)
    has_uncompressed_source = bool(flags & 0x04)
    if flags & ~0x07:
        raise UnsupportedMetadataCompression(
            f"Unsupported zchunk header flags: {flags:#x}"
        )

    comp_type, hpos = _read_zck_compint(header_view, hpos)
    if comp_type not in {_ZCK_COMP_NONE, _ZCK_COMP_ZSTD}:
        raise UnsupportedMetadataCompression(
            f"Unsupported zchunk compression type {comp_type}"
        )

    if has_optional_elems:
        opt_count, hpos = _read_zck_compint(header_view, hpos)
        if opt_count > 1024:
            raise ValueError("Too many optional elements in zchunk header")
        for _ in range(opt_count):
            _opt_id, hpos = _read_zck_compint(header_view, hpos)
            opt_size, hpos = _read_zck_compint(header_view, hpos)
            if opt_size > len(header_view) - hpos:
                raise EOFError("Truncated optional element in zchunk header")
            hpos += opt_size

    index_size, hpos = _read_zck_compint(header_view, hpos)
    if index_size > len(header_view) - hpos:
        raise EOFError("Truncated zchunk index")

    idx_view = header_view[hpos : hpos + index_size]
    hpos += index_size
    ipos = 0
    chunk_hash_type, ipos = _read_zck_compint(idx_view, ipos)
    if chunk_hash_type not in _ZCK_HASH_SPECS:
        raise UnsupportedMetadataCompression(
            f"Unsupported zchunk chunk hash type {chunk_hash_type}"
        )
    chunk_digest_len = _ZCK_HASH_SPECS[chunk_hash_type][1]
    chunk_count, ipos = _read_zck_compint(idx_view, ipos)
    if chunk_count == 0 or chunk_count > _MAX_ZCK_CHUNKS:
        raise ValueError(f"Invalid zchunk chunk count {chunk_count}")

    entries: list[_ZckIndexEntry] = []
    total_comp_size = 0
    total_uncomp_size = 0
    for _ in range(chunk_count):
        if ipos + chunk_digest_len > len(idx_view):
            raise EOFError("Truncated zchunk index digest")
        digest = bytes(idx_view[ipos : ipos + chunk_digest_len])
        ipos += chunk_digest_len
        digest_uncomp = b""
        if has_uncompressed_source:
            if ipos + chunk_digest_len > len(idx_view):
                raise EOFError("Truncated zchunk uncompressed digest")
            digest_uncomp = bytes(idx_view[ipos : ipos + chunk_digest_len])
            ipos += chunk_digest_len
        comp_len, ipos = _read_zck_compint(idx_view, ipos)
        uncomp_len, ipos = _read_zck_compint(idx_view, ipos)
        total_comp_size += comp_len
        total_uncomp_size += uncomp_len
        if total_uncomp_size > _MAX_DECOMPRESSED_METADATA_BYTES:
            raise ValueError("Zchunk uncompressed payload exceeds maximum allowed size")
        entries.append(
            _ZckIndexEntry(
                digest=digest,
                digest_uncompressed=digest_uncomp,
                comp_length=comp_len,
                uncomp_length=uncomp_len,
            )
        )
    if ipos != len(idx_view):
        raise ValueError("Trailing bytes in zchunk index")

    sig_count, _ = _read_zck_compint(header_view[hpos:], 0)
    if sig_count > 0:
        raise UnsupportedMetadataCompression(
            "Signed zchunk metadata files are not supported."
        )

    data_offset = lead_size + header_length
    body = view[data_offset:]
    if len(body) < total_comp_size:
        raise EOFError("Truncated zchunk data body")
    if not has_uncompressed_source and (
        _zck_digest(lead_hash_type, body[:total_comp_size]) != expected_data_digest
    ):
        raise ValueError("Zchunk full data checksum verification failed")

    zero_chunk_digest = b"\x00" * chunk_digest_len
    cursor = 0
    zstd_dict: bytes | None = None
    dict_entry = entries[0]
    if dict_entry.comp_length > 0:
        dict_raw = body[cursor : cursor + dict_entry.comp_length]
        cursor += dict_entry.comp_length
        if _zck_digest(chunk_hash_type, dict_raw) != dict_entry.digest:
            raise ValueError("Zchunk dictionary chunk checksum verification failed")
        if comp_type == _ZCK_COMP_NONE:
            dict_bytes = bytes(dict_raw)
        else:
            dict_bytes = _decompress_zstd_bytes(
                bytes(dict_raw),
                max_output_size=dict_entry.uncomp_length
                or _MAX_DECOMPRESSED_METADATA_BYTES,
            )
        if len(dict_bytes) != dict_entry.uncomp_length:
            raise ValueError("Zchunk dictionary decompressed size mismatch")
        zstd_dict = dict_bytes

    out = bytearray()
    for entry in entries[1:]:
        chunk_raw = body[cursor : cursor + entry.comp_length]
        cursor += entry.comp_length
        if has_uncompressed_source and entry.digest == zero_chunk_digest:
            if _zck_digest(chunk_hash_type, chunk_raw) != entry.digest_uncompressed:
                raise ValueError("Zchunk uncompressed chunk checksum mismatch")
            decompressed = bytes(chunk_raw)
        else:
            if _zck_digest(chunk_hash_type, chunk_raw) != entry.digest:
                raise ValueError("Zchunk chunk checksum verification failed")
            if comp_type == _ZCK_COMP_NONE:
                decompressed = bytes(chunk_raw)
            else:
                decompressed = _decompress_zstd_bytes(
                    bytes(chunk_raw),
                    zstd_dict=zstd_dict,
                    max_output_size=entry.uncomp_length
                    or _MAX_DECOMPRESSED_METADATA_BYTES,
                )
        if len(decompressed) != entry.uncomp_length:
            raise ValueError("Zchunk chunk decompressed size mismatch")
        out.extend(decompressed)

    return bytes(out)


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
        if _backports_zstd is not None:
            with _backports_zstd.open(file_path, "rb") as stream:
                yield stream
            return
        if _third_party_zstd is not None:
            with open(file_path, "rb") as compressed_stream:
                decompressor = _third_party_zstd.ZstdDecompressor()
                with decompressor.stream_reader(compressed_stream) as stream:
                    yield stream
            return
        if _zstd_cli_binary():
            yield io.BytesIO(_decompress_zstd_cli_file(file_path))
            return
        raise UnsupportedMetadataCompression(
            "Zstandard-compressed repository metadata needs Python 3.14+, "
            "backports.zstd, zstandard, or the zstd CLI."
        )
    if file_path.endswith(".zck"):
        file_size = os.path.getsize(file_path)
        if file_size > _MAX_ZCK_FILE_SIZE:
            raise ValueError("Zchunk metadata file exceeds maximum allowed size")
        with open(file_path, "rb") as compressed_stream:
            payload = compressed_stream.read(_MAX_ZCK_FILE_SIZE + 1)
        yield io.BytesIO(_decompress_zck_bytes(payload))
        return

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
    """Thread-safe, auxiliary index of Fedora Comps group membership."""

    _instance: FedoraCompsCatalog | None = None
    _lock = threading.Lock()

    # Verified against Fedora 44 comps-f44.xml.in and taxonomy_registry.py.
    # Broad mixed groups such as ``standard`` or desktop-spin metapackages are
    # intentionally omitted so CLI utilities, docs, or themes are not forced
    # into critical system risk.
    COMPS_GROUP_MAP: Final[Mapping[str, str]] = {
        "core": "fedora_core",
        "base": "fedora_core",
        "critical-path-base": "fedora_core",
        "system-tools": "cli_tools",
        "security-lab": "cli_tools",
        "development-tools": "devel",
        "c-development": "devel",
        "rpm-development-tools": "devel",
        "buildsys-build": "devel",
        "critical-path-build": "devel",
        "gnome-software-development": "devel",
        "kde-software-development": "devel",
        "kf6-software-development": "devel",
        "x-software-development": "devel",
        "xfce-software-development": "devel",
        "libreoffice-development": "devel",
        "development-libs": "c_libs",
        "haskell": "haskell_pkgs",
        "ocaml": "ocaml_pkgs",
        "perl": "perl_pkgs",
        "perl-web": "perl_pkgs",
        "php": "php_pkgs",
        "ruby": "ruby_pkgs",
        "rubyonrails": "ruby_pkgs",
        "mingw32": "mingw_pkgs",
        "java": "jvm_pkgs",
        "java-development": "jvm_pkgs",
        "javaenterprise": "jvm_pkgs",
        "base-graphical": "graphics_drivers",
        "base-x": "graphics_drivers",
        "fonts": "fonts",
        "legacy-fonts": "fonts",
        "input-methods": "desktop_addons",
        "guest-agents": "systemd_services",
        "guest-desktop-agents": "systemd_services",
        "networkmanager-submodules": "systemd_services",
        "directory-server": "systemd_services",
        "dns-server": "systemd_services",
        "freeipa-server": "systemd_services",
        "ftp-server": "systemd_services",
        "ha": "systemd_services",
        "haproxy": "systemd_services",
        "load-balancer": "systemd_services",
        "mail-server": "systemd_services",
        "mysql": "systemd_services",
        "network-server": "systemd_services",
        "news-server": "systemd_services",
        "printing": "systemd_services",
        "smb-server": "systemd_services",
        "sql-server": "systemd_services",
        "tomcat": "systemd_services",
        "web-server": "systemd_services",
        "headless-management": "systemd_services",
    }

    _DOMAIN_PRIORITY: Final[Mapping[str, int]] = {
        "fedora_core": 100,
        "security_pkgs": 90,
        "haskell_pkgs": 85,
        "ocaml_pkgs": 85,
        "perl_pkgs": 85,
        "php_pkgs": 85,
        "ruby_pkgs": 85,
        "mingw_pkgs": 85,
        "jvm_pkgs": 85,
        "graphics_drivers": 80,
        "fonts": 80,
        "desktop_addons": 75,
        "cli_tools": 70,
        "systemd_services": 65,
        "c_libs": 60,
        "devel": 50,
    }

    _REPO_CONFIG_PATTERNS: Final[tuple[str, ...]] = (
        "/usr/share/dnf5/repos.d/*.repo",
        "/etc/dnf/repos.d/*.repo",
        "/etc/distro.repos.d/*.repo",
        "/etc/yum.repos.d/*.repo",
        "/usr/share/dnf5/repos.override.d/*.repo",
        "/etc/dnf/repos.override.d/*.repo",
    )

    def __init__(
        self,
        *,
        load_comps: bool = True,
        use_daemon: bool | None = None,
    ) -> None:
        self.package_to_comps: dict[str, str] = {}
        self._package_groups: dict[str, set[str]] = {}
        self._instance_lock = threading.RLock()
        self._daemon_fetch_lock = threading.Lock()
        self._loaded = False
        self._daemon_groups_loaded = False
        self._daemon_groups_unsupported = False
        if load_comps:
            self._load_comps(use_daemon=use_daemon)

    @property
    def daemon_groups_loaded(self) -> bool:
        with self._instance_lock:
            return self._daemon_groups_loaded

    @classmethod
    def get_instance(cls) -> FedoraCompsCatalog:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def _configured_repo_states(cls) -> dict[str, bool]:
        """Return explicit repo enabled states from DNF/DNF5 ``.repo`` files."""
        states: dict[str, bool] = {}
        for pattern in cls._REPO_CONFIG_PATTERNS:
            for repo_file in sorted(glob.glob(pattern, recursive=False)):
                parser = configparser.RawConfigParser()
                try:
                    with open(repo_file, encoding="utf-8", errors="replace") as handle:
                        parser.read_file(handle)
                except (OSError, configparser.Error):
                    continue
                for section in parser.sections():
                    repoid = section.strip()
                    if not repoid or repoid.lower() == "main":
                        continue
                    if parser.has_option(section, "enabled"):
                        try:
                            states[repoid] = parser.getboolean(section, "enabled")
                        except ValueError:
                            raw_val = parser.get(section, "enabled").strip().lower()
                            if raw_val in {"1", "true", "yes", "on"}:
                                states[repoid] = True
                            elif raw_val in {"0", "false", "no", "off"}:
                                states[repoid] = False
        return states

    @staticmethod
    def _repoid_for_cache_dir(cache_dir: str) -> str:
        repoid_file = os.path.join(cache_dir, "attrs", "repoid")
        try:
            with open(repoid_file, encoding="utf-8", errors="replace") as handle:
                value = handle.read().strip()
                if value:
                    return value
        except OSError:
            pass
        return re.sub(r"-[0-9a-fA-F]{8,}$", "", os.path.basename(cache_dir))

    @staticmethod
    def _parse_repomd_group_file(repomd_path: str, cache_dir: str) -> str | None:
        """Select the single active supported group metadata path in ``repomd.xml``."""
        zstd_supported = _zstd_available()
        zck_supported = _python_zstd_available()
        type_rank = {
            "group": 0,
            "group_zst": 1 if zstd_supported else 99,
            "group_xz": 2,
            "group_bz2": 3,
            "group_gz": 4,
            "group_zck": 5 if zck_supported else 99,
        }
        try:
            tree = ET.parse(repomd_path)
        except (OSError, ET.ParseError, DefusedXmlException):
            return None

        root = tree.getroot()
        if root is None:
            return None

        cache_real = os.path.realpath(cache_dir)
        repomd_dir = os.path.dirname(repomd_path)
        candidates: list[tuple[int, str]] = []
        for elem in root:
            if _local_name(elem.tag) != "data":
                continue
            data_type = (elem.attrib.get("type") or "").strip().lower()
            rank = type_rank.get(data_type)
            if rank is None or rank >= 99:
                continue
            loc = _first_child(elem, "location")
            if loc is None:
                continue
            href = (loc.attrib.get("href") or "").strip()
            if not href or os.path.isabs(href):
                continue
            for base_dir in (cache_dir, repomd_dir):
                candidate = os.path.realpath(os.path.join(base_dir, href))
                try:
                    common = os.path.commonpath((cache_real, candidate))
                except ValueError:
                    continue
                if common == cache_real and os.path.isfile(candidate):
                    candidates.append((rank, candidate))
                    break

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][1]

    @staticmethod
    def _deduplicate_comps_paths(paths: Sequence[str]) -> list[str]:
        zstd_supported = _zstd_available()
        zck_supported = _python_zstd_available()
        rank = {
            ".zst": 0 if zstd_supported else 99,
            ".zstd": 0 if zstd_supported else 99,
            ".xz": 1,
            ".bz2": 2,
            ".gz": 3,
            "": 4,
            ".zck": 5 if zck_supported else 99,
        }
        chosen: dict[str, tuple[int, str]] = {}
        for path in sorted(paths):
            base_name, comp_suffix = _strip_compression_suffix(os.path.basename(path))
            file_rank = rank.get(comp_suffix, 50)
            previous = chosen.get(base_name)
            if previous is None or file_rank < previous[0]:
                chosen[base_name] = (file_rank, path)
        return [chosen[key][1] for key in sorted(chosen)]

    @classmethod
    def _discover_local_comps_files(cls) -> list[str]:
        """Discover local ``@System`` and active enabled repo Comps XML files."""
        sysimage_patterns = (
            "/usr/lib/sysimage/libdnf5/comps_groups/groups/*.xml",
            "/usr/lib/sysimage/libdnf5/comps_groups/*.xml",
        )
        sysimage_chosen: dict[str, str] = {}
        for pattern in sysimage_patterns:
            for path in sorted(glob.glob(pattern, recursive=False)):
                stem = os.path.basename(path)
                if stem not in sysimage_chosen:
                    sysimage_chosen[stem] = path

        repomd_patterns = (
            "/var/cache/libdnf5/*/repodata/repomd.xml",
            "/var/cache/dnf/*/repodata/repomd.xml",
            "/var/cache/rpm-ostree/repomd/*/repomd.xml",
            os.path.expanduser("~/.cache/libdnf5/*/repodata/repomd.xml"),
        )
        comps_patterns = (
            "/var/cache/libdnf5/*/repodata/*comps*.xml*",
            "/var/cache/dnf/*/repodata/*comps*.xml*",
            "/var/cache/rpm-ostree/repomd/*/*comps*.xml*",
            os.path.expanduser("~/.cache/libdnf5/*/repodata/*comps*.xml*"),
        )

        repo_states = cls._configured_repo_states()
        repomd_by_dir: dict[str, str] = {}
        comps_by_dir: dict[str, list[str]] = {}

        def _cache_dir_for_metadata(path: str) -> str:
            parent = os.path.dirname(path)
            if os.path.basename(parent) == "repodata":
                return os.path.dirname(parent)
            return parent

        for pattern in repomd_patterns:
            for path in glob.glob(pattern, recursive=False):
                cache_dir = _cache_dir_for_metadata(path)
                if os.path.basename(path) == "repomd.xml":
                    repomd_by_dir.setdefault(cache_dir, path)
                else:
                    comps_by_dir.setdefault(cache_dir, []).append(path)

        for pattern in comps_patterns:
            for path in glob.glob(pattern, recursive=False):
                cache_dir = _cache_dir_for_metadata(path)
                comps_by_dir.setdefault(cache_dir, []).append(path)

        # Keep only the freshest active cache directory per repoid and skip
        # explicitly disabled repositories.
        best_dir_by_repoid: dict[str, tuple[float, int, str]] = {}
        for cache_dir in sorted(set(repomd_by_dir) | set(comps_by_dir)):
            repoid = cls._repoid_for_cache_dir(cache_dir)
            if repo_states.get(repoid) is False:
                continue
            mtime = 0.0
            repomd_path = repomd_by_dir.get(cache_dir)
            if repomd_path is not None:
                try:
                    mtime = os.path.getmtime(repomd_path)
                except OSError:
                    mtime = 0.0
            else:
                for path in comps_by_dir.get(cache_dir, ()):
                    with suppress(OSError):
                        mtime = max(mtime, os.path.getmtime(path))
            is_libdnf5 = 1 if "libdnf5" in cache_dir else 0
            score = (mtime, is_libdnf5, cache_dir)
            existing = best_dir_by_repoid.get(repoid)
            if existing is None or score > existing:
                best_dir_by_repoid[repoid] = score

        selected_files = [sysimage_chosen[key] for key in sorted(sysimage_chosen)]
        for repoid in sorted(best_dir_by_repoid):
            cache_dir = best_dir_by_repoid[repoid][2]
            repomd_path = repomd_by_dir.get(cache_dir)
            if repomd_path is not None and os.path.isfile(repomd_path):
                active = cls._parse_repomd_group_file(repomd_path, cache_dir)
                if active is not None:
                    selected_files.append(active)
                continue
            direct_paths = comps_by_dir.get(cache_dir, ())
            selected_files.extend(cls._deduplicate_comps_paths(direct_paths))

        return selected_files

    def _load_comps(self, *, use_daemon: bool | None = None) -> None:
        if self._loaded:
            return

        files = self._discover_local_comps_files()
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
                ValueError,
                *_ZSTD_ERROR_TYPES,
            ):
                LOGGER.warning(
                    "Could not parse Fedora Comps metadata %s; skipping that file.",
                    file_path,
                    exc_info=True,
                )

        if use_daemon is True or (use_daemon is None and is_running_in_flatpak()):
            self.ensure_daemon_groups(
                scope="all",
                load_system_repo=True,
                load_available_repos=True,
            )
        elif use_daemon is None and not self._package_groups:
            # Query only installed @System groups when local caches are absent so
            # startup never blocks on network metadata refreshes.
            self.ensure_daemon_groups(
                scope="installed",
                load_system_repo=True,
                load_available_repos=False,
            )

        self._loaded = True

    def _record_group_package(self, group_id: str, package_name: str) -> None:
        clean_group = group_id.strip().lower()
        clean_pkg = package_name.strip().lower()
        if not clean_group or not clean_pkg:
            return
        with self._instance_lock:
            self._package_groups.setdefault(clean_pkg, set()).add(clean_group)
            domain = self.COMPS_GROUP_MAP.get(clean_group)
            if not domain:
                return
            existing = self.package_to_comps.get(clean_pkg)
            if existing is None or (
                self._DOMAIN_PRIORITY.get(domain, 0)
                > self._DOMAIN_PRIORITY.get(existing, -1)
            ):
                self.package_to_comps[clean_pkg] = domain

    def merge_daemon_groups(self, groups: Iterable[Any]) -> int:
        """Index ``DnfCompsGroup`` items returned by ``org.rpm.dnf.v0.comps.Group``."""
        count = 0
        with self._instance_lock:
            for group in groups:
                groupid = (
                    group.get("groupid", "")
                    if isinstance(group, Mapping)
                    else getattr(group, "groupid", "")
                )
                clean_group = str(groupid or "").strip().lower()
                if not clean_group:
                    continue
                packages = (
                    group.get("packages", ())
                    if isinstance(group, Mapping)
                    else getattr(group, "packages", ())
                )
                count += 1
                for pkg in packages or ():
                    pkg_name = (
                        pkg
                        if isinstance(pkg, str)
                        else (
                            pkg.get("name", "")
                            if isinstance(pkg, Mapping)
                            else getattr(pkg, "name", "")
                        )
                    )
                    if pkg_name:
                        self._record_group_package(clean_group, str(pkg_name))
            self._daemon_groups_loaded = True
        return count

    def ensure_daemon_groups(
        self,
        client: Any = None,
        *,
        scope: str = "all",
        load_system_repo: bool = True,
        load_available_repos: bool = True,
    ) -> bool:
        """Best-effort load of Comps groups from ``dnf5daemon-server``."""
        with self._instance_lock:
            if self._daemon_groups_loaded or self._daemon_groups_unsupported:
                return False

        with self._daemon_fetch_lock:
            with self._instance_lock:
                if self._daemon_groups_loaded or self._daemon_groups_unsupported:
                    return False

            try:
                from dendro.core.dnf5daemon import (
                    Dnf5DaemonClient,
                    Dnf5DaemonError,
                    Dnf5DaemonUnsupportedError,
                )
            except ImportError:
                return False

            owns_client = False
            active_client = client
            if active_client is None:
                try:
                    active_client = Dnf5DaemonClient()
                    owns_client = True
                except (Dnf5DaemonError, OSError, RuntimeError):
                    return False

            list_groups_fn = getattr(active_client, "list_groups", None)
            if not callable(list_groups_fn):
                return False

            try:
                groups = list_groups_fn(
                    scope=scope,
                    with_hidden=True,
                    load_system_repo=load_system_repo,
                    load_available_repos=load_available_repos,
                )
                self.merge_daemon_groups(groups)
                return True
            except Dnf5DaemonUnsupportedError:
                with self._instance_lock:
                    self._daemon_groups_unsupported = True
                return False
            except (Dnf5DaemonError, OSError, RuntimeError, ValueError):
                LOGGER.debug(
                    "Could not load Fedora Comps groups via DNF5 daemon.",
                    exc_info=True,
                )
                return False
            finally:
                if owns_client:
                    close_fn = getattr(active_client, "close", None)
                    if callable(close_fn):
                        try:
                            close_fn()
                        except Exception:
                            LOGGER.debug(
                                "Failed closing temporary DNF5 daemon client.",
                                exc_info=True,
                            )

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
                self._record_group_package(current_group_id, element.text)
            elif tag == "group":
                current_group_id = ""
                element.clear()
            element_stack.pop()

    def get_groups_for_package(self, package_name: str) -> tuple[str, ...]:
        with self._instance_lock:
            groups = self._package_groups.get(package_name.lower())
            return tuple(sorted(groups)) if groups else ()

    def get_domain_for_package(self, package_name: str) -> str | None:
        with self._instance_lock:
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
    return _python_zstd_available() or bool(_zstd_cli_binary())


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
    zck_supported = _python_zstd_available()
    rank = {
        ".zst": 0 if zstd_supported else 99,
        ".zstd": 0 if zstd_supported else 99,
        ".xz": 1,
        ".bz2": 2,
        ".gz": 3,
        "": 4,
        ".zck": 5 if zck_supported else 99,
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
    has_sysusers_dir: bool = False

    # Declarative language/platform evidence.  These IDs retain every positive
    # source so the classifier can use one as a primary role and expose the
    # others as searchable facets without reparsing the RPM payload.
    ecosystem_capability_ids: set[str] = field(default_factory=set)
    ecosystem_path_ids: set[str] = field(default_factory=set)
    ecosystem_bundled_ids: set[str] = field(default_factory=set)

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
    provides_mimehandler: bool = False
    provides_sysuser_user: bool = False
    provides_sysuser_group: bool = False
    provides_rpm_macro: bool = False
    provides_bundled: bool = False
    provided_desktop_ids: set[str] = field(default_factory=set)
    provided_metainfo_files: set[str] = field(default_factory=set)
    provided_mimehandlers: set[str] = field(default_factory=set)

    @property
    def has_plugins_dir(self) -> bool:
        return self.has_media_plugins_dir or self.has_gui_toolkit_dir

    @property
    def provides_sysuser(self) -> bool:
        return (
            self.provides_sysuser_user
            or self.provides_sysuser_group
            or self.has_sysusers_dir
        )

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
        clean_basenames = tuple(basenames or ())
        for directory in dirnames:
            anatomy._inspect_directory(directory)
        anatomy._inspect_basenames(clean_basenames)
        if not anatomy.has_themes_dir:
            anatomy._inspect_icon_theme_dirs(dirnames, clean_basenames)
        anatomy._inspect_provides(provides)
        return anatomy

    def _inspect_icon_theme_dirs(
        self, dirnames: Sequence[str], basenames: Sequence[str]
    ) -> None:
        clean_basenames = {b.strip().casefold() for b in basenames if b.strip()}
        has_index_theme = "index.theme" in clean_basenames
        for raw_dir in dirnames:
            path = raw_dir.strip().rstrip("/")
            normalized = path.casefold()
            if not normalized.startswith("/usr/share/icons/"):
                continue
            pieces = normalized.split("/")
            if len(pieces) != 5 or not pieces[4]:
                continue
            if has_index_theme:
                self.has_themes_dir = True
                return
            if not clean_basenames and pieces[4] not in _SHARED_ICON_THEME_NAMES:
                self.has_themes_dir = True
                return

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
        if _matches_directory_prefix(path, ("/usr/libexec",)):
            self.has_libexec = True
            return True
        return False

    def _inspect_system_directory(self, path: str) -> bool:
        normalized = path.casefold()
        if _matches_directory_prefix(path, _SYSTEM_APPLICATIONS_DIRS):
            self.has_desktop_file = True
            return True
        if _matches_directory_prefix(
            normalized, ("/usr/lib/systemd/system", "/lib/systemd/system")
        ):
            self.has_systemd_system = True
            return True
        if _matches_directory_prefix(
            normalized, ("/usr/lib/systemd/user", "/lib/systemd/user")
        ):
            self.has_systemd_user = True
            return True
        if _matches_directory_prefix(normalized, ("/usr/lib/sysusers.d",)):
            self.has_sysusers_dir = True
            return True
        if _matches_directory_prefix(path, ("/usr/include",)):
            self.has_c_headers = True
            return True
        return False

    def _inspect_asset_directory(self, path: str) -> bool:
        normalized = path.casefold()
        if _matches_directory_prefix(
            normalized, ("/usr/share/fonts", "/usr/share/x11/fonts")
        ):
            self.has_fonts_dir = True
            return True
        if _matches_directory_prefix(normalized, _NON_ICON_THEME_DIRS):
            self.has_themes_dir = True
            return True
        if _matches_directory_prefix(normalized, REFERENCE_DATA_DIRS):
            self.has_reference_data_dir = True
            return True
        if _matches_directory_prefix(normalized, REPOSITORY_CONFIGURATION_DIRS):
            self.has_repository_config_dir = True
            return True
        if _matches_directory_prefix(normalized, METAINFO_DATA_DIRS):
            self.has_metainfo_dir = True
            return True
        if _matches_directory_prefix(normalized, SYSTEM_DATA_DIRS):
            self.has_system_data_dir = True
            return True
        if _matches_directory_prefix(path, ("/usr/share/doc", "/usr/share/help")):
            self.has_docs_dir = True
            return True
        # RPM metadata may retain the legacy /lib path even on UsrMerge
        # systems where it resolves to /usr/lib. Both forms identify firmware.
        if _matches_directory_prefix(path, ("/usr/lib/firmware", "/lib/firmware")):
            self.has_firmware_dir = True
            return True
        return False

    def _inspect_hardware_or_plugin_directory(self, path: str) -> bool:
        normalized = path.casefold()
        if _matches_directory_prefix(path, ("/usr/lib/modules", "/lib/modules")):
            self.has_kernel_modules_dir = True
            return True
        if _matches_directory_prefix(normalized, XORG_DRIVER_DIRS):
            self.has_xorg_driver_dir = True
            return True
        if _matches_directory_prefix(
            normalized,
            (
                "/usr/lib64/dri",
                "/usr/lib/dri",
                "/usr/share/vulkan/icd.d",
                "/etc/vulkan/icd.d",
            ),
        ):
            self.has_dri_dir = True
            return True
        if _matches_directory_prefix(normalized, DEVELOPMENT_DATA_DIRS):
            self.has_development_data_dir = True
            return True
        # KIO workers reside under Qt plugin trees (e.g. /usr/lib64/qt6/plugins/kf6/kio)
        # and must match on a slash-delimited segment before Qt plugin checks return.
        path_segments = tuple(seg for seg in normalized.split("/") if seg)
        if "kio" in path_segments and _matches_directory_prefix(
            normalized, ("/usr/lib", "/usr/lib64", "/lib", "/lib64")
        ):
            self.has_kio_dir = True
            return True
        if any(
            f"{keyword}/" in f"{normalized}/" for keyword in MEDIA_PLUGIN_DIR_KEYWORDS
        ):
            self.has_media_plugins_dir = True
            return True
        if any(
            f"{keyword}/" in f"{normalized}/" for keyword in GUI_TOOLKIT_DIR_KEYWORDS
        ):
            self.has_gui_toolkit_dir = True
            return True
        if _matches_directory_prefix(normalized, DESKTOP_SESSION_DIRS):
            self.has_shell_or_session_dir = True
            return True
        return False

    def _inspect_runtime_directory(self, path: str) -> bool:
        normalized = path.casefold()
        if _matches_directory_prefix(normalized, DEVELOPMENT_DATA_DIRS):
            self.has_development_data_dir = True
            return True
        if _matches_directory_prefix(normalized, ("/usr/share/java", "/usr/lib/jvm")):
            self.has_jvm_dir = True
            return True
        if _matches_directory_prefix(normalized, ("/usr/share/locale",)):
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
        if _PYTHON_RUNTIME_DIR_RE.match(normalized):
            self.has_python_runtime = True
            self.ecosystem_path_ids.add("python")
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
            cap_head = (
                provide.split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].strip()
            )
            cap_lower = cap_head.casefold()
            self.ecosystem_capability_ids.update(ecosystem_ids_for_capability(cap_head))
            bundled_ecosystems = bundled_ecosystem_ids_for_capability(cap_head)
            if bundled_ecosystems:
                self.ecosystem_bundled_ids.update(bundled_ecosystems)
                self.provides_bundled = True
            if _ELF_SONAME_RE.match(cap_head):
                self.exported_sonames.append(provide)
            elif cap_lower.startswith("pkgconfig("):
                self.provides_pkgconfig = True
            elif cap_lower.startswith("font("):
                self.provides_font = True
            elif cap_lower.startswith("kmod("):
                self.provides_kmod = True
            elif cap_lower.startswith(("appdata(", "metainfo(")):
                self.provides_appstream = True
                if cap_head.endswith(")"):
                    inner = cap_head.split("(", 1)[1][:-1].strip()
                    if inner:
                        self.provided_metainfo_files.add(
                            os.path.basename(inner).lower()
                        )
            elif "windowmanager" in cap_lower:
                self.provides_wm = True
            elif cap_lower.startswith("gstreamer1("):
                self.provides_gstreamer = True
            elif is_python_dist_capability(cap_head):
                self.provides_python_dist = True
            elif cap_lower.startswith("crate("):
                self.provides_rust_crate = True
            elif cap_lower.startswith(("mvn(", "osgi(")):
                self.provides_jvm_artifact = True
            elif cap_lower.startswith("npm("):
                self.provides_nodejs_pkg = True
            elif cap_lower.startswith(("rpm_macro(", "rpm_lua(")):
                self.provides_rpm_macro = True
            elif cap_lower.startswith("user(") and cap_head.endswith(")"):
                if cap_head[5:-1].strip():
                    self.provides_sysuser_user = True
            elif cap_lower.startswith("group(") and cap_head.endswith(")"):
                if cap_head[6:-1].strip():
                    self.provides_sysuser_group = True
            elif cap_lower.startswith("bundled("):
                self.provides_bundled = True
            elif cap_lower.startswith("mimehandler(") and cap_head.endswith(")"):
                mimetype = cap_head[12:-1].strip().casefold()
                if mimetype:
                    self.provides_mimehandler = True
                    self.provided_mimehandlers.add(mimetype)
            elif cap_head == "application()":
                self.has_desktop_file = True
            elif cap_lower.startswith("application(") and cap_head.endswith(")"):
                desktop_id = cap_head[12:-1].strip()
                if desktop_id:
                    self.provided_desktop_ids.add(desktop_id)
                    self.has_desktop_file = True

    @classmethod
    def from_file_paths(
        cls,
        file_paths: Sequence[str],
        provides: Sequence[str],
        *,
        dirnames: Sequence[str] | None = None,
    ) -> PackagePhysicalAnatomy:
        """Build anatomy using exact full file paths for directory ownership checks."""
        derived_dirs: list[str] = []
        seen_dirs: set[str] = set()
        for raw_dir in dirnames or ():
            cleaned = raw_dir.strip()
            if cleaned and cleaned not in seen_dirs:
                seen_dirs.add(cleaned)
                derived_dirs.append(cleaned)

        basenames: list[str] = []
        clean_paths: list[str] = []
        for raw_path in file_paths:
            cleaned_path = raw_path.strip()
            if not cleaned_path:
                continue
            clean_paths.append(cleaned_path)
            normalized = posixpath.normpath(cleaned_path.replace("\\", "/"))
            parent = posixpath.dirname(normalized)
            if parent and parent not in seen_dirs:
                seen_dirs.add(parent)
                derived_dirs.append(parent)
            if not normalized.endswith("/"):
                base = posixpath.basename(normalized)
                if base:
                    basenames.append(base)

        anatomy = cls.from_manifest_data(derived_dirs, provides, basenames)
        exact_desktop_ids = set(anatomy.provided_desktop_ids)
        exact_metainfo = {
            os.path.basename(inner).lower()
            for p in provides
            for stripped in (p.strip().split("=", 1)[0].strip(),)
            if stripped.casefold().startswith(("appdata(", "metainfo("))
            and stripped.endswith(")")
            for inner in (stripped.split("(", 1)[1][:-1].strip(),)
            if inner
        }
        has_non_icon_theme = any(
            _matches_directory_prefix(
                d.strip().rstrip("/").casefold(), _NON_ICON_THEME_DIRS
            )
            for d in derived_dirs
        )
        has_icon_index_theme = False
        for file_path in clean_paths:
            desktop_id = desktop_file_id_from_path(file_path)
            if desktop_id is not None:
                exact_desktop_ids.add(desktop_id)
                continue
            normalized = posixpath.normpath(file_path.replace("\\", "/"))
            norm_lower = normalized.casefold()
            if norm_lower.startswith("/usr/share/icons/"):
                parts = norm_lower.split("/")
                if len(parts) == 6 and parts[5] == "index.theme" and parts[4]:
                    has_icon_index_theme = True
            base_lower = posixpath.basename(normalized).lower()
            if base_lower.endswith((".metainfo.xml", ".appdata.xml")):
                parent_lower = posixpath.dirname(normalized).casefold()
                if _matches_directory_prefix(parent_lower, METAINFO_DATA_DIRS):
                    exact_metainfo.add(base_lower)

        anatomy.provided_desktop_ids = exact_desktop_ids
        anatomy.provided_metainfo_files = exact_metainfo
        anatomy.has_desktop_file = bool(exact_desktop_ids) or any(
            p.strip().split("=", 1)[0].strip() == "application()" for p in provides
        )
        anatomy.has_themes_dir = has_non_icon_theme or has_icon_index_theme
        return anatomy

    @classmethod
    def from_rpm_header(
        cls,
        header: Any,
        *,
        rpm_module: Any = None,
        raw_provides: Sequence[str] | None = None,
    ) -> PackagePhysicalAnatomy:
        """Build anatomy from an RPM header without coupling to backend state."""
        if header is None:
            return cls()
        rpm_mod = rpm_module
        if rpm_mod is None:
            try:
                import rpm as imported_rpm  # type: ignore[import-not-found, import-untyped]
            except ImportError:
                return cls()
            rpm_mod = imported_rpm
        try:

            def decode(value: Any) -> str:
                return (
                    value.decode("utf-8", errors="replace")
                    if isinstance(value, bytes)
                    else str(value or "")
                )

            def read_strings(tag_attr: str) -> list[str]:
                tag = getattr(rpm_mod, tag_attr, None)
                if tag is None:
                    return []
                try:
                    raw = header[tag]
                except (KeyError, IndexError, TypeError):
                    return []
                return [decode(value) for value in (raw or ())]

            def read_ints(tag_attr: str) -> list[int]:
                tag = getattr(rpm_mod, tag_attr, None)
                if tag is None:
                    return []
                try:
                    raw = header[tag]
                except (KeyError, IndexError, TypeError):
                    return []
                return [int(value) for value in (raw or ())]

            provs = (
                list(raw_provides)
                if raw_provides is not None
                else read_strings("RPMTAG_PROVIDENAME")
            )
            raw_dirs = read_strings("RPMTAG_DIRNAMES")
            raw_basenames = read_strings("RPMTAG_BASENAMES")
            directory_indexes = read_ints("RPMTAG_DIRINDEXES")

            if (
                raw_dirs
                and raw_basenames
                and len(directory_indexes) == len(raw_basenames)
            ):
                file_paths = [
                    (
                        f"/{basename}"
                        if raw_dirs[directory_index].rstrip("/") == ""
                        else f"{raw_dirs[directory_index].rstrip('/')}/{basename}"
                    )
                    for basename, directory_index in zip(
                        raw_basenames, directory_indexes, strict=True
                    )
                    if 0 <= directory_index < len(raw_dirs) and basename
                ]
                return cls.from_file_paths(file_paths, provs, dirnames=raw_dirs)

            old_filenames = [
                path for path in read_strings("RPMTAG_OLDFILENAMES") if path.strip()
            ]
            if old_filenames:
                return cls.from_file_paths(old_filenames, provs)

            anatomy = cls.from_manifest_data(raw_dirs, provs, raw_basenames)
            has_apps_dir = any(
                _matches_directory_prefix(
                    directory.strip().rstrip("/"), _SYSTEM_APPLICATIONS_DIRS
                )
                for directory in raw_dirs
            )
            real_desktop_entry = False
            if has_apps_dir:
                for basename in raw_basenames:
                    if basename.endswith(".desktop"):
                        real_desktop_entry = True
                        anatomy.provided_desktop_ids.add(basename)
            if not real_desktop_entry and not anatomy.provided_desktop_ids:
                anatomy.has_desktop_file = False
            return anatomy
        except Exception:
            LOGGER.warning(
                "Could not inspect an RPM header's file manifest.", exc_info=True
            )
            return cls()


class DesktopEntryMetadata:
    """Defensive parser for FreeDesktop Desktop Entry Specification 1.5 files."""

    __slots__ = (
        "categories",
        "dbus_activatable",
        "desktop_id",
        "entry_type",
        "exec_cmd",
        "is_auxiliary",
        "is_hidden",
        "is_im",
        "is_nodisplay",
        "is_settings",
        "is_terminal",
        "is_wm",
        "name",
        "not_show_in",
        "only_show_in",
        "shown_in_current_desktop",
        "try_exec",
        "try_exec_available",
    )

    def __init__(self, file_path: str, *, desktop_id: str | None = None) -> None:
        self.desktop_id = (
            desktop_id
            or desktop_file_id_from_path(file_path)
            or os.path.basename(file_path)
        )
        self.entry_type = ""
        self.name = ""
        self.exec_cmd = ""
        self.try_exec = ""
        self.try_exec_available = True
        self.dbus_activatable = False
        self.is_hidden = False
        self.only_show_in: tuple[str, ...] = ()
        self.not_show_in: tuple[str, ...] = ()
        self.shown_in_current_desktop = True
        self.categories: set[str] = set()
        self.is_nodisplay = False
        self.is_terminal = False
        self.is_wm = False
        self.is_im = False
        self.is_settings = False
        self.is_auxiliary = False
        self._parse(file_path)

    def _parse(self, file_path: str) -> None:
        saw_desktop_entry = False
        try:
            with open(file_path, encoding="utf-8", errors="replace") as stream:
                in_entry = False
                for raw_line in stream:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        in_entry = line == "[Desktop Entry]"
                        if in_entry:
                            saw_desktop_entry = True
                        continue
                    if in_entry and "=" in line:
                        key, raw_value = line.split("=", 1)
                        self._consume_field(key.strip(), raw_value.strip())
            self._finalize_entry(saw_desktop_entry)
        except (OSError, UnicodeError, ValueError):
            self.is_nodisplay = True
            LOGGER.warning(
                "Could not parse desktop entry %s; treating it as non-displayable.",
                file_path,
                exc_info=True,
            )

    def _consume_field(self, key: str, value: str) -> None:
        if key == "Type":
            self.entry_type = _unescape_desktop_string(value)
            if self.entry_type != "Application":
                self.is_nodisplay = True
        elif key == "Name" and not self.name:
            self.name = _unescape_desktop_string(value)
        elif key == "NoDisplay":
            if _unescape_desktop_string(value).lower() == "true":
                self.is_nodisplay = True
        elif key == "Hidden":
            if _unescape_desktop_string(value).lower() == "true":
                self.is_hidden = True
                self.is_nodisplay = True
        elif key == "Terminal":
            self.is_terminal = _unescape_desktop_string(value).lower() == "true"
        elif key == "DBusActivatable":
            self.dbus_activatable = _unescape_desktop_string(value).lower() == "true"
        elif key == "TryExec":
            self.try_exec = _unescape_desktop_string(value)
        elif key == "Exec" and not self.exec_cmd:
            unescaped = _unescape_desktop_string(value)
            try:
                tokens = shlex.split(unescaped, posix=True)
            except ValueError:
                tokens = unescaped.split()
            command = tokens[0] if tokens else ""
            self.exec_cmd = os.path.basename(command.strip("\"' ")).lower()
        elif key == "Categories":
            self.categories = {
                item.lower() for item in _parse_desktop_string_list(value)
            }
        elif key == "OnlyShowIn":
            self.only_show_in = _parse_desktop_string_list(value)
        elif key == "NotShowIn":
            self.not_show_in = _parse_desktop_string_list(value)
        elif key in {"X-GNOME-Provides", "X-GNOME-Autostart-Phase"}:
            normalized = _unescape_desktop_string(value).lower()
            self.is_wm = self.is_wm or "windowmanager" in normalized
            self.is_im = self.is_im or "inputmethod" in normalized

    def _finalize_entry(self, saw_desktop_entry: bool) -> None:
        if not saw_desktop_entry or self.entry_type != "Application":
            self.is_nodisplay = True
        if not self.exec_cmd and not self.dbus_activatable:
            self.is_nodisplay = True
        self.try_exec_available = _is_try_exec_available(self.try_exec)
        if not self.try_exec_available:
            self.is_nodisplay = True
        self.shown_in_current_desktop = self.is_shown_in_desktop()
        self._finalize_categories()

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

    def is_shown_in_desktop(
        self, current_desktop: str | Sequence[str] | None = None
    ) -> bool:
        """Return whether ``OnlyShowIn``/``NotShowIn`` allow display in this desktop."""
        return _is_shown_in_desktop(
            self.only_show_in, self.not_show_in, current_desktop
        )

    def is_launchable(self, current_desktop: str | Sequence[str] | None = None) -> bool:
        """Return whether the entry represents a visible, launchable application."""
        return (
            not self.is_hidden
            and not self.is_nodisplay
            and self.try_exec_available
            and (bool(self.exec_cmd) or self.dbus_activatable)
            and self.is_shown_in_desktop(current_desktop)
        )


class SystemDesktopIndex:
    """Lazy map of installed XDG desktop entries ordered by ``$XDG_DATA_DIRS``."""

    _instance: SystemDesktopIndex | None = None
    _lock = threading.Lock()

    def __init__(self, data_dirs: Sequence[str] | None = None) -> None:
        if data_dirs is None:
            self.data_dirs = resolve_xdg_data_dirs()
        else:
            self.data_dirs = tuple(
                posixpath.normpath(directory.strip().replace("\\", "/"))
                for directory in data_dirs
                if directory and directory.strip()
            )
        self.desktop_map: dict[str, DesktopEntryMetadata] = {}
        self._load_all_desktop_files()

    @classmethod
    def get_instance(cls) -> SystemDesktopIndex:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Clear the singleton cache (intended for tests or desktop-index reload)."""
        with cls._lock:
            cls._instance = None

    def lookup(
        self,
        desktop_id: str,
        *,
        respect_desktop_environment: bool = False,
        current_desktop: str | Sequence[str] | None = None,
    ) -> DesktopEntryMetadata | None:
        """Look up an indexed desktop entry by spec Desktop File ID."""
        entry = self.desktop_map.get(desktop_id) or self.desktop_map.get(
            desktop_id.lower()
        )
        if entry is None:
            return None
        if respect_desktop_environment and not entry.is_shown_in_desktop(
            current_desktop
        ):
            return None
        return entry

    def _load_all_desktop_files(self) -> None:
        seen_desktop_ids: set[str] = set()
        for data_dir in self.data_dirs:
            apps_dir = os.path.join(data_dir, "applications")
            if not os.path.isdir(apps_dir):
                continue
            seen_real_dirs: set[str] = set()
            for root, directories, filenames in os.walk(apps_dir, followlinks=True):
                real_root = os.path.realpath(root)
                if real_root in seen_real_dirs:
                    directories.clear()
                    continue
                seen_real_dirs.add(real_root)
                directories.sort()
                for filename in sorted(filenames):
                    if not filename.endswith(".desktop"):
                        continue
                    full_path = os.path.join(root, filename)
                    if not os.path.isfile(full_path):
                        continue
                    rel_path = os.path.relpath(full_path, apps_dir).replace(os.sep, "/")
                    rel_path = rel_path.removeprefix("./")
                    if (
                        not rel_path
                        or rel_path == ".desktop"
                        or rel_path.endswith("/.desktop")
                    ):
                        continue
                    desktop_id = rel_path.replace("/", "-")
                    folded_id = desktop_id.casefold()
                    if folded_id in seen_desktop_ids:
                        continue
                    seen_desktop_ids.add(folded_id)
                    entry = DesktopEntryMetadata(full_path, desktop_id=desktop_id)
                    if entry.is_hidden:
                        continue
                    self.desktop_map[desktop_id] = entry
                    self.desktop_map[desktop_id.lower()] = entry
