# dendro/ui/settings_dialog.py
"""
Preferences and About dialog for Dendro.
Allows configuring appearance and RPM epoch display, and viewing legal metadata.
Zero font emoji glyphs to prevent Fontconfig crashes.
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import QCoreApplication, QSettings, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ui.external_links import open_external_web_url
from ui.icons import theme_icon
from ui.styles import THEME_DISPLAY_OPTIONS, get_resolved_theme_key


class DendroSettingsDialog(QDialog):
    """
    Dedicated Preferences & About dialog for Dendro.
    """

    epoch_display_toggled = pyqtSignal(bool)
    theme_changed = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Preferences & About Dendro")
        self.resize(560, 400)
        self.settings = QSettings("FedoraCommunity", "Dendro")

        self._init_ui()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 14, 16, 14)
        main_layout.setSpacing(12)

        self.tabs = QTabWidget()

        # Tab 1: Preferences
        tab_prefs = QWidget()
        prefs_layout = QVBoxLayout(tab_prefs)
        prefs_layout.setContentsMargins(14, 14, 14, 14)
        prefs_layout.setSpacing(14)

        # Appearance Setting
        theme_group = QFrame()
        theme_group.setObjectName("StatsFrame")
        theme_layout = QVBoxLayout(theme_group)
        theme_layout.setContentsMargins(12, 12, 12, 12)
        theme_layout.setSpacing(6)

        theme_title = QLabel("Application appearance")
        theme_title.setStyleSheet("font-weight: bold;")
        self.theme_selector = QComboBox()
        self.theme_selector.setObjectName("ThemeSelector")
        for theme_key, theme_label in THEME_DISPLAY_OPTIONS:
            self.theme_selector.addItem(theme_label, theme_key)

        saved_theme = self.settings.value("theme", "auto", type=str)
        selected_index = self.theme_selector.findData(saved_theme)
        if selected_index < 0:
            # Show the effective choice for a legacy palette key without
            # overwriting the user's existing preference until they select one.
            selected_index = self.theme_selector.findData(
                get_resolved_theme_key(saved_theme)
            )
        self.theme_selector.setCurrentIndex(max(selected_index, 0))
        self.theme_selector.currentIndexChanged.connect(self._on_theme_changed)

        theme_desc = QLabel(
            "Follow System keeps Dendro aligned with your desktop appearance. "
            "Choose Light or Dark only when you want Dendro to stay in that mode."
        )
        theme_desc.setWordWrap(True)
        theme_desc.setStyleSheet("font-size: 11px;")

        theme_layout.addWidget(theme_title)
        theme_layout.addWidget(self.theme_selector)
        theme_layout.addWidget(theme_desc)
        prefs_layout.addWidget(theme_group)

        # Epoch Display Setting
        epoch_group = QFrame()
        epoch_group.setObjectName("StatsFrame")
        epoch_layout = QVBoxLayout(epoch_group)
        epoch_layout.setContentsMargins(12, 12, 12, 12)
        epoch_layout.setSpacing(6)

        self.chk_show_epoch = QCheckBox(
            "Display RPM Epoch tag in package versions (%{epoch}:%{evr})"
        )
        self.chk_show_epoch.setStyleSheet("font-weight: bold;")
        self.chk_show_epoch.setChecked(
            self.settings.value("show_epoch", False, type=bool)
        )
        self.chk_show_epoch.toggled.connect(self._on_epoch_toggled)

        epoch_desc = QLabel(
            "When enabled, versions display with their epoch prefix (e.g. <code>0:1.10.3-1.fc45</code>). "
            "Essential for RPM power users comparing version upgrades where epoch overrides standard semantic ordering."
        )
        epoch_desc.setWordWrap(True)
        epoch_desc.setStyleSheet("color: #a6adc8; font-size: 11px;")

        epoch_layout.addWidget(self.chk_show_epoch)
        epoch_layout.addWidget(epoch_desc)
        prefs_layout.addWidget(epoch_group)
        prefs_layout.addStretch(1)

        self.tabs.addTab(tab_prefs, "Preferences")

        # Tab 2: About & Legal
        tab_about = QWidget()
        about_layout = QVBoxLayout(tab_about)
        about_layout.setContentsMargins(14, 14, 14, 14)
        about_layout.setSpacing(10)

        # Header with Logo & Version
        header_card = QFrame()
        header_card.setObjectName("AICard")
        h_layout = QHBoxLayout(header_card)
        h_layout.setContentsMargins(12, 12, 12, 12)
        h_layout.setSpacing(12)

        icon_lbl = QLabel()
        icon_lbl.setObjectName("AboutApplicationIcon")

        # main.py has already resolved Dendro's icon from the installed theme
        # or from its bundled fallback. Reuse it here so this header also works
        # when the desktop theme does not register Dendro's icon name.
        app_icon = QApplication.windowIcon()
        if app_icon.isNull():
            app_icon = QIcon.fromTheme("io.github.xyasharx.Dendro")

        logo_pixmap = app_icon.pixmap(48, 48)
        if not logo_pixmap.isNull():
            icon_lbl.setPixmap(logo_pixmap)

        h_layout.addWidget(icon_lbl)

        title_vbox = QVBoxLayout()
        application_version = QCoreApplication.applicationVersion() or "unknown"
        title_lbl = QLabel(f"<b>Dendro</b> {application_version}")
        title_lbl.setStyleSheet("font-size: 16px; font-weight: 800;")
        subtitle_lbl = QLabel(
            "Visual Package Manager & Dependency Hierarchy Explorer for Fedora Linux"
        )
        subtitle_lbl.setStyleSheet("color: #a6adc8; font-size: 11px;")
        title_vbox.addWidget(title_lbl)
        title_vbox.addWidget(subtitle_lbl)
        h_layout.addLayout(title_vbox, stretch=1)
        about_layout.addWidget(header_card)

        # Metadata Grid
        grid = QGridLayout()
        grid.setSpacing(8)

        grid.addWidget(QLabel("<b>License:</b>"), 0, 0)
        grid.addWidget(
            QLabel("GNU General Public License v3.0 or later (GPL-3.0-or-later)"), 0, 1
        )

        grid.addWidget(QLabel("<b>Runtime:</b>"), 1, 0)
        grid.addWidget(QLabel(f"Python {sys.version.split()[0]} | PyQt6"), 1, 1)

        grid.addWidget(QLabel("<b>Backend Bindings:</b>"), 2, 0)
        grid.addWidget(QLabel("Native librpm (C) & libdnf5 (C++)"), 2, 1)

        grid.addWidget(QLabel("<b>Author:</b>"), 3, 0)
        grid.addWidget(QLabel("Yashar &lt;yashar@duck.com&gt;"), 3, 1)

        about_layout.addLayout(grid)

        # Link Buttons
        links_box = QHBoxLayout()
        btn_github = QPushButton("GitHub Repository")
        btn_github.setIcon(theme_icon("applications-internet", "web-browser"))
        btn_github.clicked.connect(self._open_github_repository)

        btn_issues = QPushButton("Issue Tracker")
        btn_issues.setIcon(theme_icon("help-browser", "system-help"))
        btn_issues.clicked.connect(self._open_issue_tracker)

        links_box.addWidget(btn_github)
        links_box.addWidget(btn_issues)
        links_box.addStretch(1)
        about_layout.addLayout(links_box)
        about_layout.addStretch(1)

        self.tabs.addTab(tab_about, "About & Legal")
        main_layout.addWidget(self.tabs, stretch=1)

        # Bottom Close Button
        btn_bar = QHBoxLayout()
        btn_bar.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_bar.addWidget(btn_close)
        main_layout.addLayout(btn_bar)

    def _open_github_repository(self) -> None:
        """Open Dendro's repository through the shared feedback gateway."""
        open_external_web_url(
            self,
            "https://github.com/xyasharx/Dendro",
            link_label="Dendro repository",
        )

    def _open_issue_tracker(self) -> None:
        """Open Dendro's issue tracker through the shared feedback gateway."""
        open_external_web_url(
            self,
            "https://github.com/xyasharx/Dendro/issues",
            link_label="Dendro issue tracker",
        )

    def _on_theme_changed(self, index: int) -> None:
        """Persist and apply a deliberate system/light/dark appearance choice."""
        theme_key = self.theme_selector.itemData(index)
        if theme_key not in {"auto", "dark", "light"}:
            return
        self.settings.setValue("theme", theme_key)
        self.theme_changed.emit(theme_key)

    def _on_epoch_toggled(self, checked: bool):
        self.settings.setValue("show_epoch", checked)
        self.epoch_display_toggled.emit(checked)
