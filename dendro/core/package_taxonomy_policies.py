# dendro/core/package_taxonomy_policies.py
"""Declarative package-taxonomy overrides, core package sets, and fallback policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from dendro.core.taxonomy import PackageArchetype, validate_classification_path

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
class PackageRoleSpec:
    """A narrow, reviewable package-name policy for an otherwise opaque RPM.

    RPM file manifests are preferred whenever they identify a physical role. A
    policy entry is a fallback for deliberately small meta, filesystem, common,
    or data RPMs whose package name is itself a stable Fedora/upstream contract.
    Entries therefore use exact names or a constrained project namespace — never
    broad suffix guessing such as ``-data`` or ``-common``.
    """

    identifier: str
    archetype: PackageArchetype
    parent_pillar: str
    category_id: str
    rationale: str
    exact_package_names: tuple[str, ...] = ()
    package_name_prefixes: tuple[str, ...] = ()
    package_name_suffixes: tuple[str, ...] = ()
    sub_category: str | None = None

    def match_priority(self, package_name: str) -> tuple[int, int] | None:
        """Return an auditable specificity score, or ``None`` when unmatched."""
        normalized = package_name.strip().casefold()
        if normalized in self.exact_package_names:
            return (3, len(normalized))
        prefixes = tuple(
            prefix
            for prefix in self.package_name_prefixes
            if normalized.startswith(prefix)
        )
        if prefixes:
            return (2, max(map(len, prefixes)))
        suffixes = tuple(
            suffix
            for suffix in self.package_name_suffixes
            if normalized.endswith(suffix)
        )
        if suffixes:
            return (1, max(map(len, suffixes)))
        return None


# Fallback policies intentionally cover RPMs whose small manifest cannot expose
# their role by itself. They are grouped by stable Fedora/upstream namespace or
# exact package identity, and are only consulted after manifest/AppStream rules.
FALLBACK_PACKAGE_ROLE_SPECS: Final[tuple[PackageRoleSpec, ...]] = (
    PackageRoleSpec(
        "command_line_entry_points",
        PackageArchetype.CLI_UTILITY,
        "pillar_cli",
        "cli_tools",
        "Explicit package policy identifies a command-line tooling entry point",
        exact_package_names=("catatonit", "docker-compose", "git", "moby-filesystem"),
    ),
    PackageRoleSpec(
        "terminal_editor_support",
        PackageArchetype.CLI_UTILITY,
        "pillar_cli",
        "cli_tools",
        "Explicit package policy identifies terminal editor support files",
        package_name_prefixes=("emacs-", "vim-"),
    ),
    PackageRoleSpec(
        "service_stack_support",
        PackageArchetype.SYSTEM_DAEMON,
        "pillar_system",
        "systemd_services",
        "Explicit package policy identifies supporting files for a system service stack",
        exact_package_names=(
            "cups-filesystem",
            "dbus",
            "dnf5daemon-server-polkit",
            "nginx-mimetypes",
            "nginx-mod-fancyindex",
            "samba-common",
        ),
    ),
    PackageRoleSpec(
        "fedora_system_scaffolding",
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "fedora_core",
        "Explicit package policy identifies Fedora base-system scaffolding",
        exact_package_names=(
            "fuse-common",
            "libdnf5-plugin-appstream",
            "ncurses-base",
            "rootfiles",
        ),
    ),
    PackageRoleSpec(
        "security_policy_support",
        PackageArchetype.CORE_SYSTEM,
        "pillar_system",
        "security_pkgs",
        "Explicit package policy identifies a system security-policy component",
        exact_package_names=("elfutils-default-yama-scope",),
    ),
    PackageRoleSpec(
        "graphics_stack_support",
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "graphics_drivers",
        "Explicit package policy identifies graphics-stack infrastructure",
        exact_package_names=("mesa-filesystem", "xorg-x11-server-common"),
    ),
    PackageRoleSpec(
        "xorg_driver_namespace",
        PackageArchetype.HARDWARE_DRIVER,
        "pillar_hardware",
        "graphics_drivers",
        "The X.Org driver package namespace identifies a graphics driver",
        package_name_prefixes=("xorg-x11-drv-",),
    ),
    PackageRoleSpec(
        "desktop_runtime_support",
        PackageArchetype.DESKTOP_ADDON,
        "pillar_system",
        "desktop_addons",
        "Explicit package policy identifies desktop-shell or graphical-runtime support",
        exact_package_names=(
            "chromium-common",
            "kde-settings",
            "kde-settings-plasma",
            "kde-settings-pulseaudio",
            "libksysguard-common",
            "plasma-workspace-common",
        ),
    ),
    PackageRoleSpec(
        "desktop_theme_assets",
        PackageArchetype.STATIC_ASSET,
        "pillar_libs",
        "themes",
        "Explicit package policy identifies desktop visual assets",
        exact_package_names=(
            "kvantum-data",
            "plasma-breeze-common",
            "plasma-lookandfeel-fedora",
        ),
        package_name_prefixes=("fedora-logos-",),
        package_name_suffixes=("-backgrounds-kde", "-wallpapers"),
    ),
    PackageRoleSpec(
        "development_tool_data",
        PackageArchetype.DEVELOPMENT_SDK,
        "pillar_libs",
        "devel",
        "Explicit package policy identifies build-tool or SDK support data",
        exact_package_names=("llvm-filesystem", "pkgconf-m4"),
        package_name_prefixes=("cmake-",),
    ),
    PackageRoleSpec(
        "gui_framework_support",
        PackageArchetype.GUI_TOOLKIT,
        "pillar_libs",
        "gui_toolkits",
        "Explicit package policy identifies a graphical framework support component",
        exact_package_names=(
            "kf5-frameworkintegration",
            "kf5-sonnet",
            "kf6-frameworkintegration",
            "libx11-common",
            "qt-settings",
        ),
    ),
    PackageRoleSpec(
        "font_support_data",
        PackageArchetype.STATIC_ASSET,
        "pillar_libs",
        "fonts",
        "Explicit package policy identifies shared font infrastructure",
        exact_package_names=("fonts-filesystem", "google-noto-fonts-common"),
        package_name_prefixes=("xorg-x11-fonts-",),
    ),
    PackageRoleSpec(
        "localization_support_data",
        PackageArchetype.STATIC_ASSET,
        "pillar_libs",
        "locales",
        "Explicit package policy identifies locale or spelling-dictionary data",
        exact_package_names=("glibc-locale-source",),
        package_name_prefixes=("hunspell-",),
    ),
    PackageRoleSpec(
        "image_loader_plugins",
        PackageArchetype.MEDIA_CODEC,
        "pillar_libs",
        "media_plugins",
        "Explicit package policy identifies image decoding loader plugins",
        exact_package_names=("glycin-loaders",),
    ),
    PackageRoleSpec(
        "reference_and_format_data",
        PackageArchetype.STATIC_ASSET,
        "pillar_libs",
        "reference_data",
        "Explicit package policy identifies shared documentation, OCR, or format-reference data",
        exact_package_names=(
            "pcre2-syntax",
            "poppler-data",
            "tesseract-common",
            "xml-common",
        ),
        package_name_prefixes=("adobe-mappings-", "docbook-"),
    ),
    PackageRoleSpec(
        "system_metadata_data",
        PackageArchetype.SYSTEM_DATA,
        "pillar_system",
        "system_data",
        "Explicit package policy identifies system-wide metadata or configuration data",
        exact_package_names=(
            "appstream-data",
            "color-filesystem",
            "hwdata",
            "kbd-legacy",
            "kbd-misc",
            "libssh-config",
            "libwacom-data",
            "publicsuffix-list-dafsa",
            "rdma-core-common",
            "tzdata",
        ),
    ),
    PackageRoleSpec(
        "repository_release_configuration",
        PackageArchetype.SYSTEM_DATA,
        "pillar_maintenance",
        "repository_sources",
        "Explicit package policy identifies repository definitions or RPM signing keys",
        exact_package_names=(
            "fedora-gpg-keys",
            "fedora-repos",
            "rpmfusion-free-release",
            "rpmfusion-nonfree-release",
        ),
    ),
)


def package_role_policy_integrity_errors() -> tuple[str, ...]:
    """Return role-registry defects for tests and startup diagnostics."""
    errors: list[str] = []
    seen_identifiers: set[str] = set()
    exact_owners: dict[str, str] = {}
    for spec in FALLBACK_PACKAGE_ROLE_SPECS:
        if not spec.identifier or spec.identifier in seen_identifiers:
            errors.append(
                f"duplicate or empty package-role identifier: {spec.identifier!r}"
            )
        seen_identifiers.add(spec.identifier)
        if not (
            spec.exact_package_names
            or spec.package_name_prefixes
            or spec.package_name_suffixes
        ):
            errors.append(f"package-role policy {spec.identifier!r} has no matcher")
        try:
            validate_classification_path(
                spec.parent_pillar,
                spec.category_id,
                spec.sub_category or spec.category_id,
            )
        except ValueError as exc:
            errors.append(f"package-role policy {spec.identifier!r}: {exc}")
        for name in spec.exact_package_names:
            previous = exact_owners.setdefault(name, spec.identifier)
            if previous != spec.identifier:
                errors.append(
                    f"package-role exact name {name!r} is owned by both "
                    f"{previous!r} and {spec.identifier!r}"
                )
    return tuple(errors)


__all__ = (
    "DEFAULT_FEDORA_CORE_PACKAGES",
    "DEFAULT_SYSTEM_ROOT_PILLARS",
    "FALLBACK_PACKAGE_ROLE_SPECS",
    "FEDORA_SYSTEM_ROOT_PILLARS",
    "PACKAGE_TAXONOMY_OVERRIDES",
    "PackageRoleSpec",
    "package_role_policy_integrity_errors",
)
