# dendro/core/backend.py
"""Native package backend and compatibility facade for Dendro.

Metadata parsing and taxonomy policy are intentionally implemented in focused,
Qt-independent modules.  This facade retains the historical public imports and
owns native RPM/DNF access plus Qt worker orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, Final, List, Optional, Set, Tuple

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot

from core.dnf5daemon import Dnf5DaemonClient, Dnf5DaemonError
import core.package_classifier as _package_classifier
import core.package_metadata as _package_metadata
import core.taxonomy as _taxonomy
from core.package_classifier import (
    DEFAULT_SYSTEM_ROOT_PILLARS,
    FEDORA_SYSTEM_ROOT_PILLARS,
    ProductionTaxonomyEngine,
)
from core.package_metadata import (
    AppStreamCatalog,
    FedoraCompsCatalog,
    PackagePhysicalAnatomy,
)
from core.taxonomy import (
    ClassificationDecision,
    ClassificationEvidence,
    TaxonomyInputScope,
)

# Historical backend import compatibility. New code should import its focused
# module directly; these aliases let existing integrations migrate gradually.
DEFAULT_FEDORA_CORE_PACKAGES = _package_classifier.DEFAULT_FEDORA_CORE_PACKAGES
FreeDesktopTaxonomyEngine = _package_classifier.FreeDesktopTaxonomyEngine
IntelligentPackageClassifier = _package_classifier.IntelligentPackageClassifier
PACKAGE_TAXONOMY_OVERRIDES = _package_classifier.PACKAGE_TAXONOMY_OVERRIDES
DESKTOP_SESSION_DIRS = _package_metadata.DESKTOP_SESSION_DIRS
DesktopEntryMetadata = _package_metadata.DesktopEntryMetadata
GUI_TOOLKIT_DIR_KEYWORDS = _package_metadata.GUI_TOOLKIT_DIR_KEYWORDS
MEDIA_PLUGIN_DIR_KEYWORDS = _package_metadata.MEDIA_PLUGIN_DIR_KEYWORDS
SystemDesktopIndex = _package_metadata.SystemDesktopIndex
_UnsupportedMetadataCompression = _package_metadata.UnsupportedMetadataCompression
_open_compressed_xml_metadata = _package_metadata.open_compressed_xml_metadata
PackageArchetype = _taxonomy.PackageArchetype
RemovalRiskLevel = _taxonomy.RemovalRiskLevel
RemovalSafetyAssessment = _taxonomy.RemovalSafetyAssessment

# Thread-safety reentrant lock for native C librpm calls
RPM_GLOBAL_LOCK: Final[threading.RLock] = threading.RLock()
LOGGER = logging.getLogger(__name__)

# Precompiled regular expression used by changelog inspection.
CVE_REGEX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(CVE-\d{4}-\d{4,7})\b", re.IGNORECASE
)
# DNF ``repoquery --queryformat %{name}`` promises bare RPM names, not human
# diagnostics. Validate that narrow contract before metadata flags are changed.
RPM_PACKAGE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+~-]*"
)

try:
    import rpm  # type: ignore[import-untyped]

    HAS_NATIVE_RPM: Final[bool] = True
except ImportError:
    HAS_NATIVE_RPM = False

try:
    import libdnf5  # type: ignore[import-untyped]
    import libdnf5.base  # type: ignore[import-untyped]
    import libdnf5.rpm  # type: ignore[import-untyped]
    import libdnf5.repo  # type: ignore[import-untyped]
    import libdnf5.transaction  # type: ignore[import-untyped]

    HAS_LIBDNF5: Final[bool] = True
except ImportError:
    HAS_LIBDNF5 = False


def _decode_rpm_str(val: Any) -> str:
    """Safely normalise a native RPM value to UTF-8 text."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def is_running_in_flatpak() -> bool:
    """Detect whether Dendro runs in a Flatpak sandbox."""
    return os.path.exists("/.flatpak-info")


def flatpak_read_only_unavailable(feature: str) -> str:
    return (
        f"{feature} is unavailable in Flatpak. Dendro does not inspect the host RPM database "
        "or run host commands from the sandbox."
    )


def get_clean_env() -> Dict[str, str]:
    """Return an environment safe for host RPM/DNF subprocesses."""
    env = os.environ.copy()
    for variable in (
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
    ):
        env.pop(variable, None)
    return env


def get_dnf_binary_path() -> str:
    """Return a local DNF binary only outside a Flatpak sandbox."""
    if is_running_in_flatpak():
        return ""
    for candidate in ("/usr/bin/dnf5", "/usr/bin/dnf"):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("dnf5") or shutil.which("dnf")
    return os.path.realpath(found) if found and os.path.exists(found) else ""


def _parse_dnf_package_name_output(output: str, query_name: str) -> Set[str]:
    """Parse a DNF name-only query without treating diagnostics as package names."""
    package_names: Set[str] = set()
    for raw_line in output.splitlines():
        package_name = raw_line.strip()
        if not package_name:
            continue
        if (
            raw_line != package_name
            or RPM_PACKAGE_NAME_PATTERN.fullmatch(package_name) is None
        ):
            raise RuntimeError(
                f"{query_name} returned an invalid package-name record: {raw_line}"
            )
        package_names.add(package_name)
    return package_names


def create_rpm_transaction_set() -> Optional[object]:
    """Create a read-only RPM transaction set when native bindings are available."""
    if not HAS_NATIVE_RPM or is_running_in_flatpak():
        return None
    with RPM_GLOBAL_LOCK:
        try:
            transaction_set = rpm.TransactionSet()
            if hasattr(rpm, "_RPMVSF_NOSIGNATURES"):
                transaction_set.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
            return transaction_set
        except Exception:
            LOGGER.warning(
                "Could not create an RPM transaction set; native RPM queries are unavailable.",
                exc_info=True,
            )
            return None


def create_libdnf5_base(load_repos: bool = False) -> Optional[object]:
    """Instantiate an isolated libdnf5 base under the RPM lock."""
    if not HAS_LIBDNF5 or is_running_in_flatpak():
        return None
    with RPM_GLOBAL_LOCK:
        try:
            base = libdnf5.base.Base()
            if hasattr(base, "load_config"):
                base.load_config()
            elif hasattr(base, "load_config_from_file"):
                base.load_config_from_file("/etc/dnf/dnf.conf")
            base.setup()
            if load_repos:
                repo_sack = base.get_repo_sack()
                repo_sack.create_repos_from_system_configuration()
                repo_sack.update_and_load_enabled_repos(False)
            return base
        except Exception:
            LOGGER.warning(
                "Could not initialize the optional libdnf5 read-only base.",
                exc_info=True,
            )
            return None


def get_system_protected_packages() -> Set[str]:
    """Read DNF protected packages and retain a conservative static fallback."""
    protected: Set[str] = set()
    protected_dirs = ["/etc/dnf/protected.d"] if not is_running_in_flatpak() else []
    for protected_dir in protected_dirs:
        if not os.path.isdir(protected_dir):
            continue
        try:
            for filename in os.listdir(protected_dir):
                if not filename.endswith(".conf"):
                    continue
                with open(
                    os.path.join(protected_dir, filename),
                    "r",
                    encoding="utf-8",
                    errors="replace",
                ) as stream:
                    for line in stream:
                        package_name = line.strip()
                        if package_name and not package_name.startswith("#"):
                            protected.add(package_name.lower())
        except OSError:
            LOGGER.warning(
                "Could not read protected-package configuration from %s.",
                protected_dir,
                exc_info=True,
            )
    try:
        uname_release = os.uname().release
        protected.update({f"kernel-{uname_release}", "kernel", "kernel-core"})
    except OSError:
        LOGGER.warning(
            "Could not determine the running kernel release for protected-package checks.",
            exc_info=True,
        )
    if not protected:
        protected.update(DEFAULT_SYSTEM_ROOT_PILLARS)
    else:
        protected.update(
            {"systemd", "glibc", "dnf", "dnf5", "rpm", "shared-mime-info", "glib2"}
        )
    return protected


class PackageState(Enum):
    INSTALLED = auto()
    AVAILABLE = auto()
    MISSING = auto()
    QUEUED_INSTALL = auto()
    QUEUED_REMOVE = auto()
    QUEUED_UPGRADE = auto()


@dataclass(slots=True)
class DependencyNode:
    raw_requirement: str
    resolved_package_name: str
    version_constraint: str = ""
    is_satisfied: bool = True
    is_cycle: bool = False
    is_reverse: bool = False
    installed_version: str = ""
    sub_dependencies: List[DependencyNode] = field(default_factory=list)


@dataclass(slots=True)
class PackageFileInfo:
    path: str
    size_bytes: int = 0
    mode: str = ""
    is_dir: bool = False
    is_config: bool = False
    is_executable: bool = False


@dataclass(slots=True)
class HistoryEntry:
    id: int
    command_line: str
    date_time: str
    action: str
    altered_count: int
    return_code: int


@dataclass(slots=True)
class PackageChangelogEntry:
    author: str
    timestamp: int
    date_str: str
    text: str
    cves: List[str] = field(default_factory=list)


@dataclass(slots=True)
class PackageScriptlets:
    prein: str = ""
    postin: str = ""
    preun: str = ""
    postun: str = ""

    @property
    def has_any(self) -> bool:
        return bool(self.prein or self.postin or self.preun or self.postun)


@dataclass(slots=True)
class FileVerificationResult:
    path: str
    status_flags: str
    is_config: bool
    is_missing: bool
    size_differs: bool
    mode_differs: bool
    digest_differs: bool
    mtime_differs: bool
    raw_line: str


@dataclass(slots=True)
class AvailableUpdateInfo:
    name: str
    new_version: str
    new_release: str = ""
    arch: str = ""
    repository: str = ""
    is_security: bool = False
    advisory_id: str = ""

    @property
    def full_version(self) -> str:
        """Returns the canonical version-release upgrade target without trailing hyphens."""
        v = self.new_version.strip()
        r = self.new_release.strip()
        if v and r:
            return f"{v}-{r}"
        return v or r


@dataclass(slots=True)
class RepoInfo:
    id: str
    name: str
    enabled: bool
    repo_file: str
    is_copr: bool
    is_rpmfusion: bool
    is_core: bool
    baseurl: str = ""


@dataclass(slots=True)
class LocalRpmInspectionResult:
    file_path: str
    name: str
    version: str
    release: str
    arch: str
    summary: str
    description: str
    license: str
    url: str
    size_bytes: int
    requires: List[str] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
    is_already_installed: bool = False
    installed_version: str = ""


