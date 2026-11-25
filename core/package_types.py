# dendro/core/package_types.py
"""Pure RPM/DNF package domain dataclasses, enums, and formatting helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from core.taxonomy import (
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


def format_byte_size(size_bytes: int) -> str:
    """Format a byte count for display with one decimal place.

    Shared by the tree's SIZE column and the inspector's file manifest so the
    same file never shows two different sizes. ``0`` is a real value for an
    empty RPM-owned file and must not render as a blank cell.
    """
    if size_bytes < 0:
        return ""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


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

    # Ecosystem Flags
    is_python_pkg: bool = False
    is_rust_pkg: bool = False
    is_jvm_pkg: bool = False
    is_nodejs_pkg: bool = False

    # Repository & packaging metadata
    repository: str = "Fedora Project"
    ostree_origin: str = ""

    # Pending Updates
    has_update: bool = False
    available_update_version: str = ""
    available_update_repo: str = ""

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
    "format_byte_size",
)
