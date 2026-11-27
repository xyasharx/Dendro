# dendro/core/package_subcategories.py
"""Desktop/CLI subcategory heuristics and conservative removal-safety guidance."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from dendro.core.taxonomy import (
    ClassificationEvidence,
    RemovalRiskLevel,
    RemovalSafetyAssessment,
    category_removal_risk,
)

if TYPE_CHECKING:
    from dendro.core.backend import PackageInfo

_CLI_EDITOR_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "ed",
        "emacs",
        "gvim",
        "helix",
        "jed",
        "joe",
        "kakoune",
        "less",
        "micro",
        "nano",
        "neovim",
        "nvim",
        "vim",
    }
)
_CLI_SHELL_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "ash",
        "bash",
        "byobu",
        "csh",
        "dash",
        "elvish",
        "fish",
        "ksh",
        "mksh",
        "nu",
        "nushell",
        "screen",
        "starship",
        "tcsh",
        "tmux",
        "xonsh",
        "zellij",
        "zsh",
    }
)
_CLI_ARCHIVE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "7z",
        "7zip",
        "awk",
        "bzip2",
        "cabextract",
        "cpio",
        "gawk",
        "gtar",
        "gzip",
        "jq",
        "lbzip2",
        "lz4",
        "lzop",
        "mawk",
        "p7zip",
        "pbzip2",
        "pigz",
        "pxz",
        "pzstd",
        "rar",
        "rpm2cpio",
        "sed",
        "squashfs",
        "tar",
        "unar",
        "unrar",
        "unzip",
        "xz",
        "yq",
        "zarchive",
        "zip",
        "zstd",
    }
)
_CLI_NETWORK_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "aria2",
        "arp",
        "autossh",
        "curl",
        "dig",
        "ethtool",
        "httpie",
        "iperf",
        "iperf3",
        "iproute",
        "iproute2",
        "iw",
        "mtr",
        "nc",
        "ncat",
        "netcat",
        "nmap",
        "openssh",
        "rsync",
        "socat",
        "ssh",
        "sshfs",
        "sshpass",
        "tailscale",
        "tcpdump",
        "traceroute",
        "tshark",
        "wget",
        "wget2",
        "whois",
        "wireguard",
        "wireshark",
        "wpa_supplicant",
        "xh",
    }
)
_CLI_MONITORING_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "atop",
        "bandwhich",
        "bpftop",
        "btop",
        "dstat",
        "duf",
        "dust",
        "gdb",
        "glances",
        "htop",
        "hyperfine",
        "iftop",
        "iostat",
        "iotop",
        "lldb",
        "lsof",
        "ltrace",
        "ncdu",
        "nethogs",
        "nvtop",
        "perf",
        "procps",
        "sar",
        "strace",
        "sysstat",
        "valgrind",
        "vmstat",
    }
)
_CLI_SEARCH_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "ack",
        "ag",
        "bat",
        "batcat",
        "colordiff",
        "delta",
        "diffutils",
        "exa",
        "eza",
        "fd",
        "findutils",
        "fzf",
        "grep",
        "lf",
        "locate",
        "lsd",
        "mc",
        "mlocate",
        "nnn",
        "patch",
        "patchutils",
        "plocate",
        "ranger",
        "rg",
        "ripgrep",
        "skim",
        "stow",
        "the_silver_searcher",
        "tree",
        "yazi",
    }
)

_DESKTOP_SUMMARY_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "desktop_games",
        re.compile(
            r"\b(?:video game|game engine|arcade game|puzzle game|board game|emulator|roguelike|chess)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "desktop_internet",
        re.compile(
            r"\b(?:web browser|e-?mail client|mail client|irc client|bittorrent|torrent client|feed reader|rss reader|instant messag\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "desktop_multimedia",
        re.compile(
            r"\b(?:media player|video player|music player|audio player|audio editor|video editor|screen recorder|podcast player|digital audio workstation|synthesizer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "desktop_graphics",
        re.compile(
            r"\b(?:image editor|photo editor|vector graphics|3d model\w*|digital painting|image viewer|screenshot tool|raw photo)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "desktop_office",
        re.compile(
            r"\b(?:word processor|spreadsheet|presentation software|pdf viewer|pdf reader|document viewer|e-?book reader|note-?taking|personal finance|calendar)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "desktop_development",
        re.compile(
            r"\b(?:integrated development environment|\bide\b|code editor|source code editor|git client|graphical debugger|hex editor)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "desktop_science",
        re.compile(
            r"\b(?:astronomy|planetarium|scientific|chemistry|physics|mathematics|\bcad\b|schematic|pcb layout|educational|flashcard|language learning|fitness tracker|health tracker)\b",
            re.IGNORECASE,
        ),
    ),
)

_CLI_SUMMARY_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "cli_editors",
        re.compile(
            r"\b(?:text editor|modal editor|terminal pager)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "cli_shells",
        re.compile(
            r"\b(?:command shell|interactive shell|terminal multiplexer|shell prompt)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "cli_data_archiving",
        re.compile(
            r"\b(?:file archiver|compression utility|decompression|archive extractor|json processor|yaml processor)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "cli_networking",
        re.compile(
            r"\b(?:dns client|http client|network scanner|packet analyzer|file transfer|remote login|ssh client|network diagnostic)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "cli_monitoring",
        re.compile(
            r"\b(?:process viewer|system monitor|disk usage analyzer|resource monitor|performance profiler|system call tracer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "cli_search_files",
        re.compile(
            r"\b(?:fuzzy finder|file search|directory tree|file manager|diff viewer|pattern search)\b",
            re.IGNORECASE,
        ),
    ),
)


def desktop_subcategory(
    categories: set[str] | frozenset[str],
    is_settings_panel: bool,
    *,
    mimehandlers: Sequence[str] = (),
    summary: str = "",
    description: str = "",
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
    if categories.intersection(
        {
            "science",
            "education",
            "healthfitness",
            "artificialintelligence",
            "astronomy",
            "biology",
            "cad",
            "chemistry",
            "computerscience",
            "datavisualization",
            "electricity",
            "electronics",
            "engineering",
            "geography",
            "geology",
            "geoscience",
            "history",
            "humanities",
            "languages",
            "literature",
            "maps",
            "math",
            "medicalsoftware",
            "numericalanalysis",
            "parallelcomputing",
            "physics",
            "robotics",
            "spirituality",
            "sports",
            "teaching",
        }
    ):
        return "desktop_science"

    if not categories:
        for raw_mime in mimehandlers:
            mime = raw_mime.strip().casefold()
            if mime.startswith(("audio/", "video/")):
                return "desktop_multimedia"
            if mime.startswith("image/"):
                return "desktop_graphics"
            if mime.startswith(
                ("x-scheme-handler/http", "x-scheme-handler/mailto", "message/rfc822")
            ):
                return "desktop_internet"
            if mime in {
                "application/pdf",
                "text/csv",
                "application/rtf",
            } or mime.startswith(
                (
                    "application/vnd.oasis.opendocument.",
                    "application/vnd.openxmlformats-officedocument.",
                )
            ):
                return "desktop_office"
            if mime in {
                "text/x-python",
                "text/x-csrc",
                "text/x-c++src",
                "application/x-shellscript",
            }:
                return "desktop_development"

        text = f"{summary} {description}".strip()
        if text:
            for category_id, pattern in _DESKTOP_SUMMARY_PATTERNS:
                if pattern.search(text):
                    return category_id
    return "desktop_utilities"


def cli_subcategory(
    name: str,
    provides: tuple[str, ...],
    summary: str = "",
    description: str = "",
) -> str:
    normalized = name.strip().casefold()
    tokens = frozenset(part for part in re.split(r"[-_]+", normalized) if part)
    prov_names = {
        p.split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].strip().casefold()
        for p in provides
        if p.strip()
    }
    if "editor" in prov_names or tokens & _CLI_EDITOR_TOKENS:
        return "cli_editors"
    if "shell" in prov_names or tokens & _CLI_SHELL_TOKENS:
        return "cli_shells"
    if tokens & _CLI_ARCHIVE_TOKENS:
        return "cli_data_archiving"
    if (
        tokens & _CLI_NETWORK_TOKENS
        or normalized in {"net-tools", "bind-utils"}
        or normalized.startswith(("net-tools-", "bind-utils-"))
    ):
        return "cli_networking"
    if tokens & _CLI_MONITORING_TOKENS:
        return "cli_monitoring"
    if (
        tokens & _CLI_SEARCH_TOKENS
        or normalized == "fd-find"
        or normalized.startswith("fd-find-")
    ):
        return "cli_search_files"

    summary_text = f"{summary} {description}".strip()
    if summary_text:
        for category_id, pattern in _CLI_SUMMARY_PATTERNS:
            if pattern.search(summary_text):
                return category_id
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