@dataclass(slots=True)
class PackageInfo:
    name: str
    version: str = ""
    release: str = ""
    arch: str = ""
    epoch: str = "0"
    summary: str = ""
    description: str = ""
    license: str = ""
    url: str = ""
    packager: str = ""
    vendor: str = ""
    build_time: str = ""
    install_time: str = ""
    group: str = "System"
    size_bytes: int = 0
    state: PackageState = PackageState.AVAILABLE

    # Two-Tier Hierarchical Taxonomy
    parent_pillar: str = "pillar_maintenance"
    sub_category: str = "unclassified"
    primary_category: str = "unclassified"
    # Numeric confidence is retained only for backwards-compatible consumers.
    classification_confidence: float = 0.0
    classification_evidence: str = ClassificationEvidence.INSUFFICIENT_METADATA.value
    classification_input_scope: str = TaxonomyInputScope.UNKNOWN.value
    classification_rationale: List[str] = field(default_factory=list)
    secondary_tags: List[str] = field(default_factory=list)

    # Core & Application Flags
    is_orphan: bool = False
    is_user_installed: bool = False
    is_desktop_app: bool = False
    is_cli_tool: bool = False
    is_system_settings: bool = False
    is_fedora_core: bool = False
    is_protected: bool = False

    # Hardware & System Architecture Flags
    is_graphics_driver: bool = False
    is_audio_sound: bool = False
    is_kernel_module: bool = False
    is_firmware: bool = False
    is_systemd_service: bool = False
    is_security_pkg: bool = False

    # Libraries, Toolkits & Plugins
    is_media_plugin: bool = False
    is_desktop_addon: bool = False
    is_gui_toolkit: bool = False
    is_c_lib: bool = False
    is_font: bool = False
    is_locale: bool = False
    is_devel: bool = False
    is_theme: bool = False
    is_library: bool = False

    # Ecosystem Flags
    is_python_pkg: bool = False
    is_rust_pkg: bool = False
    is_jvm_pkg: bool = False
    is_nodejs_pkg: bool = False

    # Repository & packaging metadata
    repository: str = "Fedora Project"

    # Pending Updates
    has_update: bool = False
    available_update_version: str = ""
    available_update_repo: str = ""

    # Hierarchy and file collections
    has_dependencies: bool = True
    dependencies_loaded: bool = False
    dependencies: List[DependencyNode] = field(default_factory=list)
    reverse_dependencies: List[DependencyNode] = field(default_factory=list)
    files: List[PackageFileInfo] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)

    @property
    def full_version(self) -> str:
        return f"{self.version}-{self.release}" if self.release else self.version

    def format_version(self, show_epoch: bool = False) -> str:
        """Returns version with optional RPM epoch prefix (e.g. 0:1.10.3-1.fc45)."""
        base = self.full_version
        if show_epoch:
            ep = self.epoch.strip() if self.epoch else "0"
            return f"{ep}:{base}"
        return base

    @property
    def human_size(self) -> str:
        size = float(self.size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024.0 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"


# =============================================================================
# Standalone Local .rpm Inspection (Direct In-Process Analysis)
# =============================================================================


class LocalRpmInspectionError(RuntimeError):
    """A local RPM could not be inspected, rather than merely being absent."""


def inspect_local_rpm_file(
    file_path: str, *, raise_on_failure: bool = False
) -> Optional[LocalRpmInspectionResult]:
    """Read one local RPM header without confusing a failed read with bad data.

    The optional exception contract lets interactive callers explain a host RPM
    or subprocess failure precisely while retaining the legacy optional result
    for best-effort callers.
    """

    def fail(detail: str) -> Optional[LocalRpmInspectionResult]:
        LOGGER.warning("Local RPM inspection failed for %r: %s", file_path, detail)
        if raise_on_failure:
            raise LocalRpmInspectionError(detail)
        return None

    if is_running_in_flatpak():
        return fail(flatpak_read_only_unavailable("Local RPM inspection"))
    if not os.path.isfile(file_path):
        return fail("The selected RPM file does not exist or is not a regular file.")

    if HAS_NATIVE_RPM:
        with RPM_GLOBAL_LOCK:
            try:
                ts = rpm.TransactionSet()
                if hasattr(rpm, "_RPMVSF_NOSIGNATURES"):
                    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

                with open(file_path, "rb") as fd:
                    hdr = ts.hdrFromFdno(fd.fileno())
                    if hdr is None:
                        raise RuntimeError("librpm returned no package header")

                    name = _decode_rpm_str(hdr[rpm.RPMTAG_NAME]).strip()
                    ver = _decode_rpm_str(hdr[rpm.RPMTAG_VERSION]).strip()
                    rel = _decode_rpm_str(hdr[rpm.RPMTAG_RELEASE]).strip()
                    arch = _decode_rpm_str(hdr[rpm.RPMTAG_ARCH]).strip()
                    if not all((name, ver, rel, arch)):
                        raise RuntimeError(
                            "librpm returned a package header with missing identity fields"
                        )
                    summary = _decode_rpm_str(hdr[rpm.RPMTAG_SUMMARY])
                    desc = _decode_rpm_str(hdr[rpm.RPMTAG_DESCRIPTION])
                    lic = _decode_rpm_str(hdr[rpm.RPMTAG_LICENSE])
                    url = _decode_rpm_str(hdr[rpm.RPMTAG_URL])
                    size = int(hdr[rpm.RPMTAG_SIZE] or 0)
                    if size < 0:
                        raise RuntimeError("librpm returned a negative package size")

                    raw_reqs = [
                        _decode_rpm_str(r) for r in (hdr[rpm.RPMTAG_REQUIRENAME] or [])
                    ]
                    raw_provs = [
                        _decode_rpm_str(p) for p in (hdr[rpm.RPMTAG_PROVIDENAME] or [])
                    ]

                    is_installed = False
                    installed_ver = ""
                    matches = None
                    try:
                        matches = ts.dbMatch("name", name)
                        for inst_hdr in matches:
                            is_installed = True
                            inst_v = _decode_rpm_str(inst_hdr[rpm.RPMTAG_VERSION])
                            inst_r = _decode_rpm_str(inst_hdr[rpm.RPMTAG_RELEASE])
                            installed_ver = f"{inst_v}-{inst_r}" if inst_r else inst_v
                            break
                    finally:
                        if matches is not None:
                            del matches

                    return LocalRpmInspectionResult(
                        file_path=os.path.abspath(file_path),
                        name=name,
                        version=ver,
                        release=rel,
                        arch=arch,
                        summary=summary,
                        description=desc,
                        license=lic,
                        url=url,
                        size_bytes=size,
                        requires=raw_reqs,
                        provides=raw_provs,
                        is_already_installed=is_installed,
                        installed_version=installed_ver,
                    )
            except Exception:
                # A native binding failure does not prove the RPM is bad. Try
                # the independent command parser before reporting a failure.
                LOGGER.warning(
                    "Native RPM inspection failed for %r; trying the rpm command fallback.",
                    file_path,
                    exc_info=True,
                )

    field_separator = "\x1f"
    query_format = (
        f"%{{NAME}}{field_separator}%{{VERSION}}{field_separator}"
        f"%{{RELEASE}}{field_separator}%{{ARCH}}{field_separator}"
        f"%{{SUMMARY}}{field_separator}%{{LICENSE}}{field_separator}"
        f"%{{URL}}{field_separator}%{{SIZE}}\n"
    )
    try:
        proc = subprocess.run(
            ["rpm", "-qp", "--queryformat", query_format, "--", file_path],
            capture_output=True,
            text=True,
            errors="replace",
            env=get_clean_env(),
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return fail(
            f"Could not read the RPM package header: {type(exc).__name__}: {exc}"
        )

    if proc.returncode != 0:
        detail = (
            proc.stderr.strip()
            or f"rpm package-header query exited with status {proc.returncode}."
        )
        return fail(f"Could not read the RPM package header: {detail}")

    records = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(records) != 1:
        return fail(
            "rpm package-header query returned an unexpected number of records."
        )
    parts = records[0].split(field_separator)
    if len(parts) != 8:
        return fail("rpm package-header query returned a malformed package record.")
    name, ver, rel, arch, summary, lic, url, size_str = parts
    name, ver, rel, arch = (value.strip() for value in (name, ver, rel, arch))
    if not all((name, ver, rel, arch)):
        return fail("rpm package-header query returned missing identity fields.")
    try:
        size = int(size_str.strip())
    except ValueError:
        return fail("rpm package-header query returned an invalid package size.")
    if size < 0:
        return fail("rpm package-header query returned a negative package size.")

    try:
        chk_proc = subprocess.run(
            [
                "rpm",
                "-q",
                "--queryformat",
                "%{VERSION}-%{RELEASE}\n",
                "--",
                name,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            env=get_clean_env(),
            check=False,
            timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return fail(
            "Could not check whether the local RPM is already installed: "
            f"{type(exc).__name__}: {exc}"
        )

    if chk_proc.returncode == 0:
        inst_ver = chk_proc.stdout.strip()
        if not inst_ver:
            return fail(
                "rpm reported that the package is installed but returned no installed version."
            )
        is_installed = True
    elif chk_proc.returncode == 1:
        inst_ver = ""
        is_installed = False
    else:
        detail = (
            chk_proc.stderr.strip()
            or f"rpm installed-version query exited with status {chk_proc.returncode}."
        )
        return fail(
            "Could not check whether the local RPM is already installed: " + detail
        )

    return LocalRpmInspectionResult(
        file_path=os.path.abspath(file_path),
        name=name,
        version=ver,
        release=rel,
        arch=arch,
        summary=summary,
        description="",
        license=lic,
        url=url,
        size_bytes=size,
        is_already_installed=is_installed,
        installed_version=inst_ver,
    )


class LocalRpmInspectionSignals(QObject):
    """Terminal outcomes for one local RPM header inspection."""

    inspected = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)


class LocalRpmInspectionWorker(QRunnable):
    """Inspect a local RPM without pausing the application event loop."""

    def __init__(self, file_path: str) -> None:
        super().__init__()
        self.file_path = file_path
        self.signals = LocalRpmInspectionSignals()
        self._is_cancelled = threading.Event()
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self) -> None:
        if self._is_cancelled.is_set():
            return
        try:
            result = inspect_local_rpm_file(self.file_path, raise_on_failure=True)
        except LocalRpmInspectionError as error:
            if not self._is_cancelled.is_set():
                self.signals.failed.emit(self.file_path, str(error))
        except (
            Exception
        ) as error:  # Preserve a terminal UI result for unexpected parser failures.
            if not self._is_cancelled.is_set():
                self.signals.failed.emit(
                    self.file_path, f"{type(error).__name__}: {error}"
                )
        else:
            if not self._is_cancelled.is_set():
                self.signals.inspected.emit(self.file_path, result)


class FileOwnerLookupError(RuntimeError):
    """RPM ownership inspection could not complete, rather than finding no owner."""


def find_package_owning_file(
    file_path: str, *, raise_on_failure: bool = False
) -> Optional[str]:
    """
    Resolve the installed RPM owner of ``file_path`` (``rpm -qf``).

    The default preserves the legacy optional-result API for callers that only
    need a best-effort answer. Interactive callers can request a typed failure
    so an unavailable RPM database is not presented as "no owner found".
    """
    if is_running_in_flatpak():
        if raise_on_failure:
            raise FileOwnerLookupError(
                flatpak_read_only_unavailable("File-owner lookup")
            )
        return None

    def fail(detail: str) -> None:
        LOGGER.warning("RPM file-owner lookup failed for %r: %s", file_path, detail)
        if raise_on_failure:
            raise FileOwnerLookupError(detail)

    clean_path = file_path.strip().removeprefix("file:").strip()
    if not clean_path.startswith("/"):
        return None

    clean_path = os.path.normpath(clean_path)
    real_path = os.path.realpath(clean_path)
    target_paths = {clean_path, real_path}
    candidate_basenames = [os.path.basename(real_path), os.path.basename(clean_path)]

    ts = create_rpm_transaction_set()
    if ts is not None:
        with RPM_GLOBAL_LOCK:
            matches = None
            try:
                # Query both real and symlink basenames (e.g. python vs python3.12)
                for base_name in dict.fromkeys(candidate_basenames):
                    matches = ts.dbMatch("basenames", base_name)
                    for hdr in matches:
                        dirs = [
                            _decode_rpm_str(d) for d in (hdr[rpm.RPMTAG_DIRNAMES] or [])
                        ]
                        bases = [
                            _decode_rpm_str(b)
                            for b in (hdr[rpm.RPMTAG_BASENAMES] or [])
                        ]
                        d_indices = hdr[rpm.RPMTAG_DIRINDEXES] or []

                        if len(d_indices) == len(bases):
                            for b, d_idx in zip(bases, d_indices):
                                if b == base_name and 0 <= d_idx < len(dirs):
                                    full_file = dirs[d_idx].rstrip("/") + "/" + b
                                    if full_file in target_paths:
                                        pkg_name = _decode_rpm_str(
                                            hdr[rpm.RPMTAG_NAME]
                                        ).strip()
                                        if pkg_name:
                                            return pkg_name
                                        raise RuntimeError(
                                            "Native RPM file-owner record is missing its package name."
                                        )
                        else:
                            files = [
                                _decode_rpm_str(f)
                                for f in (hdr[rpm.RPMTAG_FILENAMES] or [])
                            ]
                            if any(p in files for p in target_paths):
                                pkg_name = _decode_rpm_str(hdr[rpm.RPMTAG_NAME]).strip()
                                if pkg_name:
                                    return pkg_name
                                raise RuntimeError(
                                    "Native RPM file-owner record is missing its package name."
                                )
                    del matches
                    matches = None
            except Exception:
                # Native binding failures are recoverable here because rpm -qf
                # below provides an independent host-RPM fallback.
                LOGGER.warning(
                    "Native RPM ownership lookup failed for %r; trying rpm -qf.",
                    clean_path,
                    exc_info=True,
                )
            finally:
                if matches is not None:
                    del matches
                del ts

    # Subprocess fallback (rpm -qf). Exit status one is the documented
    # no-owner result; other statuses mean the RPM database could not be read.
    try:
        for query_target in dict.fromkeys((real_path, clean_path)):
            cmd = ["rpm", "-qf", "--queryformat", "%{NAME}\n", query_target]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=4,
            )
            if res.returncode == 1:
                continue
            if res.returncode != 0:
                fail(
                    res.stderr.strip()
                    or f"rpm file-owner query exited with status {res.returncode}."
                )
                return None
            lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            if (
                len(lines) == 1
                and not lines[0].startswith("error:")
                and RPM_PACKAGE_NAME_PATTERN.fullmatch(lines[0]) is not None
            ):
                return lines[0]
            fail(
                "rpm file-owner query succeeded without returning one valid package name."
            )
            return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"rpm file-owner query failed: {type(exc).__name__}: {exc}")

    return None


