# dendro/core/package_classifier.py
"""Deterministic, evidence-aware package taxonomy engine.

Classification policy lives here; XML/RPM/desktop metadata acquisition lives in
``core.package_metadata`` and category presentation/filter definitions live in
``core.taxonomy``.  Keeping these responsibilities separate makes rule changes
reviewable and prevents UI/category drift.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from dendro.core.ecosystems import (
    ECOSYSTEM_SPECS,
    EcosystemSpec,
    ecosystem_ids_for_package_name,
)
from dendro.core.package_metadata import (
    AppStreamCatalog,
    AppStreamComponent,
    FedoraCompsCatalog,
    PackagePhysicalAnatomy,
    SystemDesktopIndex,
)
from dendro.core.package_subcategories import (
    assess_package_removal_safety,
    cli_subcategory,
    desktop_subcategory,
)
from dendro.core.package_taxonomy_policies import (
    DEFAULT_FEDORA_CORE_PACKAGES,
    DEFAULT_SYSTEM_ROOT_PILLARS,
    FALLBACK_PACKAGE_ROLE_SPECS,
    FEDORA_SYSTEM_ROOT_PILLARS,
    PACKAGE_TAXONOMY_OVERRIDES,
    PackageRoleSpec,
    package_role_policy_integrity_errors,
)
from dendro.core.taxonomy import (
    ClassificationDecision,
    ClassificationEvidence,
    PackageArchetype,
    RemovalSafetyAssessment,
    TaxonomyInputScope,
    validate_classification_path,
)

if TYPE_CHECKING:
    from dendro.core.backend import PackageInfo


_COMPS_DOMAIN_RESOLUTIONS: dict[str, tuple[PackageArchetype, str, str, str]] = {
    "fedora_core": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "Fedora Comps classifies package under base operating-system infrastructure",
    ),
    "security_pkgs": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "Fedora Comps classifies package under system security",
    ),
    "systemd_services": (
        PackageArchetype.SYSTEM_DAEMON,
        "pillar_system",
        "systemd_services",
        "Fedora Comps classifies package as a system service or daemon",
    ),
    "desktop_addons": (
        PackageArchetype.DESKTOP_ADDON,
        "pillar_system",
        "desktop_addons",
        "Fedora Comps classifies package as a desktop or input-method addon",
    ),
    "graphics_drivers": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "graphics_drivers",
        "Fedora Comps classifies package under graphics driver infrastructure",
    ),
    "firmware": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "firmware",
        "Fedora Comps classifies package under hardware firmware support",
    ),
    "cli_tools": (
        PackageArchetype.CLI_UTILITY,
        "pillar_cli",
        "cli_tools",
        "Fedora Comps classifies package as a command-line utility",
    ),
    "devel": (
        PackageArchetype.DEVELOPMENT_SDK,
        "pillar_libs",
        "devel",
        "Fedora Comps classifies package under development tools",
    ),
    "c_libs": (
        PackageArchetype.SHARED_LIBRARY,
        "pillar_libs",
        "c_libs",
        "Fedora Comps classifies package under shared C/C++ libraries",
    ),
    "fonts": (
        PackageArchetype.STATIC_ASSET,
        "pillar_libs",
        "fonts",
        "Fedora Comps classifies package as font assets",
    ),
    "themes": (
        PackageArchetype.STATIC_ASSET,
        "pillar_libs",
        "themes",
        "Fedora Comps classifies package as desktop theme assets",
    ),
    "locales": (
        PackageArchetype.STATIC_ASSET,
        "pillar_libs",
        "locales",
        "Fedora Comps classifies package as locale assets",
    ),
    **{
        spec.category_id: (
            PackageArchetype.ECOSYSTEM_RUNTIME,
            "pillar_libs",
            spec.category_id,
            f"Fedora Comps classifies package in the {spec.label} ecosystem",
        )
        for spec in ECOSYSTEM_SPECS
    },
}


_COMPANION_SUBPACKAGE_SUFFIXES: Final[tuple[str, ...]] = (
    "-common",
    "-data",
    "-filesystem",
    "-assets",
    "-resources",
    "-base",
)

_DOCS_SUMMARY_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:documentation|manual|user guide|help files|reference guide)\b",
    re.IGNORECASE,
)

_SUMMARY_ROLE_PATTERNS: Final[
    tuple[tuple[re.Pattern[str], PackageArchetype, str, str, str], ...]
] = (
    (
        re.compile(
            r"\b(?:development files|header files and libraries|headers and development|static libraries)\b",
            re.IGNORECASE,
        ),
        PackageArchetype.DEVELOPMENT_SDK,
        "pillar_libs",
        "devel",
        "Package summary identifies development headers or SDK files",
    ),
    (
        re.compile(
            r"\b(?:truetype font|opentype font|bitmap font|monospace font|serif font|sans-serif font|font family)\b",
            re.IGNORECASE,
        ),
        PackageArchetype.STATIC_ASSET,
        "pillar_libs",
        "fonts",
        "Package summary identifies font assets",
    ),
    (
        re.compile(
            r"\b(?:icon theme|desktop theme|wallpaper pack|cursor theme)\b",
            re.IGNORECASE,
        ),
        PackageArchetype.STATIC_ASSET,
        "pillar_libs",
        "themes",
        "Package summary identifies visual theme assets",
    ),
    (
        re.compile(
            r"\b(?:firmware files|hardware firmware|cpu microcode|microcode update)\b",
            re.IGNORECASE,
        ),
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "firmware",
        "Package summary identifies hardware firmware",
    ),
    (
        re.compile(
            r"\b(?:language pack|localization files|translation files)\b",
            re.IGNORECASE,
        ),
        PackageArchetype.STATIC_ASSET,
        "pillar_libs",
        "locales",
        "Package summary identifies localization assets",
    ),
)


def _extract_source_package_name(source_rpm: str) -> str:
    """Return the normalized SRPM package name from ``source_rpm``."""
    cleaned = source_rpm.strip()
    if not cleaned:
        return ""
    for suffix in (".src.rpm", ".nosrc.rpm", ".rpm"):
        if cleaned.casefold().endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    if cleaned.count("-") >= 2:
        cleaned = cleaned.rsplit("-", 2)[0]
    return cleaned.strip().casefold()


def _extract_requirement_names(requires: Sequence[str]) -> frozenset[str]:
    """Return normalized capability/package base names from RPM ``requires``."""
    names: set[str] = set()
    for raw_req in requires:
        stripped = raw_req.strip()
        if not stripped or stripped.startswith(("(", "rpmlib(", "rtld(", "config(")):
            continue
        base = stripped
        for op in ("<=", ">=", "==", "=", "<", ">"):
            if op in base:
                base = base.split(op, 1)[0].strip()
        # Strip trailing architecture qualifier on capability names like pkg(x86-64)
        if base.endswith((")", "")):
            head = base.split(None, 1)[0].strip().casefold()
            if head:
                names.add(head)
    return frozenset(names)


@dataclass(frozen=True, slots=True)
class _DesktopEvidence:
    xdg_categories: frozenset[str]
    has_gui_launcher: bool
    is_settings_panel: bool
    is_terminal_launcher: bool
    is_window_manager: bool
    is_input_method: bool
    source: ClassificationEvidence
    is_web_app: bool = False


@dataclass(frozen=True, slots=True)
class _ClassificationContext:
    name: str
    summary: str
    description: str
    anatomy: PackagePhysicalAnatomy
    provides: tuple[str, ...]
    appstream: AppStreamCatalog
    comps: FedoraCompsCatalog
    desktop: _DesktopEvidence
    input_scope: TaxonomyInputScope
    appstream_components: tuple[AppStreamComponent, ...] = ()
    compulsory_desktops: tuple[str, ...] = ()
    vendor: str = ""
    packager: str = ""
    source_rpm: str = ""
    source_name: str = ""
    requires: tuple[str, ...] = ()
    requirement_names: frozenset[str] = frozenset()

    @property
    def appstream_types(self) -> frozenset[str]:
        return frozenset(comp.component_type for comp in self.appstream_components)


@dataclass(frozen=True, slots=True)
class _EcosystemMatch:
    """All affirmative signals for one registered ecosystem in one package."""

    spec: EcosystemSpec
    has_capability: bool
    has_path: bool
    has_name: bool
    has_bundled: bool = False

    @property
    def has_manifest_evidence(self) -> bool:
        return self.has_capability or self.has_path


@dataclass(frozen=True, slots=True)
class _ArchetypeResolution:
    archetype: PackageArchetype
    parent_pillar: str
    primary_category: str
    evidence: ClassificationEvidence
    rationale: str
    # Explicit package policy may intentionally choose a narrower leaf than a
    # generic archetype rule could infer from the package name alone.
    sub_category: str | None = None


class ProductionTaxonomyEngine:
    """Classify packages using ordered affirmative evidence, never guess a library.

    A final ``unclassified`` result is intentional.  It preserves an honest
    boundary when an available/remote package lacks RPM-manifest facts or an
    installed package has no signal covered by the current taxonomy.
    """

    CORE_PILLARS = FEDORA_SYSTEM_ROOT_PILLARS

    @classmethod
    def classify(
        cls,
        name: str,
        summary: str = "",
        description: str = "",
        anatomy: PackagePhysicalAnatomy | None = None,
        desktop_entry_files: list[str] | None = None,
        provides: list[str] | None = None,
        appstream: AppStreamCatalog | None = None,
        comps: FedoraCompsCatalog | None = None,
        vendor: str = "",
        packager: str = "",
        *,
        source_rpm: str = "",
        requires: Sequence[str] = (),
        input_scope: TaxonomyInputScope | str | None = None,
    ) -> ClassificationDecision:
        """Return a deterministic category and transparent evidence source.

        ``input_scope`` is required by callers that only have remote metadata.
        Existing callers remain compatible: a supplied ``anatomy`` means RPM
        manifest input; no anatomy means unknown metadata unless AppStream or
        an explicit package policy supplies affirmative evidence.
        """
        manifest_was_supplied = anatomy is not None
        anatomy = anatomy or PackagePhysicalAnatomy()
        provides = provides or []
        appstream = appstream or AppStreamCatalog.get_instance()
        comps = comps or FedoraCompsCatalog.get_instance()
        scope = cls._resolve_input_scope(input_scope, manifest_was_supplied)
        desktop_files = (
            desktop_entry_files
            if desktop_entry_files is not None
            else list(anatomy.provided_desktop_ids)
        )
        appstream_components = (
            appstream.components_for_package(
                name,
                metainfo_files=anatomy.provided_metainfo_files,
                desktop_ids=desktop_files,
            )
            if hasattr(appstream, "components_for_package")
            else ()
        )
        compulsory_desktops = (
            appstream.compulsory_desktops_for_package(
                name,
                metainfo_files=anatomy.provided_metainfo_files,
                desktop_ids=desktop_files,
            )
            if hasattr(appstream, "compulsory_desktops_for_package")
            else ()
        )
        has_manifest_desktop = (
            anatomy.has_desktop_file
            or bool(anatomy.provided_desktop_ids)
            or anatomy.provides_mimehandler
        )
        desktop = cls._collect_desktop_evidence(
            name,
            desktop_files,
            appstream,
            appstream_components,
            allow_launchable_lookup=(
                manifest_was_supplied and scope != TaxonomyInputScope.REMOTE_METADATA
            ),
            has_manifest_desktop=has_manifest_desktop,
        )
        req_tuple = tuple(str(r) for r in requires if r)
        context = _ClassificationContext(
            name=name.lower(),
            summary=summary,
            description=description,
            anatomy=anatomy,
            provides=tuple(provides),
            appstream=appstream,
            comps=comps,
            desktop=desktop,
            input_scope=scope,
            appstream_components=appstream_components,
            compulsory_desktops=compulsory_desktops,
            vendor=vendor.strip(),
            packager=packager.strip(),
            source_rpm=source_rpm.strip(),
            source_name=_extract_source_package_name(source_rpm),
            requires=req_tuple,
            requirement_names=_extract_requirement_names(req_tuple),
        )

        resolution = cls._resolve_archetype(context)
        subcategory = cls._resolve_subcategory(resolution, context)
        validate_classification_path(
            resolution.parent_pillar, resolution.primary_category, subcategory
        )
        secondary_tags = cls._secondary_tags(resolution.archetype, context)
        flags = cls._flags_for(resolution, subcategory, secondary_tags)
        appstream_rationales = cls._appstream_rationales(context)
        ecosystem_rationales = cls._ecosystem_rationales(context)
        origin_rationales = cls._origin_rationales(context)
        return ClassificationDecision(
            primary_category=resolution.primary_category,
            sub_category=subcategory,
            parent_pillar=resolution.parent_pillar,
            evidence=resolution.evidence,
            input_scope=scope,
            rationale=(
                resolution.rationale,
                *appstream_rationales,
                *ecosystem_rationales,
                *origin_rationales,
            ),
            secondary_tags=tuple(secondary_tags),
            flags=flags,
        )

    @staticmethod
    def _resolve_input_scope(
        input_scope: TaxonomyInputScope | str | None,
        manifest_was_supplied: bool,
    ) -> TaxonomyInputScope:
        if input_scope is not None:
            return TaxonomyInputScope(input_scope)
        return (
            TaxonomyInputScope.INSTALLED_MANIFEST
            if manifest_was_supplied
            else TaxonomyInputScope.UNKNOWN
        )

    @classmethod
    def _collect_desktop_evidence(
        cls,
        package_name: str,
        desktop_entry_files: list[str],
        appstream: AppStreamCatalog,
        appstream_components: tuple[AppStreamComponent, ...] = (),
        *,
        allow_launchable_lookup: bool = False,
        has_manifest_desktop: bool = False,
    ) -> _DesktopEvidence:
        pkg = package_name.lower()
        categories: set[str] = set(appstream.pkg_xdg_categories.get(pkg, set()))
        component_types: set[str] = set()
        lookup_desktop_ids = list(desktop_entry_files)
        seen_lookup = set(lookup_desktop_ids)
        for comp in appstream_components:
            categories.update(comp.categories)
            component_types.add(comp.component_type)
            if allow_launchable_lookup or desktop_entry_files:
                for desktop_id in comp.launchable_desktop_ids:
                    if desktop_id not in seen_lookup:
                        seen_lookup.add(desktop_id)
                        lookup_desktop_ids.append(desktop_id)

        # The system desktop index walks the XDG applications tree.  Avoid that
        # one-time I/O cost for remote/no-manifest classification where there is
        # no package-owned desktop ID to resolve.
        desktop_index = (
            SystemDesktopIndex.get_instance() if lookup_desktop_ids else None
        )
        has_gui_launcher = False
        matched_disk_entries = 0
        is_settings_panel = False
        has_appstream_desktop = pkg in appstream.desktop_packages or bool(
            component_types & {"desktop-application", "web-application"}
        )
        is_terminal_launcher = (
            pkg in appstream.console_packages
            or "console-application" in component_types
        )
        is_window_manager = False
        is_input_method = (
            pkg in appstream.inputmethod_packages or "inputmethod" in component_types
        )
        is_web_app = (
            pkg in getattr(appstream, "web_app_packages", ())
            or "web-application" in component_types
        )
        source = ClassificationEvidence.NAME_HEURISTIC

        for desktop_id in lookup_desktop_ids:
            if desktop_index is None:
                continue
            metadata = desktop_index.desktop_map.get(
                desktop_id
            ) or desktop_index.desktop_map.get(desktop_id.lower())
            if metadata is None and "/" in desktop_id:
                dash_id = desktop_id.replace("/", "-")
                metadata = desktop_index.desktop_map.get(
                    dash_id
                ) or desktop_index.desktop_map.get(dash_id.lower())
            if metadata is None:
                continue
            matched_disk_entries += 1
            if metadata.is_hidden:
                continue
            categories.update(metadata.categories)
            is_window_manager = is_window_manager or metadata.is_wm
            is_input_method = is_input_method or metadata.is_im
            is_settings_panel = is_settings_panel or metadata.is_settings
            if (
                not metadata.is_nodisplay
                and not metadata.is_auxiliary
                and not metadata.is_im
                and not metadata.is_wm
                and not metadata.is_terminal
            ):
                has_gui_launcher = True
                source = ClassificationEvidence.DESKTOP_ENTRY
            elif metadata.is_terminal:
                is_terminal_launcher = True

        if has_gui_launcher and not (
            (
                pkg in appstream.console_packages
                or "console-application" in component_types
            )
            and not has_appstream_desktop
        ):
            is_terminal_launcher = False

        if has_appstream_desktop:
            has_gui_launcher = True
            is_terminal_launcher = False
            source = ClassificationEvidence.APPSTREAM
        if (
            not has_gui_launcher
            and matched_disk_entries == 0
            and has_manifest_desktop
            and not is_terminal_launcher
            and not is_window_manager
            and not is_input_method
        ):
            has_gui_launcher = True
            source = ClassificationEvidence.RPM_MANIFEST
        if is_input_method and (
            pkg in appstream.inputmethod_packages or "inputmethod" in component_types
        ):
            source = ClassificationEvidence.APPSTREAM
        return _DesktopEvidence(
            xdg_categories=frozenset(categories),
            has_gui_launcher=has_gui_launcher,
            is_settings_panel=is_settings_panel,
            is_terminal_launcher=is_terminal_launcher,
            is_window_manager=is_window_manager,
            is_input_method=is_input_method,
            source=source,
            is_web_app=is_web_app,
        )

    @classmethod
    def _resolve_archetype(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution:
        override = PACKAGE_TAXONOMY_OVERRIDES.get(context.name)
        if override is not None:
            archetype, pillar, primary, sub_category, rationale = override
            return _ArchetypeResolution(
                archetype,
                pillar,
                primary,
                ClassificationEvidence.MANUAL_OVERRIDE,
                rationale,
                sub_category,
            )

        for resolver in (
            cls._core_system_rule,
            cls._hardware_manifest_rule,
            cls._desktop_rule,
            cls._service_and_security_manifest_rule,
            cls._desktop_addon_rule,
            cls._repository_configuration_rule,
            cls._development_rule,
            cls._media_and_toolkit_rule,
            cls._ecosystem_rule,
            cls._cli_rule,
            cls._shared_library_rule,
            cls._static_asset_rule,
            cls._system_data_rule,
            cls._hardware_and_security_name_rule,
            cls._fallback_role_policy_rule,
            cls._bundled_ecosystem_rule,
            cls._subpackage_pairing_rule,
            cls._comps_rule,
            cls._summary_fallback_rule,
        ):
            result = resolver(context)
            if result is not None:
                return result

        return _ArchetypeResolution(
            PackageArchetype.UNCLASSIFIED,
            "pillar_maintenance",
            "unclassified",
            ClassificationEvidence.INSUFFICIENT_METADATA,
            cls._unclassified_rationale(context),
        )

    @staticmethod
    def _resolution(
        archetype: PackageArchetype,
        pillar: str,
        primary: str,
        evidence: ClassificationEvidence,
        rationale: str,
        sub_category: str | None = None,
    ) -> _ArchetypeResolution:
        return _ArchetypeResolution(
            archetype, pillar, primary, evidence, rationale, sub_category
        )

    @staticmethod
    def _looks_like_development_package(context: _ClassificationContext) -> bool:
        anatomy, name = context.anatomy, context.name
        return bool(
            anatomy.has_c_headers
            or (anatomy.provides_pkgconfig and not anatomy.has_user_bin)
            or (
                (anatomy.has_development_data_dir or anatomy.provides_rpm_macro)
                and not anatomy.exported_sonames
                and not anatomy.has_binaries
            )
            or name.endswith(("-devel", "-static", "-dev", "-doc", "-docs", "-man"))
        )

    @staticmethod
    def _is_rust_or_go_source_package(context: _ClassificationContext) -> bool:
        anatomy, name = context.anatomy, context.name
        if (
            anatomy.has_c_headers
            or (anatomy.provides_pkgconfig and not anatomy.has_user_bin)
            or anatomy.exported_sonames
        ):
            return False
        ecosystem_ids = anatomy.ecosystem_ids | ecosystem_ids_for_package_name(name)
        return bool(ecosystem_ids & {"rust", "go"})

    @classmethod
    def _core_system_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        if context.name in DEFAULT_FEDORA_CORE_PACKAGES:
            return cls._resolution(
                PackageArchetype.CORE_SYSTEM,
                "pillar_system",
                "fedora_core",
                ClassificationEvidence.NAME_HEURISTIC,
                "Known Fedora base operating-system component",
            )
        if any(
            value.startswith(("system-release", "fedora-release", "generic-release"))
            for value in context.provides
        ) or any(value in {"filesystem", "setup"} for value in context.provides):
            return cls._resolution(
                PackageArchetype.CORE_SYSTEM,
                "pillar_system",
                "fedora_core",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM capability identifies Fedora base operating-system infrastructure",
            )
        if (
            context.name in getattr(context.appstream, "operating_system_packages", ())
            or "operating-system" in context.appstream_types
        ):
            return cls._resolution(
                PackageArchetype.CORE_SYSTEM,
                "pillar_system",
                "fedora_core",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares an operating-system component",
            )
        return None

    @classmethod
    def _hardware_manifest_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        anatomy, name, appstream = context.anatomy, context.name, context.appstream
        is_devel_name = name.endswith(("-devel", "-headers", "-static", "-dev", "-doc"))
        if anatomy.provides_kmod or (
            anatomy.has_kernel_modules_dir and not is_devel_name
        ):
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "kernel_modules",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships compiled kernel modules",
            )
        if anatomy.has_firmware_dir:
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "firmware",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships hardware firmware microcode",
            )
        if (
            name in appstream.firmware_packages
            or "firmware" in context.appstream_types
            or any(
                comp.provided_firmware_runtime or comp.provided_firmware_flashed
                for comp in context.appstream_components
            )
        ):
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "firmware",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a firmware component",
            )
        if anatomy.has_dri_dir or anatomy.has_xorg_driver_dir:
            rationale = (
                "RPM manifest ships a low-level graphics-driver component"
                if anatomy.has_dri_dir
                else "RPM manifest ships an X.Org graphics-driver module"
            )
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "graphics_drivers",
                ClassificationEvidence.RPM_MANIFEST,
                rationale,
            )
        if name in appstream.driver_packages or "driver" in context.appstream_types:
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "graphics_drivers",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a driver component",
            )
        if not cls._looks_like_development_package(context) and any(
            req.startswith("xserver-abi(") for req in context.requirement_names
        ):
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "graphics_drivers",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM dependency declares an X.Org server driver ABI",
            )
        return None

    @classmethod
    def _desktop_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        desktop = context.desktop
        if (
            desktop.is_window_manager
            or context.anatomy.provides_wm
            or any("windowmanager" in value.lower() for value in context.provides)
        ):
            evidence = (
                ClassificationEvidence.DESKTOP_ENTRY
                if desktop.is_window_manager
                else ClassificationEvidence.RPM_MANIFEST
            )
            return cls._resolution(
                PackageArchetype.DESKTOP_ADDON,
                "pillar_system",
                "desktop_addons",
                evidence,
                "Window manager or display compositor",
            )
        if desktop.is_input_method:
            evidence = (
                ClassificationEvidence.APPSTREAM
                if context.name in context.appstream.inputmethod_packages
                or "inputmethod" in context.appstream_types
                else ClassificationEvidence.DESKTOP_ENTRY
            )
            return cls._resolution(
                PackageArchetype.DESKTOP_ADDON,
                "pillar_system",
                "desktop_addons",
                evidence,
                "Input method declared by desktop metadata",
            )
        if desktop.has_gui_launcher and not desktop.is_terminal_launcher:
            primary = "system_settings" if desktop.is_settings_panel else "user_apps"
            return cls._resolution(
                PackageArchetype.DESKTOP_APP,
                "pillar_apps",
                primary,
                desktop.source,
                "Interactive graphical application with verified desktop metadata",
            )
        return None

    @classmethod
    def _service_and_security_manifest_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        anatomy, name, appstream = context.anatomy, context.name, context.appstream
        if anatomy.has_systemd_system or anatomy.has_systemd_user:
            return cls._resolution(
                PackageArchetype.SYSTEM_DAEMON,
                "pillar_system",
                "systemd_services",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest includes a systemd unit",
            )
        if (
            anatomy.provides_sysuser_user
            or anatomy.has_sysusers_dir
            or (anatomy.provides_sysuser_group and not anatomy.has_user_bin)
        ):
            return cls._resolution(
                PackageArchetype.SYSTEM_DAEMON,
                "pillar_system",
                "systemd_services",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest registers systemd-sysusers service account metadata",
            )
        if name in appstream.service_packages or "service" in context.appstream_types:
            return cls._resolution(
                PackageArchetype.SYSTEM_DAEMON,
                "pillar_system",
                "systemd_services",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a service component",
            )
        if any(comp.provided_dbus_system for comp in context.appstream_components):
            return cls._resolution(
                PackageArchetype.SYSTEM_DAEMON,
                "pillar_system",
                "systemd_services",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a system D-Bus service",
            )
        if (
            not cls._looks_like_development_package(context)
            and not anatomy.has_user_bin
            and any(
                req == "selinux-policy"
                or req.startswith(("selinux-policy-", "selinux-policy("))
                for req in context.requirement_names
            )
        ):
            return cls._resolution(
                PackageArchetype.CORE_SYSTEM,
                "pillar_system",
                "security_pkgs",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM dependency identifies an SELinux policy extension",
            )
        return None

    @classmethod
    def _desktop_addon_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        if context.anatomy.has_kio_dir or context.anatomy.has_shell_or_session_dir:
            rationale = (
                "RPM manifest ships a desktop KIO/addon component"
                if context.anatomy.has_kio_dir
                else "RPM manifest ships a desktop-session or shell component"
            )
            return cls._resolution(
                PackageArchetype.DESKTOP_ADDON,
                "pillar_system",
                "desktop_addons",
                ClassificationEvidence.RPM_MANIFEST,
                rationale,
            )
        if (
            context.name in context.appstream.addon_packages
            or "addon" in context.appstream_types
        ):
            return cls._resolution(
                PackageArchetype.DESKTOP_ADDON,
                "pillar_system",
                "desktop_addons",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a desktop addon component",
            )
        if (
            not context.anatomy.has_binaries
            and not context.anatomy.exported_sonames
            and not cls._looks_like_development_package(context)
            and "gnome-shell" in context.requirement_names
        ):
            return cls._resolution(
                PackageArchetype.DESKTOP_ADDON,
                "pillar_system",
                "desktop_addons",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM dependency identifies a GNOME Shell extension or addon",
            )
        if any(
            token in context.name
            for token in ("gnome-shell-extension-", "kwin-script-", "plymouth-plugin-")
        ):
            return cls._resolution(
                PackageArchetype.DESKTOP_ADDON,
                "pillar_system",
                "desktop_addons",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a desktop shell addon",
            )
        return None

    @classmethod
    def _repository_configuration_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        if context.anatomy.has_repository_config_dir:
            return cls._resolution(
                PackageArchetype.SYSTEM_DATA,
                "pillar_maintenance",
                "repository_sources",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest owns DNF repository configuration or RPM signing-key data",
            )
        if (
            context.name in getattr(context.appstream, "repository_packages", ())
            or "repository" in context.appstream_types
        ):
            return cls._resolution(
                PackageArchetype.SYSTEM_DATA,
                "pillar_maintenance",
                "repository_sources",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a repository configuration component",
            )
        return None

    @classmethod
    def _development_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        anatomy, name = context.anatomy, context.name
        if anatomy.has_c_headers or (
            anatomy.provides_pkgconfig and not anatomy.has_user_bin
        ):
            return cls._resolution(
                PackageArchetype.DEVELOPMENT_SDK,
                "pillar_libs",
                "devel",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest exposes development headers or pkg-config interface",
            )
        if (
            (anatomy.has_development_data_dir or anatomy.provides_rpm_macro)
            and not anatomy.exported_sonames
            and not anatomy.has_binaries
        ):
            return cls._resolution(
                PackageArchetype.DEVELOPMENT_SDK,
                "pillar_libs",
                "devel",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships CMake, aclocal, RPM macros, or another SDK interface",
            )
        if name.endswith(
            ("-devel", "-static", "-dev")
        ) and not cls._is_rust_or_go_source_package(context):
            return cls._resolution(
                PackageArchetype.DEVELOPMENT_SDK,
                "pillar_libs",
                "devel",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a development SDK",
            )
        if (
            (anatomy.has_docs_dir or anatomy.has_man1 or anatomy.has_man3)
            and not anatomy.exported_sonames
            and not anatomy.has_binaries
        ):
            if name.endswith(("-doc", "-docs", "-man", "man-pages")):
                return cls._resolution(
                    PackageArchetype.DEVELOPMENT_SDK,
                    "pillar_libs",
                    "devel",
                    ClassificationEvidence.RPM_MANIFEST,
                    "RPM manifest contains reference documentation",
                )
            if (
                name.endswith("-help")
                or "-help-" in name
                or _DOCS_SUMMARY_RE.search(f"{context.summary} {context.description}")
            ):
                return cls._resolution(
                    PackageArchetype.STATIC_ASSET,
                    "pillar_libs",
                    "reference_data",
                    ClassificationEvidence.RPM_MANIFEST,
                    "RPM manifest contains help or reference documentation",
                )
        return None

    @classmethod
    def _media_and_toolkit_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        anatomy, name, appstream = context.anatomy, context.name, context.appstream
        if name in appstream.codec_packages or "codec" in context.appstream_types:
            return cls._resolution(
                PackageArchetype.MEDIA_CODEC,
                "pillar_libs",
                "media_plugins",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a codec component",
            )
        if anatomy.has_media_plugins_dir or anatomy.provides_gstreamer:
            return cls._resolution(
                PackageArchetype.MEDIA_CODEC,
                "pillar_libs",
                "media_plugins",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest exposes media plugins or GStreamer capability",
            )
        if anatomy.has_gui_toolkit_dir:
            return cls._resolution(
                PackageArchetype.GUI_TOOLKIT,
                "pillar_libs",
                "gui_toolkits",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships GUI toolkit plugin architecture",
            )
        if not anatomy.has_user_bin and not cls._looks_like_development_package(
            context
        ):
            if any(
                token in name
                for token in (
                    "gstreamer1-",
                    "ffmpeg-libs",
                    "libavcodec",
                    "kimageformats",
                    "openh264",
                )
            ):
                return cls._resolution(
                    PackageArchetype.MEDIA_CODEC,
                    "pillar_libs",
                    "media_plugins",
                    ClassificationEvidence.NAME_HEURISTIC,
                    "Package name identifies a media codec or format plugin",
                )
            if any(
                token in name
                for token in (
                    "qt6-",
                    "qt5-",
                    "gtk3",
                    "gtk4",
                    "wxwidgets",
                    "tkinter",
                    "libadwaita",
                )
            ):
                return cls._resolution(
                    PackageArchetype.GUI_TOOLKIT,
                    "pillar_libs",
                    "gui_toolkits",
                    ClassificationEvidence.NAME_HEURISTIC,
                    "Package name identifies a GUI toolkit",
                )
        return None

    @classmethod
    def _static_asset_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        anatomy, name, appstream = context.anatomy, context.name, context.appstream
        if name in appstream.font_packages or "font" in context.appstream_types:
            return cls._resolution(
                PackageArchetype.STATIC_ASSET,
                "pillar_libs",
                "fonts",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a font component",
            )
        if anatomy.has_fonts_dir or anatomy.provides_font:
            return cls._resolution(
                PackageArchetype.STATIC_ASSET,
                "pillar_libs",
                "fonts",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships typography assets",
            )
        if name.endswith(("-fonts", "-font")):
            return cls._resolution(
                PackageArchetype.STATIC_ASSET,
                "pillar_libs",
                "fonts",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies font assets",
            )
        if (
            name in appstream.icon_theme_packages
            or "icon-theme" in context.appstream_types
        ):
            return cls._resolution(
                PackageArchetype.STATIC_ASSET,
                "pillar_libs",
                "themes",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a visual theme component",
            )
        if anatomy.has_themes_dir:
            return cls._resolution(
                PackageArchetype.STATIC_ASSET,
                "pillar_libs",
                "themes",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships theme or icon assets",
            )
        if any(token in name for token in ("-theme", "-icon-theme")):
            return cls._resolution(
                PackageArchetype.STATIC_ASSET,
                "pillar_libs",
                "themes",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies theme assets",
            )
        if (
            name in appstream.localization_packages
            or "localization" in context.appstream_types
        ):
            return cls._resolution(
                PackageArchetype.STATIC_ASSET,
                "pillar_libs",
                "locales",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a localization component",
            )
        if anatomy.has_locales_dir:
            return cls._resolution(
                PackageArchetype.STATIC_ASSET,
                "pillar_libs",
                "locales",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships localization assets",
            )
        if name.startswith(("glibc-langpack-", "langpacks-")) or "-langpack-" in name:
            return cls._resolution(
                PackageArchetype.STATIC_ASSET,
                "pillar_libs",
                "locales",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a language pack",
            )
        return None

    @classmethod
    def _system_data_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        anatomy = context.anatomy
        if anatomy.has_reference_data_dir:
            return cls._resolution(
                PackageArchetype.STATIC_ASSET,
                "pillar_libs",
                "reference_data",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships shared documentation, schema, OCR, or format-reference data",
            )
        if anatomy.has_system_data_dir:
            return cls._resolution(
                PackageArchetype.SYSTEM_DATA,
                "pillar_system",
                "system_data",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships system-wide metadata or configuration data",
            )
        return None

    @classmethod
    def _hardware_and_security_name_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        name, anatomy = context.name, context.anatomy
        if cls._looks_like_development_package(context):
            return None
        if name.startswith(("kmod-", "akmod-")):
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "kernel_modules",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a kernel-module delivery package",
            )
        if (
            name == "linux-firmware"
            or name.startswith("linux-firmware-")
            or name.endswith("-firmware")
        ):
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "firmware",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a firmware delivery package",
            )
        if not context.desktop.has_gui_launcher and (
            name == "libdrm"
            or name.startswith(("mesa-dri-", "mesa-vulkan-", "nvidia-", "libdrm-"))
        ):
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "graphics_drivers",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a graphics-driver component",
            )
        if not context.desktop.has_gui_launcher and (
            name
            in {
                "pipewire",
                "wireplumber",
                "pulseaudio",
                "alsa-lib",
                "alsa-ucm",
                "alsa-ucm-conf",
                "alsa-topology",
                "alsa-topology-conf",
            }
            or name.startswith("alsa-plugins-")
        ):
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "audio_sound",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies core sound architecture",
            )
        if not anatomy.ecosystem_ids and (
            name
            in {
                "pam",
                "pambase",
                "selinux-policy",
                "libselinux",
                "policycoreutils",
                "checkpolicy",
                "setools",
                "shadow-utils",
                "audit",
                "audit-libs",
                "audit-rules",
                "firewalld",
                "polkit",
                "polkit-libs",
            }
            or name.startswith(
                (
                    "pam-",
                    "selinux-policy-",
                    "libselinux-",
                    "policycoreutils-",
                    "shadow-utils-",
                    "audit-",
                    "firewalld-",
                    "polkit-",
                )
            )
        ):
            return cls._resolution(
                PackageArchetype.CORE_SYSTEM,
                "pillar_system",
                "security_pkgs",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies security or access-control policy",
            )
        return None

    @classmethod
    def _ecosystem_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        matches = tuple(
            match
            for match in cls._ecosystem_matches(context)
            if match.has_capability or match.has_path or match.has_name
        )
        selected = cls._select_primary_ecosystem(matches)
        if selected is None:
            return None

        # A package with an un-namespaced executable is more safely represented
        # as a CLI tool when its only ecosystem proof is an embedded runtime
        # module. A package explicitly named for the ecosystem (for example
        # perl-Foo or python3-Bar) retains the ecosystem as its primary role.
        if (
            context.anatomy.has_user_bin
            and selected.has_manifest_evidence
            and not selected.has_name
        ):
            return None

        if selected.has_manifest_evidence:
            source = (
                "RPM virtual capability"
                if selected.has_capability
                else "RPM payload path"
            )
            return cls._resolution(
                PackageArchetype.ECOSYSTEM_RUNTIME,
                "pillar_libs",
                selected.spec.category_id,
                ClassificationEvidence.RPM_MANIFEST,
                f"{source} identifies the {selected.spec.label} ecosystem",
            )
        return cls._resolution(
            PackageArchetype.ECOSYSTEM_RUNTIME,
            "pillar_libs",
            selected.spec.category_id,
            ClassificationEvidence.NAME_HEURISTIC,
            f"Package name identifies the {selected.spec.label} ecosystem",
        )

    @classmethod
    def _bundled_ecosystem_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        bundled_matches = tuple(
            match for match in cls._ecosystem_matches(context) if match.has_bundled
        )
        if len(bundled_matches) != 1:
            return None
        selected = bundled_matches[0]
        return cls._resolution(
            PackageArchetype.ECOSYSTEM_RUNTIME,
            "pillar_libs",
            selected.spec.category_id,
            ClassificationEvidence.RPM_MANIFEST,
            f"RPM bundled dependency identifies the {selected.spec.label} ecosystem",
        )

    @staticmethod
    def _ecosystem_matches(
        context: _ClassificationContext,
    ) -> tuple[_EcosystemMatch, ...]:
        anatomy = context.anatomy
        name_ids = ecosystem_ids_for_package_name(context.name)
        requires_python_abi = (
            "python(abi)" in context.requirement_names
            and not anatomy.has_binaries
            and not anatomy.exported_sonames
            and not anatomy.ecosystem_ids
            and not ProductionTaxonomyEngine._looks_like_development_package(context)
        )
        matches: list[_EcosystemMatch] = []
        for spec in ECOSYSTEM_SPECS:
            has_capability = spec.identifier in anatomy.ecosystem_capability_ids
            has_path = spec.identifier in anatomy.ecosystem_path_ids
            has_bundled = spec.identifier in anatomy.ecosystem_bundled_ids
            if spec.identifier == "python":
                has_capability = (
                    has_capability
                    or anatomy.provides_python_dist
                    or requires_python_abi
                )
                has_path = has_path or anatomy.has_python_runtime
            elif spec.identifier == "rust":
                has_capability = has_capability or anatomy.provides_rust_crate
            elif spec.identifier == "jvm":
                has_capability = has_capability or anatomy.provides_jvm_artifact
                has_path = has_path or anatomy.has_jvm_dir or anatomy.has_jar_files
            elif spec.identifier == "nodejs":
                has_capability = has_capability or anatomy.provides_nodejs_pkg
            has_name = spec.identifier in name_ids
            if has_capability or has_path or has_name or has_bundled:
                matches.append(
                    _EcosystemMatch(
                        spec, has_capability, has_path, has_name, has_bundled
                    )
                )
        return tuple(matches)

    @staticmethod
    def _select_primary_ecosystem(
        matches: tuple[_EcosystemMatch, ...],
    ) -> _EcosystemMatch | None:
        """Choose only an unambiguous primary ecosystem.

        Multiple ecosystem tags can be true for a package. An explicit package
        namespace can resolve an ambiguity only when it agrees with a manifest
        ecosystem; otherwise an automatic primary category would be a guess and
        the package should retain its facets for review or a stronger role rule.
        """
        named_matches = tuple(match for match in matches if match.has_name)
        manifest_matches = tuple(
            match for match in matches if match.has_manifest_evidence
        )
        if len(manifest_matches) == 1:
            return manifest_matches[0]
        if len(manifest_matches) > 1:
            # A conventional RPM namespace may resolve a genuinely multi-role
            # manifest, but it must agree with one of the strong signals. A
            # name that conflicts with all manifest facts is only a facet.
            named_manifest_matches = tuple(
                match for match in manifest_matches if match.has_name
            )
            return (
                named_manifest_matches[0] if len(named_manifest_matches) == 1 else None
            )
        if len(named_matches) == 1:
            return named_matches[0]
        if len(matches) == 1:
            return matches[0]
        return None

    @classmethod
    def _appstream_rationales(cls, context: _ClassificationContext) -> tuple[str, ...]:
        """Describe auxiliary AppStream provenance such as compulsory desktops."""
        if not context.compulsory_desktops:
            return ()
        return (
            "AppStream marks component compulsory for desktop: "
            f"{', '.join(context.compulsory_desktops)}",
        )

    @classmethod
    def _ecosystem_rationales(cls, context: _ClassificationContext) -> tuple[str, ...]:
        """Describe each ecosystem facet and the concrete signal behind it."""
        rationales: list[str] = []
        for match in cls._ecosystem_matches(context):
            signals: list[str] = []
            if match.has_capability:
                signals.append("RPM virtual capability")
            if match.has_path:
                signals.append("RPM payload path")
            if match.has_bundled:
                signals.append("RPM bundled capability")
            if match.has_name:
                signals.append("conventional package namespace")
            rationales.append(
                f"Ecosystem facet {match.spec.label}: {', '.join(signals)}"
            )
        return tuple(rationales)

    @classmethod
    def _origin_rationales(cls, context: _ClassificationContext) -> tuple[str, ...]:
        """Surface verified vendor, packager, or source-RPM provenance."""
        rationales: list[str] = []
        origin_blob = f"{context.vendor} {context.packager}".casefold()
        if "copr" in origin_blob:
            rationales.append("Origin provenance: Fedora COPR repository")
        elif "rpmfusion" in origin_blob or "rpm fusion" in origin_blob:
            rationales.append("Origin provenance: RPM Fusion repository")
        elif context.vendor:
            rationales.append(f"Vendor metadata: {context.vendor}")
        if context.source_name and context.source_name != context.name:
            rationales.append(f"Source package: {context.source_name}")
        return tuple(rationales)

    @classmethod
    def _cli_rule(cls, context: _ClassificationContext) -> _ArchetypeResolution | None:
        if context.anatomy.has_binaries:
            return cls._resolution(
                PackageArchetype.CLI_UTILITY,
                "pillar_cli",
                "cli_tools",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest installs an executable in system PATH",
            )
        if context.desktop.is_terminal_launcher:
            evidence = (
                ClassificationEvidence.APPSTREAM
                if context.name in context.appstream.console_packages
                or "console-application" in context.appstream_types
                else ClassificationEvidence.DESKTOP_ENTRY
            )
            return cls._resolution(
                PackageArchetype.CLI_UTILITY,
                "pillar_cli",
                "cli_tools",
                evidence,
                "Desktop metadata declares a terminal application",
            )
        return None

    @classmethod
    def _shared_library_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        if context.anatomy.exported_sonames:
            return cls._resolution(
                PackageArchetype.SHARED_LIBRARY,
                "pillar_libs",
                "c_libs",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM capabilities export dynamic ELF shared-object libraries",
            )
        return None

    @classmethod
    def _fallback_role_policy_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        """Resolve one unambiguous name-policy role after all physical evidence.

        The normal resolver chain runs first, so an RPM manifest, AppStream
        component, desktop entry, or exported ABI remains the stronger source
        whenever it supplies a role. This is specifically for tiny companion
        RPMs such as ``git``, ``*-filesystem``, and repository-release packages.
        """
        matches = tuple(
            (spec, priority)
            for spec in FALLBACK_PACKAGE_ROLE_SPECS
            if (priority := spec.match_priority(context.name)) is not None
        )
        if not matches:
            return None
        highest_priority = max(priority for _spec, priority in matches)
        strongest_specs = tuple(
            spec for spec, priority in matches if priority == highest_priority
        )
        role_paths = {
            (spec.archetype, spec.parent_pillar, spec.category_id, spec.sub_category)
            for spec in strongest_specs
        }
        if len(role_paths) != 1:
            # Registry integrity tests should prevent this. Do not manufacture
            # precedence if an administrator adds a conflicting policy later.
            return None
        spec = strongest_specs[0]
        return _ArchetypeResolution(
            spec.archetype,
            spec.parent_pillar,
            spec.category_id,
            ClassificationEvidence.MANUAL_OVERRIDE,
            spec.rationale,
            spec.sub_category,
        )

    @classmethod
    def _comps_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        """Use Fedora Comps group membership as an auxiliary fallback domain."""
        get_domain = getattr(context.comps, "get_domain_for_package", None)
        if not callable(get_domain):
            return None
        domain = get_domain(context.name)
        if not domain:
            return None
        resolution = _COMPS_DOMAIN_RESOLUTIONS.get(domain)
        if resolution is None:
            return None
        archetype, pillar, primary, rationale = resolution
        return cls._resolution(
            archetype,
            pillar,
            primary,
            ClassificationEvidence.COMPS,
            rationale,
        )

    @classmethod
    def _subpackage_pairing_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        """Infer role for split ``-data``/``-common`` subpackages from verified parent."""
        name = context.name
        candidates: list[str] = []
        if context.source_name and context.source_name != name:
            candidates.append(context.source_name)
        for suffix in _COMPANION_SUBPACKAGE_SUFFIXES:
            if name.endswith(suffix):
                stem = name[: -len(suffix)]
                if stem and stem not in candidates:
                    candidates.append(stem)
                break
        if not candidates or not any(
            name.endswith(suffix) for suffix in _COMPANION_SUBPACKAGE_SUFFIXES
        ):
            return None

        appstream = context.appstream
        source_components = getattr(appstream, "source_pkg_components", {})
        desktop_pkgs = getattr(appstream, "desktop_packages", ())
        web_app_pkgs = getattr(appstream, "web_app_packages", ())
        service_pkgs = getattr(appstream, "service_packages", ())
        font_pkgs = getattr(appstream, "font_packages", ())
        codec_pkgs = getattr(appstream, "codec_packages", ())
        driver_pkgs = getattr(appstream, "driver_packages", ())
        firmware_pkgs = getattr(appstream, "firmware_packages", ())
        console_pkgs = getattr(appstream, "console_packages", ())
        localization_pkgs = getattr(appstream, "localization_packages", ())
        icon_theme_pkgs = getattr(appstream, "icon_theme_packages", ())

        for base in candidates:
            comp_types = {
                comp.component_type for comp in source_components.get(base, ())
            }
            if (
                base in desktop_pkgs
                or base in web_app_pkgs
                or bool(comp_types & {"desktop-application", "web-application"})
            ):
                return cls._resolution(
                    PackageArchetype.DESKTOP_ADDON,
                    "pillar_system",
                    "desktop_addons",
                    ClassificationEvidence.APPSTREAM,
                    f"Companion subpackage paired with AppStream desktop application '{base}'",
                )
            if base in service_pkgs or "service" in comp_types:
                return cls._resolution(
                    PackageArchetype.SYSTEM_DAEMON,
                    "pillar_system",
                    "systemd_services",
                    ClassificationEvidence.APPSTREAM,
                    f"Companion subpackage paired with AppStream service '{base}'",
                )
            if base in font_pkgs or "font" in comp_types:
                return cls._resolution(
                    PackageArchetype.STATIC_ASSET,
                    "pillar_libs",
                    "fonts",
                    ClassificationEvidence.APPSTREAM,
                    f"Companion subpackage paired with AppStream font '{base}'",
                )
            if base in icon_theme_pkgs or "icon-theme" in comp_types:
                return cls._resolution(
                    PackageArchetype.STATIC_ASSET,
                    "pillar_libs",
                    "themes",
                    ClassificationEvidence.APPSTREAM,
                    f"Companion subpackage paired with AppStream theme '{base}'",
                )
            if base in localization_pkgs or "localization" in comp_types:
                return cls._resolution(
                    PackageArchetype.STATIC_ASSET,
                    "pillar_libs",
                    "locales",
                    ClassificationEvidence.APPSTREAM,
                    f"Companion subpackage paired with AppStream localization '{base}'",
                )
            if base in codec_pkgs or "codec" in comp_types:
                return cls._resolution(
                    PackageArchetype.MEDIA_CODEC,
                    "pillar_libs",
                    "media_plugins",
                    ClassificationEvidence.APPSTREAM,
                    f"Companion subpackage paired with AppStream codec '{base}'",
                )
            if base in driver_pkgs or "driver" in comp_types:
                return cls._resolution(
                    PackageArchetype.HARDWARE_DRIVER,
                    "pillar_hardware",
                    "graphics_drivers",
                    ClassificationEvidence.APPSTREAM,
                    f"Companion subpackage paired with AppStream driver '{base}'",
                )
            if base in firmware_pkgs or "firmware" in comp_types:
                return cls._resolution(
                    PackageArchetype.HARDWARE_DRIVER,
                    "pillar_hardware",
                    "firmware",
                    ClassificationEvidence.APPSTREAM,
                    f"Companion subpackage paired with AppStream firmware '{base}'",
                )
            if base in console_pkgs or "console-application" in comp_types:
                return cls._resolution(
                    PackageArchetype.SYSTEM_DATA,
                    "pillar_system",
                    "system_data",
                    ClassificationEvidence.APPSTREAM,
                    f"Companion subpackage paired with AppStream console package '{base}'",
                )

            override = PACKAGE_TAXONOMY_OVERRIDES.get(base)
            if override is not None:
                archetype, pillar, primary, sub_category, _rationale = override
                if archetype == PackageArchetype.DESKTOP_APP:
                    return cls._resolution(
                        PackageArchetype.DESKTOP_ADDON,
                        "pillar_system",
                        "desktop_addons",
                        ClassificationEvidence.NAME_HEURISTIC,
                        f"Companion subpackage paired with desktop application '{base}'",
                    )
                if primary in {
                    "systemd_services",
                    "fonts",
                    "themes",
                    "locales",
                    "media_plugins",
                    "graphics_drivers",
                    "firmware",
                    "reference_data",
                    "system_data",
                    "security_pkgs",
                    "devel",
                    "gui_toolkits",
                }:
                    return cls._resolution(
                        archetype,
                        pillar,
                        primary,
                        ClassificationEvidence.NAME_HEURISTIC,
                        f"Companion subpackage paired with '{base}'",
                        sub_category,
                    )
        return None

    @classmethod
    def _summary_fallback_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        """Resolve unambiguous RPM summary descriptions when stronger proof is absent."""
        summary = context.summary.strip()
        if not summary:
            return None
        for pattern, archetype, pillar, primary, rationale in _SUMMARY_ROLE_PATTERNS:
            if pattern.search(summary):
                return cls._resolution(
                    archetype,
                    pillar,
                    primary,
                    ClassificationEvidence.NAME_HEURISTIC,
                    rationale,
                )
        return None

    @classmethod
    def _unclassified_rationale(cls, context: _ClassificationContext) -> str:
        manifest_ecosystems = tuple(
            match
            for match in cls._ecosystem_matches(context)
            if match.has_manifest_evidence
        )
        if len(manifest_ecosystems) > 1:
            labels = ", ".join(match.spec.label for match in manifest_ecosystems)
            return (
                "RPM metadata identifies multiple ecosystems "
                f"({labels}) but does not establish a trustworthy primary role"
            )
        if context.input_scope == TaxonomyInputScope.REMOTE_METADATA:
            return "Remote metadata did not include enough package-manifest evidence for a trustworthy category"
        if context.input_scope in {
            TaxonomyInputScope.INSTALLED_MANIFEST,
            TaxonomyInputScope.LOCAL_RPM_MANIFEST,
        }:
            return "Package manifest did not provide enough affirmative evidence for a trustworthy category"
        return "Package metadata did not provide enough affirmative evidence for a trustworthy category"

    @classmethod
    def _resolve_subcategory(
        cls, resolution: _ArchetypeResolution, context: _ClassificationContext
    ) -> str:
        if resolution.sub_category is not None:
            return resolution.sub_category
        # Settings is both a primary category and its sole leaf category. Keep
        # explicit package-policy assignments stable even when desktop metadata
        # is unavailable (for example, KDE control modules without a launcher).
        if resolution.primary_category == "system_settings":
            return "system_settings"
        if resolution.archetype == PackageArchetype.DESKTOP_APP:
            subcat = cls._desktop_subcategory(
                context.desktop.xdg_categories,
                context.desktop.is_settings_panel,
                mimehandlers=context.anatomy.provided_mimehandlers,
                summary=context.summary,
                description=context.description,
            )
            if (
                context.desktop.is_web_app
                and subcat == "desktop_utilities"
                and "utility" not in context.desktop.xdg_categories
            ):
                return "desktop_internet"
            return subcat
        if resolution.archetype == PackageArchetype.CLI_UTILITY:
            return cls._cli_subcategory(
                context.name,
                context.provides,
                summary=context.summary,
            )
        return resolution.primary_category

    @staticmethod
    def _desktop_subcategory(
        categories: set[str] | frozenset[str],
        is_settings_panel: bool,
        *,
        mimehandlers: Sequence[str] = (),
        summary: str = "",
        description: str = "",
    ) -> str:
        return desktop_subcategory(
            categories,
            is_settings_panel,
            mimehandlers=mimehandlers,
            summary=summary,
            description=description,
        )

    @staticmethod
    def _cli_subcategory(
        name: str,
        provides: tuple[str, ...],
        *,
        summary: str = "",
    ) -> str:
        return cli_subcategory(name, provides, summary=summary)

    @staticmethod
    def _secondary_tags(
        archetype: PackageArchetype, context: _ClassificationContext
    ) -> list[str]:
        tags: list[str] = [
            match.spec.tag
            for match in ProductionTaxonomyEngine._ecosystem_matches(context)
        ]
        anatomy = context.anatomy
        if (
            archetype != PackageArchetype.CLI_UTILITY
            and anatomy.has_binaries
            and not anatomy.has_libexec
        ):
            tags.append("CLI Tool")
        if archetype != PackageArchetype.SHARED_LIBRARY and anatomy.exported_sonames:
            tags.append("Library")
        if (
            archetype != PackageArchetype.DESKTOP_APP
            and context.desktop.has_gui_launcher
        ):
            tags.append("Desktop App")
        return tags

    @staticmethod
    def _flags_for(
        resolution: _ArchetypeResolution,
        subcategory: str,
        secondary_tags: list[str],
    ) -> dict[str, bool]:
        archetype, primary = resolution.archetype, resolution.primary_category
        return {
            "is_desktop_app": archetype == PackageArchetype.DESKTOP_APP
            and primary != "system_settings",
            "is_cli_tool": archetype == PackageArchetype.CLI_UTILITY,
            "is_system_settings": primary == "system_settings"
            or subcategory == "system_settings",
            "is_graphics_driver": primary == "graphics_drivers",
            "is_audio_sound": primary == "audio_sound",
            "is_media_plugin": archetype == PackageArchetype.MEDIA_CODEC,
            "is_desktop_addon": archetype == PackageArchetype.DESKTOP_ADDON,
            "is_gui_toolkit": archetype == PackageArchetype.GUI_TOOLKIT,
            "is_fedora_core": primary == "fedora_core",
            "is_c_lib": archetype == PackageArchetype.SHARED_LIBRARY,
            "is_systemd_service": archetype == PackageArchetype.SYSTEM_DAEMON,
            "is_firmware": primary == "firmware",
            "is_kernel_module": primary == "kernel_modules",
            "is_font": primary == "fonts",
            "is_devel": archetype == PackageArchetype.DEVELOPMENT_SDK,
            "is_locale": primary == "locales",
            "is_theme": primary == "themes",
            "is_security_pkg": primary == "security_pkgs",
            "is_library": archetype
            in {
                PackageArchetype.SHARED_LIBRARY,
                PackageArchetype.DEVELOPMENT_SDK,
                PackageArchetype.GUI_TOOLKIT,
                PackageArchetype.MEDIA_CODEC,
            }
            or "Library" in secondary_tags,
        }

    @classmethod
    def assess_removal_safety(cls, pkg: PackageInfo) -> RemovalSafetyAssessment:
        """Return conservative removal guidance; DNF's resolved plan remains authority."""
        return assess_package_removal_safety(pkg)


# Backward-compatible public aliases.
FreeDesktopTaxonomyEngine = ProductionTaxonomyEngine
IntelligentPackageClassifier = ProductionTaxonomyEngine


__all__ = (
    "DEFAULT_FEDORA_CORE_PACKAGES",
    "DEFAULT_SYSTEM_ROOT_PILLARS",
    "FALLBACK_PACKAGE_ROLE_SPECS",
    "FEDORA_SYSTEM_ROOT_PILLARS",
    "PACKAGE_TAXONOMY_OVERRIDES",
    "FreeDesktopTaxonomyEngine",
    "IntelligentPackageClassifier",
    "PackageRoleSpec",
    "ProductionTaxonomyEngine",
    "package_role_policy_integrity_errors",
)
