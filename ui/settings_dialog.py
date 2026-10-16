# dendro/ui/settings_dialog.py
"""
Preferences and About dialog for Dendro.
Allows configuring RPM epoch display and viewing legal/license metadata.
Zero font emoji glyphs to prevent Fontconfig crashes.
"""
from __future__ import annotations

import sys
from typing import Optional
from PyQt6.QtCore import QSettings, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
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


class DendroSettingsDialog(QDialog):
    """
    Dedicated Preferences & About dialog for Dendro.
    """

    epoch_display_toggled = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None):
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

        # Epoch Display Setting
        epoch_group = QFrame()
        epoch_group.setObjectName("StatsFrame")
        epoch_layout = QVBoxLayout(epoch_group)
        epoch_layout.setContentsMargins(12, 12, 12, 12)
        epoch_layout.setSpacing(6)

        self.chk_show_epoch = QCheckBox("Display RPM Epoch tag in package versions (%{epoch}:%{evr})")
        self.chk_show_epoch.setStyleSheet("font-weight: bold;")
        self.chk_show_epoch.setChecked(self.settings.value("show_epoch", False, type=bool))
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
        app_icon = QIcon.fromTheme("io.github.xyasharx.Dendro")
        if not app_icon.isNull():
            icon_lbl.setPixmap(app_icon.pixmap(48, 48))
        h_layout.addWidget(icon_lbl)

        title_vbox = QVBoxLayout()
        title_lbl = QLabel("<b>Dendro</b> 2.10.1")
        title_lbl.setStyleSheet("font-size: 16px; font-weight: 800;")
        subtitle_lbl = QLabel("Visual Package Manager & Dependency Hierarchy Explorer for Fedora Linux")
        subtitle_lbl.setStyleSheet("color: #a6adc8; font-size: 11px;")
        title_vbox.addWidget(title_lbl)
        title_vbox.addWidget(subtitle_lbl)
        h_layout.addLayout(title_vbox, stretch=1)
        about_layout.addWidget(header_card)

        # Metadata Grid
        grid = QGridLayout()
        grid.setSpacing(8)

        grid.addWidget(QLabel("<b>License:</b>"), 0, 0)
        grid.addWidget(QLabel("GNU General Public License v3.0 or later (GPL-3.0-or-later)"), 0, 1)

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
        btn_github.setIcon(QIcon.fromTheme("applications-internet") or QIcon.fromTheme("web-browser"))
        btn_github.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/xyasharx/Dendro")))

        btn_issues = QPushButton("Issue Tracker")
        btn_issues.setIcon(QIcon.fromTheme("help-browser") or QIcon.fromTheme("system-help"))
        btn_issues.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/xyasharx/Dendro/issues")))

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

    def _on_epoch_toggled(self, checked: bool):
        self.settings.setValue("show_epoch", checked)
        self.epoch_display_toggled.emit(checked)