class FileOwnerLookupSignals(QObject):
    """Terminal outcomes for one potentially slow RPM file-owner lookup."""

    resolved = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)


class FileOwnerLookupWorker(QRunnable):
    """Resolve one file owner without blocking interactive search input."""

    def __init__(self, file_path: str) -> None:
        super().__init__()
        self.file_path = file_path
        self.signals = FileOwnerLookupSignals()
        self._is_cancelled = threading.Event()
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self) -> None:
        if self._is_cancelled.is_set():
            return
        try:
            owner = find_package_owning_file(self.file_path, raise_on_failure=True)
        except FileOwnerLookupError as error:
            if not self._is_cancelled.is_set():
                self.signals.failed.emit(self.file_path, str(error))
        except Exception as error:  # Every worker path must reach visible feedback.
            if not self._is_cancelled.is_set():
                self.signals.failed.emit(
                    self.file_path, f"{type(error).__name__}: {error}"
                )
        else:
            if not self._is_cancelled.is_set():
                self.signals.resolved.emit(self.file_path, owner or "")


# =============================================================================
# Two-Tier Capability & Provider Cache (L1 RAM + L2 SQLite WAL)
# =============================================================================


class SQLiteCapabilityCache:
    """Thread-safe L1 cache with an optional, failure-contained SQLite L2 cache."""

    _instance: Optional["SQLiteCapabilityCache"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._memory_cache: Dict[str, Tuple[bool, str]] = {}
        self._local_storage = threading.local()
        self._disk_cache_enabled = True
        self.db_path = ""
        try:
            cache_dir = os.path.expanduser("~/.cache/dendro")
            os.makedirs(cache_dir, exist_ok=True)
            # v6 starts a clean generation: v5 could persist a missing provider
            # as satisfied, so carrying that result into a corrected build would
            # still mislabel dependencies until a manual cache deletion.
            self.db_path = os.path.join(cache_dir, "capabilities_v6.db")
            self._init_db()
        except (OSError, sqlite3.Error):
            # Dependency inspection remains correct without persistence. A
            # read-only home, corrupt cache, or unavailable filesystem must not
            # crash the UI while constructing a worker.
            self._disk_cache_enabled = False
            LOGGER.warning(
                "Capability cache persistence is unavailable; using the in-memory cache only.",
                exc_info=True,
            )

    @classmethod
    def get_instance(cls) -> "SQLiteCapabilityCache":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _get_connection(self) -> sqlite3.Connection:
        if not self._disk_cache_enabled or not self.db_path:
            raise sqlite3.OperationalError("capability cache persistence is disabled")
        if not hasattr(self._local_storage, "conn") or self._local_storage.conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local_storage.conn = conn
        return self._local_storage.conn

    def _init_db(self) -> None:
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS capabilities (
                cap_name TEXT PRIMARY KEY,
                is_satisfied INTEGER,
                provider_name TEXT
            )
        """)
        conn.commit()

    def _disable_disk_cache(self, operation: str) -> None:
        """Switch permanently to L1 after a disk-cache failure in this process."""
        with self._lock:
            was_enabled = self._disk_cache_enabled
            self._disk_cache_enabled = False
        connection = getattr(self._local_storage, "conn", None)
        self._local_storage.conn = None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                LOGGER.warning(
                    "Could not close the failed capability-cache connection.",
                    exc_info=True,
                )
        if was_enabled:
            LOGGER.warning(
                "Capability cache %s failed; disabling persistent caching for this process.",
                operation,
                exc_info=True,
            )

    def get(self, cap_name: str) -> Optional[Tuple[bool, str]]:
        with self._lock:
            if cap_name in self._memory_cache:
                return self._memory_cache[cap_name]
            disk_cache_enabled = self._disk_cache_enabled
        if not disk_cache_enabled:
            return None

        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT is_satisfied, provider_name FROM capabilities WHERE cap_name = ?",
                (cap_name,),
            )
            row = cur.fetchone()
            if row:
                raw_satisfied, raw_provider = row
                if (
                    not isinstance(raw_satisfied, int)
                    or isinstance(raw_satisfied, bool)
                    or raw_satisfied not in {0, 1}
                    or not isinstance(raw_provider, str)
                    or not raw_provider
                ):
                    raise sqlite3.DatabaseError(
                        "capability cache contains an invalid provider record"
                    )
                result = (bool(raw_satisfied), raw_provider)
                with self._lock:
                    self._memory_cache[cap_name] = result
                return result
        except (OSError, sqlite3.Error):
            self._disable_disk_cache("read")
        return None

    def set_batch(self, items: List[Tuple[str, bool, str]]) -> None:
        if not items:
            return
        with self._lock:
            for name, satisfied, provider in items:
                self._memory_cache[name] = (satisfied, provider)
            disk_cache_enabled = self._disk_cache_enabled
        if not disk_cache_enabled:
            return

        try:
            conn = self._get_connection()
            conn.executemany(
                "INSERT OR REPLACE INTO capabilities (cap_name, is_satisfied, provider_name) VALUES (?, ?, ?)",
                [
                    (name, int(satisfied), provider)
                    for name, satisfied, provider in items
                ],
            )
            conn.commit()
        except (OSError, sqlite3.Error):
            self._disable_disk_cache("write")

    def invalidate(self) -> None:
        """Discard provider facts after a successful RPM transaction.

        A dependency's provider is a property of the installed RPM database, not
        a stable application setting. Both cache tiers must therefore be cleared
        after DNF changes that database.
        """
        with self._lock:
            self._memory_cache.clear()
            disk_cache_enabled = self._disk_cache_enabled
        if not disk_cache_enabled:
            return

        try:
            conn = self._get_connection()
            conn.execute("DELETE FROM capabilities")
            conn.commit()
        except (OSError, sqlite3.Error):
            self._disable_disk_cache("invalidation")


def classification_package_fields(decision: ClassificationDecision) -> Dict[str, Any]:
    """Adapt immutable taxonomy output to legacy :class:`PackageInfo` fields.

    Keeping this one adapter prevents native, CLI, and remote query paths from
    accidentally dropping provenance or diverging on feature flags.
    """
    return {
        "parent_pillar": decision.parent_pillar,
        "sub_category": decision.sub_category,
        "primary_category": decision.primary_category,
        "classification_confidence": decision.confidence,
        "classification_evidence": decision.evidence.value,
        "classification_input_scope": decision.input_scope.value,
        "classification_rationale": list(decision.rationale),
        "secondary_tags": list(decision.secondary_tags),
        **dict(decision.flags),
    }


# =============================================================================
# Unified Backend Signals
# =============================================================================


class BackendSignals(QObject):
    packages_loaded = pyqtSignal(list)
    orphans_loaded = pyqtSignal(set)
    userinstalled_loaded = pyqtSignal(set)
    dependencies_resolved = pyqtSignal(str, list, object)
    reverse_dependencies_resolved = pyqtSignal(str, list)
    package_files_loaded = pyqtSignal(str, list)
    package_details_loaded = pyqtSignal(object)
    history_loaded = pyqtSignal(list)
    dry_run_finished = pyqtSignal(object)
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)
    package_changelog_loaded = pyqtSignal(str, list)
    package_verification_finished = pyqtSignal(str, list)
    package_scriptlets_loaded = pyqtSignal(str, object)
    remote_search_finished = pyqtSignal(str, list)
    system_updates_loaded = pyqtSignal(dict)
    repo_list_loaded = pyqtSignal(list)


# =============================================================================
# Worker: System Package Discovery
# =============================================================================


class PackageQueryWorker(QRunnable):
    def __init__(self, category: str = "all", search_query: str = ""):
        super().__init__()
        self.signals = BackendSignals()
        self.category = category.lower()
        self.search_query = search_query.strip().lower()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        if is_running_in_flatpak():
            self.signals.packages_loaded.emit([])
            self.signals.status_update.emit(
                flatpak_read_only_unavailable("Installed-package browsing")
            )
            return

        import time

        t_start = time.time()
        try:
            self.signals.status_update.emit(
                "Scanning AppStream catalog, comps groups & RPM database..."
            )

            t0 = time.time()
            appstream = AppStreamCatalog.get_instance()
            t_appstream = time.time() - t0

            t0 = time.time()
            comps = FedoraCompsCatalog.get_instance()
            t_comps = time.time() - t0

            t0 = time.time()
            if HAS_NATIVE_RPM and not is_running_in_flatpak():
                packages = self._query_native_librpm(appstream, comps)
            else:
                packages = self._query_cli_subprocess(appstream, comps)
            t_rpm = time.time() - t0

            if self._is_cancelled.is_set():
                return

            packages.sort(key=lambda p: p.name.lower())

            if os.environ.get("DENDRO_DEBUG"):
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                print(
                    f"[{ts}] [Dendro:debug] Startup timings -> AppStream: {t_appstream:.2f}s | Comps: {t_comps:.2f}s | RPM DB: {t_rpm:.2f}s | Total: {time.time() - t_start:.2f}s ({len(packages)} pkgs)",
                    file=sys.stderr,
                )

            self.signals.packages_loaded.emit(packages)
            self.signals.status_update.emit(
                f"Loaded {len(packages):,} packages successfully."
            )

        except Exception as ex:
            self.signals.error_occurred.emit("", f"Failed to query database: {str(ex)}")

    def _query_native_librpm(
        self, appstream: AppStreamCatalog, comps: FedoraCompsCatalog
    ) -> List[PackageInfo]:
        packages: List[PackageInfo] = []
        if not HAS_NATIVE_RPM or is_running_in_flatpak():
            return self._query_cli_subprocess(appstream, comps)

        protected_pkgs = get_system_protected_packages()
        native_query_failed = False
        with RPM_GLOBAL_LOCK:
            ts = None
            match_iterator = None
            try:
                ts = rpm.TransactionSet()
                if hasattr(rpm, "_RPMVSF_NOSIGNATURES"):
                    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
                match_iterator = ts.dbMatch()
                for header in match_iterator:
                    if self._is_cancelled.is_set():
                        break

                    name = _decode_rpm_str(header[rpm.RPMTAG_NAME])
                    if not name:
                        raise RuntimeError(
                            "Native RPM scan returned a package record with missing identity fields."
                        )
                    ver = _decode_rpm_str(header[rpm.RPMTAG_VERSION])
                    rel = _decode_rpm_str(header[rpm.RPMTAG_RELEASE])
                    arch = _decode_rpm_str(header[rpm.RPMTAG_ARCH])
                    if not all((ver, rel, arch)):
                        raise RuntimeError(
                            "Native RPM scan returned a package record with missing identity fields."
                        )
                    raw_epoch = header[rpm.RPMTAG_EPOCH]
                    epoch_str = str(raw_epoch) if raw_epoch is not None else "0"
                    group = _decode_rpm_str(header[rpm.RPMTAG_GROUP]) or "General"
                    summary = _decode_rpm_str(header[rpm.RPMTAG_SUMMARY])
                    description = _decode_rpm_str(header[rpm.RPMTAG_DESCRIPTION])
                    license_str = _decode_rpm_str(header[rpm.RPMTAG_LICENSE])
                    url = _decode_rpm_str(header[rpm.RPMTAG_URL])
                    packager = _decode_rpm_str(header[rpm.RPMTAG_PACKAGER])
                    vendor = _decode_rpm_str(header[rpm.RPMTAG_VENDOR])

                    b_time_raw = header[rpm.RPMTAG_BUILDTIME]
                    build_time = ""
                    if b_time_raw and b_time_raw > 0:
                        try:
                            build_time = datetime.fromtimestamp(b_time_raw).strftime(
                                "%Y-%m-%d %H:%M"
                            )
                        except (OSError, ValueError, OverflowError):
                            build_time = ""

                    i_time_raw = header[rpm.RPMTAG_INSTALLTIME]
                    install_time = ""
                    if i_time_raw and i_time_raw > 0:
                        try:
                            install_time = datetime.fromtimestamp(i_time_raw).strftime(
                                "%Y-%m-%d %H:%M"
                            )
                        except (OSError, ValueError, OverflowError):
                            install_time = ""

                    size_bytes = int(header[rpm.RPMTAG_SIZE] or 0)
                    if size_bytes < 0:
                        raise RuntimeError(
                            "Native RPM scan returned a package record with a negative installed size."
                        )
                    raw_dirs = [
                        _decode_rpm_str(d) for d in (header[rpm.RPMTAG_DIRNAMES] or [])
                    ]
                    raw_provs = [
                        _decode_rpm_str(p)
                        for p in (header[rpm.RPMTAG_PROVIDENAME] or [])
                    ]
                    raw_basenames = [
                        _decode_rpm_str(b) for b in (header[rpm.RPMTAG_BASENAMES] or [])
                    ]

                    anatomy = PackagePhysicalAnatomy.from_manifest_data(
                        raw_dirs, raw_provs, raw_basenames
                    )

                    owned_desktop_files = [
                        b for b in raw_basenames if b.endswith(".desktop")
                    ]
                    for did in anatomy.provided_desktop_ids:
                        if did not in owned_desktop_files:
                            owned_desktop_files.append(did)

                    # Check if package has real requirements (ignoring rpmlib/config/rtld internals)
                    raw_reqs = [
                        _decode_rpm_str(r)
                        for r in (header[rpm.RPMTAG_REQUIRENAME] or [])
                    ]
                    has_real_deps = any(
                        not r.startswith(("rpmlib(", "config(", "rtld("))
                        for r in raw_reqs
                    )

                    repo = "Fedora Project"
                    packager_lower = packager.lower()
                    vendor_lower = vendor.lower()
                    if "copr" in packager_lower or "copr" in vendor_lower:
                        repo = "COPR Repository"
                    elif "rpmfusion" in packager_lower or "rpmfusion" in vendor_lower:
                        repo = "RPM Fusion"
                    elif vendor:
                        repo = vendor

                    decision = ProductionTaxonomyEngine.classify(
                        name=name,
                        summary=summary,
                        description=description,
                        anatomy=anatomy,
                        desktop_entry_files=owned_desktop_files,
                        provides=raw_provs,
                        appstream=appstream,
                        comps=comps,
                        vendor=vendor,
                        packager=packager,
                    )

                    packages.append(
                        PackageInfo(
                            name=name,
                            version=ver,
                            release=rel,
                            arch=arch,
                            epoch=epoch_str,
                            summary=summary,
                            description=description,
                            license=license_str,
                            url=url,
                            packager=packager,
                            vendor=vendor,
                            build_time=build_time,
                            install_time=install_time,
                            group=group,
                            size_bytes=size_bytes,
                            state=PackageState.INSTALLED,
                            repository=repo,
                            is_orphan=False,
                            is_user_installed=False,
                            is_protected=(
                                name.lower() in protected_pkgs
                                or name in FEDORA_SYSTEM_ROOT_PILLARS
                            ),
                            has_dependencies=has_real_deps,
                            dependencies_loaded=(not has_real_deps),
                            **classification_package_fields(decision),
                        )
                    )
            except Exception:
                # A partial native scan is not a valid package inventory. Do
                # not display it as successful; retry the complete query via
                # the independent rpm command implementation instead.
                native_query_failed = True
                LOGGER.warning(
                    "Native RPM database scan failed; discarding its partial result and trying rpm -qa.",
                    exc_info=True,
                )
            finally:
                if match_iterator is not None:
                    del match_iterator
                if ts is not None:
                    del ts

        if self._is_cancelled.is_set():
            return []
        if native_query_failed:
            return self._query_cli_subprocess(appstream, comps)
        return packages

    def _query_cli_subprocess(
        self, appstream: AppStreamCatalog, comps: FedoraCompsCatalog
    ) -> List[PackageInfo]:
        if is_running_in_flatpak():
            raise RuntimeError(
                flatpak_read_only_unavailable("Installed-package browsing")
            )
        protected_pkgs = get_system_protected_packages()
        # Use an ASCII control separator rather than a printable character so
        # arbitrary package summaries cannot silently shift later fields.
        field_separator = "\x1f"
        query_format = (
            f"%{{NAME}}{field_separator}%{{VERSION}}{field_separator}%{{RELEASE}}{field_separator}"
            f"%{{ARCH}}{field_separator}%{{GROUP}}{field_separator}%{{SIZE}}{field_separator}"
            f"%{{LICENSE}}{field_separator}%{{URL}}{field_separator}%{{PACKAGER}}{field_separator}"
            f"%{{VENDOR}}{field_separator}%{{INSTALLTIME:date}}{field_separator}%{{SUMMARY}}{field_separator}"
            f"[%{{DIRNAMES}};]{field_separator}[%{{PROVIDENAME}};]{field_separator}[%{{BASENAMES}};]\n"
        )
        cmd = ["rpm", "-qa", "--queryformat", query_format]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            env=get_clean_env(),
            check=False,
            timeout=30,
        )

        if proc.returncode != 0:
            raise RuntimeError(f"RPM query failed: {proc.stderr}")

        packages: List[PackageInfo] = []
        for line in proc.stdout.splitlines():
            if self._is_cancelled.is_set():
                return []
            if not line.strip():
                continue

            parts = line.split(field_separator)
            if len(parts) != 15:
                raise RuntimeError(
                    "RPM query returned a malformed package record; refusing to display a partial inventory."
                )

            (
                name,
                ver,
                rel,
                arch,
                group,
                size_str,
                license_str,
                url,
                packager,
                vendor,
                inst_time,
                summary,
                raw_dirs_field,
                raw_provs_field,
                raw_base_field,
            ) = parts
            if not all((name, ver, rel, arch)):
                raise RuntimeError(
                    "RPM query returned a package record with missing identity fields."
                )
            try:
                size_bytes = int(size_str)
            except ValueError as exc:
                raise RuntimeError(
                    "RPM query returned a package record with an invalid installed size."
                ) from exc
            if size_bytes < 0:
                raise RuntimeError(
                    "RPM query returned a package record with a negative installed size."
                )

            repo = "Fedora Project"
            packager_lower = packager.lower()
            vendor_lower = vendor.lower()
            if "copr" in packager_lower or "copr" in vendor_lower:
                repo = "COPR Repository"
            elif "rpmfusion" in packager_lower or "rpmfusion" in vendor_lower:
                repo = "RPM Fusion"
            elif vendor:
                repo = vendor

            raw_dirs = raw_dirs_field.split(";")
            raw_provs = raw_provs_field.split(";")
            raw_base = raw_base_field.split(";")

            anatomy = PackagePhysicalAnatomy.from_manifest_data(
                raw_dirs, raw_provs, raw_base
            )

            owned_desktop_files = [b for b in raw_base if b.endswith(".desktop")]
            for did in anatomy.provided_desktop_ids:
                if did not in owned_desktop_files:
                    owned_desktop_files.append(did)

            if not owned_desktop_files and not anatomy.provided_desktop_ids:
                anatomy.has_desktop_file = False

            decision = ProductionTaxonomyEngine.classify(
                name=name,
                summary=summary,
                description="",
                anatomy=anatomy,
                desktop_entry_files=owned_desktop_files,
                provides=raw_provs,
                appstream=appstream,
                comps=comps,
                vendor=vendor,
                packager=packager,
            )

            packages.append(
                PackageInfo(
                    name=name,
                    version=ver,
                    release=rel,
                    arch=arch,
                    summary=summary,
                    license=license_str,
                    url=url,
                    packager=packager,
                    vendor=vendor,
                    install_time=inst_time,
                    group=group or "General",
                    size_bytes=size_bytes,
                    state=PackageState.INSTALLED,
                    repository=repo,
                    is_orphan=False,
                    is_user_installed=False,
                    is_protected=(
                        name.lower() in protected_pkgs
                        or name in FEDORA_SYSTEM_ROOT_PILLARS
                    ),
                    **classification_package_fields(decision),
                )
            )

        return packages


# =============================================================================
# Worker: User-Installed Query (Subprocess-Safe Engine)
# =============================================================================


class UserInstalledQueryWorker(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = BackendSignals()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        dnf_bin = get_dnf_binary_path()
        if not dnf_bin:
            if not self._is_cancelled.is_set():
                detail = (
                    flatpak_read_only_unavailable("User-installed package detection")
                    if is_running_in_flatpak()
                    else "User-installed package detection requires an installed dnf5 or dnf command on this RPM host."
                )
                self.signals.error_occurred.emit("", detail)
            return

        try:
            # Explicit newline in format ensures DNF5 outputs one line per package
            cmd = [
                dnf_bin,
                "repoquery",
                "--userinstalled",
                "-q",
                "--queryformat",
                "%{name}\n",
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=35,
            )
            if self._is_cancelled.is_set():
                return
            if res.returncode != 0:
                detail = (
                    res.stderr.strip()
                    or f"{dnf_bin} repoquery exited with status {res.returncode}."
                )
                self.signals.error_occurred.emit(
                    "", f"Could not identify user-installed packages: {detail}"
                )
                return
            user_pkgs = _parse_dnf_package_name_output(
                res.stdout, "DNF user-installed package query"
            )
            self.signals.userinstalled_loaded.emit(user_pkgs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Could not identify user-installed packages: {type(exc).__name__}: {exc}",
                )
        except Exception as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Could not identify user-installed packages: {type(exc).__name__}: {exc}",
                )


# =============================================================================
# Worker: Leaf / Orphan Packages Query (Subprocess-Safe Engine)
# =============================================================================


class OrphanQueryWorker(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = BackendSignals()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        dnf_bin = get_dnf_binary_path()
        if not dnf_bin:
            if not self._is_cancelled.is_set():
                detail = (
                    flatpak_read_only_unavailable("Unneeded-package detection")
                    if is_running_in_flatpak()
                    else "Unneeded-package detection requires an installed dnf5 or dnf command on this RPM host."
                )
                self.signals.error_occurred.emit("", detail)
            return

        try:
            # Explicit newline in format ensures DNF5 outputs one line per package
            cmd = [
                dnf_bin,
                "repoquery",
                "--unneeded",
                "--installed",
                "-q",
                "--queryformat",
                "%{name}\n",
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=35,
            )
            if self._is_cancelled.is_set():
                return
            if res.returncode != 0:
                detail = (
                    res.stderr.strip()
                    or f"{dnf_bin} repoquery exited with status {res.returncode}."
                )
                self.signals.error_occurred.emit(
                    "", f"Could not identify unneeded packages: {detail}"
                )
                return
            orphans = _parse_dnf_package_name_output(
                res.stdout, "DNF unneeded-package query"
            )
            self.signals.orphans_loaded.emit(orphans)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Could not identify unneeded packages: {type(exc).__name__}: {exc}",
                )
        except Exception as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Could not identify unneeded packages: {type(exc).__name__}: {exc}",
                )


# =============================================================================
# Worker: Direct Dependency Tree Hierarchy
# =============================================================================


class DependencyTreeWorker(QRunnable):
    def __init__(
        self, root_package: str, max_depth: int = 1, target_index: Optional[Any] = None
    ):
        super().__init__()
        self.signals = BackendSignals()
        self.root_package = root_package
        self.max_depth = max_depth
        self.target_index = target_index
        self._is_cancelled = threading.Event()
        self.cache = SQLiteCapabilityCache.get_instance()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        if is_running_in_flatpak():
            self.signals.error_occurred.emit(
                self.root_package,
                flatpak_read_only_unavailable("Dependency inspection"),
            )
            return

        ts = create_rpm_transaction_set()
        try:
            raw_reqs, parsed_reqs = self._fetch_package_requires(self.root_package, ts)
            if not parsed_reqs or self._is_cancelled.is_set():
                self.signals.dependencies_resolved.emit(
                    self.root_package, [], self.target_index
                )
                return

            caps_to_query: List[str] = []
            for _, cap_name, _ in parsed_reqs:
                cached = self.cache.get(cap_name)
                if cached is None:
                    caps_to_query.append(cap_name)

            if caps_to_query and not self._is_cancelled.is_set():
                self._resolve_capabilities_batch(caps_to_query, ts)

            resolved_nodes: List[DependencyNode] = []
            seen_clean_names: Set[str] = {self.root_package}

            # Query installed version for resolved providers
            provider_versions: Dict[str, str] = {}
            if ts is not None:
                with RPM_GLOBAL_LOCK:
                    for _, cap_name, _ in parsed_reqs:
                        cached = self.cache.get(cap_name)
                        p_name = cached[1] if cached else cap_name
                        if p_name and p_name not in provider_versions:
                            match = None
                            try:
                                match = ts.dbMatch("name", p_name)
                                for hdr in match:
                                    v = _decode_rpm_str(hdr[rpm.RPMTAG_VERSION])
                                    r = _decode_rpm_str(hdr[rpm.RPMTAG_RELEASE])
                                    provider_versions[p_name] = f"{v}-{r}" if r else v
                                    break
                            finally:
                                del match

            for raw_req, cap_name, constraint in parsed_reqs:
                if self._is_cancelled.is_set():
                    return

                cached = self.cache.get(cap_name)
                # A failed or cancelled lookup is not evidence that a dependency
                # is installed. Show it as unresolved until a provider is known.
                is_sat, provider = cached if cached else (False, cap_name)

                if provider in seen_clean_names:
                    continue
                seen_clean_names.add(provider)

                inst_ver = provider_versions.get(provider, "")

                resolved_nodes.append(
                    DependencyNode(
                        raw_requirement=raw_req,
                        resolved_package_name=provider,
                        version_constraint=constraint,
                        is_satisfied=is_sat,
                        is_cycle=False,
                        installed_version=inst_ver,
                    )
                )

            if not self._is_cancelled.is_set():
                self.signals.dependencies_resolved.emit(
                    self.root_package, resolved_nodes, self.target_index
                )

        except Exception as ex:
            self.signals.error_occurred.emit(
                self.root_package, f"Dependency error: {str(ex)}"
            )
        finally:
            if ts is not None:
                del ts

    def _fetch_package_requires(
        self, pkg_name: str, ts: Optional[object]
    ) -> Tuple[List[str], List[Tuple[str, str, str]]]:
        raw_reqs: List[str] = []
        parsed_reqs: List[Tuple[str, str, str]] = []

        if ts is not None:
            package_found = False
            with RPM_GLOBAL_LOCK:
                match = None
                try:
                    match = ts.dbMatch("name", pkg_name)
                    for hdr in match:
                        package_found = True
                        requires = hdr[rpm.RPMTAG_REQUIRENAME] or []
                        flags = hdr[rpm.RPMTAG_REQUIREFLAGS] or []
                        versions = hdr[rpm.RPMTAG_REQUIREVERSION] or []

                        for i, req in enumerate(requires):
                            req_str = _decode_rpm_str(req)
                            raw_reqs.append(req_str)

                            if req_str.startswith(("rpmlib(", "config(", "/", "rtld(")):
                                continue

                            constraint = ""
                            if i < len(flags) and i < len(versions) and versions[i]:
                                flag = flags[i]
                                op = ""
                                if (flag & rpm.RPMSENSE_LESS) and (
                                    flag & rpm.RPMSENSE_EQUAL
                                ):
                                    op = "<="
                                elif (flag & rpm.RPMSENSE_GREATER) and (
                                    flag & rpm.RPMSENSE_EQUAL
                                ):
                                    op = ">="
                                elif flag & rpm.RPMSENSE_LESS:
                                    op = "<"
                                elif flag & rpm.RPMSENSE_GREATER:
                                    op = ">"
                                elif flag & rpm.RPMSENSE_EQUAL:
                                    op = "="

                                ver_str = _decode_rpm_str(versions[i])
                                if op and ver_str:
                                    constraint = f"{op} {ver_str}"

                            parsed_reqs.append((req_str, req_str, constraint))
                        break
                finally:
                    del match
            if not package_found:
                raise LookupError(f"Package '{pkg_name}' is not installed.")
        else:
            cmd = ["rpm", "-qR", pkg_name]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=5,
            )
            if proc.returncode != 0:
                detail = (
                    proc.stderr.strip()
                    or f"rpm requirement query exited with status {proc.returncode}."
                )
                raise RuntimeError(
                    f"Could not read requirements for '{pkg_name}': {detail}"
                )
            raw_reqs = proc.stdout.splitlines()

            for req in raw_reqs:
                req = req.strip()
                if not req or req.startswith(("rpmlib(", "config(", "/", "rtld(")):
                    continue

                tokens = re.split(r"([<>=]+)", req, maxsplit=1)
                cap_name = tokens[0].strip()
                constraint = (tokens[1] + tokens[2]) if len(tokens) == 3 else ""
                parsed_reqs.append((req, cap_name, constraint))

        return raw_reqs, parsed_reqs

    def _resolve_capabilities_batch(
        self, capabilities: List[str], ts: Optional[object]
    ):
        batch_results: List[Tuple[str, bool, str]] = []
        if ts is not None:
            with RPM_GLOBAL_LOCK:
                for cap in capabilities:
                    match_name = None
                    try:
                        match_name = ts.dbMatch("name", cap)
                        if match_name.count() > 0:
                            batch_results.append((cap, True, cap))
                            continue
                    finally:
                        del match_name

                    matches = None
                    provider = None
                    try:
                        matches = ts.dbMatch("providename", cap)
                        for hdr in matches:
                            provider = _decode_rpm_str(hdr[rpm.RPMTAG_NAME])
                            break
                    finally:
                        del matches

                    if provider:
                        batch_results.append((cap, True, provider))
                    else:
                        clean_name = re.sub(r"\.so(\.[0-9]+)*(\([^\)]*\))?$", "", cap)
                        batch_results.append((cap, False, clean_name))
        else:
            # Query capabilities individually in fallback mode to prevent multi-provider off-by-one desynchronization
            clean_env = get_clean_env()
            for cap in capabilities:
                if self._is_cancelled.is_set():
                    return
                cmd = ["rpm", "-q", "--whatprovides", "--queryformat", "%{NAME}\n", cap]
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=clean_env,
                    check=False,
                    timeout=4,
                )
                first_line = (
                    proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
                )
                if proc.returncode == 0:
                    if not first_line:
                        raise RuntimeError(
                            f"rpm provider query returned no provider for '{cap}'."
                        )
                    batch_results.append((cap, True, first_line.strip()))
                elif proc.returncode == 1:
                    clean_name = re.sub(r"\.so(\.[0-9]+)*(\([^\)]*\))?$", "", cap)
                    batch_results.append((cap, False, clean_name))
                else:
                    detail = (
                        proc.stderr.strip()
                        or f"rpm provider query exited with status {proc.returncode}."
                    )
                    raise RuntimeError(
                        f"Could not resolve provider for '{cap}': {detail}"
                    )

        self.cache.set_batch(batch_results)


# =============================================================================
# Worker: Reverse Dependencies
# =============================================================================


class ReverseDependencyWorker(QRunnable):
    def __init__(self, target_package: str):
        super().__init__()
        self.signals = BackendSignals()
        self.target_package = target_package
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        if is_running_in_flatpak():
            self.signals.error_occurred.emit(
                self.target_package,
                flatpak_read_only_unavailable("Reverse-dependency inspection"),
            )
            return

        try:
            self.signals.status_update.emit(
                f"Finding packages that depend on '{self.target_package}'..."
            )

            ts = create_rpm_transaction_set()
            reverse_nodes: List[DependencyNode] = []
            seen: Set[str] = set()

            if ts is not None:
                with RPM_GLOBAL_LOCK:
                    capabilities: List[str] = [self.target_package]
                    target_found = False
                    target_match = None
                    try:
                        target_match = ts.dbMatch("name", self.target_package)
                        for hdr in target_match:
                            target_found = True
                            raw_provs = hdr[rpm.RPMTAG_PROVIDENAME] or []
                            for p in raw_provs:
                                p_str = _decode_rpm_str(p)
                                if p_str and not p_str.startswith(
                                    ("rpmlib(", "config(")
                                ):
                                    capabilities.append(p_str)
                            break
                    finally:
                        del target_match
                    if not target_found:
                        raise LookupError(
                            f"Package '{self.target_package}' is not installed."
                        )

                    for cap in set(capabilities):
                        if self._is_cancelled.is_set():
                            del ts
                            return

                        matches = None
                        try:
                            matches = ts.dbMatch("requirename", cap)
                            for hdr in matches:
                                if self._is_cancelled.is_set():
                                    break

                                pkg_name = _decode_rpm_str(hdr[rpm.RPMTAG_NAME])
                                if (
                                    pkg_name
                                    and pkg_name != self.target_package
                                    and pkg_name not in seen
                                ):
                                    seen.add(pkg_name)
                                    reverse_nodes.append(
                                        DependencyNode(
                                            raw_requirement=cap,
                                            resolved_package_name=pkg_name,
                                            is_satisfied=True,
                                            is_reverse=True,
                                        )
                                    )
                        finally:
                            del matches

                        if self._is_cancelled.is_set():
                            del ts
                            return
                    del ts
            else:
                caps_cmd = ["rpm", "-q", "--provides", self.target_package]
                caps_proc = subprocess.run(
                    caps_cmd,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=get_clean_env(),
                    check=False,
                    timeout=8,
                )
                if caps_proc.returncode != 0:
                    detail = (
                        caps_proc.stderr.strip()
                        or f"rpm capability query exited with status {caps_proc.returncode}."
                    )
                    raise RuntimeError(
                        f"Could not read provided capabilities for '{self.target_package}': {detail}"
                    )

                caps_to_check = [self.target_package]
                for line in caps_proc.stdout.splitlines():
                    c_clean = line.split("=")[0].strip()
                    if c_clean and not c_clean.startswith(("rpmlib(", "config(")):
                        caps_to_check.append(c_clean)

                for cap in set(caps_to_check):
                    if self._is_cancelled.is_set():
                        return
                    cmd = [
                        "rpm",
                        "-q",
                        "--whatrequires",
                        "--queryformat",
                        "%{NAME}\\n",
                        cap,
                    ]
                    res = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        errors="replace",
                        env=get_clean_env(),
                        check=False,
                        timeout=6,
                    )
                    if res.returncode not in {0, 1}:
                        detail = (
                            res.stderr.strip()
                            or f"rpm reverse-dependency query exited with status {res.returncode}."
                        )
                        raise RuntimeError(
                            f"Could not find dependents for '{cap}': {detail}"
                        )
                    if res.returncode == 0 and not self._is_cancelled.is_set():
                        for line in res.stdout.splitlines():
                            package_name = line.strip()
                            if not package_name:
                                continue
                            if RPM_PACKAGE_NAME_PATTERN.fullmatch(package_name) is None:
                                raise RuntimeError(
                                    "rpm reverse-dependency query returned an invalid package-name record: "
                                    + line
                                )
                            if (
                                package_name not in seen
                                and package_name != self.target_package
                            ):
                                seen.add(package_name)
                                reverse_nodes.append(
                                    DependencyNode(
                                        raw_requirement=cap,
                                        resolved_package_name=package_name,
                                        is_satisfied=True,
                                        is_reverse=True,
                                    )
                                )

            if not self._is_cancelled.is_set():
                self.signals.reverse_dependencies_resolved.emit(
                    self.target_package, reverse_nodes
                )
                self.signals.status_update.emit(
                    f"Found {len(reverse_nodes)} dependents for '{self.target_package}'."
                )

        except Exception as ex:
            self.signals.error_occurred.emit(
                self.target_package, f"Reverse dependency error: {str(ex)}"
            )


# =============================================================================
# Worker: Package File Hierarchy Inspector
# =============================================================================


class PackageFilesWorker(QRunnable):
    def __init__(self, package_name: str):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        if is_running_in_flatpak():
            self.signals.error_occurred.emit(
                self.package_name,
                flatpak_read_only_unavailable("Installed-package file inspection"),
            )
            return

        try:
            files: List[PackageFileInfo] = []
            ts = create_rpm_transaction_set()

            if ts is not None:
                package_found = False
                with RPM_GLOBAL_LOCK:
                    matches = None
                    try:
                        matches = ts.dbMatch("name", self.package_name)
                        for hdr in matches:
                            package_found = True
                            if self._is_cancelled.is_set():
                                return

                            filenames = hdr[rpm.RPMTAG_FILENAMES] or []
                            filesizes = hdr[rpm.RPMTAG_FILESIZES] or []
                            filemodes = hdr[rpm.RPMTAG_FILEMODES] or []
                            if len(filesizes) != len(filenames) or len(
                                filemodes
                            ) != len(filenames):
                                raise RuntimeError(
                                    "Native RPM file manifest has mismatched path, size, or mode fields."
                                )

                            for i, raw_path in enumerate(filenames):
                                path = _decode_rpm_str(raw_path)
                                if not path.startswith("/"):
                                    raise RuntimeError(
                                        "Native RPM file manifest contains an invalid path."
                                    )
                                size = int(filesizes[i])
                                if size < 0:
                                    raise RuntimeError(
                                        "Native RPM file manifest contains a negative file size."
                                    )
                                mode_int = int(filemodes[i])
                                mode_octal = oct(mode_int)

                                is_dir = bool(mode_int & 0o040000) or (
                                    size == 0 and not os.path.splitext(path)[1]
                                )
                                is_config = path.startswith("/etc/")
                                is_executable = bool(mode_int & 0o111) or (
                                    "/bin/" in path or "/sbin/" in path
                                )

                                files.append(
                                    PackageFileInfo(
                                        path=path,
                                        size_bytes=size,
                                        mode=mode_octal,
                                        is_dir=is_dir,
                                        is_config=is_config,
                                        is_executable=is_executable,
                                    )
                                )
                            break
                    finally:
                        del matches
                        del ts
                if not package_found:
                    raise LookupError(
                        f"Package '{self.package_name}' is not installed."
                    )
            else:
                cmd = ["rpm", "-ql", "--dump", self.package_name]
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=get_clean_env(),
                    check=False,
                    timeout=10,
                )

                if res.returncode != 0:
                    detail = (
                        res.stderr.strip()
                        or f"rpm file-list query exited with status {res.returncode}."
                    )
                    raise RuntimeError(
                        f"Could not read files for '{self.package_name}': {detail}"
                    )
                if not self._is_cancelled.is_set():
                    for line in res.stdout.splitlines():
                        if not line.strip():
                            continue
                        parts = line.split()
                        if len(parts) < 6:
                            raise RuntimeError(
                                "rpm file-list query returned a malformed file record; "
                                "refusing to display a partial manifest."
                            )
                        path = parts[0]
                        if not path.startswith("/"):
                            raise RuntimeError(
                                "rpm file-list query returned a file record with an invalid path."
                            )
                        size = int(parts[1])
                        if size < 0:
                            raise RuntimeError(
                                "rpm file-list query returned a file record with a negative size."
                            )
                        is_dir = size == 0 and not os.path.splitext(path)[1]
                        is_config = path.startswith("/etc/")
                        is_executable = "/bin/" in path or "/sbin/" in path

                        files.append(
                            PackageFileInfo(
                                path=path,
                                size_bytes=size,
                                mode=parts[4],
                                is_dir=is_dir,
                                is_config=is_config,
                                is_executable=is_executable,
                            )
                        )

            if not self._is_cancelled.is_set():
                self.signals.package_files_loaded.emit(self.package_name, files)

        except Exception as ex:
            self.signals.error_occurred.emit(
                self.package_name, f"File query error: {str(ex)}"
            )


# =============================================================================
# Worker: DNF Transaction History (Subprocess-Safe Engine)
# =============================================================================


class DnfHistoryWorker(QRunnable):
    """Read DNF history only on an RPM host; Flatpak has no host CLI bridge."""

    def __init__(self):
        super().__init__()
        self.signals = BackendSignals()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        if is_running_in_flatpak():
            self.signals.error_occurred.emit(
                "", flatpak_read_only_unavailable("DNF history")
            )
            return

        dnf_bin = get_dnf_binary_path()
        if not dnf_bin:
            self.signals.error_occurred.emit(
                "",
                "DNF history requires an installed dnf5 or dnf command on this RPM host.",
            )
            return

        try:
            res = subprocess.run(
                [dnf_bin, "history", "list"],
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "", f"Could not read DNF history: {type(exc).__name__}: {exc}"
                )
            return

        if self._is_cancelled.is_set():
            return
        if res.returncode != 0:
            detail = (
                res.stderr.strip()
                or f"{dnf_bin} history list exited with status {res.returncode}."
            )
            self.signals.error_occurred.emit(
                "", f"Could not read DNF history: {detail}"
            )
            return

        history_list: List[HistoryEntry] = []
        malformed_line = ""
        for raw_line in res.stdout.splitlines():
            if self._is_cancelled.is_set():
                return
            line = raw_line.strip()
            if not line:
                continue
            normalized = line.casefold()
            if (
                "command line" in normalized
                and "date and time" in normalized
                and "action" in normalized
                and "altered" in normalized
            ):
                continue
            if all(character in "-|+ " for character in line):
                continue
            if normalized.startswith("no transactions"):
                continue

            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 5 or not parts[0].isdigit():
                malformed_line = line
                break
            try:
                altered_count = int(parts[4])
            except ValueError:
                malformed_line = line
                break
            if altered_count < 0:
                malformed_line = line
                break
            history_list.append(
                HistoryEntry(
                    id=int(parts[0]),
                    command_line=parts[1],
                    date_time=parts[2],
                    action=parts[3],
                    altered_count=altered_count,
                    return_code=0,
                )
            )

        if malformed_line:
            self.signals.error_occurred.emit(
                "",
                "Could not read DNF history: dnf history returned an unrecognized "
                f"record: {malformed_line}",
            )
            return
        self.signals.history_loaded.emit(history_list)


# =============================================================================
# Transaction planning is implemented by core.dnf5daemon.Dnf5DaemonClient.
# =============================================================================


# =============================================================================
# Worker: Package Changelog Extractor
# =============================================================================


class PackageChangelogWorker(QRunnable):
    """Read installed-package changelogs without hiding RPM or I/O failures."""

    def __init__(self, package_name: str):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name
        self._cve_pattern = CVE_REGEX_PATTERN

    @pyqtSlot()
    def run(self):
        if is_running_in_flatpak():
            self.signals.error_occurred.emit(
                self.package_name,
                flatpak_read_only_unavailable("Package changelog inspection"),
            )
            return

        entries: List[PackageChangelogEntry] = []
        failures: List[str] = []
        ts = create_rpm_transaction_set()
        if ts is not None:
            matches = None
            try:
                with RPM_GLOBAL_LOCK:
                    matches = ts.dbMatch("name", self.package_name)
                    for hdr in matches:
                        names = hdr[rpm.RPMTAG_CHANGELOGNAME] or []
                        times = hdr[rpm.RPMTAG_CHANGELOGTIME] or []
                        texts = hdr[rpm.RPMTAG_CHANGELOGTEXT] or []
                        for index, raw_author in enumerate(names):
                            timestamp = int(times[index]) if index < len(times) else 0
                            text = (
                                _decode_rpm_str(texts[index])
                                if index < len(texts)
                                else ""
                            )
                            entries.append(
                                PackageChangelogEntry(
                                    author=_decode_rpm_str(raw_author),
                                    timestamp=timestamp,
                                    date_str=datetime.fromtimestamp(timestamp).strftime(
                                        "%Y-%m-%d"
                                    )
                                    if timestamp
                                    else "",
                                    text=text,
                                    cves=sorted(set(self._cve_pattern.findall(text))),
                                )
                            )
                        break
            except Exception as exc:
                failures.append(
                    f"native RPM changelog query failed: {type(exc).__name__}: {exc}"
                )
            finally:
                if matches is not None:
                    del matches
                del ts

        if not entries:
            try:
                res = subprocess.run(
                    ["rpm", "-q", "--changelog", self.package_name],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=get_clean_env(),
                    check=False,
                    timeout=8,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(
                    f"rpm changelog command failed: {type(exc).__name__}: {exc}"
                )
            else:
                if res.returncode != 0:
                    failures.append(
                        res.stderr.strip()
                        or f"rpm changelog query exited with status {res.returncode}."
                    )
                else:
                    failures.clear()
                    header = ""
                    body: List[str] = []
                    for line in res.stdout.splitlines():
                        if line.startswith("* "):
                            if header:
                                content = "\n".join(body).strip()
                                entries.append(
                                    PackageChangelogEntry(
                                        author=header,
                                        timestamp=0,
                                        date_str="",
                                        text=content,
                                        cves=sorted(
                                            set(self._cve_pattern.findall(content))
                                        ),
                                    )
                                )
                            header, body = line[2:].strip(), []
                        else:
                            body.append(line)
                    if header:
                        content = "\n".join(body).strip()
                        entries.append(
                            PackageChangelogEntry(
                                author=header,
                                timestamp=0,
                                date_str="",
                                text=content,
                                cves=sorted(set(self._cve_pattern.findall(content))),
                            )
                        )
                    if res.stdout.strip() and not entries:
                        failures.append(
                            "rpm changelog output did not contain recognizable entries."
                        )

        if failures and not entries:
            self.signals.error_occurred.emit(
                self.package_name,
                "Could not read package changelog: " + " | ".join(failures),
            )
            return
        self.signals.package_changelog_loaded.emit(self.package_name, entries)


# =============================================================================
# Worker: RPM Maintainer Scriptlets & Triggers (%pre, %post)
# =============================================================================


class PackageScriptletsWorker(QRunnable):
    """Read installed RPM scriptlets without misreporting failed reads as empty data."""

    def __init__(self, package_name: str):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name

    @pyqtSlot()
    def run(self):
        if is_running_in_flatpak():
            self.signals.error_occurred.emit(
                self.package_name,
                flatpak_read_only_unavailable("RPM scriptlet inspection"),
            )
            return

        scripts = PackageScriptlets()
        failures: List[str] = []
        ts = create_rpm_transaction_set()
        if ts is not None:
            matches = None
            try:
                with RPM_GLOBAL_LOCK:
                    matches = ts.dbMatch("name", self.package_name)
                    for hdr in matches:
                        scripts.prein = _decode_rpm_str(
                            hdr[rpm.RPMTAG_PREIN] or ""
                        ).strip()
                        scripts.postin = _decode_rpm_str(
                            hdr[rpm.RPMTAG_POSTIN] or ""
                        ).strip()
                        scripts.preun = _decode_rpm_str(
                            hdr[rpm.RPMTAG_PREUN] or ""
                        ).strip()
                        scripts.postun = _decode_rpm_str(
                            hdr[rpm.RPMTAG_POSTUN] or ""
                        ).strip()
                        break
            except Exception as exc:
                failures.append(
                    f"native RPM scriptlet query failed: {type(exc).__name__}: {exc}"
                )
            finally:
                if matches is not None:
                    del matches
                del ts

        if not scripts.has_any:
            try:
                res = subprocess.run(
                    ["rpm", "-q", "--scripts", self.package_name],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=get_clean_env(),
                    check=False,
                    timeout=6,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(
                    f"rpm scriptlet command failed: {type(exc).__name__}: {exc}"
                )
            else:
                if res.returncode != 0:
                    failures.append(
                        res.stderr.strip()
                        or f"rpm scriptlet query exited with status {res.returncode}."
                    )
                else:
                    failures.clear()
                if res.returncode == 0 and res.stdout.strip():
                    current_tag: str | None = None
                    recognized_heading = False
                    buffer: List[str] = []
                    headings = {
                        "preinstall scriptlet": "prein",
                        "postinstall scriptlet": "postin",
                        "preuninstall scriptlet": "preun",
                        "postuninstall scriptlet": "postun",
                    }
                    for line in res.stdout.splitlines():
                        next_tag = next(
                            (
                                tag
                                for heading, tag in headings.items()
                                if line.strip().lower().startswith(heading)
                            ),
                            None,
                        )
                        if next_tag:
                            recognized_heading = True
                            if current_tag:
                                setattr(scripts, current_tag, "\n".join(buffer).strip())
                            current_tag, buffer = next_tag, []
                        else:
                            buffer.append(line)
                    if current_tag:
                        setattr(scripts, current_tag, "\n".join(buffer).strip())
                    if not recognized_heading:
                        failures.append(
                            "rpm scriptlet output did not contain recognizable scriptlet headings."
                        )

        if failures and not scripts.has_any:
            self.signals.error_occurred.emit(
                self.package_name,
                "Could not read RPM scriptlets: " + " | ".join(failures),
            )
            return
        self.signals.package_scriptlets_loaded.emit(self.package_name, scripts)


# =============================================================================
# Worker: File Integrity & Tamper Auditor (rpm -V)
# =============================================================================


class PackageVerifyWorker(QRunnable):
    def __init__(self, package_name: str):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name

    @pyqtSlot()
    def run(self):
        if is_running_in_flatpak():
            self.signals.error_occurred.emit(
                self.package_name,
                flatpak_read_only_unavailable("RPM file verification"),
            )
            return

        try:
            res = subprocess.run(
                ["rpm", "-V", self.package_name],
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=18,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.signals.error_occurred.emit(
                self.package_name,
                f"Could not verify package files: {type(exc).__name__}: {exc}",
            )
            return

        # rpm returns 1 when it successfully found verification differences.
        # A status-one response with no report but diagnostic stderr instead
        # means the request itself failed (for example, the package vanished).
        if res.returncode not in {0, 1} or (
            res.returncode == 1 and not res.stdout.strip() and res.stderr.strip()
        ):
            detail = (
                res.stderr.strip()
                or f"rpm verification exited with status {res.returncode}."
            )
            self.signals.error_occurred.emit(
                self.package_name, f"Could not verify package files: {detail}"
            )
            return

        results: List[FileVerificationResult] = []
        unparsed_lines: List[str] = []
        for line in res.stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue
            if "missing" in line:
                parts = line.split(None, 1)
                results.append(
                    FileVerificationResult(
                        path=parts[1] if len(parts) > 1 else "",
                        status_flags="missing",
                        is_config=False,
                        is_missing=True,
                        size_differs=False,
                        mode_differs=False,
                        digest_differs=False,
                        mtime_differs=False,
                        raw_line=line,
                    )
                )
                continue

            parts = line.split()
            if len(parts) < 2:
                unparsed_lines.append(line)
                continue
            flags = parts[0]
            if re.fullmatch(r"[.SM5DLUGT?]{8,9}", flags) is None:
                unparsed_lines.append(line)
                continue
            results.append(
                FileVerificationResult(
                    path=parts[-1],
                    status_flags=flags,
                    is_config="c" in parts[1:-1],
                    is_missing=False,
                    size_differs="S" in flags,
                    mode_differs="M" in flags,
                    digest_differs="5" in flags,
                    mtime_differs="T" in flags,
                    raw_line=line,
                )
            )
        if unparsed_lines:
            self.signals.error_occurred.emit(
                self.package_name,
                "Could not verify package files: rpm verification output contained an unrecognized line: "
                + unparsed_lines[0],
            )
            return
        self.signals.package_verification_finished.emit(self.package_name, results)


# =============================================================================
# Worker: Available System Updates & Security Advisories
# =============================================================================


class SystemUpdatesCheckWorker(QRunnable):
    """Fetch upgrade candidates through the typed DNF5 daemon API.

    This deliberately replaces ``dnf check-upgrade`` subprocess handling.  It
    works with the Flatpak's narrowly scoped system-bus permission, has one
    terminal signal path, and leaves metadata refresh policy to the daemon.
    """

    def __init__(
        self,
        force_refresh: bool = False,
        *,
        client_factory: Callable[[], Dnf5DaemonClient] = Dnf5DaemonClient,
    ):
        super().__init__()
        self.signals = BackendSignals()
        self.force_refresh = force_refresh
        self._client_factory = client_factory
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        if self._is_cancelled.is_set():
            return

        try:
            if self.force_refresh:
                self.signals.status_update.emit(
                    "Refreshing DNF metadata through the system daemon…"
                )
            else:
                self.signals.status_update.emit(
                    "Checking DNF5 daemon for available updates…"
                )

            updates = self._client_factory().list_available_updates(
                refresh=self.force_refresh
            )
            if self._is_cancelled.is_set():
                return

            updates_map = {
                update.name: AvailableUpdateInfo(
                    name=update.name,
                    new_version=update.version,
                    new_release=update.release,
                    arch=update.arch,
                    repository=update.repository or "Updates",
                )
                for update in updates
            }
        except Dnf5DaemonError as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit("", f"{exc.title}: {exc.detail}")
            return
        except Exception as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "", f"Unexpected update-query error: {type(exc).__name__}: {exc}"
                )
            return

        if not self._is_cancelled.is_set():
            self.signals.system_updates_loaded.emit(updates_map)


# =============================================================================
# Worker: On-Demand Remote Repository Package Search (DNF5)
# =============================================================================


class RemotePackageSearchWorker(QRunnable):
    """
    Asynchronously queries remote enabled DNF repositories for uninstalled software.
    Runs on-demand without memory bloat or startup latency.
    """

    def __init__(self, query: str, limit: int = 150):
        super().__init__()
        self.signals = BackendSignals()
        self.query = query.strip()
        self.limit = limit
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        if self._is_cancelled.is_set():
            return
        if is_running_in_flatpak():
            self.signals.error_occurred.emit(
                "",
                flatpak_read_only_unavailable("Remote repository search"),
            )
            return

        dnf_bin = get_dnf_binary_path()
        if not dnf_bin:
            self.signals.error_occurred.emit(
                "",
                "Remote repository search requires an installed dnf5 or dnf command on this RPM host.",
            )
            return

        # Multi-word queries (e.g. 'gnome shell') join with wildcards (*gnome*shell*)
        # to match hyphenated RPM naming conventions (gnome-shell)
        words = self.query.split()
        if len(words) > 1 and not any(c in self.query for c in ("*", "?")):
            spec = f"*{'*'.join(words)}*"
        elif "*" in self.query or "?" in self.query:
            spec = self.query
        else:
            spec = f"*{self.query}*"

        # Query remote repositories using unit-separator (\x1f) delimiter
        qf = "%{name}\x1f%{version}\x1f%{release}\x1f%{arch}\x1f%{repo}\x1f%{size}\x1f%{summary}\n"
        # The final specification is user-derived and may legitimately contain
        # wildcards. End option parsing before it so an explicit pattern that
        # starts with '-' can never be interpreted as a DNF option.
        cmd = [
            dnf_bin,
            "repoquery",
            "--available",
            "-q",
            f"--queryformat={qf}",
            "--",
            spec,
        ]

        try:
            self.signals.status_update.emit(
                f"Searching remote repositories for '{self.query}'..."
            )
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=20,
            )
            if self._is_cancelled.is_set():
                return

            results: List[PackageInfo] = []
            seen_names: Set[str] = set()

            if res.returncode != 0:
                detail = (
                    res.stderr.strip()
                    or f"{dnf_bin} repoquery exited with status {res.returncode}."
                )
                self.signals.error_occurred.emit(
                    "", f"Remote repository search failed: {detail}"
                )
                return

            if res.stdout.strip():
                appstream = AppStreamCatalog.get_instance()
                comps = FedoraCompsCatalog.get_instance()

                for line in res.stdout.splitlines():
                    if len(results) >= self.limit or self._is_cancelled.is_set():
                        break
                    parts = line.split("\x1f")
                    if len(parts) != 7:
                        raise RuntimeError(
                            "DNF repoquery returned a malformed package record."
                        )

                    name, ver, rel, arch, repo, size_str, summary = parts
                    name_clean = name.strip()
                    if not all((name_clean, ver.strip(), rel.strip(), arch.strip())):
                        raise RuntimeError(
                            "DNF repoquery returned a package record with missing identity fields."
                        )
                    if name_clean in seen_names:
                        continue
                    seen_names.add(name_clean)

                    try:
                        size_bytes = int(size_str.strip())
                    except ValueError as exc:
                        raise RuntimeError(
                            "DNF repoquery returned a package record with an invalid size."
                        ) from exc
                    if size_bytes < 0:
                        raise RuntimeError(
                            "DNF repoquery returned a package record with a negative size."
                        )

                    decision = ProductionTaxonomyEngine.classify(
                        name=name_clean,
                        summary=summary.strip(),
                        appstream=appstream,
                        comps=comps,
                        input_scope=TaxonomyInputScope.REMOTE_METADATA,
                    )

                    results.append(
                        PackageInfo(
                            name=name_clean,
                            version=ver.strip(),
                            release=rel.strip(),
                            arch=arch.strip(),
                            summary=summary.strip(),
                            size_bytes=size_bytes,
                            state=PackageState.AVAILABLE,
                            repository=repo.strip() or "Repositories",
                            **classification_package_fields(decision),
                        )
                    )

            if not self._is_cancelled.is_set():
                self.signals.remote_search_finished.emit(self.query, results)

        except (OSError, subprocess.TimeoutExpired) as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Remote repository search failed: {type(exc).__name__}: {exc}",
                )
        except Exception as exc:
            # Parsing and classification must never let a QRunnable terminate
            # without a terminal UI result. Include the exception type because
            # several native bindings produce an empty string representation.
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Remote repository search failed: {type(exc).__name__}: {exc}",
                )


# =============================================================================
# Repository records are sourced from core.dnf5daemon.Dnf5DaemonClient.  Dendro
# does not parse or mutate host repository files from a Flatpak sandbox.
# =============================================================================


# =============================================================================
# Package-changing operations are implemented by core.dnf5daemon and
# core.transaction_controller.  No generic privileged command runner is shipped.
# =============================================================================
