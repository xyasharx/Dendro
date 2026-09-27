# dendro/ui/sidebar.py
"""
Navigation sidebar for two-tier hierarchical package categories.
Organizes all installed packages across 6 System Pillars with fine-grained subcategories
adhering to FreeDesktop XDG Menu Specifications and POSIX FHS directory standards.
Uses distinct, full-color FreeDesktop vector icons visible on both light and dark backgrounds.
Zero emoji glyphs to prevent Fontconfig shaping and layout crashes.
"""
from __future__ import annotations

from typing import Dict, Final, List, Optional, Tuple
from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QWidget


class CategorySidebar(QListWidget):
    """
    Navigation sidebar for two-tier hierarchical package categories.
    Features live item counters, non-selectable section headers, semantic tooltips,
    and distinct full-color desktop theme vector icons.
    """

    category_selected = pyqtSignal(str)

    # Structure: (Display Label, Technical Filter Tag, Is Group Header)
    CATEGORIES_CONFIG: Final[List[Tuple[str, str, bool]]] = [
        # =====================================================================
        # Pillar 1: Desktop Applications (GUI)
        # =====================================================================
        ("1. DESKTOP APPLICATIONS", "", True),
        ("All Desktop Applications", "user_apps", False),
        ("  Web Browsers & Internet", "desktop_internet", False),
        ("  Audio & Video Players", "desktop_multimedia", False),
        ("  Graphics & Design", "desktop_graphics", False),
        ("  Office & Productivity", "desktop_office", False),
        ("  Developer Tools & IDEs", "desktop_development", False),
        ("  Games & Emulators", "desktop_games", False),
        ("  Settings & Control Panels", "system_settings", False),
        ("  Accessories & Utilities", "desktop_utilities", False),

        # =====================================================================
        # Pillar 2: Command-Line & Console (CLI)
        # =====================================================================
        ("2. COMMAND-LINE UTILITIES", "", True),
        ("All Command-Line Tools", "cli_tools", False),
        ("  Terminal Editors & Pagers", "cli_editors", False),
        ("  Shells & Multiplexers", "cli_shells", False),
        ("  Search & File Utilities", "cli_search_files", False),
        ("  Network & Remote Access", "cli_networking", False),
        ("  Monitoring & Diagnostics", "cli_monitoring", False),
        ("  Archiving & Compression", "cli_data_archiving", False),

        # =====================================================================
        # Pillar 3: Hardware & Driver Stack
        # =====================================================================
        ("3. HARDWARE & DRIVER STACK", "", True),
        ("All Hardware Stack", "pillar_hardware", False),
        ("  Graphics & 3D Drivers", "graphics_drivers", False),
        ("  Audio & Sound Architecture", "audio_sound", False),
        ("  Kernel Modules & DKMS", "kernel_modules", False),
        ("  Firmware & Microcode", "firmware", False),

        # =====================================================================
        # Pillar 4: System Architecture
        # =====================================================================
        ("4. SYSTEM ARCHITECTURE", "", True),
        ("All System Architecture", "pillar_system", False),
        ("  Fedora Core Infrastructure", "fedora_core", False),
        ("  Systemd Services & Daemons", "systemd_services", False),
        ("  Security, PAM & SELinux", "security_pkgs", False),
        ("  Window Managers & Addons", "desktop_addons", False),

        # =====================================================================
        # Pillar 5: Libraries & Development
        # =====================================================================
        ("5. LIBRARIES & DEVELOPMENT", "", True),
        ("All Libraries & Runtimes", "pillar_libs", False),
        ("  C/C++ Shared Libraries", "c_libs", False),
        ("  Development Headers & SDKs", "devel", False),
        ("  GUI Frameworks & Toolkits", "gui_toolkits", False),
        ("  Media Codecs & Plugins", "media_plugins", False),
        ("  Python Ecosystem", "python_pkgs", False),
        ("  Rust & Cargo Crates", "rust_pkgs", False),
        ("  Java & JVM Platform", "jvm_pkgs", False),
        ("  Node.js & Web Runtimes", "nodejs_pkgs", False),
        ("  Fonts & Typography", "fonts", False),
        ("  Themes & Visual Assets", "themes", False),
        ("  Locales & Translations", "locales", False),

        # =====================================================================
        # Pillar 6: Maintenance & Repositories
        # =====================================================================
        ("6. MAINTENANCE & SOURCES", "", True),
        ("Available Updates", "updates_available", False),
        ("User-Installed Packages", "user_installed", False),
        ("Orphan Packages", "orphans", False),
        ("COPR Repositories", "copr_repos", False),
        ("RPM Fusion Packages", "rpmfusion_repos", False),
        ("Pending Changes", "queued", False),
        ("All Raw RPMs", "all", False),
    ]

    # Full-color, high-contrast FreeDesktop icon chains (tested on both dark and light themes)
    CATEGORY_ICONS: Final[Dict[str, List[str]]] = {
        # Desktop GUI Categories
        "user_apps": ["applications-other", "preferences-desktop-apps", "application-x-executable"],
        "desktop_internet": ["applications-internet", "web-browser", "network-workgroup"],
        "desktop_multimedia": ["applications-multimedia", "multimedia-player", "audio-x-generic"],
        "desktop_graphics": ["applications-graphics", "image-x-generic", "view-preview"],
        "desktop_office": ["applications-office", "x-office-document", "text-x-generic"],
        "desktop_development": ["applications-development", "text-x-c++src", "system-run"],
        "desktop_games": ["applications-games", "input-gaming", "gamepad"],
        "system_settings": ["preferences-system", "preferences-desktop", "emblem-system"],
        "desktop_utilities": ["applications-utilities", "accessories-calculator", "utility"],

        # CLI Tool Categories
        "cli_tools": ["utilities-terminal", "terminal", "system-run"],
        "cli_editors": ["accessories-text-editor", "text-editor", "text-x-generic"],
        "cli_shells": ["utilities-terminal", "terminal", "system-run"],
        "cli_search_files": ["system-search", "edit-find", "folder-saved-search"],
        "cli_networking": ["network-wired", "network-transmit-receive", "preferences-system-network"],
        "cli_monitoring": ["utilities-system-monitor", "preferences-system-performance", "system-run"],
        "cli_data_archiving": ["package-x-generic", "application-x-archive", "utilities-file-archiver"],

        # Hardware & Driver Stack
        "pillar_hardware": ["computer", "drive-harddisk", "system-run"],
        "graphics_drivers": ["video-display", "preferences-desktop-display", "display"],
        "audio_sound": ["multimedia-volume-control", "audio-card", "audio-volume-high"],
        "kernel_modules": ["system-run", "preferences-system-performance", "cpu"],
        "firmware": ["media-flash", "drive-removable-media", "computer", "applications-system"],

        # System Architecture
        "pillar_system": ["emblem-default", "system-software-install", "system-run"],
        "fedora_core": ["distributor-logo-fedora", "fedora-logo-icon", "distributor-logo", "system-software-install"],
        "systemd_services": ["preferences-system-services", "system-run", "applications-system"],
        "security_pkgs": ["dialog-password", "system-lock-screen", "security-high", "emblem-locked"],
        "desktop_addons": ["application-x-addon", "preferences-desktop", "emblem-favorite"],

        # Libraries & Development
        "pillar_libs": ["applications-development", "package-x-generic", "application-x-sharedlib"],
        "c_libs": ["application-x-sharedlib", "applications-development", "package-x-generic"],
        "devel": ["applications-development", "text-x-c++src", "package-x-generic"],
        "gui_toolkits": ["applications-graphics", "preferences-desktop-theme", "applications-development"],
        "media_plugins": ["applications-multimedia", "video-x-generic", "audio-x-generic"],
        "python_pkgs": ["text-x-python", "applications-development", "package-x-generic"],
        "rust_pkgs": ["application-x-executable", "applications-engineering", "text-x-rust", "system-run"],
        "jvm_pkgs": ["application-x-java", "text-x-java", "application-x-jar", "package-x-generic"],
        "nodejs_pkgs": ["text-javascript", "application-javascript", "text-x-javascript", "text-html"],
        "fonts": ["preferences-desktop-font", "font-x-generic", "format-text-bold"],
        "themes": ["preferences-desktop-theme", "applications-graphics", "preferences-desktop-wallpaper"],
        "locales": ["preferences-desktop-locale", "config-language", "locale"],

        # Maintenance & Sources
        "updates_available": ["software-update-available", "system-software-update", "emblem-important"],
        "user_installed": ["user-home", "emblem-default", "system-software-install"],
        "orphans": ["user-trash", "edit-delete", "trash-empty"],
        "copr_repos": ["package-x-generic", "system-software-install", "application-x-addon"],
        "rpmfusion_repos": ["drive-optical", "media-optical", "system-software-install"],
        "queued": ["document-save", "emblem-default", "dialog-ok-apply"],
        "all": ["system-software-install", "package-x-generic", "system-run"],
    }

    # Semantic Tooltips Explaining the Authoritative Taxonomy
    CATEGORY_TOOLTIPS: Final[Dict[str, str]] = {
        # Desktop GUI
        "user_apps": "All interactive user-facing graphical software applications with system desktop entries.",
        "desktop_internet": "Web browsers, email clients, instant messaging, and web applications.",
        "desktop_multimedia": "Audio players, video playback engines, sound recorders, and media managers.",
        "desktop_graphics": "Raster photo editors, vector illustration suites, 3D modeling tools, and image viewers.",
        "desktop_office": "Word processors, spreadsheets, presentation software, PDF readers, and finance tools.",
        "desktop_development": "Graphical integrated development environments (IDEs), diff tools, and code editors.",
        "desktop_games": "Native Linux games, game engine runtimes, and console emulators.",
        "system_settings": "Desktop control panels, KCM configuration applets, and system preference dialogs.",
        "desktop_utilities": "Desktop accessories, calculators, archive managers, and file tools.",

        # CLI Tools
        "cli_tools": "All command-line utilities and interactive tools residing in user PATH (/usr/bin).",
        "cli_editors": "Terminal-based text editors (vim, nano, micro) and pagers (less, bat).",
        "cli_shells": "Command interpreters (bash, zsh, fish) and terminal multiplexers (tmux, screen).",
        "cli_search_files": "Directory search utilities (ripgrep, fd, fzf) and file navigation tools (tree, eza).",
        "cli_networking": "Data transfer tools (curl, wget), port scanners (nmap), and remote access utilities (ssh).",
        "cli_monitoring": "Interactive process viewers (htop, btop), diagnostic tracers (strace), and benchmarks.",
        "cli_data_archiving": "Stream processors (jq, sed, gawk) and archive compressors (tar, 7z, zstd, xz).",

        # Hardware & Driver Stack
        "pillar_hardware": "All hardware acceleration drivers, sound servers, kernel modules, and firmware.",
        "graphics_drivers": "DRM kernel drivers, 3D DRI acceleration stacks, and Vulkan/Mesa libraries.",
        "audio_sound": "Core PipeWire, WirePlumber, ALSA, and sound server architecture.",
        "kernel_modules": "Dynamic kernel modules (DKMS), kmod packages, and hardware drivers.",
        "firmware": "Binary device microcode and hardware firmware (/usr/lib/firmware).",

        # System Architecture
        "pillar_system": "Core Fedora boot infrastructure, system services, security policies, and window managers.",
        "fedora_core": "Protected Fedora minimal boot infrastructure, packaging tools, and base libraries.",
        "systemd_services": "Init units, background service daemons, and system tasks.",
        "security_pkgs": "Dedicated authentication, PAM security modules, and SELinux policies.",
        "desktop_addons": "Window managers, compositors, shell extensions, and KIO workers.",

        # Libraries & Development
        "pillar_libs": "Shared C libraries, developer headers, widget toolkits, language packages, and fonts.",
        "c_libs": "Dynamic C/C++ ELF shared object libraries (.so) and ABI providers.",
        "devel": "C/C++ header interfaces (/usr/include), static libraries, and pkg-config files.",
        "gui_toolkits": "Widget frameworks (Qt, GTK, Tkinter, WxWidgets) and bindings.",
        "media_plugins": "Audio/video codecs, format decoders, and media player extensions.",
        "python_pkgs": "Python language runtime libraries and site-packages modules.",
        "rust_pkgs": "Rust ecosystem binaries, compiled tools, and Cargo crates.",
        "jvm_pkgs": "Java virtual machine runtimes, JAR packages, and JVM development tools.",
        "nodejs_pkgs": "Node.js ecosystem packages, npm modules, and web runtimes.",
        "fonts": "TrueType, OpenType, and bitmap typography assets (/usr/share/fonts).",
        "themes": "Desktop visual styles, icon packs, cursors, and wallpaper collections.",
        "locales": "System translations, linguistic dictionaries, and locale definitions.",

        # Maintenance & Sources
        "updates_available": "Installed packages with newer versions or security errata pending in enabled repos.",
        "user_installed": "Packages explicitly requested by the user, separated from background dependencies.",
        "orphans": "Leaf dependencies that are no longer required by any installed package.",
        "copr_repos": "Packages built and installed from Fedora Community COPR repositories.",
        "rpmfusion_repos": "Packages sourced from RPM Fusion Free and Nonfree repositories.",
        "queued": "Packages currently staged for installation or removal in this session.",
        "all": "Complete unfiltered list of all installed RPM packages.",
    }

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SidebarList")
        self.setFixedWidth(290)
        self.setIconSize(QSize(18, 18))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._category_items: Dict[str, QListWidgetItem] = {}
        self._category_base_labels: Dict[str, str] = {}
        self._counts: Dict[str, int] = {}

        self._init_items()
        self.itemClicked.connect(self._on_item_clicked)

    def _get_theme_icon(self, tag: str) -> QIcon:
        """Finds the first existing full-color icon from the fallback chain in the active theme."""
        icon_names = self.CATEGORY_ICONS.get(tag, ["package-x-generic"])
        for name in icon_names:
            icon = QIcon.fromTheme(name)
            if not icon.isNull():
                return icon
        return QIcon.fromTheme("package-x-generic")

    def _init_items(self):
        for label, tag, is_header in self.CATEGORIES_CONFIG:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, tag)

            if is_header:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                item.setIcon(self._get_theme_icon(tag))
                tooltip = self.CATEGORY_TOOLTIPS.get(tag, "")
                if tooltip:
                    item.setToolTip(tooltip)
                self._category_items[tag] = item
                self._category_base_labels[tag] = label

            self.addItem(item)

        # Default selection: All Desktop Applications (row 1)
        self.setCurrentRow(1)

    def update_category_counts(self, counts: Dict[str, int]):
        """Updates live package counters for every category in the sidebar."""
        self._counts.update(counts)

        for tag, item in self._category_items.items():
            base_label = self._category_base_labels.get(tag, "")
            count = self._counts.get(tag, 0)
            if count > 0:
                item.setText(f"{base_label} ({count:,})")
            else:
                item.setText(base_label)

    def _on_item_clicked(self, item: QListWidgetItem):
        tag = item.data(Qt.ItemDataRole.UserRole)
        if tag:
            self.category_selected.emit(tag)
