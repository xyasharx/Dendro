# dendro/core/taxonomy.py
"""Single source of truth for Dendro's package-taxonomy vocabulary.

This module intentionally has no Qt, RPM, DNF, or filesystem dependency.  Both
classification and presentation import the same category registry so adding a
new category cannot silently create a classifier/sidebar/filter mismatch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from core.taxonomy_registry import (
    _CATEGORY_SPECS,
    CATEGORY_LABELS,
    CATEGORY_SPECS,
    PILLAR_LABELS,
)
from core.taxonomy_schema import (
    _DEFAULT_REMOVAL_RISK_BY_PILLAR,
    _REMOVAL_RISK_OVERRIDES,
    PILLAR_SPECS,
    SIDEBAR_SECTIONS,
    CategoryKind,
    CategorySpec,
    ClassificationDecision,
    ClassificationEvidence,
    PackageArchetype,
    PillarId,
    PillarSpec,
    RemovalRiskLevel,
    RemovalSafetyAssessment,
    SidebarRow,
    SidebarSection,
    TaxonomyInputScope,
    _category,
    classification_evidence_label,
    taxonomy_input_scope_label,
)


def category_spec(identifier: str) -> CategorySpec | None:
    return CATEGORY_SPECS.get(str(identifier).lower())


def category_label(identifier: str, fallback: str = "Unclassified") -> str:
    """Return the display name for a category, translated.

    ``fallback`` is used verbatim when the identifier is unknown; callers that
    pass a message id should wrap it in :func:`core.i18n.tr` themselves, because
    this function cannot distinguish a message id from rendered text.
    """

    spec = category_spec(identifier)
    return spec.label_text if spec is not None else fallback


def category_removal_risk(identifier: str) -> RemovalRiskLevel | None:
    """Return registry-defined removal guidance for one stable category ID."""
    spec = category_spec(identifier)
    return spec.removal_risk if spec is not None else None


def pillar_label(identifier: str, fallback: str = "Package classification") -> str:
    """Return the display name for a top-level pillar, translated."""

    spec = PILLAR_SPECS.get(str(identifier))
    return spec.label_text if spec is not None else fallback


_UMBRELLA_PARENT_KINDS: Final[frozenset[CategoryKind]] = frozenset(
    {CategoryKind.PILLAR, CategoryKind.TOP_LEVEL}
)


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
        active_parent_tag = ""
        for spec in specs:
            if not spec.is_subcategory:
                active_parent_tag = (
                    spec.identifier if spec.kind in _UMBRELLA_PARENT_KINDS else ""
                )
                rows.append(SidebarRow(spec.label, spec.identifier, False, False, ""))
            else:
                rows.append(
                    SidebarRow(
                        spec.label,
                        spec.identifier,
                        False,
                        True,
                        active_parent_tag,
                    )
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
        repo_lower = str(getattr(package, "repository", "")).lower()
        return "rpm fusion" in repo_lower or "rpmfusion" in repo_lower
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


def sidebar_hierarchy_errors(
    sections: Sequence[SidebarSection] = SIDEBAR_SECTIONS,
    specs: Sequence[CategorySpec] = _CATEGORY_SPECS,
) -> tuple[str, ...]:
    """Validate that indented sidebar rows always nest under a real umbrella row.

    Without this invariant, setting ``subcategory=True`` on a category placed
    below a standalone facet (for example, the session transaction queue) makes
    the sidebar visually imply a parent/child relationship that does not exist.
    """
    errors: list[str] = []
    section_ids_list = [section.identifier for section in sections]
    duplicate_sections = sorted(
        {
            identifier
            for identifier in section_ids_list
            if section_ids_list.count(identifier) > 1
        }
    )
    if duplicate_sections:
        errors.append("duplicate sidebar section(s): " + ", ".join(duplicate_sections))

    section_orders = [section.order for section in sections]
    duplicate_orders = sorted(
        {order for order in section_orders if section_orders.count(order) > 1}
    )
    errors.extend(
        f"duplicate sidebar section order {order}" for order in duplicate_orders
    )

    section_ids = set(section_ids_list)
    visible_specs = [spec for spec in specs if spec.sidebar_section]
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

    for section in sorted(sections, key=lambda item: item.order):
        section_specs = sorted(
            (
                spec
                for spec in visible_specs
                if spec.sidebar_section == section.identifier
            ),
            key=lambda spec: spec.sidebar_order,
        )
        if not section_specs:
            errors.append(f"sidebar section {section.identifier!r} has no categories")
            continue

        parent_spec: CategorySpec | None = None
        previous_spec: CategorySpec | None = None
        for spec in section_specs:
            if not spec.is_subcategory:
                parent_spec = spec if spec.kind in _UMBRELLA_PARENT_KINDS else None
                previous_spec = spec
                continue

            if parent_spec is None:
                if previous_spec is None:
                    errors.append(
                        f"subcategory {spec.identifier!r} starts sidebar section "
                        f"{section.identifier!r} without a parent umbrella row"
                    )
                else:
                    errors.append(
                        f"subcategory {spec.identifier!r} in section "
                        f"{section.identifier!r} has no umbrella parent row "
                        f"(preceded by {previous_spec.identifier!r} of kind "
                        f"{previous_spec.kind.value!r})"
                    )
            elif spec.pillar != parent_spec.pillar:
                errors.append(
                    f"subcategory {spec.identifier!r} pillar {spec.pillar!r} does not "
                    f"match umbrella parent {parent_spec.identifier!r} "
                    f"({parent_spec.pillar!r})"
                )
            previous_spec = spec

    return tuple(errors)


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
    errors.extend(sidebar_hierarchy_errors())

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


__all__ = (
    "CATEGORY_LABELS",
    "CATEGORY_SPECS",
    "PILLAR_LABELS",
    "PILLAR_SPECS",
    "SIDEBAR_SECTIONS",
    "_CATEGORY_SPECS",
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
    "category_label",
    "category_matches",
    "category_removal_risk",
    "category_spec",
    "classification_evidence_label",
    "matching_sidebar_categories",
    "pillar_label",
    "registry_integrity_errors",
    "sidebar_category_ids",
    "sidebar_hierarchy_errors",
    "sidebar_rows",
    "taxonomy_input_scope_label",
    "taxonomy_output_ids",
    "validate_classification_path",
    "validate_taxonomy_ids",
)
