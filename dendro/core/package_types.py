# dendro/core/package_types.py
"""Pure RPM/DNF package domain dataclasses, enums, and formatting helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from dendro.core.i18n import format_byte_size
from dendro.core.taxonomy import (
    ClassificationDecision,
    ClassificationEvidence,
    TaxonomyInputScope,
)


def _decode_rpm_str(val: Any) -> str:
    """Safely normalise a native RPM value to UTF-8 text."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _normalise_rpm_epoch(value: Any) -> str:
    """Normalize librpm and query-format representations of an absent epoch."""
    epoch = _decode_rpm_str(value).strip()
    return "0" if epoch in {"", "(none)"} else epoch


def _is_ascii_alnum(ch: str) -> bool:
    return ("0" <= ch <= "9") or ("A" <= ch <= "Z") or ("a" <= ch <= "z")


def _is_ascii_digit(ch: str) -> bool:
    return "0" <= ch <= "9"


def _is_ascii_alpha(ch: str) -> bool:
    return ("A" <= ch <= "Z") or ("a" <= ch <= "z")


def rpm_vercmp(a: str, b: str) -> int:
    """Compare two RPM version or release strings using ``rpmvercmp`` semantics."""
    if a == b:
        return 0
    la = len(a)
    lb = len(b)
    i = 0
    j = 0
    while i < la or j < lb:
        while i < la and not _is_ascii_alnum(a[i]) and a[i] not in "~^":
            i += 1
        while j < lb and not _is_ascii_alnum(b[j]) and b[j] not in "~^":
            j += 1

        if (i < la and a[i] == "~") or (j < lb and b[j] == "~"):
            if not (i < la and a[i] == "~"):
                return 1
            if not (j < lb and b[j] == "~"):
                return -1
            i += 1
            j += 1
            continue

        if (i < la and a[i] == "^") or (j < lb and b[j] == "^"):
            if i >= la:
                return -1
            if j >= lb:
                return 1
            if a[i] != "^":
                return 1
            if b[j] != "^":
                return -1
            i += 1
            j += 1
            continue

        if i >= la or j >= lb:
            break

        is_num = _is_ascii_digit(a[i])
        pred = _is_ascii_digit if is_num else _is_ascii_alpha

        start_a = i
        while i < la and pred(a[i]):
            i += 1
        seg_a = a[start_a:i]

        start_b = j
        while j < lb and pred(b[j]):
            j += 1
        seg_b = b[start_b:j]

        if not seg_b:
            return 1 if is_num else -1

        if is_num:
            seg_a = seg_a.lstrip("0")
            seg_b = seg_b.lstrip("0")
            if len(seg_a) != len(seg_b):
                return 1 if len(seg_a) > len(seg_b) else -1
        if seg_a != seg_b:
            return 1 if seg_a > seg_b else -1

    if i >= la and j >= lb:
        return 0
    return -1 if i >= la else 1


def compare_rpm_evr(evr_a: tuple[str, str, str], evr_b: tuple[str, str, str]) -> int:
    """Compare two ``(epoch, version, release)`` tuples (-1, 0, or 1)."""
    ep_a = _normalise_rpm_epoch(evr_a[0])
    ep_b = _normalise_rpm_epoch(evr_b[0])
    if ep_a != ep_b:
        try:
            ia, ib = int(ep_a), int(ep_b)
        except ValueError:
            cmp_ep = rpm_vercmp(ep_a, ep_b)
            if cmp_ep != 0:
                return cmp_ep
        else:
            if ia != ib:
                return 1 if ia > ib else -1

    cmp_ver = rpm_vercmp(evr_a[1], evr_b[1])
    if cmp_ver != 0:
        return cmp_ver
    return rpm_vercmp(evr_a[2], evr_b[2])


class PackageState(Enum):
    INSTALLED = auto()
    AVAILABLE = auto()
    MISSING = auto()
    QUEUED_INSTALL = auto()
    QUEUED_REMOVE = auto()
    QUEUED_UPGRADE = auto()
    QUEUED_REINSTALL = auto()
    QUEUED_DOWNGRADE = auto()
    QUEUED_DISTRO_SYNC = auto()


@dataclass(slots=True)
class DependencyNode:
    raw_requirement: str
    resolved_package_name: str
    version_constraint: str = ""
    is_satisfied: bool = True
    is_cycle: bool = False
    is_reverse: bool = False
    installed_version: str = ""
    sub_dependencies: list[DependencyNode] = field(default_factory=list)


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
    cves: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PackageScriptlets:
    pretrans: str = ""
    prein: str = ""
    postin: str = ""
    preun: str = ""
    postun: str = ""
    posttrans: str = ""
    preuntrans: str = ""
    postuntrans: str = ""
    verify: str = ""

    @property
    def has_any(self) -> bool:
        return any(
            (
                self.pretrans,
                self.prein,
                self.postin,
                self.preun,
                self.postun,
                self.posttrans,
                self.preuntrans,
                self.postuntrans,
                self.verify,
            )
        )


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
    annotation: str = ""
    is_unreadable: bool = False


