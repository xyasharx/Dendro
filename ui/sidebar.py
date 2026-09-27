# dendro/ui/sidebar.py
"""
Navigation sidebar for fine-grained, specialized package categories.
Groups packages cleanly into Applications, Hardware & Drivers, System Core,
Libraries & Plugins, Programming Ecosystems, and Maintenance/Updates.
Uses native FreeDesktop theme icons instead of raw unicode emojis.
"""
from __future__ import annotations

from typing import Dict, Final, List, Optional, Tuple
from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QWidget


class CategorySidebar(QListWidget):
    """
    Navigation sidebar for fine-grained, specialized package categories.
    Features live item counters, section headers, and native desktop theme icons.
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

    # Standard FreeDesktop Icon Theme Mapping
    CATEGORY_ICONS: Final[Dict[str, str]] = {
        "user_apps": "applications-other",
        "cli_tools": "utilities-terminal",
        "system_settings": "preferences-system",
        "graphics_drivers": "video-display",
        "audio_sound": "audio-card",
        "kernel_modules": "system-run",
        "firmware": "drive-harddisk",
        "fedora_core": "emblem-default",
        "systemd_services": "system-software-update",
        "security_pkgs": "security-high",
        "media_plugins": "video-x-generic",
        "desktop_addons": "preferences-desktop",
        "gui_toolkits": "applications-graphics",
        "c_libs": "applications-development",
        "devel": "applications-development",
        "fonts": "font-x-generic",
        "locales": "preferences-desktop-locale",
        "themes": "preferences-desktop-theme",
        "python_pkgs": "text-x-python",
        "rust_pkgs": "applications-development",
        "jvm_pkgs": "applications-development",
        "nodejs_pkgs": "applications-development",
        "updates_available": "software-update-available",
        "user_installed": "emblem-favorite",
        "orphans": "user-trash",
        "copr_repos": "package-x-generic",
        "rpmfusion_repos": "drive-optical",
        "queued": "document-save",
        "all": "system-software-install",
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

    def _init_items(self):
        for label, tag, is_header in self.CATEGORIES_CONFIG:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, tag)

            if is_header:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                icon_name = self.CATEGORY_ICONS.get(tag, "package-x-generic")
                item.setIcon(QIcon.fromTheme(icon_name))
                self._category_items[tag] = item
                self._category_base_labels[tag] = label

            self.addItem(item)

        # Default selection: Desktop Applications (index 1)
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
