"""Single source of truth for Dendro's package-taxonomy vocabulary.

This module intentionally has no Qt, RPM, DNF, or filesystem dependency.  Both
classification and presentation import the same category registry so adding a
new category cannot silently create a classifier/sidebar/filter mismatch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from typing import Final

from dendro.core.i18n import tr


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
            ClassificationEvidence.MANUAL_OVERRIDE: tr("Explicit package policy"),
            ClassificationEvidence.APPSTREAM: tr("AppStream metadata"),
            ClassificationEvidence.DESKTOP_ENTRY: tr("Desktop-entry metadata"),
            ClassificationEvidence.RPM_MANIFEST: tr("RPM manifest"),
            ClassificationEvidence.COMPS: tr("Fedora Comps metadata"),
            ClassificationEvidence.NAME_HEURISTIC: tr("Package-name heuristic"),
            ClassificationEvidence.INSUFFICIENT_METADATA: tr("Insufficient metadata"),
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
            TaxonomyInputScope.INSTALLED_MANIFEST: tr("Installed RPM manifest"),
            TaxonomyInputScope.LOCAL_RPM_MANIFEST: tr("Local RPM manifest"),
            TaxonomyInputScope.REMOTE_METADATA: tr("Remote repository metadata"),
            TaxonomyInputScope.UNKNOWN: tr("Unknown metadata scope"),
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
    """A top-level taxonomy domain.

    ``label`` is the English message id, not a rendered string: translation
    happens in :attr:`label_text` so switching language does not require
    re-importing this module.  ``label`` stays public because it is the catalog
    key and is what tests and the extractor compare against.
    """

    identifier: str
    label: str

    @property
    def label_text(self) -> str:
        """The label rendered in the active language."""
        return tr(self.label)


@dataclass(frozen=True, slots=True)
class CategorySpec:
    identifier: str
    #: English message id for the category name; see :attr:`label_text`.
    label: str
    #: English message id for the tooltip; see :attr:`tooltip_text`.
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

    @property
    def label_text(self) -> str:
        """The category name rendered in the active language."""
        return tr(self.label)

    @property
    def tooltip_text(self) -> str:
        """The category description rendered in the active language."""
        return tr(self.tooltip)


@dataclass(frozen=True, slots=True)
class SidebarSection:
    identifier: str
    #: English message id; see :attr:`label_text`.
    label: str
    order: int

    @property
    def label_text(self) -> str:
        """The section heading rendered in the active language."""
        return tr(self.label)


@dataclass(frozen=True, slots=True)
class SidebarRow:
    #: English message id for the row label; see :attr:`label_text`.
    label: str
    tag: str
    is_header: bool
    is_subcategory: bool = False
    parent_tag: str = ""

    @property
    def label_text(self) -> str:
        """The row label rendered in the active language."""
        return tr(self.label)


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
    SidebarSection("maintenance", "6. Maintenance & Queue", 6),
    SidebarSection("sources", "7. Sources & Catalog", 7),
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


__all__ = (
    "PILLAR_SPECS",
    "SIDEBAR_SECTIONS",
    "_DEFAULT_REMOVAL_RISK_BY_PILLAR",
    "_REMOVAL_RISK_OVERRIDES",
    "CategoryKind",
    "CategorySpec",
    "ClassificationDecision",
    "ClassificationEvidence",
    "PackageArchetype",
    "PillarId",
    "PillarSpec",
    "RemovalRiskLevel",
    "RemovalSafetyAssessment",
    "SidebarRow",
    "SidebarSection",
    "TaxonomyInputScope",
    "_category",
    "classification_evidence_label",
    "taxonomy_input_scope_label",
)
