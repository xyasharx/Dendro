"""Single source of truth for Dendro's package-taxonomy vocabulary.

This module intentionally has no Qt, RPM, DNF, or filesystem dependency.  Both
classification and presentation import the same category registry so adding a
new category cannot silently create a classifier/sidebar/filter mismatch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from typing import Any, Final


class PillarId(StrEnum):
    """Stable identifiers for Dendro's top-level package domains."""

    APPS = "pillar_apps"
    CLI = "pillar_cli"
    HARDWARE = "pillar_hardware"
    SYSTEM = "pillar_system"
    LIBRARIES = "pillar_libs"
    MAINTENANCE = "pillar_maintenance"


class CategoryKind(StrEnum):
    """How a registry item behaves when it is selected as a filter."""

    TAXONOMY = "taxonomy"
    PILLAR = "pillar"
    TOP_LEVEL = "top_level"
    FACET = "facet"
    ALL = "all"


class ClassificationEvidence(StrEnum):
    """The strongest affirmative source behind a classification decision.

    Values are deliberately provenance labels, not claimed statistical
    probabilities.  ``legacy_confidence`` exists only while older consumers
    still store a numeric field on :class:`PackageInfo`.
    """

    MANUAL_OVERRIDE = "manual_override"
    APPSTREAM = "appstream"
    DESKTOP_ENTRY = "desktop_entry"
    RPM_MANIFEST = "rpm_manifest"
    COMPS = "fedora_comps"
    NAME_HEURISTIC = "name_heuristic"
    INSUFFICIENT_METADATA = "insufficient_metadata"

    @property
    def label(self) -> str:
        return {
            ClassificationEvidence.MANUAL_OVERRIDE: "Explicit package policy",
            ClassificationEvidence.APPSTREAM: "AppStream metadata",
            ClassificationEvidence.DESKTOP_ENTRY: "Desktop-entry metadata",
            ClassificationEvidence.RPM_MANIFEST: "RPM manifest",
            ClassificationEvidence.COMPS: "Fedora Comps metadata",
            ClassificationEvidence.NAME_HEURISTIC: "Package-name heuristic",
            ClassificationEvidence.INSUFFICIENT_METADATA: "Insufficient metadata",
        }[self]

    @property
    def legacy_confidence(self) -> float:
        """Compatibility hint only; not a calibrated probability."""
        return {
            ClassificationEvidence.MANUAL_OVERRIDE: 1.0,
            ClassificationEvidence.APPSTREAM: 0.95,
            ClassificationEvidence.DESKTOP_ENTRY: 0.90,
            ClassificationEvidence.RPM_MANIFEST: 0.80,
            ClassificationEvidence.COMPS: 0.65,
            ClassificationEvidence.NAME_HEURISTIC: 0.45,
            ClassificationEvidence.INSUFFICIENT_METADATA: 0.0,
        }[self]


class TaxonomyInputScope(StrEnum):
    """What package facts were available when classification ran."""

    INSTALLED_MANIFEST = "installed_manifest"
    LOCAL_RPM_MANIFEST = "local_rpm_manifest"
    REMOTE_METADATA = "remote_metadata"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            TaxonomyInputScope.INSTALLED_MANIFEST: "Installed RPM manifest",
            TaxonomyInputScope.LOCAL_RPM_MANIFEST: "Local RPM manifest",
            TaxonomyInputScope.REMOTE_METADATA: "Remote repository metadata",
            TaxonomyInputScope.UNKNOWN: "Unknown metadata scope",
        }[self]


def classification_evidence_label(value: str) -> str:
    """Return a resilient user-facing evidence label for a stored value."""
    try:
        return ClassificationEvidence(value).label
    except ValueError:
        return "Unknown evidence"


def taxonomy_input_scope_label(value: str) -> str:
    """Return a resilient user-facing input-scope label for a stored value."""
    try:
        return TaxonomyInputScope(value).label
    except ValueError:
        return "Unknown metadata scope"


class PackageArchetype(Enum):
    """The physical or semantic delivery form resolved by the classifier."""

    UNCLASSIFIED = auto()
    CORE_SYSTEM = auto()
    HARDWARE_DRIVER = auto()
    DESKTOP_APP = auto()
    SYSTEM_DAEMON = auto()
    DESKTOP_ADDON = auto()
    DEVELOPMENT_SDK = auto()
    MEDIA_CODEC = auto()
    GUI_TOOLKIT = auto()
    ECOSYSTEM_RUNTIME = auto()
    STATIC_ASSET = auto()
    SYSTEM_DATA = auto()
    CLI_UTILITY = auto()
    SHARED_LIBRARY = auto()


class RemovalRiskLevel(Enum):
    """User-facing removal guidance derived from evidence and package role."""

    CRITICAL = auto()
    HIGH = auto()
    MEDIUM = auto()
    LOW = auto()
    SAFE = auto()
    REVIEW = auto()


