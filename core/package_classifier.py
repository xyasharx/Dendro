"""Deterministic, evidence-aware package taxonomy engine.

Classification policy lives here; XML/RPM/desktop metadata acquisition lives in
``core.package_metadata`` and category presentation/filter definitions live in
``core.taxonomy``.  Keeping these responsibilities separate makes rule changes
reviewable and prevents UI/category drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from core.package_metadata import (
    AppStreamCatalog,
    FedoraCompsCatalog,
    PackagePhysicalAnatomy,
    SystemDesktopIndex,
)
from core.taxonomy import (
    ClassificationDecision,
    ClassificationEvidence,
    PackageArchetype,
    RemovalRiskLevel,
    RemovalSafetyAssessment,
    TaxonomyInputScope,
    category_removal_risk,
    validate_classification_path,
)

if TYPE_CHECKING:
    from core.backend import PackageInfo


DEFAULT_SYSTEM_ROOT_PILLARS: Final[set[str]] = {
    "kernel",
    "kernel-core",
    "kernel-modules",
    "grub2-common",
    "grub2-efi-x64",
    "dracut",
    "systemd",
    "systemd-udev",
    "systemd-libs",
    "glibc",
    "glibc-common",
    "coreutils",
    "bash",
    "sudo",
    "shadow-utils",
    "util-linux",
    "polkit",
    "pam",
    "chrony",
    "btrfs-progs",
    "e2fsprogs",
    "lvm2",
    "cryptsetup",
    "dosfstools",
    "mdadm",
    "networkmanager",
    "firewalld",
    "selinux-policy",
    "audit",
    "iptables",
    "dnf5",
    "dnf",
    "rpm",
    "flatpak",
    "shared-mime-info",
    "desktop-file-utils",
    "glib2",
}

DEFAULT_FEDORA_CORE_PACKAGES: Final[set[str]] = {
    "filesystem",
    "setup",
    "glibc",
    "glibc-common",
    "glibc-minimal-langpack",
    "glibc-all-langpacks",
    "coreutils",
    "coreutils-common",
    "bash",
    "sh",
    "util-linux",
    "util-linux-core",
    "systemd",
    "systemd-udev",
    "systemd-libs",
    "rpm",
    "rpm-libs",
    "dnf",
    "dnf5",
    "libdnf5",
    "dnf5-plugins",
    "grub2-common",
    "grub2-efi-x64",
    "grub2-pc",
    "dracut",
    "btrfs-progs",
    "e2fsprogs",
    "lvm2",
    "cryptsetup",
    "dosfstools",
    "mdadm",
}
FEDORA_SYSTEM_ROOT_PILLARS: Final[set[str]] = DEFAULT_SYSTEM_ROOT_PILLARS


PACKAGE_TAXONOMY_OVERRIDES: Final[
    dict[str, tuple[PackageArchetype, str, str, str, str]]
] = {
    # Format: package_name_lower: (archetype, parent_pillar, primary_category, sub_category, rationale)
    # Hardware & Audio Architecture
    "pipewire": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "audio_sound",
        "audio_sound",
        "Core Linux sound server and routing infrastructure",
    ),
    "wireplumber": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "audio_sound",
        "audio_sound",
        "Session and policy manager for PipeWire",
    ),
    "pulseaudio": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "audio_sound",
        "audio_sound",
        "Sound server for POSIX systems",
    ),
    "pulseaudio-daemon": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "audio_sound",
        "audio_sound",
        "PulseAudio sound daemon",
    ),
    "alsa-lib": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "audio_sound",
        "audio_sound",
        "Advanced Linux Sound Architecture library",
    ),
    "alsa-ucm": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "audio_sound",
        "audio_sound",
        "ALSA Use Case Manager configuration",
    ),
    "alsa-topology": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "audio_sound",
        "audio_sound",
        "ALSA topology configuration",
    ),
    "alsa-plugins": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "audio_sound",
        "audio_sound",
        "ALSA sound system plugins",
    ),
    "jack-audio-connection-kit": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "audio_sound",
        "audio_sound",
        "Low-latency professional audio server",
    ),
    # Hardware Graphics & DRI Stack
    "mesa-dri-drivers": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "graphics_drivers",
        "graphics_drivers",
        "Mesa Direct Rendering Infrastructure drivers",
    ),
    "mesa-vulkan-drivers": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "graphics_drivers",
        "graphics_drivers",
        "Mesa Vulkan graphics acceleration drivers",
    ),
    "libdrm": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "graphics_drivers",
        "graphics_drivers",
        "Direct Rendering Manager userspace library",
    ),
    # Hardware Kernel Modules & Drivers
    "kernel": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "kernel_modules",
        "kernel_modules",
        "Linux kernel package",
    ),
    "kernel-core": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "kernel_modules",
        "kernel_modules",
        "Core Linux kernel binary and basic modules",
    ),
    "kernel-modules": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "kernel_modules",
        "kernel_modules",
        "Kernel modules for core Linux functionality",
    ),
    "kernel-modules-core": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "kernel_modules",
        "kernel_modules",
        "Standard kernel modules",
    ),
    "kernel-modules-extra": (
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "kernel_modules",
        "kernel_modules",
        "Extra kernel drivers and hardware modules",
    ),
    # Security & Access Control
    "selinux-policy": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "SELinux policy configuration",
    ),
    "selinux-policy-targeted": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "SELinux targeted policy",
    ),
    "libselinux": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "SELinux core security library",
    ),
    "polkit": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "Authorization manager and privilege control framework",
    ),
    "polkit-libs": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "Polkit authorization libraries",
    ),
    "pam": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "Pluggable Authentication Modules architecture",
    ),
    "pam-libs": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "Pluggable Authentication Modules shared libraries",
    ),
    "shadow-utils": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "System password and shadow account utilities",
    ),
    "sudo": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "Privileged command execution utility",
    ),
    "audit": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "Linux audit framework daemon and utilities",
    ),
    "audit-libs": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "Linux audit subsystem libraries",
    ),
    "firewalld": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "Dynamically managed firewall daemon",
    ),
    "iptables": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "IPv4 packet filtering administration",
    ),
    "nftables": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "security_pkgs",
        "Netfilter packet classification framework",
    ),
    # Desktop Environment Shells, Window Managers & Compositors
    "plasma-desktop": (
        PackageArchetype.DESKTOP_ADDON,
        "pillar_system",
        "desktop_addons",
        "desktop_addons",
        "KDE Plasma desktop shell environment",
    ),
    "plasma-workspace": (
        PackageArchetype.DESKTOP_ADDON,
        "pillar_system",
        "desktop_addons",
        "desktop_addons",
        "KDE Plasma workspace session and tools",
    ),
    "gnome-shell": (
        PackageArchetype.DESKTOP_ADDON,
        "pillar_system",
        "desktop_addons",
        "desktop_addons",
        "GNOME desktop environment shell and compositor",
    ),
    "kamera": (
        PackageArchetype.DESKTOP_APP,
        "pillar_apps",
        "system_settings",
        "system_settings",
        "KDE digital camera configuration in System Settings",
    ),
    "mutter": (
        PackageArchetype.DESKTOP_ADDON,
        "pillar_system",
        "desktop_addons",
        "desktop_addons",
        "Wayland display compositor and window manager for GNOME",
    ),
    "kwin": (
        PackageArchetype.DESKTOP_ADDON,
        "pillar_system",
        "desktop_addons",
        "desktop_addons",
        "KDE Plasma window manager and Wayland compositor",
    ),
    "cinnamon": (
        PackageArchetype.DESKTOP_ADDON,
        "pillar_system",
        "desktop_addons",
        "desktop_addons",
        "Cinnamon desktop environment shell",
    ),
    "mate-desktop": (
        PackageArchetype.DESKTOP_ADDON,
        "pillar_system",
        "desktop_addons",
        "desktop_addons",
        "MATE desktop environment core library and interface",
    ),
    "xfdesktop": (
        PackageArchetype.DESKTOP_ADDON,
        "pillar_system",
        "desktop_addons",
        "desktop_addons",
        "Xfce desktop environment manager",
    ),
    # System Daemons & Services
    "networkmanager": (
        PackageArchetype.SYSTEM_DAEMON,
        "pillar_system",
        "systemd_services",
        "systemd_services",
        "Network management system daemon",
    ),
    "chrony": (
        PackageArchetype.SYSTEM_DAEMON,
        "pillar_system",
        "systemd_services",
        "systemd_services",
        "NTP network time synchronization daemon",
    ),
    "localsearch": (
        PackageArchetype.SYSTEM_DAEMON,
        "pillar_system",
        "systemd_services",
        "systemd_services",
        "GNOME desktop file indexing daemon",
    ),
    "tinysparql": (
        PackageArchetype.SHARED_LIBRARY,
        "pillar_libs",
        "c_libs",
        "c_libs",
        "Low-footprint RDF database engine for desktop search",
    ),
    # Core Base Infrastructure
    "systemd": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "fedora_core",
        "System and Service Manager",
    ),
    "systemd-udev": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "fedora_core",
        "Rule-based device node manager",
    ),
    "systemd-libs": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "fedora_core",
        "systemd shared libraries",
    ),
    "glibc": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "fedora_core",
        "The GNU C Library core system runtime",
    ),
    "coreutils": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "fedora_core",
        "Core GNU command-line utilities",
    ),
    "bash": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "fedora_core",
        "GNU Bourne-Again Shell and core interpreter",
    ),
    "rpm": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "fedora_core",
        "RPM Package Manager core executable",
    ),
    "dnf5": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "fedora_core",
        "Next-generation package manager",
    ),
    "shared-mime-info": (
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "fedora_core",
        "Core FreeDesktop MIME-info database",
    ),
    # Desktop Applications with Packaging Quirks
    "dendro": (
        PackageArchetype.DESKTOP_APP,
        "pillar_apps",
        "user_apps",
        "desktop_utilities",
        "Native graphical package manager and dependency hierarchy explorer",
    ),
    # CLI Utilities with Special Packaging Quirks
    "7zip": (
        PackageArchetype.CLI_UTILITY,
        "pillar_cli",
        "cli_tools",
        "cli_data_archiving",
        "High-ratio file archiver CLI utility",
    ),
    "p7zip": (
        PackageArchetype.CLI_UTILITY,
        "pillar_cli",
        "cli_tools",
        "cli_data_archiving",
        "POSIX port of 7-Zip archiver",
    ),
    "flatpak": (
        PackageArchetype.CLI_UTILITY,
        "pillar_cli",
        "cli_tools",
        "cli_general",
        "Application sandboxing and deployment tool",
    ),
    "desktop-file-utils": (
        PackageArchetype.CLI_UTILITY,
        "pillar_cli",
        "cli_tools",
        "cli_search_files",
        "Utilities for working with desktop entries",
    ),
    # GUI Toolkits
    "python3-tkinter": (
        PackageArchetype.GUI_TOOLKIT,
        "pillar_libs",
        "gui_toolkits",
        "gui_toolkits",
        "Python interface to Tcl/Tk GUI toolkit",
    ),
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
        desktop = cls._collect_desktop_evidence(name, desktop_files, appstream)
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
        )

        resolution = cls._resolve_archetype(context)
        subcategory = cls._resolve_subcategory(resolution, context)
        validate_classification_path(
            resolution.parent_pillar, resolution.primary_category, subcategory
        )
        secondary_tags = cls._secondary_tags(resolution.archetype, context)
        flags = cls._flags_for(resolution, subcategory, secondary_tags)
        return ClassificationDecision(
            primary_category=resolution.primary_category,
            sub_category=subcategory,
            parent_pillar=resolution.parent_pillar,
            evidence=resolution.evidence,
            input_scope=scope,
            rationale=(resolution.rationale,),
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
    ) -> _DesktopEvidence:
        categories: set[str] = set(
            appstream.pkg_xdg_categories.get(package_name.lower(), set())
        )
        # The system desktop index walks the XDG applications tree.  Avoid that
        # one-time I/O cost for remote/no-manifest classification where there is
        # no package-owned desktop ID to resolve.
        desktop_index = (
            SystemDesktopIndex.get_instance() if desktop_entry_files else None
        )
        has_gui_launcher = False
        is_settings_panel = False
        is_terminal_launcher = package_name.lower() in appstream.console_packages
        is_window_manager = False
        is_input_method = package_name.lower() in appstream.inputmethod_packages
        source = ClassificationEvidence.NAME_HEURISTIC

        for desktop_id in desktop_entry_files:
            if desktop_index is None:
                continue
            metadata = desktop_index.desktop_map.get(
                desktop_id
            ) or desktop_index.desktop_map.get(desktop_id.lower())
            if metadata is None:
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

        if package_name.lower() in appstream.desktop_packages:
            has_gui_launcher = True
            source = ClassificationEvidence.APPSTREAM
        if package_name.lower() in appstream.inputmethod_packages:
            source = ClassificationEvidence.APPSTREAM
        return _DesktopEvidence(
            xdg_categories=frozenset(categories),
            has_gui_launcher=has_gui_launcher,
            is_settings_panel=is_settings_panel,
            is_terminal_launcher=is_terminal_launcher,
            is_window_manager=is_window_manager,
            is_input_method=is_input_method,
            source=source,
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
            cls._development_rule,
            cls._media_and_toolkit_rule,
            cls._static_asset_rule,
            cls._ecosystem_rule,
            cls._cli_rule,
            cls._shared_library_rule,
        ):
            result = resolver(context)
            if result is not None:
                return result

        return _ArchetypeResolution(
            PackageArchetype.UNCLASSIFIED,
            "pillar_maintenance",
            "unclassified",
            ClassificationEvidence.INSUFFICIENT_METADATA,
            cls._unclassified_rationale(context.input_scope),
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
        if name in appstream.firmware_packages:
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
        if anatomy.has_dri_dir:
            return cls._resolution(
                PackageArchetype.HARDWARE_DRIVER,
                "pillar_hardware",
                "graphics_drivers",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships a low-level graphics-driver component",
            )
        if name in appstream.driver_packages:
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
        if name in appstream.service_packages:
            return cls._resolution(
                PackageArchetype.SYSTEM_DAEMON,
                "pillar_system",
                "systemd_services",
                ClassificationEvidence.APPSTREAM,
                "AppStream declares a service component",
            )
        if context.comps.get_domain_for_package(name) == "security_pkgs":
            return cls._resolution(
                PackageArchetype.CORE_SYSTEM,
                "pillar_system",
                "security_pkgs",
                ClassificationEvidence.COMPS,
                "Fedora Comps declares a security package",
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
        if context.anatomy.has_kio_dir:
            return cls._resolution(
                PackageArchetype.DESKTOP_ADDON,
                "pillar_system",
                "desktop_addons",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest ships a desktop KIO/addon component",
            )
        if context.name in context.appstream.addon_packages:
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
        if name in appstream.codec_packages:
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
        if name in appstream.font_packages:
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
        if name in appstream.icon_theme_packages:
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
        if name in appstream.localization_packages:
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
    def _ecosystem_rule(
        cls, context: _ClassificationContext
    ) -> _ArchetypeResolution | None:
        anatomy, name = context.anatomy, context.name
        if (
            anatomy.has_python_runtime
            and not (
                anatomy.has_user_bin and not name.startswith(("python3-", "python-"))
            )
        ) or (
            anatomy.provides_python_dist
            and not (
                anatomy.has_user_bin and not name.startswith(("python3-", "python-"))
            )
        ):
            return cls._resolution(
                PackageArchetype.ECOSYSTEM_RUNTIME,
                "pillar_libs",
                "python_pkgs",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest exposes a Python runtime package",
            )
        if name.startswith(("python3-", "python-")):
            return cls._resolution(
                PackageArchetype.ECOSYSTEM_RUNTIME,
                "pillar_libs",
                "python_pkgs",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a Python ecosystem package",
            )
        if anatomy.provides_rust_crate:
            return cls._resolution(
                PackageArchetype.ECOSYSTEM_RUNTIME,
                "pillar_libs",
                "rust_pkgs",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM capability exposes a Rust crate",
            )
        if name.startswith(("rust-", "cargo-")):
            return cls._resolution(
                PackageArchetype.ECOSYSTEM_RUNTIME,
                "pillar_libs",
                "rust_pkgs",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a Rust ecosystem package",
            )
        if (
            anatomy.has_jvm_dir
            or anatomy.has_jar_files
            or anatomy.provides_jvm_artifact
        ):
            return cls._resolution(
                PackageArchetype.ECOSYSTEM_RUNTIME,
                "pillar_libs",
                "jvm_pkgs",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM manifest exposes a JVM runtime or artifact",
            )
        if name.startswith(("java-", "openjdk-")):
            return cls._resolution(
                PackageArchetype.ECOSYSTEM_RUNTIME,
                "pillar_libs",
                "jvm_pkgs",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a Java/JVM package",
            )
        if anatomy.provides_nodejs_pkg:
            return cls._resolution(
                PackageArchetype.ECOSYSTEM_RUNTIME,
                "pillar_libs",
                "nodejs_pkgs",
                ClassificationEvidence.RPM_MANIFEST,
                "RPM capability exposes an npm package",
            )
        if name.startswith(("nodejs-", "npm-")):
            return cls._resolution(
                PackageArchetype.ECOSYSTEM_RUNTIME,
                "pillar_libs",
                "nodejs_pkgs",
                ClassificationEvidence.NAME_HEURISTIC,
                "Package name identifies a Node.js ecosystem package",
            )
        return None

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

    @staticmethod
    def _unclassified_rationale(scope: TaxonomyInputScope) -> str:
        if scope == TaxonomyInputScope.REMOTE_METADATA:
            return "Remote metadata did not include enough package-manifest evidence for a trustworthy category"
        if scope in {
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
            return cls._desktop_subcategory(
                context.desktop.xdg_categories, context.desktop.is_settings_panel
            )
        if resolution.archetype == PackageArchetype.CLI_UTILITY:
            return cls._cli_subcategory(context.name, context.provides)
        return resolution.primary_category

    @staticmethod
    def _desktop_subcategory(
        categories: set[str] | frozenset[str], is_settings_panel: bool
    ) -> str:
        if is_settings_panel:
            return "system_settings"
        # Steam and similar launchers use Network and FileTransfer for their
        # storefront or download capabilities. A declared Game category is the
        # stronger user-facing identity and must win over those broad tags.
        if categories.intersection(
            {
                "game",
                "simulation",
                "emulator",
                "arcade",
                "boardgame",
                "actiongame",
                "adventuregame",
                "blocksgame",
                "cardgame",
                "kidsgame",
                "logicgame",
                "roleplaying",
                "shooter",
                "sportsgame",
                "strategygame",
            }
        ):
            return "desktop_games"
        has_network_communication = bool(
            categories.intersection(
                {
                    "webbrowser",
                    "email",
                    "chat",
                    "ircclient",
                    "feed",
                    "news",
                    "filetransfer",
                    "p2p",
                    "remoteaccess",
                    "telephony",
                    "videoconference",
                    "instantmessaging",
                }
            )
        )
        generic_network_tool = (
            "network" in categories
            and not has_network_communication
            and bool(
                categories.intersection({"utility", "system", "filetools", "settings"})
            )
        )
        if generic_network_tool:
            return "desktop_utilities"
        if has_network_communication or "network" in categories:
            return "desktop_internet"
        if categories.intersection(
            {
                "audiovideo",
                "audio",
                "video",
                "player",
                "recorder",
                "music",
                "audiovideoediting",
                "discburning",
                "mixer",
                "sequencer",
                "midi",
                "tuner",
                "tv",
            }
        ):
            return "desktop_multimedia"
        if categories.intersection(
            {
                "graphics",
                "2dgraphics",
                "rastergraphics",
                "vectorgraphics",
                "3dgraphics",
                "photography",
                "viewer",
                "scanning",
                "ocr",
            }
        ):
            return "desktop_graphics"
        if categories.intersection(
            {
                "office",
                "wordprocessor",
                "spreadsheet",
                "presentation",
                "publishing",
                "finance",
                "calendar",
                "contactmanagement",
                "database",
                "dictionary",
                "chart",
                "flowchart",
                "projectmanagement",
            }
        ):
            return "desktop_office"
        if categories.intersection(
            {
                "development",
                "ide",
                "debugger",
                "building",
                "texteditor",
                "revisioncontrol",
                "translation",
                "guidesigner",
                "profiling",
                "webdevelopment",
            }
        ):
            return "desktop_development"
        return "desktop_utilities"

    @staticmethod
    def _cli_subcategory(name: str, provides: tuple[str, ...]) -> str:
        if "editor" in provides or any(
            token in name
            for token in (
                "vim",
                "nano",
                "less",
                "micro",
                "emacs",
                "neovim",
                "helix",
                "kakoune",
            )
        ):
            return "cli_editors"
        if "shell" in provides or any(
            token in name
            for token in ("bash", "zsh", "fish", "tmux", "screen", "zellij")
        ):
            return "cli_shells"
        if any(
            token in name
            for token in (
                "tar",
                "gzip",
                "7z",
                "7zip",
                "p7zip",
                "zip",
                "unzip",
                "bzip2",
                "xz",
                "zstd",
                "lz4",
                "cpio",
                "jq",
                "yq",
                "sed",
                "gawk",
                "awk",
            )
        ):
            return "cli_data_archiving"
        if any(
            token in name
            for token in (
                "curl",
                "wget",
                "nmap",
                "rsync",
                "iproute",
                "ssh",
                "openssh",
                "traceroute",
                "net-tools",
                "tcpdump",
                "socat",
                "iperf",
                "bind-utils",
                "whois",
                "dig",
            )
        ):
            return "cli_networking"
        if any(
            token in name
            for token in (
                "htop",
                "btop",
                "atop",
                "iotop",
                "iftop",
                "strace",
                "glances",
                "gdb",
                "valgrind",
                "ncdu",
                "duf",
                "procps",
                "perf",
                "lsof",
                "sysstat",
            )
        ):
            return "cli_monitoring"
        if any(
            token in name
            for token in (
                "findutils",
                "grep",
                "ripgrep",
                "fd-find",
                "fzf",
                "tree",
                "eza",
                "bat",
                "diffutils",
                "patch",
            )
        ):
            return "cli_search_files"
        return "cli_general"

    @staticmethod
    def _secondary_tags(
        archetype: PackageArchetype, context: _ClassificationContext
    ) -> list[str]:
        tags: list[str] = []
        name, anatomy = context.name, context.anatomy
        if anatomy.has_python_runtime or name.startswith("python3-"):
            tags.append("Python")
        if name.startswith(("rust-", "cargo-")):
            tags.append("Rust")
        if name.startswith(("java-", "openjdk-")):
            tags.append("Java/JVM")
        if name.startswith("nodejs-"):
            tags.append("Node.js")
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
        if getattr(pkg, "is_protected", False) or getattr(pkg, "is_fedora_core", False):
            return RemovalSafetyAssessment(
                RemovalRiskLevel.CRITICAL,
                "Removal Risk: CRITICAL (Protected System Component)",
                "Essential Fedora base operating-system component. Removal may break boot, core runtime, or package management.",
            )
        category_risk = category_removal_risk(getattr(pkg, "primary_category", ""))
        category_risk = category_risk or category_removal_risk(
            getattr(pkg, "sub_category", "")
        )
        if any(
            getattr(pkg, attribute, False)
            for attribute in (
                "is_graphics_driver",
                "is_kernel_module",
                "is_firmware",
                "is_audio_sound",
                "is_desktop_addon",
                "is_systemd_service",
                "is_security_pkg",
            )
        ):
            return RemovalSafetyAssessment(
                RemovalRiskLevel.HIGH,
                "Removal Risk: HIGH (Hardware Driver or Core Service)",
                "Removing this package may disable networking, audio, graphics acceleration, or the desktop session.",
            )
        if category_risk == RemovalRiskLevel.CRITICAL:
            return RemovalSafetyAssessment(
                RemovalRiskLevel.CRITICAL,
                "Removal Risk: CRITICAL (Core System Category)",
                "This category represents core Fedora infrastructure. Review the DNF-resolved plan carefully before removal.",
            )
        if category_risk == RemovalRiskLevel.HIGH:
            return RemovalSafetyAssessment(
                RemovalRiskLevel.HIGH,
                "Removal Risk: HIGH (System Architecture Category)",
                "This category represents hardware support or system architecture. Review the DNF-resolved plan carefully before removal.",
            )
        # A leaf package still needs a warning when its affirmative role is a
        # driver, service, desktop shell component, or security package. For an
        # ordinary leaf, DNF's orphan result remains useful positive evidence.
        if getattr(pkg, "is_orphan", False):
            return RemovalSafetyAssessment(
                RemovalRiskLevel.SAFE,
                "Removal Risk: SAFE (Unneeded Leaf Package)",
                "No installed package depends on this component. Review the DNF plan before applying changes.",
            )
        metadata_limited = (
            getattr(pkg, "primary_category", "") == "unclassified"
            or getattr(pkg, "classification_evidence", "")
            == ClassificationEvidence.INSUFFICIENT_METADATA.value
        )
        affirmative_role_flag = any(
            getattr(pkg, attribute, False)
            for attribute in (
                "is_desktop_app",
                "is_cli_tool",
                "is_user_installed",
                "is_c_lib",
                "is_gui_toolkit",
                "is_media_plugin",
                "is_library",
            )
        )
        # A remembered pillar alone is not proof that an incompletely described
        # package is a shared library. Prefer a human review over a misleading
        # medium-risk library warning.
        if metadata_limited and not affirmative_role_flag:
            return RemovalSafetyAssessment(
                RemovalRiskLevel.REVIEW,
                "Removal Risk: REVIEW (Classification Incomplete)",
                "Dendro does not have enough metadata to infer this package's role. Review the DNF-resolved plan and dependency details before removal.",
            )
        if (
            getattr(pkg, "is_desktop_app", False)
            or getattr(pkg, "is_cli_tool", False)
            or getattr(pkg, "is_user_installed", False)
        ):
            return RemovalSafetyAssessment(
                RemovalRiskLevel.LOW,
                "Removal Risk: LOW (Standard User Software)",
                "Standard user software. Still review the DNF plan because dependencies can change the final transaction.",
            )
        if any(
            getattr(pkg, attribute, False)
            for attribute in (
                "is_c_lib",
                "is_gui_toolkit",
                "is_media_plugin",
                "is_library",
            )
        ):
            return RemovalSafetyAssessment(
                RemovalRiskLevel.MEDIUM,
                "Removal Risk: CAUTION (Shared System Library)",
                "Shared runtime library or framework. Other applications or system tools may depend on it.",
            )

        if category_risk == RemovalRiskLevel.MEDIUM:
            return RemovalSafetyAssessment(
                RemovalRiskLevel.MEDIUM,
                "Removal Risk: CAUTION (Shared System Library)",
                "Shared runtime library or framework. Other applications or system tools may depend on it.",
            )
        if category_risk == RemovalRiskLevel.REVIEW:
            return RemovalSafetyAssessment(
                RemovalRiskLevel.REVIEW,
                "Removal Risk: REVIEW (Classification Incomplete)",
                "Dendro does not have enough metadata to infer this package's role. Review the DNF-resolved plan and dependency details before removal.",
            )
        return RemovalSafetyAssessment(
            RemovalRiskLevel.LOW,
            "Removal Risk: LOW (Standard Package)",
            "Non-critical package classification. Review the DNF plan before removal.",
        )


# Backward-compatible public aliases.
FreeDesktopTaxonomyEngine = ProductionTaxonomyEngine
IntelligentPackageClassifier = ProductionTaxonomyEngine
