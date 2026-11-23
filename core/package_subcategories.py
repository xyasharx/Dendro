# dendro/core/package_subcategories.py
"""Desktop/CLI subcategory heuristics and conservative removal-safety guidance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.taxonomy import (
    ClassificationEvidence,
    RemovalRiskLevel,
    RemovalSafetyAssessment,
    category_removal_risk,
)

if TYPE_CHECKING:
    from core.backend import PackageInfo


def desktop_subcategory(
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


def cli_subcategory(name: str, provides: tuple[str, ...]) -> str:
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
        token in name for token in ("bash", "zsh", "fish", "tmux", "screen", "zellij")
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


def assess_package_removal_safety(pkg: PackageInfo) -> RemovalSafetyAssessment:
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


__all__ = (
    "assess_package_removal_safety",
    "cli_subcategory",
    "desktop_subcategory",
)
