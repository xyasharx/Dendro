# dendro/ui/sidebar.py
from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QWidget


class CategorySidebar(QListWidget):
    """
    سایدبار ناوبری دسته‌بندی‌های هوشمند سیستم فدورا
    شامل گروه‌بندی برنامه‌ها، ران‌تایم‌ها، هسته، کتابخانه‌ها و مخازن
    """

    category_selected = pyqtSignal(str)

    # ساختار دسته‌بندی‌ها: (عنوان نمایشی, برچسب فنی, آیا هدر گروه است؟)
    CATEGORIES_CONFIG: List[Tuple[str, str, bool]] = [
        # بخش ۱: برنامه‌های کاربر
        ("🚀 APPLICATIONS", "", True),
        ("  📱 Desktop Apps", "user_apps", False),
        ("  💻 Command-Line Tools", "cli_tools", False),

        # بخش ۲: ران‌تایم‌ها و اکوسیستم زبان‌های برنامه‌نویسی
        ("⚙️ RUNTIMES & ECOSYSTEM", "", True),
        ("  🐍 Python Modules", "python_pkgs", False),
        ("  🦀 Rust & Cargo Crates", "rust_pkgs", False),
        ("  ☕ Java & JVM Ecosystem", "jvm_pkgs", False),
        ("  🌐 Node.js & Web Runtimes", "nodejs_pkgs", False),

        # بخش ۳: معماری هسته و سیستم فدورا
        ("🏛️ SYSTEM ARCHITECTURE", "", True),
        ("  🏢 Fedora Core Pillars", "fedora_core", False),
        ("  🐧 Kernel & DKMS Modules", "kernel_modules", False),
        ("  🔄 Systemd Services", "systemd_services", False),
        ("  🛡️ Security & SELinux", "security_pkgs", False),

        # بخش ۴: کتابخانه‌ها و اجزای سیستم
        ("📦 LIBRARIES & ASSETS", "", True),
        ("  📚 C/C++ & Shared Libs", "c_libs", False),
        ("  💾 Firmware & Drivers", "firmware", False),
        ("  🔤 Fonts & Typography", "fonts", False),
        ("  🌐 Locales & Languages", "locales", False),
        ("  🛠️ Devel Headers & SDKs", "devel", False),
        ("  🎨 Themes, Icons & Sounds", "themes", False),

        # بخش ۵: منابع مخازن و پسماندها
        ("🧹 MAINTENANCE & SOURCES", "", True),
        ("  🍂 Orphan Packages", "orphans", False),
        ("  🏗️ COPR Repositories", "copr_repos", False),
        ("  💿 RPM Fusion Packages", "rpmfusion_repos", False),
        ("  ⏳ Pending Changes", "queued", False),
        ("  📦 All Raw RPMs", "all", False),
    ]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SidebarList")
        self.setFixedWidth(280)
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
                self._category_items[tag] = item
                self._category_base_labels[tag] = label

            self.addItem(item)

        # انتخاب پیش‌فرض: برنامه‌های دسکتاپ کاربر (آیتم ایندکس 1)
        self.setCurrentRow(1)

    def update_category_counts(self, counts: Dict[str, int]):
        """به‌روزرسانی تعداد پکیج‌های هر دسته‌بندی در سایدبار"""
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
