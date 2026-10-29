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
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

LOGGER = logging.getLogger(__name__)

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


class UnsupportedMetadataCompression(OSError):
    """Raised when an optional local repository-metadata codec is absent."""


@contextmanager
def open_compressed_xml_metadata(file_path: str) -> Iterator[Any]:
    """Yield a binary XML stream for a supported local metadata file.

    The caller always parses through ``defusedxml``.  Unsupported compression
    formats fail explicitly rather than producing a misleading XML error.
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


def _local_name(tag: object) -> str:
    """Return an XML local name for both namespaced and legacy metadata."""
    text = str(tag)
    return text.rsplit("}", 1)[-1]


def _children(element: Any, name: str) -> list[Any]:
    return [child for child in element if _local_name(child.tag) == name]


def _first_child(element: Any, name: str) -> Any | None:
    return next(iter(_children(element, name)), None)


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


class AppStreamCatalog:
    """Defused, multi-package AppStream catalog index.

    All ``pkgname`` values in a component receive the component's type and XDG
    categories.  This matters for font families and other AppStream components
    that legitimately describe several RPM subpackages.
    """

    _instance: AppStreamCatalog | None = None
    _lock = threading.Lock()

    # Component types listed here are indexed faithfully. ``runtime`` is kept
    # for compatibility and diagnostics, but is deliberately not consumed by
    # the classifier as a desktop or library signal; unknown types likewise
    # receive no role by implication.
    _TYPE_TO_ATTRIBUTE: Final[Mapping[str, str]] = {
        "desktop": "desktop_packages",
        "desktop-application": "desktop_packages",
        "webapp": "desktop_packages",
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
    }

    def __init__(self, *, load_catalog: bool = True) -> None:
        self.desktop_packages: set[str] = set()
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
        # Preserved for callers that inspect AppStream runtime components. It
        # is intentionally not a classifier signal without other evidence.
        self.runtime_packages: set[str] = set()
        self.pkg_xdg_categories: dict[str, set[str]] = {}
        self._loaded = False
        if load_catalog:
            self._load_catalog()

    @classmethod
    def get_instance(cls) -> AppStreamCatalog:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _load_catalog(self) -> None:
        if self._loaded:
            return

        catalog_dirs = (
            "/usr/share/swcatalog/xml",
            "/var/lib/swcatalog/xml",
            "/var/cache/swcatalog/xml",
            "/usr/share/metainfo",
        )
        supported_suffixes = (
            ".xml",
            ".xml.gz",
            ".xml.bz2",
            ".xml.xz",
            ".xml.zst",
            ".xml.zstd",
        )
        for catalog_dir in catalog_dirs:
            if not os.path.isdir(catalog_dir):
                continue
            try:
                for filename in os.listdir(catalog_dir):
                    if filename.endswith(supported_suffixes):
                        self._parse_appstream_file(os.path.join(catalog_dir, filename))
            except OSError:
                LOGGER.warning(
                    "Could not enumerate AppStream metadata directory %s; skipping it.",
                    catalog_dir,
                    exc_info=True,
                )

        self._loaded = True

    def _parse_appstream_file(self, file_path: str) -> None:
        try:
            with open_compressed_xml_metadata(file_path) as stream:
                context = ET.iterparse(stream, events=("start", "end"))
                _event, root = next(context)
                for event, element in context:
                    if event != "end" or _local_name(element.tag) not in {
                        "component",
                        "application",
                    }:
                        continue
                    self._index_component(element)
                    element.clear()
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

    def _index_component(self, component: Any) -> None:
        component_type = (component.get("type") or "").strip().lower()
        package_names = {
            child.text.strip().lower()
            for child in _children(component, "pkgname")
            if child.text and child.text.strip()
        }
        if not package_names:
            package_names = {
                child.text.strip().lower()
                for child in _children(component, "bundle")
                if child.get("type") == "package" and child.text and child.text.strip()
            }
        if not package_names:
            return

        categories_element = _first_child(component, "categories")
        categories = (
            {
                child.text.strip().lower()
                for child in _children(categories_element, "category")
                if child.text and child.text.strip()
            }
            if categories_element is not None
            else set()
        )
        type_attribute = self._TYPE_TO_ATTRIBUTE.get(component_type)
        for package_name in package_names:
            if type_attribute is not None:
                getattr(self, type_attribute).add(package_name)
            if categories:
                self.pkg_xdg_categories.setdefault(package_name, set()).update(
                    categories
                )


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
    has_media_plugins_dir: bool = False
    has_gui_toolkit_dir: bool = False
    has_kio_dir: bool = False
    has_shell_or_session_dir: bool = False
    has_jvm_dir: bool = False
    has_jar_files: bool = False
    has_locales_dir: bool = False
    has_shared_libs_dir: bool = False
    has_man1: bool = False
    has_man8: bool = False
    has_man3: bool = False
    has_python_runtime: bool = False

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

    @property
    def has_plugins_dir(self) -> bool:
        return self.has_media_plugins_dir or self.has_gui_toolkit_dir

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
        if path == "/usr/share/fonts" or path.startswith("/usr/share/fonts/"):
            self.has_fonts_dir = True
            return True
        if path.startswith(("/usr/share/themes", "/usr/share/backgrounds")):
            self.has_themes_dir = True
            return True
        if path.startswith("/usr/share/icons/"):
            # GUI applications commonly install hicolor icons; only independent
            # icon themes qualify as a theme package.
            pieces = path.split("/")
            if len(pieces) >= 5 and pieces[4] != "hicolor":
                self.has_themes_dir = True
            return True
        if path.startswith(("/usr/share/doc", "/usr/share/help")):
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
        if path.startswith(("/usr/lib/modules/", "/lib/modules/")):
            self.has_kernel_modules_dir = True
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
        self.has_jar_files = any(name.endswith(".jar") for name in basenames)

    def _inspect_provides(self, provides: Iterable[str]) -> None:
        for raw_provide in provides:
            provide = raw_provide.strip()
            if not provide:
                continue
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
                for basename, directory_index in zip(
                    raw_basenames, directory_indexes, strict=True
                ):
                    if not basename.endswith(
                        ".desktop"
                    ) or not 0 <= directory_index < len(raw_dirs):
                        continue
                    directory = raw_dirs[directory_index].rstrip("/")
                    if directory.startswith(
                        ("/usr/share/applications", "/usr/local/share/applications")
                    ):
                        real_desktop_entry = True
                        anatomy.provided_desktop_ids.add(basename)
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
