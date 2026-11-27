# dendro/core/package_classifier.py
"""Deterministic, evidence-aware package taxonomy engine.

Classification policy lives here; XML/RPM/desktop metadata acquisition lives in
``core.package_metadata`` and category presentation/filter definitions live in
``core.taxonomy``.  Keeping these responsibilities separate makes rule changes
reviewable and prevents UI/category drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
        desktop = cls._collect_desktop_evidence(
            name,
            desktop_files,
            appstream,
            appstream_components,
            allow_launchable_lookup=(
                manifest_was_supplied and scope != TaxonomyInputScope.REMOTE_METADATA
            ),
        )
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
        is_settings_panel = False
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
            if metadata is None or metadata.is_hidden:
                continue
            categories.update(metadata.categories)
            is_window_manager = is_window_manager or metadata.is_wm
            is_input_method = is_input_method or metadata.is_im
            is_settings_panel = is_settings_panel or metadata.is_settings
            is_terminal_launcher = is_terminal_launcher or metadata.is_terminal
            if (
                not metadata.is_nodisplay
                and not metadata.is_auxiliary
                and not metadata.is_im
                and not metadata.is_wm
            ):
                has_gui_launcher = True
                source = ClassificationEvidence.DESKTOP_ENTRY

        if pkg in appstream.desktop_packages or bool(
            component_types & {"desktop-application", "web-application"}
        ):
            has_gui_launcher = True
            source = ClassificationEvidence.APPSTREAM
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
            cls._hardware_rule,
            cls._desktop_rule,
            cls._service_and_security_rule,
            cls._desktop_addon_rule,
            cls._repository_configuration_rule,
            cls._development_rule,
            cls._media_and_toolkit_rule,
            cls._static_asset_rule,
            cls._system_data_rule,
            cls._ecosystem_rule,
            cls._cli_rule,
            cls._shared_library_rule,
            cls._fallback_role_policy_rule,
            cls._comps_rule,
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
    ) -> _ArchetypeResolution:
        return _ArchetypeResolution(archetype, pillar, primary, evidence, rationale)

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
    def _hardware_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        anatomy, name, appstream = context.anatomy, context.name, context.appstream
        if anatomy.has_kernel_modules_dir or anatomy.provides_kmod:
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "kernel_modules",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships compiled kernel modules",
            )
        if name.startswith(("kmod-", "akmod-")):
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "kernel_modules",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a kernel-module delivery package",
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
        if name.startswith("linux-firmware") or name.endswith("-firmware"):
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "firmware",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a firmware delivery package",
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
        if not context.desktop.has_gui_launcher and name.startswith(
            ("mesa-dri-", "mesa-vulkan-", "nvidia-", "libdrm")
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
                "alsa-topology",
            }
            or name.startswith("alsa-")
        ):
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "audio_sound",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies core sound architecture",
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
    def _service_and_security_rule(
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
        if "selinux" in name or name.startswith(
            ("pam", "shadow-utils", "audit", "firewalld", "polkit")
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
        if name.endswith(("-devel", "-static", "-dev")):
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
            and name.endswith(("-doc", "-docs", "-man", "man-pages"))
        ):
            return cls._resolution(
                PackageArchetype.DEVELOPMENT_SDK,
                "pillar_libs",
                "devel",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest contains reference documentation",
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
        if anatomy.has_gui_toolkit_dir:
            return cls._resolution(
                PackageArchetype.GUI_TOOLKIT,
                "pillar_libs",
                "gui_toolkits",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships GUI toolkit plugin architecture",
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
        if anatomy.has_development_data_dir:
            return cls._resolution(
                PackageArchetype.DEVELOPMENT_SDK,
                "pillar_libs",
                "devel",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships CMake, aclocal, or another SDK data interface",
            )
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
    def _ecosystem_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        matches = cls._ecosystem_matches(context)
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

    @staticmethod
    def _ecosystem_matches(
        context: _ClassificationContext,
    ) -> tuple[_EcosystemMatch, ...]:
        anatomy = context.anatomy
        name_ids = ecosystem_ids_for_package_name(context.name)
        matches: list[_EcosystemMatch] = []
        for spec in ECOSYSTEM_SPECS:
            has_capability = spec.identifier in anatomy.ecosystem_capability_ids
            has_path = spec.identifier in anatomy.ecosystem_path_ids
            if spec.identifier == "python":
                has_capability = has_capability or anatomy.provides_python_dist
                has_path = has_path or anatomy.has_python_runtime
            elif spec.identifier == "rust":
                has_capability = has_capability or anatomy.provides_rust_crate
            elif spec.identifier == "jvm":
                has_capability = has_capability or anatomy.provides_jvm_artifact
                has_path = has_path or anatomy.has_jvm_dir or anatomy.has_jar_files
            elif spec.identifier == "nodejs":
                has_capability = has_capability or anatomy.provides_nodejs_pkg
            has_name = spec.identifier in name_ids
            if has_capability or has_path or has_name:
                matches.append(
                    _EcosystemMatch(spec, has_capability, has_path, has_name)
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
            if match.has_name:
                signals.append("conventional package namespace")
            rationales.append(
                f"Ecosystem facet {match.spec.label}: {', '.join(signals)}"
            )
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
                context.desktop.xdg_categories, context.desktop.is_settings_panel
            )
            if (
                context.desktop.is_web_app
                and subcat == "desktop_utilities"
                and "utility" not in context.desktop.xdg_categories
            ):
                return "desktop_internet"
            return subcat
        if resolution.archetype == PackageArchetype.CLI_UTILITY:
            return cls._cli_subcategory(context.name, context.provides)
        return resolution.primary_category

    @staticmethod
    def _desktop_subcategory(
        categories: set[str] | frozenset[str], is_settings_panel: bool
    ) -> str:
        return desktop_subcategory(categories, is_settings_panel)

    @staticmethod
    def _cli_subcategory(name: str, provides: tuple[str, ...]) -> str:
        return cli_subcategory(name, provides)

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
            "is_python_pkg": "Python" in secondary_tags or primary == "python_pkgs",
            "is_rust_pkg": "Rust" in secondary_tags or primary == "rust_pkgs",
            "is_jvm_pkg": "Java/JVM" in secondary_tags or primary == "jvm_pkgs",
            "is_nodejs_pkg": "Node.js" in secondary_tags or primary == "nodejs_pkgs",
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
