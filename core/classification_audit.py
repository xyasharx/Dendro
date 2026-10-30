"""Deterministic taxonomy-coverage auditing for RPM package facts.

This module deliberately has no Qt, RPM-binding, DNF, network, or filesystem
catalog dependency.  It accepts normalized facts from any RPM source (installed
headers, local RPM files, repository metadata, or a saved JSONL corpus), routes
them through Dendro's production classifier, and reports the evidence gaps that
remain.  It is therefore suitable for reproducible CI and corpus reviews.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final

from core.package_classifier import ProductionTaxonomyEngine
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

AUDIT_SCHEMA_VERSION: Final = 1


class ClassificationAuditInputError(ValueError):
    """One supplied audit record is malformed or cannot be classified safely."""


@dataclass(frozen=True, slots=True)
class PackageClassificationRecord:
    """Normalized, serializable RPM facts consumed by a taxonomy audit.

    The record intentionally retains raw RPM facts rather than an earlier
    classification result.  This makes a saved corpus useful when rules change:
    rerunning the audit reclassifies exactly the same package evidence.
    """

    name: str
    summary: str = ""
    description: str = ""
    directories: tuple[str, ...] = ()
    basenames: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    architecture: str = ""
    source_rpm: str = ""
    repository: str = ""
    vendor: str = ""
    packager: str = ""
    input_scope: TaxonomyInputScope = TaxonomyInputScope.UNKNOWN

    def __post_init__(self) -> None:
        name = _normalise_required_text(self.name, "name")
        object.__setattr__(self, "name", name)
        for field_name in (
            "summary",
            "description",
            "architecture",
            "source_rpm",
            "repository",
            "vendor",
            "packager",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalise_optional_text(getattr(self, field_name), field_name),
            )
        for field_name in ("directories", "basenames", "provides", "requires"):
            object.__setattr__(
                self,
                field_name,
                _normalise_text_sequence(getattr(self, field_name), field_name),
            )
        try:
            scope = TaxonomyInputScope(self.input_scope)
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(scope.value for scope in TaxonomyInputScope)
            raise ClassificationAuditInputError(
                f"input_scope must be one of: {allowed}"
            ) from exc
        object.__setattr__(self, "input_scope", scope)

    @classmethod
    def from_json_mapping(cls, raw: Mapping[str, Any]) -> PackageClassificationRecord:
        """Validate and construct one record from the JSONL interchange schema."""
        if not isinstance(raw, Mapping):
            raise ClassificationAuditInputError("each JSONL row must be an object")
        known_fields = {
            "name",
            "summary",
            "description",
            "directories",
            "basenames",
            "provides",
            "requires",
            "architecture",
            "source_rpm",
            "repository",
            "vendor",
            "packager",
            "input_scope",
        }
        unexpected = sorted(set(raw).difference(known_fields))
        if unexpected:
            raise ClassificationAuditInputError(
                "unknown audit field(s): " + ", ".join(unexpected)
            )
        if "name" not in raw:
            raise ClassificationAuditInputError("missing required audit field: name")
        return cls(
            name=raw["name"],
            summary=raw.get("summary", ""),
            description=raw.get("description", ""),
            directories=raw.get("directories", ()),
            basenames=raw.get("basenames", ()),
            provides=raw.get("provides", ()),
            requires=raw.get("requires", ()),
            architecture=raw.get("architecture", ""),
            source_rpm=raw.get("source_rpm", ""),
            repository=raw.get("repository", ""),
            vendor=raw.get("vendor", ""),
            packager=raw.get("packager", ""),
            input_scope=raw.get("input_scope", TaxonomyInputScope.UNKNOWN.value),
        )

    def to_json_mapping(self) -> dict[str, object]:
        """Return a stable JSON-serializable representation of this record."""
        return {
            "name": self.name,
            "summary": self.summary,
            "description": self.description,
            "directories": list(self.directories),
            "basenames": list(self.basenames),
            "provides": list(self.provides),
            "requires": list(self.requires),
            "architecture": self.architecture,
            "source_rpm": self.source_rpm,
            "repository": self.repository,
            "vendor": self.vendor,
            "packager": self.packager,
            "input_scope": self.input_scope.value,
        }

    def classify(
        self,
        *,
        appstream: AppStreamCatalog,
        comps: FedoraCompsCatalog,
    ) -> ClassificationDecision:
        """Classify this record with the same public production-engine boundary."""
        anatomy = PackagePhysicalAnatomy.from_manifest_data(
            self.directories,
            self.provides,
            self.basenames,
        )
        # A saved corpus must not accidentally inherit desktop metadata from the
        # machine that happens to run the audit.  AppStream evidence is still
        # injectable above; package-owned desktop files are retained in the raw
        # record for future deterministic desktop-metadata enrichment.
        return ProductionTaxonomyEngine.classify(
            name=self.name,
            summary=self.summary,
            description=self.description,
            anatomy=anatomy,
            desktop_entry_files=[],
            provides=list(self.provides),
            appstream=appstream,
            comps=comps,
            vendor=self.vendor,
            packager=self.packager,
            input_scope=self.input_scope,
        )


@dataclass(frozen=True, slots=True)
class AuditCluster:
    """One recurring unresolved package pattern, with bounded examples."""

    key: str
    count: int
    examples: tuple[str, ...]

    def to_json_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "count": self.count,
            "examples": list(self.examples),
        }


@dataclass(frozen=True, slots=True)
class TaxonomyAuditReport:
    """Aggregate, reproducible classification-coverage result for one corpus."""

    total_records: int
    automatically_classified: int
    unresolved: int
    by_scope: tuple[tuple[str, int], ...]
    by_primary_category: tuple[tuple[str, int], ...]
    by_evidence: tuple[tuple[str, int], ...]
    unresolved_capability_clusters: tuple[AuditCluster, ...]
    unresolved_directory_clusters: tuple[AuditCluster, ...]
    unresolved_name_clusters: tuple[AuditCluster, ...]

    def to_json_mapping(self) -> dict[str, object]:
        """Return a stable JSON report suitable for comparisons in CI."""
        return {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "total_records": self.total_records,
            "automatically_classified": self.automatically_classified,
            "unresolved": self.unresolved,
            "by_scope": _counter_mapping(self.by_scope),
            "by_primary_category": _counter_mapping(self.by_primary_category),
            "by_evidence": _counter_mapping(self.by_evidence),
            "unresolved_clusters": {
                "capability": [
                    cluster.to_json_mapping()
                    for cluster in self.unresolved_capability_clusters
                ],
                "directory": [
                    cluster.to_json_mapping()
                    for cluster in self.unresolved_directory_clusters
                ],
                "name": [
                    cluster.to_json_mapping()
                    for cluster in self.unresolved_name_clusters
                ],
            },
        }


def audit_taxonomy_records(
    records: Iterable[PackageClassificationRecord],
    *,
    appstream: AppStreamCatalog | None = None,
    comps: FedoraCompsCatalog | None = None,
    cluster_limit: int = 25,
    examples_per_cluster: int = 5,
) -> TaxonomyAuditReport:
    """Classify a corpus and summarize its evidence-backed coverage.

    ``appstream`` and ``comps`` are injectable so tests and saved corpora stay
    deterministic.  A caller that wants host catalogs can explicitly pass their
    loaded instances; the default deliberately performs no host metadata I/O.
    """
    if cluster_limit < 1:
        raise ValueError("cluster_limit must be at least one")
    if examples_per_cluster < 1:
        raise ValueError("examples_per_cluster must be at least one")

    appstream = appstream or AppStreamCatalog(load_catalog=False)
    comps = comps or FedoraCompsCatalog(load_comps=False)
    by_scope: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    by_evidence: Counter[str] = Counter()
    capability_clusters: dict[str, _ClusterAccumulator] = {}
    directory_clusters: dict[str, _ClusterAccumulator] = {}
    name_clusters: dict[str, _ClusterAccumulator] = {}
    total_records = 0
    unresolved = 0

    for record in records:
        if not isinstance(record, PackageClassificationRecord):
            raise TypeError("records must contain PackageClassificationRecord values")
        decision = record.classify(appstream=appstream, comps=comps)
        total_records += 1
        by_scope[record.input_scope.value] += 1
        by_category[decision.primary_category] += 1
        by_evidence[decision.evidence.value] += 1
        if decision.evidence != ClassificationEvidence.INSUFFICIENT_METADATA:
            continue

        unresolved += 1
        _add_unresolved_clusters(
            record,
            capability_clusters=capability_clusters,
            directory_clusters=directory_clusters,
            name_clusters=name_clusters,
            examples_per_cluster=examples_per_cluster,
        )

    return TaxonomyAuditReport(
        total_records=total_records,
        automatically_classified=total_records - unresolved,
        unresolved=unresolved,
        by_scope=_sorted_counts(by_scope),
        by_primary_category=_sorted_counts(by_category),
        by_evidence=_sorted_counts(by_evidence),
        unresolved_capability_clusters=_sorted_clusters(
            capability_clusters, cluster_limit
        ),
        unresolved_directory_clusters=_sorted_clusters(
            directory_clusters, cluster_limit
        ),
        unresolved_name_clusters=_sorted_clusters(name_clusters, cluster_limit),
    )


@dataclass(slots=True)
class _ClusterAccumulator:
    count: int = 0
    examples: list[str] | None = None

    def add(self, package_name: str, examples_per_cluster: int) -> None:
        self.count += 1
        if self.examples is None:
            self.examples = []
        if (
            len(self.examples) < examples_per_cluster
            and package_name not in self.examples
        ):
            self.examples.append(package_name)


def _normalise_required_text(value: object, field_name: str) -> str:
    text = _normalise_optional_text(value, field_name)
    if not text:
        raise ClassificationAuditInputError(f"{field_name} must be a non-empty string")
    return text


def _normalise_optional_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ClassificationAuditInputError(f"{field_name} must be a string")
    if "\x00" in value:
        raise ClassificationAuditInputError(
            f"{field_name} must not contain a NUL character"
        )
    return value.strip()


def _normalise_text_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ClassificationAuditInputError(f"{field_name} must be an array of strings")
    values: list[str] = []
    for item in value:
        normalised = _normalise_optional_text(item, field_name)
        if normalised:
            values.append(normalised)
    return tuple(values)


def _add_unresolved_clusters(
    record: PackageClassificationRecord,
    *,
    capability_clusters: dict[str, _ClusterAccumulator],
    directory_clusters: dict[str, _ClusterAccumulator],
    name_clusters: dict[str, _ClusterAccumulator],
    examples_per_cluster: int,
) -> None:
    capability_namespaces = {
        namespace
        for capability in record.provides
        if (namespace := _capability_namespace(capability)) is not None
    }
    for namespace in capability_namespaces:
        _cluster_add(
            capability_clusters,
            namespace,
            record.name,
            examples_per_cluster,
        )
    directory_roots = {
        root for directory in record.directories if (root := _directory_root(directory))
    }
    for root in directory_roots:
        _cluster_add(directory_clusters, root, record.name, examples_per_cluster)
    _cluster_add(
        name_clusters,
        _package_name_family(record.name),
        record.name,
        examples_per_cluster,
    )


def _capability_namespace(capability: str) -> str | None:
    prefix, delimiter, _remainder = capability.partition("(")
    if not delimiter:
        return None
    normalised = prefix.strip().lower()
    return normalised or None


def _directory_root(directory: str) -> str | None:
    path = PurePosixPath(directory)
    if not path.is_absolute():
        return None
    components = path.parts[1:4]
    if not components:
        return "/"
    return "/" + "/".join(components)


def _package_name_family(package_name: str) -> str:
    return package_name.lower().split("-", 1)[0]


def _cluster_add(
    clusters: dict[str, _ClusterAccumulator],
    key: str,
    package_name: str,
    examples_per_cluster: int,
) -> None:
    accumulator = clusters.setdefault(key, _ClusterAccumulator())
    accumulator.add(package_name, examples_per_cluster)


def _sorted_counts(counter: Counter[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _sorted_clusters(
    clusters: Mapping[str, _ClusterAccumulator], limit: int
) -> tuple[AuditCluster, ...]:
    ordered = sorted(clusters.items(), key=lambda item: (-item[1].count, item[0]))
    return tuple(
        AuditCluster(
            key=key,
            count=accumulator.count,
            examples=tuple(accumulator.examples or ()),
        )
        for key, accumulator in ordered[:limit]
    )


def _counter_mapping(counts: tuple[tuple[str, int], ...]) -> dict[str, int]:
    return {key: value for key, value in counts}
