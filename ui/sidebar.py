# dendro/ui/sidebar.py
"""
Navigation sidebar for two-tier hierarchical package categories.
Organizes all installed packages across 6 System Pillars with fine-grained subcategories.
Features custom delegate rendering with right-aligned counter pill badges,
typographic section headers with hairline dividers, and full-color FreeDesktop vector icons.
Zero emoji glyphs to prevent Fontconfig shaping and layout crashes.
"""
from __future__ import annotations

from typing import Dict, Final, List, Optional, Tuple
from PyQt6.QtCore import QModelIndex, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from ui.styles import get_delegate_palette


# Custom Item Data Roles
class SidebarRoles:
    TagRole: Final[int] = Qt.ItemDataRole.UserRole
    CountRole: Final[int] = Qt.ItemDataRole.UserRole + 1
    IsHeaderRole: Final[int] = Qt.ItemDataRole.UserRole + 2
    IsSubcategoryRole: Final[int] = Qt.ItemDataRole.UserRole + 3


# =============================================================================
# Custom Item Delegate for Elevated Sidebar Aesthetics
# =============================================================================

class SidebarItemDelegate(QStyledItemDelegate):
    """
    Renders sidebar items with:
    - Typographic section headers with subtle hairline dividers
    - True hierarchical indentation for subcategories
    - Right-aligned rounded counter pill badges
    - Left-edge accent indicator for selected items
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QApplication
        font_stack = ["Cantarell", "Inter", "Segoe UI", "system-ui", "sans-serif"]

        app = QApplication.instance()
        sys_font = app.font() if app else QFont()
        base_pt = sys_font.pointSize() if sys_font.pointSize() > 0 else 11

        # Base item font inherits system point size
        self.item_font = QFont()
        self.item_font.setFamilies(font_stack)
        self.item_font.setPointSize(base_pt)
        self.item_font.setWeight(QFont.Weight.Medium)

        # Selected item font
        self.selected_font = QFont()
        self.selected_font.setFamilies(font_stack)
        self.selected_font.setPointSize(base_pt)
        self.selected_font.setBold(True)

        # Section header font scales with system font
        self.header_font = QFont()
        self.header_font.setFamilies(font_stack)
        self.header_font.setPointSize(max(9, base_pt - 1))
        self.header_font.setBold(True)
        self.header_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)

        # Counter pill badge font
        self.badge_font = QFont()
        self.badge_font.setFamilies(font_stack)
        self.badge_font.setPointSize(max(9, base_pt - 1))
        self.badge_font.setBold(True)

        self.fm_item = QFontMetrics(self.item_font)
        self.fm_badge = QFontMetrics(self.badge_font)
        self.current_theme = "auto"
        # Precompute palette once to avoid subprocess calls inside the paint loop
        self.pal = get_delegate_palette(self.current_theme)

    def set_theme(self, theme_choice: str):
        self.current_theme = theme_choice
        self.pal = get_delegate_palette(theme_choice)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        is_header = bool(index.data(SidebarRoles.IsHeaderRole))
        if is_header:
            return QSize(option.rect.width(), 36)
        return QSize(option.rect.width(), 32)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Use precomputed palette (zero disk/subprocess overhead during scrolling)
        pal = self.pal
        rect = option.rect
        is_header = bool(index.data(SidebarRoles.IsHeaderRole))

        # ---------------------------------------------------------------------
        # 1. Render Typographic Section Header
        # ---------------------------------------------------------------------
        if is_header:
            # Hairline divider above header (skip for the very first item)
            if index.row() > 0:
                painter.setPen(QPen(pal["border_subtle"], 1))
                painter.drawLine(rect.left() + 8, rect.top() + 4, rect.right() - 8, rect.top() + 4)

            header_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "").upper()
            painter.setFont(self.header_font)
            painter.setPen(pal["text_dim"])

            text_rect = QRect(rect.left() + 10, rect.top() + 8, rect.width() - 20, rect.height() - 8)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, header_text)

            painter.restore()
            return

        # ---------------------------------------------------------------------
        # 2. Render Interactive Category Item
        # ---------------------------------------------------------------------
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        is_sub = bool(index.data(SidebarRoles.IsSubcategoryRole))

        item_rect = QRectF(rect.left() + 4, rect.top() + 1, rect.width() - 8, rect.height() - 2)

        # Background Pill Highlight
        if is_selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(pal["bg_selected"]))
            painter.drawRoundedRect(item_rect, 6.0, 6.0)

            # Left Accent Bar
            accent_bar = QRectF(rect.left() + 4, rect.top() + 6, 3.5, rect.height() - 12)
            painter.setBrush(QBrush(pal["accent"]))
            painter.drawRoundedRect(accent_bar, 1.5, 1.5)

        elif is_hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(pal["bg_hover"]))
            painter.drawRoundedRect(item_rect, 6.0, 6.0)

        # Indentation for Subcategories
        left_offset = rect.left() + (26 if is_sub else 12)

        # Draw Category Vector Icon
        icon: QIcon = index.data(Qt.ItemDataRole.DecorationRole)
        icon_size = 16
        icon_y = int(rect.top() + (rect.height() - icon_size) / 2)
        if icon and not icon.isNull():
            icon_rect = QRect(left_offset, icon_y, icon_size, icon_size)
            icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter)
            text_x = left_offset + icon_size + 8
        else:
            text_x = left_offset

        # ---------------------------------------------------------------------
        # 3. Draw Right-Aligned Counter Pill Badge
        # ---------------------------------------------------------------------
        count = index.data(SidebarRoles.CountRole) or 0
        pill_width = 0

        if count > 0:
            count_str = f"{count:,}"
            badge_text_w = self.fm_badge.horizontalAdvance(count_str)
            pill_width = badge_text_w + 12
            pill_height = 18
            pill_x = rect.right() - pill_width - 10
            pill_y = rect.top() + (rect.height() - pill_height) // 2

            pill_rect = QRectF(pill_x, pill_y, pill_width, pill_height)

            # Subtle rounded badge background
            if is_selected:
                badge_bg = pal["accent"]
                badge_fg = pal["bg_base"]
            else:
                badge_bg = pal["bg_card"]
                badge_fg = pal["text_secondary"]

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(badge_bg))
            painter.drawRoundedRect(pill_rect, 9.0, 9.0)

            # Draw Counter Text
            painter.setFont(self.badge_font)
            painter.setPen(badge_fg)
            painter.drawText(QRect(int(pill_x), int(pill_y), int(pill_width), int(pill_height)),
                             Qt.AlignmentFlag.AlignCenter, count_str)

        # ---------------------------------------------------------------------
        # 4. Draw Category Title (Guaranteed Zero Overlap with Badge)
        # ---------------------------------------------------------------------
        label_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.setFont(self.selected_font if is_selected else self.item_font)

        if is_selected:
            painter.setPen(pal["accent"])
        elif is_hover:
            painter.setPen(pal["text_main"])
        else:
            painter.setPen(pal["text_secondary"])

        # Truncate text cleanly before the pill badge if label is long
        avail_width = (rect.right() - pill_width - 18) - text_x
        text_rect = QRect(text_x, rect.top(), max(0, avail_width), rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self.fm_item.elidedText(label_text, Qt.TextElideMode.ElideRight, avail_width))

        painter.restore()


# =============================================================================
# Category Sidebar Container
# =============================================================================

class CategorySidebar(QListWidget):
    """
    Two-tier hierarchical navigation sidebar for Dendro.
    Features right-aligned counter pill badges, typographic section dividers,
    and semantic FreeDesktop tooltips.
    """

    category_selected = pyqtSignal(str)

    # Structure: (Display Label, Technical Filter Tag, Is Group Header)
    CATEGORIES_CONFIG: Final[List[Tuple[str, str, bool]]] = [
        # =====================================================================
        # Pillar 1: Desktop Applications (GUI)
        # =====================================================================
        ("1. Desktop Applications", "", True),
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
        # Pillar 2: Command-Line Utilities (CLI)
        # =====================================================================
        ("2. Command-Line Utilities", "", True),
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
        ("3. Hardware & Drivers", "", True),
        ("All Hardware Stack", "pillar_hardware", False),
        ("  Graphics & 3D Drivers", "graphics_drivers", False),
        ("  Audio & Sound Architecture", "audio_sound", False),
        ("  Kernel Modules & DKMS", "kernel_modules", False),
        ("  Firmware & Microcode", "firmware", False),

        # =====================================================================
        # Pillar 4: System Architecture
        # =====================================================================
        ("4. System Architecture", "", True),
        ("All System Architecture", "pillar_system", False),
        ("  Fedora Core Infrastructure", "fedora_core", False),
        ("  Systemd Services & Daemons", "systemd_services", False),
        ("  Security, PAM & SELinux", "security_pkgs", False),
        ("  Window Managers & Addons", "desktop_addons", False),

        # =====================================================================
        # Pillar 5: Libraries & Development
        # =====================================================================
        ("5. Libraries & Development", "", True),
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
        ("6. Maintenance & Sources", "", True),
        ("Available Updates", "updates_available", False),
        ("User-Installed Packages", "user_installed", False),
        ("Orphan Packages", "orphans", False),
        ("COPR Repositories", "copr_repos", False),
        ("RPM Fusion Packages", "rpmfusion_repos", False),
        ("Pending Changes", "queued", False),
        ("All Installed Packages", "all", False),
    ]

    CATEGORY_ICONS: Final[Dict[str, List[str]]] = {
        # Desktop GUI
        "user_apps": ["applications-other", "preferences-desktop-apps", "application-x-executable"],
        "desktop_internet": ["applications-internet", "web-browser", "network-workgroup"],
        "desktop_multimedia": ["applications-multimedia", "multimedia-player", "audio-x-generic"],
        "desktop_graphics": ["applications-graphics", "image-x-generic", "view-preview"],
        "desktop_office": ["applications-office", "x-office-document", "text-x-generic"],
        "desktop_development": ["applications-development", "text-x-c++src", "system-run"],
        "desktop_games": ["applications-games", "input-gaming", "gamepad"],
        "system_settings": ["preferences-system", "preferences-desktop", "emblem-system"],
        "desktop_utilities": ["applications-utilities", "accessories-calculator", "utility"],

        # CLI Tools
        "cli_tools": ["utilities-terminal", "terminal", "system-run"],
        "cli_editors": ["accessories-text-editor", "text-editor", "text-x-generic"],
        "cli_shells": ["utilities-terminal", "terminal", "system-run"],
        "cli_search_files": ["system-search", "edit-find", "folder-saved-search"],
        "cli_networking": ["network-wired", "network-transmit-receive", "preferences-system-network"],
        "cli_monitoring": ["utilities-system-monitor", "preferences-system-performance", "system-run"],
        "cli_data_archiving": ["package-x-generic", "application-x-archive", "utilities-file-archiver"],

        # Hardware & Drivers
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

    CATEGORY_TOOLTIPS: Final[Dict[str, str]] = {
        "user_apps": "All interactive user-facing graphical software applications with system desktop entries.",
        "desktop_internet": "Web browsers, email clients, instant messaging, and web applications.",
        "desktop_multimedia": "Audio players, video playback engines, sound recorders, and media managers.",
        "desktop_graphics": "Raster photo editors, vector illustration suites, 3D modeling tools, and image viewers.",
        "desktop_office": "Word processors, spreadsheets, presentation software, PDF readers, and finance tools.",
        "desktop_development": "Graphical integrated development environments (IDEs), diff tools, and code editors.",
        "desktop_games": "Native Linux games, game engine runtimes, and console emulators.",
        "system_settings": "Desktop control panels, KCM configuration applets, and system preference dialogs.",
        "desktop_utilities": "Desktop accessories, calculators, archive managers, and file tools.",
        "cli_tools": "All command-line utilities and interactive tools residing in user PATH (/usr/bin).",
        "cli_editors": "Terminal-based text editors (vim, nano, micro) and pagers (less, bat).",
        "cli_shells": "Command interpreters (bash, zsh, fish) and terminal multiplexers (tmux, screen).",
        "cli_search_files": "Directory search utilities (ripgrep, fd, fzf) and file navigation tools (tree, eza).",
        "cli_networking": "Data transfer tools (curl, wget), port scanners (nmap), and remote access utilities (ssh).",
        "cli_monitoring": "Interactive process viewers (htop, btop), diagnostic tracers (strace), and benchmarks.",
        "cli_data_archiving": "Stream processors (jq, sed, gawk) and archive compressors (tar, 7z, zstd, xz).",
        "pillar_hardware": "All hardware acceleration drivers, sound servers, kernel modules, and firmware.",
        "graphics_drivers": "DRM kernel drivers, 3D DRI acceleration stacks, and Vulkan/Mesa libraries.",
        "audio_sound": "Core PipeWire, WirePlumber, ALSA, and sound server architecture.",
        "kernel_modules": "Dynamic kernel modules (DKMS), kmod packages, and hardware drivers.",
        "firmware": "Binary device microcode and hardware firmware (/usr/lib/firmware).",
        "pillar_system": "Core Fedora boot infrastructure, system services, security policies, and window managers.",
        "fedora_core": "Protected Fedora minimal boot infrastructure, packaging tools, and base libraries.",
        "systemd_services": "Init units, background service daemons, and system tasks.",
        "security_pkgs": "Dedicated authentication, PAM security modules, and SELinux policies.",
        "desktop_addons": "Window managers, compositors, shell extensions, and KIO workers.",
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
        "updates_available": "Installed packages with newer versions or security errata pending in enabled repos.",
        "user_installed": "Packages explicitly requested by the user, separated from background dependencies.",
        "orphans": "Leaf dependencies that are no longer required by any installed package.",
        "copr_repos": "Packages built and installed from Fedora Community COPR repositories.",
        "rpmfusion_repos": "Packages sourced from RPM Fusion Free and Nonfree repositories.",
        "queued": "Packages currently staged for installation or removal in this session.",
        "all": "Complete unfiltered list of all installed RPM packages on this system.",
    }

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SidebarList")
        self.setFixedWidth(290)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(20)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        # Install custom delegate
        self.sidebar_delegate = SidebarItemDelegate(self)
        self.setItemDelegate(self.sidebar_delegate)

        self._category_items: Dict[str, QListWidgetItem] = {}
        self._counts: Dict[str, int] = {}

        self._init_items()
        self.itemClicked.connect(self._on_item_clicked)

    def set_theme(self, theme_choice: str):
        """Notifies the delegate when the active theme changes."""
        self.sidebar_delegate.set_theme(theme_choice)
        self.viewport().update()

    def _get_theme_icon(self, tag: str) -> QIcon:
        icon_names = self.CATEGORY_ICONS.get(tag, ["package-x-generic"])
        for name in icon_names:
            icon = QIcon.fromTheme(name)
            if not icon.isNull():
                return icon
        return QIcon.fromTheme("package-x-generic")

    def _init_items(self):
        for raw_label, tag, is_header in self.CATEGORIES_CONFIG:
            is_sub = raw_label.startswith("  ")
            clean_label = raw_label.strip()

            item = QListWidgetItem(clean_label)
            item.setData(SidebarRoles.TagRole, tag)
            item.setData(SidebarRoles.IsHeaderRole, is_header)
            item.setData(SidebarRoles.IsSubcategoryRole, is_sub)
            item.setData(SidebarRoles.CountRole, 0)

            if is_header:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setIcon(self._get_theme_icon(tag))
                tooltip = self.CATEGORY_TOOLTIPS.get(tag, "")
                if tooltip:
                    item.setToolTip(tooltip)
                self._category_items[tag] = item

            self.addItem(item)

        # Default selection: All Desktop Applications (row 1)
        self.setCurrentRow(1)

    def update_category_counts(self, counts: Dict[str, int]):
        """Updates right-aligned badge counts without string concatenation."""
        self._counts.update(counts)

        for tag, item in self._category_items.items():
            count = self._counts.get(tag, 0)
            item.setData(SidebarRoles.CountRole, count)

        self.viewport().update()

    def _on_item_clicked(self, item: QListWidgetItem):
        tag = item.data(SidebarRoles.TagRole)
        if tag:
            self.category_selected.emit(tag)