@dataclass(frozen=True, slots=True)
class RemovalSafetyAssessment:
    level: RemovalRiskLevel
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class ClassificationDecision:
    """Immutable output of package classification.

    String IDs are retained for model/UI compatibility, while ``evidence`` and
    ``input_scope`` make the limits of the decision visible to callers.
    """

    primary_category: str
    sub_category: str
    parent_pillar: str
    evidence: ClassificationEvidence
    input_scope: TaxonomyInputScope
    rationale: tuple[str, ...]
    secondary_tags: tuple[str, ...]
    flags: Mapping[str, bool]

    @property
    def confidence(self) -> float:
        """Deprecated compatibility view; use :attr:`evidence` instead."""
        return self.evidence.legacy_confidence

    @property
    def is_metadata_limited(self) -> bool:
        return self.evidence == ClassificationEvidence.INSUFFICIENT_METADATA


@dataclass(frozen=True, slots=True)
class PillarSpec:
    identifier: str
    label: str


@dataclass(frozen=True, slots=True)
class CategorySpec:
    identifier: str
    label: str
    tooltip: str
    icon_names: tuple[str, ...]
    kind: CategoryKind
    pillar: str | None = None
    sidebar_section: str | None = None
    sidebar_order: int = 0
    is_subcategory: bool = False
    flag_attribute: str | None = None
    # A category can also match a factual secondary tag. This lets a package
    # retain its primary role (for example, a system service) while remaining
    # discoverable under its language/platform ecosystem.
    facet_tag: str | None = None
    # Category-level guidance used when positive classification exists. DNF's
    # resolved transaction remains authoritative for every actual removal.
    removal_risk: RemovalRiskLevel | None = None


@dataclass(frozen=True, slots=True)
class SidebarSection:
    identifier: str
    label: str
    order: int


@dataclass(frozen=True, slots=True)
class SidebarRow:
    label: str
    tag: str
    is_header: bool
    is_subcategory: bool = False


PILLAR_SPECS: Final[Mapping[str, PillarSpec]] = {
    PillarId.APPS: PillarSpec(PillarId.APPS, "Desktop Applications"),
    PillarId.CLI: PillarSpec(PillarId.CLI, "Command-Line Utilities"),
    PillarId.HARDWARE: PillarSpec(PillarId.HARDWARE, "Hardware & Driver Stack"),
    PillarId.SYSTEM: PillarSpec(PillarId.SYSTEM, "System Architecture"),
    PillarId.LIBRARIES: PillarSpec(PillarId.LIBRARIES, "Libraries & Development"),
    PillarId.MAINTENANCE: PillarSpec(PillarId.MAINTENANCE, "Maintenance & Sources"),
}

SIDEBAR_SECTIONS: Final[tuple[SidebarSection, ...]] = (
    SidebarSection("apps", "1. Desktop Applications", 1),
    SidebarSection("cli", "2. Command-Line Utilities", 2),
    SidebarSection("hardware", "3. Hardware & Drivers", 3),
    SidebarSection("system", "4. System Architecture", 4),
    SidebarSection("libraries", "5. Libraries & Development", 5),
    SidebarSection("maintenance", "6. Maintenance & Sources", 6),
)

# Risk guidance belongs with taxonomy identity rather than a separate UI or
# classifier lookup table. These defaults cover ordinary categories; exceptions
# document categories whose package role is safer or more critical than their
# containing pillar would suggest.
_DEFAULT_REMOVAL_RISK_BY_PILLAR: Final[Mapping[str, RemovalRiskLevel]] = {
    PillarId.APPS: RemovalRiskLevel.LOW,
    PillarId.CLI: RemovalRiskLevel.LOW,
    PillarId.HARDWARE: RemovalRiskLevel.HIGH,
    PillarId.SYSTEM: RemovalRiskLevel.HIGH,
    PillarId.LIBRARIES: RemovalRiskLevel.MEDIUM,
}
_REMOVAL_RISK_OVERRIDES: Final[Mapping[str, RemovalRiskLevel]] = {
    "fedora_core": RemovalRiskLevel.CRITICAL,
    "fonts": RemovalRiskLevel.LOW,
    "themes": RemovalRiskLevel.LOW,
    "locales": RemovalRiskLevel.LOW,
    "reference_data": RemovalRiskLevel.LOW,
    "system_data": RemovalRiskLevel.MEDIUM,
    "repository_sources": RemovalRiskLevel.HIGH,
    "unclassified": RemovalRiskLevel.REVIEW,
}


def _category(
    identifier: str,
    label: str,
    tooltip: str,
    icons: tuple[str, ...],
    kind: CategoryKind,
    *,
    pillar: str | None = None,
    section: str | None = None,
    order: int = 0,
    subcategory: bool = False,
    flag: str | None = None,
    facet_tag: str | None = None,
    removal_risk: RemovalRiskLevel | None = None,
) -> CategorySpec:
    resolved_risk = removal_risk or _REMOVAL_RISK_OVERRIDES.get(identifier)
    if resolved_risk is None and pillar is not None:
        resolved_risk = _DEFAULT_REMOVAL_RISK_BY_PILLAR.get(str(pillar))
    return CategorySpec(
        identifier=identifier,
        label=label,
        tooltip=tooltip,
        icon_names=icons,
        kind=kind,
        pillar=pillar,
        sidebar_section=section,
        sidebar_order=order,
        is_subcategory=subcategory,
        flag_attribute=flag,
        facet_tag=facet_tag,
        removal_risk=resolved_risk,
    )


