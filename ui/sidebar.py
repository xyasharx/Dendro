# dendro/ui/sidebar.py
"""
Navigation sidebar for fine-grained, specialized package categories.
Groups packages cleanly into Applications, Hardware & Drivers, System Architecture,
Libraries & Plugins, Programming Ecosystems, and Maintenance/Sources.
Uses native FreeDesktop theme vector icons with fallback chains.
"""
from __future__ import annotations

from typing import Dict, Final, List, Optional, Tuple
from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QWidget


class CategorySidebar(QListWidget):
    """
    Navigation sidebar for fine-grained, specialized package categories.
    Features live item counters, non-selectable section headers, semantic tooltips,
    and native desktop theme icons.
    """

    category_selected = pyqtSignal(str)

    # Structure: (Display Label, Technical Filter Tag, Is Group Header)
    CATEGORIES_CONFIG: Final[List[Tuple[str, str, bool]]] = [
        # Group 1: Applications & User Facing
        ("APPLICATIONS", "", True),
        ("Desktop Applications", "user_apps", False),
        ("Command-Line Utilities", "cli_tools", False),
        ("System Settings & Applets", "system_settings", False),

        # Group 2: Hardware, Drivers & Audio Stack
        ("HARDWARE & GRAPHICS STACK", "", True),
        ("Graphics & 3D Drivers", "graphics_drivers", False),
        ("Audio & Sound Architecture", "audio_sound", False),
        ("Kernel & DKMS Modules", "kernel_modules", False),
        ("Firmware & Microcode", "firmware", False),

        # Group 3: System Core & Infrastructure
        ("SYSTEM ARCHITECTURE", "", True),
        ("Fedora Base Infrastructure", "fedora_core", False),
        ("Systemd Services & Daemons", "systemd_services", False),
        ("Security, PAM & SELinux", "security_pkgs", False),

        # Group 4: Libraries, Toolkits & Plugins
        ("LIBRARIES & PLUGINS", "", True),
        ("Codecs & Media Plugins", "media_plugins", False),
        ("Desktop Addons & Workers", "desktop_addons", False),
        ("GUI Frameworks & Toolkits", "gui_toolkits", False),
        ("C/C++ Shared Libraries", "c_libs", False),
        ("Devel Headers & SDKs", "devel", False),
        ("Fonts & Typography", "fonts", False),
        ("Locales & Translations", "locales", False),
        ("Themes, Icons & Sounds", "themes", False),

        # Group 5: Programming Ecosystems
        ("PROGRAMMING RUNTIMES", "", True),
        ("Python Ecosystem", "python_pkgs", False),
        ("Rust & Cargo Crates", "rust_pkgs", False),
        ("Java & JVM Platform", "jvm_pkgs", False),
        ("Node.js & Web Runtimes", "nodejs_pkgs", False),

        # Group 6: Sources & Maintenance
        ("MAINTENANCE & SOURCES", "", True),
        ("Available Updates", "updates_available", False),
        ("User-Installed Packages", "user_installed", False),
        ("Orphan Packages", "orphans", False),
        ("COPR Repositories", "copr_repos", False),
        ("RPM Fusion Packages", "rpmfusion_repos", False),
        ("Pending Changes", "queued", False),
        ("All Raw RPMs", "all", False),
    ]

    # Standard FreeDesktop Icon Theme Mapping with Fallback Alternatives
    CATEGORY_ICONS: Final[Dict[str, List[str]]] = {
        "user_apps": ["applications-other", "preferences-desktop-apps", "application-x-executable"],
        "cli_tools": ["utilities-terminal", "terminal", "system-run"],
        "system_settings": ["preferences-system", "preferences-desktop", "emblem-system"],
        "graphics_drivers": ["video-display", "preferences-desktop-display", "display"],
        "audio_sound": ["audio-card", "multimedia-volume-control", "audio-volume-high"],
        "kernel_modules": ["system-run", "application-x-addon", "emblem-system"],
        "firmware": ["drive-harddisk", "system-software-install", "computer"],
        "fedora_core": ["emblem-default", "system-software-install", "distributor-logo-fedora"],
        "systemd_services": ["system-software-update", "system-run", "application-x-service"],
        "security_pkgs": ["security-high", "dialog-password", "security-medium"],
        "media_plugins": ["video-x-generic", "applications-multimedia", "audio-x-generic"],
        "desktop_addons": ["preferences-desktop", "application-x-addon", "emblem-favorite"],
        "gui_toolkits": ["applications-graphics", "applications-development", "preferences-desktop-theme"],
        "c_libs": ["applications-development", "application-x-sharedlib", "package-x-generic"],
        "devel": ["applications-development", "text-x-c++src", "package-x-generic"],
        "fonts": ["font-x-generic", "preferences-desktop-font", "format-text-bold"],
        "locales": ["preferences-desktop-locale", "config-language", "locale"],
        "themes": ["preferences-desktop-theme", "preferences-desktop-wallpaper", "applications-graphics"],
        "python_pkgs": ["text-x-python", "applications-development", "package-x-generic"],
        "rust_pkgs": ["applications-development", "application-x-executable", "package-x-generic"],
        "jvm_pkgs": ["applications-development", "application-x-java", "package-x-generic"],
        "nodejs_pkgs": ["applications-development", "text-html", "package-x-generic"],
        "updates_available": ["software-update-available", "system-software-update", "emblem-important"],
        "user_installed": ["emblem-favorite", "emblem-default", "user-home"],
        "orphans": ["user-trash", "edit-delete", "trash-empty"],
        "copr_repos": ["package-x-generic", "system-software-install", "application-x-addon"],
        "rpmfusion_repos": ["drive-optical", "media-optical", "system-software-install"],
        "queued": ["document-save", "emblem-default", "dialog-ok-apply"],
        "all": ["system-software-install", "package-x-generic", "system-run"],
    }

    # Semantic Tooltips Explaining the Authoritative Taxonomy
    CATEGORY_TOOLTIPS: Final[Dict[str, str]] = {
        "user_apps": "Interactive user-facing graphical software applications with system desktop entries.",
        "cli_tools": "Command-line utilities and interactive tools residing in user PATH (/usr/bin).",
        "system_settings": "Desktop control panels, KCM configuration applets, and system preference dialogs.",
        "graphics_drivers": "DRM kernel drivers, 3D DRI acceleration stacks, and Vulkan/Mesa libraries.",
        "audio_sound": "Core PipeWire, WirePlumber, ALSA, and sound server architecture.",
        "kernel_modules": "Dynamic kernel modules (DKMS), kmod packages, and hardware drivers.",
        "firmware": "Binary device microcode and hardware firmware (/usr/lib/firmware).",
        "fedora_core": "Protected Fedora minimal boot infrastructure, packaging tools, and base libraries.",
        "systemd_services": "Init units, background service daemons, and system tasks.",
        "security_pkgs": "Dedicated authentication, PAM security modules, and SELinux policies.",
        "media_plugins": "Audio/video codecs, format decoders, and media player extensions.",
        "desktop_addons": "Window managers, compositors, shell extensions, and KIO workers.",
        "gui_toolkits": "Widget frameworks (Qt, GTK, Tkinter, WxWidgets) and bindings.",
        "c_libs": "Dynamic C/C++ ELF shared object libraries (.so) and ABI providers.",
        "devel": "C/C++ header interfaces (/usr/include), static libraries, and pkg-config files.",
        "fonts": "TrueType, OpenType, and bitmap typography assets (/usr/share/fonts).",
        "locales": "System translations, linguistic dictionaries, and locale definitions.",
        "themes": "Desktop visual styles, icon packs, cursors, and wallpaper collections.",
        "python_pkgs": "Python language runtime libraries and site-packages modules.",
        "rust_pkgs": "Rust ecosystem binaries, compiled tools, and Cargo crates.",
        "jvm_pkgs": "Java virtual machine runtimes, JAR packages, and JVM development tools.",
        "nodejs_pkgs": "Node.js ecosystem packages, npm modules, and web runtimes.",
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
        """Finds the first existing icon from the fallback chain in the active theme."""
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

        # Default selection: Desktop Applications (row 1)
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