@dataclass(slots=True)
class AvailableUpdateInfo:
    name: str
    new_version: str
    new_release: str = ""
    arch: str = ""
    repository: str = ""
    is_security: bool = False
    advisory_id: str = ""
    epoch: str = "0"

    @property
    def name_arch(self) -> str:
        """Return DNF's name-and-architecture selector for this update."""
        name = self.name.strip()
        arch = self.arch.strip()
        return f"{name}.{arch}" if name and arch else name

    @property
    def full_version(self) -> str:
        """Returns the canonical epoch-version-release upgrade target."""
        version = self.new_version.strip()
        release = self.new_release.strip()
        epoch = self.epoch.strip()
        evr = f"{version}-{release}" if version and release else version or release
        return (
            f"{epoch}:{evr}" if evr and epoch and epoch not in {"0", "(none)"} else evr
        )


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
    requires: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    classification: ClassificationDecision | None = None
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
    classification_confidence: float = 0.0
    classification_evidence: str = ClassificationEvidence.INSUFFICIENT_METADATA.value
    classification_input_scope: str = TaxonomyInputScope.UNKNOWN.value
    classification_rationale: list[str] = field(default_factory=list)
    secondary_tags: list[str] = field(default_factory=list)

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

    # Repository & packaging metadata
    repository: str = "Fedora Project"
    ostree_origin: str = ""

    # Pending Updates & Targeted Queue Selections
    has_update: bool = False
    available_update_version: str = ""
    available_update_repo: str = ""
    queued_target_spec: str = ""
    queued_target_version: str = ""

    # Hierarchy and file collections
    has_dependencies: bool = True
    dependencies_loaded: bool = False
    dependencies: list[DependencyNode] = field(default_factory=list)
    reverse_dependencies: list[DependencyNode] = field(default_factory=list)
    files: list[PackageFileInfo] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)

    @property
    def full_version(self) -> str:
        return f"{self.version}-{self.release}" if self.release else self.version

    @property
    def evr_tuple(self) -> tuple[str, str, str]:
        return (_normalise_rpm_epoch(self.epoch), self.version, self.release)

    @property
    def name_arch(self) -> str:
        """Return the package's architecture-qualified DNF selector."""
        name = self.name.strip()
        arch = self.arch.strip()
        return f"{name}.{arch}" if name and arch else name

    @property
    def nevra(self) -> str:
        """Return epoch-free ``name-version-release.arch`` when complete."""
        name = self.name.strip()
        version = self.version.strip()
        release = self.release.strip()
        arch = self.arch.strip()
        if not all((name, version, release, arch)):
            return self.name_arch
        return f"{name}-{version}-{release}.{arch}"

    @property
    def full_nevra(self) -> str:
        """Return the concrete RPM identity when the record is complete."""
        name = self.name.strip()
        version = self.version.strip()
        release = self.release.strip()
        arch = self.arch.strip()
        if not all((name, version, release, arch)):
            return self.name_arch
        epoch = self.epoch.strip()
        evr = (
            f"{epoch}:{version}" if epoch and epoch not in {"0", "(none)"} else version
        )
        return f"{name}-{evr}-{release}.{arch}"

    @property
    def transaction_spec(self) -> str:
        """Return the narrowest DNF selector appropriate for this staged action."""
        if self.state == PackageState.QUEUED_REMOVE:
            return self.full_nevra
        if self.state == PackageState.QUEUED_DOWNGRADE and self.queued_target_spec:
            return self.queued_target_spec
        if self.state == PackageState.QUEUED_REINSTALL:
            if all(
                (
                    self.name.strip(),
                    self.version.strip(),
                    self.release.strip(),
                    self.arch.strip(),
                )
            ):
                return self.full_nevra
            return self.name_arch
        return self.name_arch

    def format_version(self, show_epoch: bool = False) -> str:
        """Returns version with optional RPM epoch prefix (e.g. 0:1.10.3-1.fc45)."""
        base = self.full_version
        if show_epoch:
            ep = self.epoch.strip() if self.epoch else "0"
            return f"{ep}:{base}"
        return base

    @property
    def human_size(self) -> str:
        return format_byte_size(self.size_bytes)

    def _has_ecosystem_facet(self, tag: str, category_id: str) -> bool:
        return tag in self.secondary_tags or self.primary_category == category_id

    @property
    def is_python_pkg(self) -> bool:
        return self._has_ecosystem_facet("Python", "python_pkgs")

    @property
    def is_rust_pkg(self) -> bool:
        return self._has_ecosystem_facet("Rust", "rust_pkgs")

    @property
    def is_jvm_pkg(self) -> bool:
        return self._has_ecosystem_facet("Java/JVM", "jvm_pkgs")

    @property
    def is_nodejs_pkg(self) -> bool:
        return self._has_ecosystem_facet("Node.js", "nodejs_pkgs")


__all__ = (
    "AvailableUpdateInfo",
    "DependencyNode",
    "FileVerificationResult",
    "HistoryEntry",
    "LocalRpmInspectionResult",
    "PackageChangelogEntry",
    "PackageFileInfo",
    "PackageInfo",
    "PackageScriptlets",
    "PackageState",
    "RepoInfo",
    "_decode_rpm_str",
    "_normalise_rpm_epoch",
    "compare_rpm_evr",
    "format_byte_size",
    "rpm_vercmp",
)