# This is the only category-definition table in the application.  Classifier
# rules emit these IDs; models, inspector labels, icons, tooltips and sidebar
# rows are derived from it.
_CATEGORY_SPECS: Final[tuple[CategorySpec, ...]] = (
    # Desktop applications
    _category(
        "user_apps",
        "All Desktop Applications",
        "All interactive user-facing graphical software applications with verified desktop metadata.",
        ("applications-other", "preferences-desktop-apps", "application-x-executable"),
        CategoryKind.TOP_LEVEL,
        pillar=PillarId.APPS,
        section="apps",
        order=0,
        flag="is_desktop_app",
    ),
    _category(
        "desktop_internet",
        "Web Browsers & Internet",
        "Web browsers, email clients, instant messaging, and web applications.",
        ("applications-internet", "web-browser", "network-workgroup"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.APPS,
        section="apps",
        order=1,
        subcategory=True,
    ),
    _category(
        "desktop_multimedia",
        "Audio & Video Players",
        "Audio players, video playback engines, sound recorders, and media managers.",
        ("applications-multimedia", "multimedia-player", "audio-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.APPS,
        section="apps",
        order=2,
        subcategory=True,
    ),
    _category(
        "desktop_graphics",
        "Graphics & Design",
        "Raster photo editors, vector illustration suites, 3D modeling tools, and image viewers.",
        ("applications-graphics", "image-x-generic", "view-preview"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.APPS,
        section="apps",
        order=3,
        subcategory=True,
    ),
    _category(
        "desktop_office",
        "Office & Productivity",
        "Word processors, spreadsheets, presentation software, PDF readers, and finance tools.",
        ("applications-office", "x-office-document", "text-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.APPS,
        section="apps",
        order=4,
        subcategory=True,
    ),
    _category(
        "desktop_development",
        "Developer Tools & IDEs",
        "Graphical integrated development environments, diff tools, and code editors.",
        ("applications-development", "text-x-c++src", "system-run"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.APPS,
        section="apps",
        order=5,
        subcategory=True,
    ),
    _category(
        "desktop_games",
        "Games & Emulators",
        "Native Linux games, game engine runtimes, and console emulators.",
        ("applications-games", "input-gaming", "gamepad"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.APPS,
        section="apps",
        order=6,
        subcategory=True,
    ),
    _category(
        "system_settings",
        "Settings & Control Panels",
        "Desktop control panels, KCM configuration applets, and system preference dialogs.",
        ("preferences-system", "preferences-desktop", "emblem-system"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.APPS,
        section="apps",
        order=7,
        subcategory=True,
        flag="is_system_settings",
    ),
    _category(
        "desktop_utilities",
        "Accessories & Utilities",
        "Desktop accessories, calculators, archive managers, and file tools.",
        ("applications-utilities", "accessories-calculator", "utility"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.APPS,
        section="apps",
        order=8,
        subcategory=True,
    ),
    # Command-line tools
    _category(
        "cli_tools",
        "All Command-Line Tools",
        "All command-line utilities and interactive tools residing in user PATH.",
        ("utilities-terminal", "terminal", "system-run"),
        CategoryKind.TOP_LEVEL,
        pillar=PillarId.CLI,
        section="cli",
        order=0,
        flag="is_cli_tool",
    ),
    _category(
        "cli_editors",
        "Terminal Editors & Pagers",
        "Terminal-based text editors and pagers.",
        ("accessories-text-editor", "text-editor", "text-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.CLI,
        section="cli",
        order=1,
        subcategory=True,
    ),
    _category(
        "cli_shells",
        "Shells & Multiplexers",
        "Command interpreters and terminal multiplexers.",
        ("utilities-terminal", "terminal", "system-run"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.CLI,
        section="cli",
        order=2,
        subcategory=True,
    ),
    _category(
        "cli_search_files",
        "Search & File Utilities",
        "Directory search utilities and file-navigation tools.",
        ("system-search", "edit-find", "folder-saved-search"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.CLI,
        section="cli",
        order=3,
        subcategory=True,
    ),
    _category(
        "cli_networking",
        "Network & Remote Access",
        "Data-transfer tools, port scanners, and remote-access utilities.",
        ("network-wired", "network-transmit-receive", "preferences-system-network"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.CLI,
        section="cli",
        order=4,
        subcategory=True,
    ),
    _category(
        "cli_monitoring",
        "Monitoring & Diagnostics",
        "Process viewers, diagnostic tracers, and benchmarks.",
        ("utilities-system-monitor", "preferences-system-performance", "system-run"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.CLI,
        section="cli",
        order=5,
        subcategory=True,
    ),
    _category(
        "cli_data_archiving",
        "Archiving & Compression",
        "Stream processors and archive/compression tools.",
        ("package-x-generic", "application-x-archive", "utilities-file-archiver"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.CLI,
        section="cli",
        order=6,
        subcategory=True,
    ),
    _category(
        "cli_general",
        "General CLI Tools",
        "Command-line tools that do not belong to a more specific CLI subcategory.",
        ("utilities-terminal", "terminal", "system-run"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.CLI,
        section="cli",
        order=7,
        subcategory=True,
    ),
    # Hardware and drivers
    _category(
        PillarId.HARDWARE,
        "All Hardware Stack",
        "All hardware acceleration drivers, sound servers, kernel modules, and firmware.",
        ("computer", "drive-harddisk", "system-run"),
        CategoryKind.PILLAR,
        pillar=PillarId.HARDWARE,
        section="hardware",
        order=0,
    ),
    _category(
        "graphics_drivers",
        "Graphics & 3D Drivers",
        "DRM kernel drivers, 3D DRI acceleration stacks, and Vulkan/Mesa libraries.",
        ("video-display", "preferences-desktop-display", "display"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.HARDWARE,
        section="hardware",
        order=1,
        subcategory=True,
        flag="is_graphics_driver",
    ),
    _category(
        "audio_sound",
        "Audio & Sound Architecture",
        "Core PipeWire, WirePlumber, ALSA, and sound-server architecture.",
        ("multimedia-volume-control", "audio-card", "audio-volume-high"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.HARDWARE,
        section="hardware",
        order=2,
        subcategory=True,
        flag="is_audio_sound",
    ),
    _category(
        "kernel_modules",
        "Kernel Modules & DKMS",
        "Dynamic kernel modules, kmod packages, and hardware drivers.",
        ("system-run", "preferences-system-performance", "cpu"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.HARDWARE,
        section="hardware",
        order=3,
        subcategory=True,
        flag="is_kernel_module",
    ),
    _category(
        "firmware",
        "Firmware & Microcode",
        "Binary device microcode and hardware firmware.",
        ("media-flash", "drive-removable-media", "computer", "applications-system"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.HARDWARE,
        section="hardware",
        order=4,
        subcategory=True,
        flag="is_firmware",
    ),
    # System architecture
    _category(
        PillarId.SYSTEM,
        "All System Architecture",
        "Core Fedora boot infrastructure, system services, security policies, and window managers.",
        ("emblem-default", "system-software-install", "system-run"),
        CategoryKind.PILLAR,
        pillar=PillarId.SYSTEM,
        section="system",
        order=0,
    ),
    _category(
        "fedora_core",
        "Fedora Core Infrastructure",
        "Protected Fedora minimal boot infrastructure, packaging tools, and base libraries.",
        (
            "distributor-logo-fedora",
            "fedora-logo-icon",
            "distributor-logo",
            "system-software-install",
        ),
        CategoryKind.TAXONOMY,
        pillar=PillarId.SYSTEM,
        section="system",
        order=1,
        subcategory=True,
        flag="is_fedora_core",
    ),
    _category(
        "systemd_services",
        "Systemd Services & Daemons",
        "Init units, background service daemons, and system tasks.",
        ("preferences-system-services", "system-run", "applications-system"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.SYSTEM,
        section="system",
        order=2,
        subcategory=True,
        flag="is_systemd_service",
    ),
    _category(
        "security_pkgs",
        "Security, PAM & SELinux",
        "Dedicated authentication, PAM security modules, and SELinux policies.",
        ("dialog-password", "system-lock-screen", "security-high", "emblem-locked"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.SYSTEM,
        section="system",
        order=3,
        subcategory=True,
        flag="is_security_pkg",
    ),
    _category(
        "desktop_addons",
        "Window Managers & Addons",
        "Window managers, compositors, shell extensions, input methods, and KIO workers.",
        ("application-x-addon", "preferences-desktop", "emblem-favorite"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.SYSTEM,
        section="system",
        order=4,
        subcategory=True,
        flag="is_desktop_addon",
    ),
    _category(
        "system_data",
        "System Data & Metadata",
        "System-wide timezone, hardware, MIME, keyboard, desktop-catalog, and configuration data.",
        ("package-x-generic", "preferences-system", "text-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.SYSTEM,
        section="system",
        order=5,
        subcategory=True,
    ),
    # Libraries and development
    _category(
        PillarId.LIBRARIES,
        "All Libraries & Runtimes",
        "Shared libraries, developer headers, widget toolkits, language packages, and fonts.",
        ("applications-development", "package-x-generic", "application-x-sharedlib"),
        CategoryKind.PILLAR,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=0,
    ),
    _category(
        "c_libs",
        "C/C++ Shared Libraries",
        "Dynamic C/C++ ELF shared-object libraries and ABI providers.",
        ("application-x-sharedlib", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=1,
        subcategory=True,
        flag="is_c_lib",
    ),
    _category(
        "devel",
        "Development Headers & SDKs",
        "C/C++ header interfaces, static libraries, and pkg-config files.",
        ("applications-development", "text-x-c++src", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=2,
        subcategory=True,
        flag="is_devel",
    ),
    _category(
        "gui_toolkits",
        "GUI Frameworks & Toolkits",
        "Widget frameworks and bindings.",
        (
            "applications-graphics",
            "preferences-desktop-theme",
            "applications-development",
        ),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=3,
        subcategory=True,
        flag="is_gui_toolkit",
    ),
    _category(
        "media_plugins",
        "Media Codecs & Plugins",
        "Audio/video codecs, format decoders, and media-player extensions.",
        ("applications-multimedia", "video-x-generic", "audio-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=4,
        subcategory=True,
        flag="is_media_plugin",
    ),
    _category(
        "python_pkgs",
        "Python Ecosystem",
        "Python language runtime libraries and site-packages modules.",
        ("text-x-python", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=5,
        subcategory=True,
        flag="is_python_pkg",
        facet_tag="Python",
    ),
    _category(
        "rust_pkgs",
        "Rust & Cargo Crates",
        "Rust ecosystem binaries, compiled tools, and Cargo crates.",
        (
            "application-x-executable",
            "applications-engineering",
            "text-x-rust",
            "system-run",
        ),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=6,
        subcategory=True,
        flag="is_rust_pkg",
        facet_tag="Rust",
    ),
    _category(
        "jvm_pkgs",
        "Java & JVM Platform",
        "Java virtual-machine runtimes, JAR packages, and JVM development tools.",
        ("application-x-java", "text-x-java", "application-x-jar", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=7,
        subcategory=True,
        flag="is_jvm_pkg",
        facet_tag="Java/JVM",
    ),
    _category(
        "nodejs_pkgs",
        "Node.js & Web Runtimes",
        "Node.js ecosystem packages, npm modules, and web runtimes.",
        ("text-javascript", "application-javascript", "text-x-javascript", "text-html"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=8,
        subcategory=True,
        flag="is_nodejs_pkg",
        facet_tag="Node.js",
    ),
    _category(
        "perl_pkgs",
        "Perl & CPAN",
        "Perl runtimes, CPAN modules, and RPM packages that expose Perl capabilities.",
        ("text-x-script", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=9,
        subcategory=True,
        facet_tag="Perl",
    ),
    _category(
        "tex_pkgs",
        "TeX & LaTeX",
        "TeX Live, LaTeX classes, styles, engines, and typesetting runtime components.",
        ("text-x-tex", "x-office-document", "applications-development"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=10,
        subcategory=True,
        facet_tag="TeX/LaTeX",
    ),
    _category(
        "go_pkgs",
        "Go Modules & Toolchain",
        "Go language modules, import-path providers, and toolchain components.",
        ("text-x-go", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=11,
        subcategory=True,
        facet_tag="Go",
    ),
    _category(
        "haskell_pkgs",
        "Haskell & GHC",
        "Glasgow Haskell Compiler runtimes, libraries, and profiling components.",
        ("text-x-haskell", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=12,
        subcategory=True,
        facet_tag="Haskell",
    ),
    _category(
        "ruby_pkgs",
        "Ruby & RubyGems",
        "Ruby runtimes, extension libraries, and RPM-packaged RubyGems.",
        ("text-x-ruby", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=13,
        subcategory=True,
        facet_tag="Ruby",
    ),
    _category(
        "r_pkgs",
        "R & CRAN",
        "R language runtimes and RPM-packaged CRAN or Bioconductor modules.",
        ("text-x-r", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=14,
        subcategory=True,
        facet_tag="R/CRAN",
    ),
    _category(
        "php_pkgs",
        "PHP & Composer",
        "PHP runtimes, Composer libraries, PEAR modules, and PECL extensions.",
        ("text-x-php", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=15,
        subcategory=True,
        facet_tag="PHP",
    ),
    _category(
        "ocaml_pkgs",
        "OCaml Ecosystem",
        "OCaml runtimes, libraries, compiler packages, and language bindings.",
        ("text-x-ocaml", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=16,
        subcategory=True,
        facet_tag="OCaml",
    ),
    _category(
        "mingw_pkgs",
        "MinGW Cross-Compilation",
        "Windows-targeted MinGW runtimes, headers, import libraries, and toolchain components.",
        ("applications-development", "computer", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=17,
        subcategory=True,
        facet_tag="MinGW",
    ),
    _category(
        "dotnet_pkgs",
        ".NET & Mono",
        "Mono/.NET runtimes, managed assemblies, bindings, and development components.",
        ("applications-development", "application-x-executable", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=18,
        subcategory=True,
        facet_tag=".NET/Mono",
    ),
    _category(
        "lua_pkgs",
        "Lua & LuaRocks",
        "Lua runtimes, modules, LuaRocks packages, and compatible language components.",
        ("text-x-script", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=19,
        subcategory=True,
        facet_tag="Lua",
    ),
    _category(
        "tcltk_pkgs",
        "Tcl/Tk",
        "Tcl and Tk runtimes, modules, extensions, and GUI language components.",
        ("text-x-script", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=20,
        subcategory=True,
        facet_tag="Tcl/Tk",
    ),
    _category(
        "erlang_pkgs",
        "Erlang & Elixir",
        "Erlang/OTP and Elixir runtimes, applications, modules, and extensions.",
        ("text-x-script", "applications-development", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=21,
        subcategory=True,
        facet_tag="Erlang/Elixir",
    ),
    _category(
        "fonts",
        "Fonts & Typography",
        "TrueType, OpenType, and bitmap typography assets.",
        ("preferences-desktop-font", "font-x-generic", "format-text-bold"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=22,
        subcategory=True,
        flag="is_font",
    ),
    _category(
        "themes",
        "Themes & Visual Assets",
        "Desktop visual styles, icon packs, cursors, and wallpaper collections.",
        (
            "preferences-desktop-theme",
            "applications-graphics",
            "preferences-desktop-wallpaper",
        ),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=23,
        subcategory=True,
        flag="is_theme",
    ),
    _category(
        "locales",
        "Locales & Translations",
        "System translations, linguistic dictionaries, and locale definitions.",
        ("preferences-desktop-locale", "config-language", "locale"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=24,
        subcategory=True,
        flag="is_locale",
    ),
    _category(
        "reference_data",
        "Reference, Standards & Format Data",
        "Shared XML, documentation, OCR, syntax, PDF, and other format-reference resources.",
        ("text-x-generic", "x-office-document", "help-browser"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.LIBRARIES,
        section="libraries",
        order=25,
        subcategory=True,
    ),
    # Maintenance and sources
    _category(
        "updates_available",
        "Available Updates",
        "Installed packages with newer versions or security errata pending in enabled repositories.",
        ("software-update-available", "system-software-update", "emblem-important"),
        CategoryKind.FACET,
        pillar=PillarId.MAINTENANCE,
        section="maintenance",
        order=0,
    ),
    _category(
        "user_installed",
        "User-Installed Packages",
        "Packages explicitly requested by the user, separated from background dependencies.",
        ("user-home", "emblem-default", "system-software-install"),
        CategoryKind.FACET,
        pillar=PillarId.MAINTENANCE,
        section="maintenance",
        order=1,
    ),
    _category(
        "orphans",
        "Orphan Packages",
        "Leaf dependencies that are no longer required by any installed package.",
        ("user-trash", "edit-delete", "trash-empty"),
        CategoryKind.FACET,
        pillar=PillarId.MAINTENANCE,
        section="maintenance",
        order=2,
    ),
    _category(
        "copr_repos",
        "COPR Repositories",
        "Packages built and installed from Fedora Community COPR repositories.",
        ("package-x-generic", "system-software-install", "application-x-addon"),
        CategoryKind.FACET,
        pillar=PillarId.MAINTENANCE,
        section="maintenance",
        order=3,
    ),
    _category(
        "rpmfusion_repos",
        "RPM Fusion Packages",
        "Packages sourced from RPM Fusion Free and Nonfree repositories.",
        ("drive-optical", "media-optical", "system-software-install"),
        CategoryKind.FACET,
        pillar=PillarId.MAINTENANCE,
        section="maintenance",
        order=4,
    ),
    _category(
        "queued",
        "Pending Changes",
        "Packages currently staged for installation, removal, or upgrade in this session.",
        ("document-save", "emblem-default", "dialog-ok-apply"),
        CategoryKind.FACET,
        pillar=PillarId.MAINTENANCE,
        section="maintenance",
        order=5,
    ),
    _category(
        "repository_sources",
        "Repository Sources & Signing Keys",
        "DNF repository definitions, distribution signing keys, and repository-release configuration packages.",
        ("folder-remote", "emblem-readonly", "security-high"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.MAINTENANCE,
        section="maintenance",
        order=6,
        subcategory=True,
    ),
    _category(
        "unclassified",
        "Needs Classification",
        "Package metadata is not sufficient to assign a trustworthy category. Review its details and DNF plan.",
        ("dialog-question", "help-browser", "package-x-generic"),
        CategoryKind.TAXONOMY,
        pillar=PillarId.MAINTENANCE,
        section="maintenance",
        order=7,
        subcategory=False,
    ),
    _category(
        "all",
        "All Installed Packages",
        "Complete unfiltered list of installed RPM packages on this system.",
        ("system-software-install", "package-x-generic", "system-run"),
        CategoryKind.ALL,
        pillar=PillarId.MAINTENANCE,
        section="maintenance",
        order=8,
    ),
    # Search-only parent filters kept registered for API/search compatibility.
    _category(
        PillarId.APPS,
        "Desktop Applications",
        "All package categories under Desktop Applications.",
        ("applications-other",),
        CategoryKind.PILLAR,
        pillar=PillarId.APPS,
    ),
    _category(
        PillarId.CLI,
        "Command-Line Utilities",
        "All package categories under Command-Line Utilities.",
        ("utilities-terminal",),
        CategoryKind.PILLAR,
        pillar=PillarId.CLI,
    ),
)

CATEGORY_SPECS: Final[Mapping[str, CategorySpec]] = {
    spec.identifier: spec for spec in _CATEGORY_SPECS
}
CATEGORY_LABELS: Final[Mapping[str, str]] = {
    identifier: spec.label for identifier, spec in CATEGORY_SPECS.items()
}
PILLAR_LABELS: Final[Mapping[str, str]] = {
    identifier: spec.label for identifier, spec in PILLAR_SPECS.items()
}


def category_spec(identifier: str) -> CategorySpec | None:
    return CATEGORY_SPECS.get(str(identifier).lower())


def category_label(identifier: str, fallback: str = "Unclassified") -> str:
    spec = category_spec(identifier)
    return spec.label if spec is not None else fallback


def category_removal_risk(identifier: str) -> RemovalRiskLevel | None:
    """Return registry-defined removal guidance for one stable category ID."""
    spec = category_spec(identifier)
    return spec.removal_risk if spec is not None else None


def pillar_label(identifier: str, fallback: str = "Package classification") -> str:
    return PILLAR_LABELS.get(str(identifier), fallback)


def sidebar_rows() -> tuple[SidebarRow, ...]:
    """Return deterministic, registry-derived sidebar rows."""
    rows: list[SidebarRow] = []
    for section in sorted(SIDEBAR_SECTIONS, key=lambda item: item.order):
        rows.append(SidebarRow(section.label, "", True))
        specs = sorted(
            (
                spec
                for spec in CATEGORY_SPECS.values()
                if spec.sidebar_section == section.identifier
            ),
            key=lambda spec: spec.sidebar_order,
        )
        rows.extend(
            SidebarRow(spec.label, spec.identifier, False, spec.is_subcategory)
            for spec in specs
        )
    return tuple(rows)


def sidebar_category_ids() -> tuple[str, ...]:
    return tuple(row.tag for row in sidebar_rows() if not row.is_header)


# Precomputed lookup tables keep sidebar badge refreshes inexpensive on systems
# with tens of thousands of installed packages. They are derived solely from the
# registry, so optimization cannot introduce a second taxonomy vocabulary.
_SIDEBAR_CATEGORY_ID_SET: Final[frozenset[str]] = frozenset(sidebar_category_ids())
_FLAG_CATEGORY_IDS: Final[Mapping[str, tuple[str, ...]]] = {
    flag: tuple(
        spec.identifier
        for spec in CATEGORY_SPECS.values()
        if spec.flag_attribute == flag
    )
    for flag in {
        spec.flag_attribute for spec in CATEGORY_SPECS.values() if spec.flag_attribute
    }
}
_FACET_CATEGORY_IDS: Final[tuple[str, ...]] = tuple(
    spec.identifier
    for spec in CATEGORY_SPECS.values()
    if spec.kind == CategoryKind.FACET
)
_TAG_CATEGORY_IDS: Final[Mapping[str, tuple[str, ...]]] = {
    tag: tuple(
        spec.identifier for spec in CATEGORY_SPECS.values() if spec.facet_tag == tag
    )
    for tag in {spec.facet_tag for spec in CATEGORY_SPECS.values() if spec.facet_tag}
}


def taxonomy_output_ids() -> frozenset[str]:
    """Categories the classifier is permitted to emit as primary/subcategory."""
    return frozenset(
        spec.identifier
        for spec in CATEGORY_SPECS.values()
        if spec.kind in {CategoryKind.TAXONOMY, CategoryKind.TOP_LEVEL}
    )


def validate_taxonomy_ids(*identifiers: str) -> None:
    missing = sorted(
        {
            str(identifier)
            for identifier in identifiers
            if str(identifier) not in CATEGORY_SPECS
        }
    )
    if missing:
        raise ValueError(
            f"Unknown taxonomy category identifier(s): {', '.join(missing)}"
        )


def validate_classification_path(
    parent_pillar: str, primary_category: str, sub_category: str
) -> None:
    """Reject a classifier result that is not a coherent registry path."""
    normalized_pillar = str(parent_pillar)
    if normalized_pillar not in PILLAR_SPECS:
        raise ValueError(f"Unknown taxonomy pillar identifier: {normalized_pillar}")
    validate_taxonomy_ids(primary_category, sub_category)
    mismatched_categories = sorted(
        identifier
        for identifier in {str(primary_category), str(sub_category)}
        if (spec := category_spec(identifier)) is not None
        and spec.pillar is not None
        and str(spec.pillar) != normalized_pillar
    )
    if mismatched_categories:
        raise ValueError(
            "Taxonomy category/pillar mismatch for "
            f"{normalized_pillar}: {', '.join(mismatched_categories)}"
        )


def _state_name(package: Any) -> str:
    state = getattr(package, "state", None)
    return getattr(state, "name", str(state)).upper()


def category_matches(package: Any, identifier: str) -> bool:
    """Central category/facet predicate used by both counts and filtering."""
    spec = category_spec(identifier)
    if spec is None:
        return False

    if spec.kind == CategoryKind.ALL:
        return True
    if spec.kind == CategoryKind.PILLAR:
        return getattr(package, "parent_pillar", "") == spec.identifier
    if spec.kind == CategoryKind.TOP_LEVEL:
        if spec.identifier == "user_apps":
            return bool(getattr(package, "is_desktop_app", False)) and not bool(
                getattr(package, "is_system_settings", False)
            )
        if spec.identifier == "cli_tools":
            return bool(getattr(package, "is_cli_tool", False))
        return False
    if spec.kind == CategoryKind.TAXONOMY:
        if spec.identifier in {
            getattr(package, "primary_category", ""),
            getattr(package, "sub_category", ""),
        }:
            return True
        if spec.flag_attribute and getattr(package, spec.flag_attribute, False):
            return True
        return bool(
            spec.facet_tag and spec.facet_tag in getattr(package, "secondary_tags", ())
        )

    if spec.identifier == "updates_available":
        return bool(getattr(package, "has_update", False))
    if spec.identifier == "orphans":
        return bool(getattr(package, "is_orphan", False))
    if spec.identifier == "user_installed":
        return bool(getattr(package, "is_user_installed", False))
    if spec.identifier == "queued":
        return _state_name(package) in {
            "QUEUED_INSTALL",
            "QUEUED_REMOVE",
            "QUEUED_UPGRADE",
        }
    if spec.identifier == "copr_repos":
        return "copr" in str(getattr(package, "repository", "")).lower()
    if spec.identifier == "rpmfusion_repos":
        return "rpm fusion" in str(getattr(package, "repository", "")).lower()
    return False


def matching_sidebar_categories(package: Any) -> frozenset[str]:
    """Return visible category memberships for one package in near-constant time.

    Badge counts need all memberships, but checking every registry entry for
    every installed package makes a refresh needlessly expensive. Candidate
    IDs come from the package's stored taxonomy fields, active registry flags,
    and the small set of dynamic facets; the final predicate is still the same
    :func:`category_matches` function used by the tree filter.
    """
    candidates = {
        "all",
        str(getattr(package, "parent_pillar", "")),
        str(getattr(package, "primary_category", "")),
        str(getattr(package, "sub_category", "")),
    }
    for flag, identifiers in _FLAG_CATEGORY_IDS.items():
        if getattr(package, flag, False):
            candidates.update(identifiers)
    for tag in getattr(package, "secondary_tags", ()):
        candidates.update(_TAG_CATEGORY_IDS.get(tag, ()))
    candidates.update(_FACET_CATEGORY_IDS)
    return frozenset(
        identifier
        for identifier in candidates
        if identifier in _SIDEBAR_CATEGORY_ID_SET
        and category_matches(package, identifier)
    )


def registry_integrity_errors() -> tuple[str, ...]:
    """Return schema defects for a regression test or developer diagnostic."""
    errors: list[str] = []
    identifiers = tuple(spec.identifier for spec in _CATEGORY_SPECS)
    duplicate_identifiers = sorted(
        {identifier for identifier in identifiers if identifiers.count(identifier) > 1}
    )
    if duplicate_identifiers:
        errors.append(
            "duplicate category identifier(s): " + ", ".join(duplicate_identifiers)
        )
    for output in taxonomy_output_ids():
        spec = category_spec(output)
        if spec is None:
            errors.append(f"classifier output {output!r} is unregistered")
            continue
        if not spec.label or not spec.tooltip or not spec.icon_names:
            errors.append(f"category {output!r} lacks complete presentation metadata")
        if spec.facet_tag is not None and not spec.facet_tag.strip():
            errors.append(f"category {output!r} has an empty facet tag")
        if spec.pillar is None or str(spec.pillar) not in PILLAR_SPECS:
            errors.append(f"category {output!r} lacks a valid taxonomy pillar")
        if spec.removal_risk is None:
            errors.append(f"category {output!r} lacks removal-risk guidance")
    errors.extend(
        f"sidebar row {row.tag!r} is unregistered"
        for row in sidebar_rows()
        if not row.is_header and category_spec(row.tag) is None
    )
    section_ids = {section.identifier for section in SIDEBAR_SECTIONS}
    visible_specs = [spec for spec in _CATEGORY_SPECS if spec.sidebar_section]
    errors.extend(
        f"category {spec.identifier!r} references unknown sidebar section "
        f"{spec.sidebar_section!r}"
        for spec in visible_specs
        if spec.sidebar_section not in section_ids
    )
    sidebar_positions = [
        (spec.sidebar_section, spec.sidebar_order)
        for spec in visible_specs
        if spec.sidebar_section in section_ids
    ]
    duplicate_positions = sorted(
        {
            position
            for position in sidebar_positions
            if sidebar_positions.count(position) > 1
        }
    )
    errors.extend(
        f"duplicate sidebar position {section}:{order}"
        for section, order in duplicate_positions
    )

    # The ecosystem registry intentionally has no taxonomy dependency. Validate
    # its cross-registry contract here, where category presentation is owned.
    from core.ecosystems import ECOSYSTEM_SPECS, ecosystem_registry_integrity_errors

    errors.extend(ecosystem_registry_integrity_errors())
    for ecosystem in ECOSYSTEM_SPECS:
        category = category_spec(ecosystem.category_id)
        if category is None:
            errors.append(
                f"ecosystem {ecosystem.identifier!r} references an unknown category"
            )
        elif category.facet_tag != ecosystem.tag:
            errors.append(
                f"ecosystem {ecosystem.identifier!r} tag does not match "
                f"category {ecosystem.category_id!r}"
            )
    return tuple(errors)
