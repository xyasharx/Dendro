# dendro/core/backend.py
"""Compatibility facade for Dendro's package backend and worker types.

Domain models live in :mod:`dendro.core.package_types`, cache persistence in
:mod:`dendro.core.capability_cache`, pure parsers in :mod:`dendro.core.backend_parsers`,
native RPM/file inspection in :mod:`dendro.core.rpm_inspection`, RPM workers in
:mod:`dendro.core.rpm_workers`, and DNF5/DNF query workers in :mod:`dendro.core.dnf_workers`.
This facade preserves every public and test-visible symbol without holding
implementation bloat.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime

import dendro.core.package_classifier as _package_classifier
import dendro.core.package_metadata as _package_metadata
import dendro.core.taxonomy as _taxonomy
from dendro.core.backend_parsers import (
    CVE_REGEX_PATTERN,
    REMOTE_QUERY_FIELD_SEPARATOR,
    REMOTE_QUERY_RECORD_SEPARATOR,
    RPM_DATABASE_PSEUDO_PACKAGE_NAMES,
    RPM_PACKAGE_NAME_PATTERN,
    RPM_VERIFY_CHANGED_PATTERN,
    RPM_VERIFY_MISSING_PATTERN,
    _classify_local_rpm_manifest,
    _dnf5_json_option_is_unsupported,
    _is_dnf5_binary,
    _parse_dnf5_history_json,
    _parse_dnf_package_identity_output,
    _parse_remote_package_records,
    _RemotePackageRecord,
    classification_package_fields,
    parse_rpm_verify_line,
)
from dendro.core.capability_cache import SQLiteCapabilityCache
from dendro.core.dnf5daemon import (
    Dnf5DaemonClient,
    Dnf5DaemonError,
    Dnf5DaemonUnavailable,
    Dnf5DaemonUnsupportedError,
    OfflineTransactionPhase,
    OfflineTransactionStatus,
)
from dendro.core.dnf_workers import (
    _DEFAULT_GET_DNF_BINARY_PATH,
    DnfHistoryWorker,
    OfflineStatusQueryWorker,
    OfflineStatusSignals,
    OrphanQueryWorker,
    RemotePackageSearchWorker,
    SecurityAdvisoryWorker,
    SystemRebootSignals,
    SystemRebootWorker,
    SystemUpdatesCheckWorker,
    UserInstalledQueryWorker,
    get_dnf_binary_path,
    get_host_environment,
)
from dendro.core.package_classifier import (
    DEFAULT_SYSTEM_ROOT_PILLARS,
    FEDORA_SYSTEM_ROOT_PILLARS,
    ProductionTaxonomyEngine,
)
from dendro.core.package_metadata import (
    AppStreamCatalog,
    AppStreamComponent,
    FedoraCompsCatalog,
    PackagePhysicalAnatomy,
)
from dendro.core.package_types import (
    AvailableUpdateInfo,
    DependencyNode,
    FileVerificationResult,
    HistoryEntry,
    LocalRpmInspectionResult,
    PackageChangelogEntry,
    PackageFileInfo,
    PackageInfo,
    PackageScriptlets,
    PackageState,
    RepoInfo,
    _decode_rpm_str,
    _normalise_rpm_epoch,
    format_byte_size,
)
from dendro.core.process_utils import (
    describe_subprocess_failure,
    flatpak_read_only_unavailable,
    get_clean_env,
    is_running_in_flatpak,
)
from dendro.core.rpm_inspection import (
    HAS_NATIVE_RPM,
    RPM_GLOBAL_LOCK,
    FileOwnerLookupError,
    FileOwnerLookupSignals,
    FileOwnerLookupWorker,
    LocalRpmInspectionError,
    LocalRpmInspectionSignals,
    LocalRpmInspectionWorker,
    _create_read_only_rpm_transaction_set_unlocked,
    _header_matches_package,
    _rpm_query_target,
    create_rpm_transaction_set,
    find_package_owning_file_info,
    get_system_protected_packages,
    inspect_local_rpm_file,
    rpm,
)
from dendro.core.rpm_workers import (
    BackendSignals,
    DependencyTreeWorker,
    PackageChangelogWorker,
    PackageFilesWorker,
    PackageQueryWorker,
    PackageScriptletsWorker,
    PackageVerifyWorker,
    ReverseDependencyWorker,
)
from dendro.core.taxonomy import (
    ClassificationDecision,
    ClassificationEvidence,
    TaxonomyInputScope,
)

# Historical backend import compatibility aliases
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

__all__ = (
    "CVE_REGEX_PATTERN",
    "DEFAULT_FEDORA_CORE_PACKAGES",
    "DEFAULT_SYSTEM_ROOT_PILLARS",
    "DESKTOP_SESSION_DIRS",
    "FEDORA_SYSTEM_ROOT_PILLARS",
    "GUI_TOOLKIT_DIR_KEYWORDS",
    "HAS_NATIVE_RPM",
    "MEDIA_PLUGIN_DIR_KEYWORDS",
    "PACKAGE_TAXONOMY_OVERRIDES",
    "REMOTE_QUERY_FIELD_SEPARATOR",
    "REMOTE_QUERY_RECORD_SEPARATOR",
    "RPM_DATABASE_PSEUDO_PACKAGE_NAMES",
    "RPM_GLOBAL_LOCK",
    "RPM_PACKAGE_NAME_PATTERN",
    "RPM_VERIFY_CHANGED_PATTERN",
    "RPM_VERIFY_MISSING_PATTERN",
    "_DEFAULT_GET_DNF_BINARY_PATH",
    "AppStreamCatalog",
    "AppStreamComponent",
    "AvailableUpdateInfo",
    "BackendSignals",
    "ClassificationDecision",
    "ClassificationEvidence",
    "DependencyNode",
    "DependencyTreeWorker",
    "DesktopEntryMetadata",
    "Dnf5DaemonClient",
    "Dnf5DaemonError",
    "Dnf5DaemonUnavailable",
    "Dnf5DaemonUnsupportedError",
    "DnfHistoryWorker",
    "FedoraCompsCatalog",
    "FileOwnerLookupError",
    "FileOwnerLookupSignals",
    "FileOwnerLookupWorker",
    "FileVerificationResult",
    "FreeDesktopTaxonomyEngine",
    "HistoryEntry",
    "IntelligentPackageClassifier",
    "LocalRpmInspectionError",
    "LocalRpmInspectionResult",
    "LocalRpmInspectionSignals",
    "LocalRpmInspectionWorker",
    "OfflineStatusQueryWorker",
    "OfflineStatusSignals",
    "OfflineTransactionPhase",
    "OfflineTransactionStatus",
    "OrphanQueryWorker",
    "PackageArchetype",
    "PackageChangelogEntry",
    "PackageChangelogWorker",
    "PackageFileInfo",
    "PackageFilesWorker",
    "PackageInfo",
    "PackagePhysicalAnatomy",
    "PackageQueryWorker",
    "PackageScriptlets",
    "PackageScriptletsWorker",
    "PackageState",
    "PackageVerifyWorker",
    "ProductionTaxonomyEngine",
    "RemotePackageSearchWorker",
    "RemovalRiskLevel",
    "RemovalSafetyAssessment",
    "RepoInfo",
    "ReverseDependencyWorker",
    "SQLiteCapabilityCache",
    "SecurityAdvisoryWorker",
    "SystemDesktopIndex",
    "SystemRebootSignals",
    "SystemRebootWorker",
    "SystemUpdatesCheckWorker",
    "TaxonomyInputScope",
    "UserInstalledQueryWorker",
    "_RemotePackageRecord",
    "_UnsupportedMetadataCompression",
    "_classify_local_rpm_manifest",
    "_create_read_only_rpm_transaction_set_unlocked",
    "_decode_rpm_str",
    "_dnf5_json_option_is_unsupported",
    "_header_matches_package",
    "_is_dnf5_binary",
    "_normalise_rpm_epoch",
    "_open_compressed_xml_metadata",
    "_parse_dnf5_history_json",
    "_parse_dnf_package_identity_output",
    "_parse_remote_package_records",
    "_rpm_query_target",
    "classification_package_fields",
    "create_rpm_transaction_set",
    "datetime",
    "describe_subprocess_failure",
    "find_package_owning_file_info",
    "flatpak_read_only_unavailable",
    "format_byte_size",
    "get_clean_env",
    "get_dnf_binary_path",
    "get_host_environment",
    "get_system_protected_packages",
    "inspect_local_rpm_file",
    "is_running_in_flatpak",
    "os",
    "parse_rpm_verify_line",
    "rpm",
    "subprocess",
)
