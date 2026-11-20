# dendro/ui/settings_dialog.py
"""
Preferences and About dialog for Dendro.
Allows configuring appearance and RPM epoch display, and viewing legal metadata.
Zero font emoji glyphs to prevent Fontconfig crashes.
"""

from __future__ import annotations

import html
import sys

from PyQt6.QtCore import QCoreApplication, QSettings, Qt, pyqtSignal
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

from core.dnf5daemon import AUTOMATIC_KEY_IMPORT_PREFERENCE
from core.host_environment import HostEnvironment, detect_host_environment
from core.i18n import (
    LOCALE_ENDONYMS,
    SYSTEM_LOCALE,
    iter_shipped_locales,
    tr,
)
from ui.external_links import open_external_web_url
from ui.fonts import apply_labelled_fonts
from ui.icons import theme_icon
from ui.styles import THEME_DISPLAY_OPTIONS, get_resolved_theme_key


class DendroSettingsDialog(QDialog):
    """
    Dedicated Preferences & About dialog for Dendro.
    """

    epoch_display_toggled = pyqtSignal(bool)
    theme_changed = pyqtSignal(str)
    language_changed = pyqtSignal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        host_environment: HostEnvironment | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(tr("Preferences & About Dendro"))
        self.settings = QSettings("FedoraCommunity", "Dendro")
        self._host_environment = host_environment or detect_host_environment()

        self._init_ui()
        apply_labelled_fonts(self)
        self._size_to_content()

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

        theme_title = QLabel(tr("Application appearance"))
        theme_title.setStyleSheet("font-weight: bold;")
        self.theme_selector = QComboBox()
        self.theme_selector.setObjectName("ThemeSelector")
        for theme_key, theme_label in THEME_DISPLAY_OPTIONS:
            # Table holds the English id; translate at display time.
            self.theme_selector.addItem(tr(theme_label), theme_key)

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
            tr(
                "Follow System keeps Dendro aligned with your desktop appearance. "
                "Choose Light or Dark only when you want Dendro to stay in that mode."
            )
        )
        theme_desc.setWordWrap(True)
        theme_desc.setObjectName("SubtleHelpLabel")

        theme_layout.addWidget(theme_title)
        theme_layout.addWidget(self.theme_selector)
        theme_layout.addWidget(theme_desc)
        prefs_layout.addWidget(theme_group)

        # Language Setting
        language_group = QFrame()
        language_group.setObjectName("StatsFrame")
        language_layout = QVBoxLayout(language_group)
        language_layout.setContentsMargins(12, 12, 12, 12)
        language_layout.setSpacing(6)

        language_title = QLabel(tr("Application language"))
        language_title.setStyleSheet("font-weight: bold;")
        self.language_selector = QComboBox()
        self.language_selector.setObjectName("LanguageSelector")
        self.language_selector.addItem(tr("Follow System"), SYSTEM_LOCALE)
        # Endonyms are never translated: a reader looking for their own
        # language should recognise it written in that language.
        for locale in iter_shipped_locales():
            if locale == "en":
                continue
            self.language_selector.addItem(LOCALE_ENDONYMS.get(locale, locale), locale)
        self.language_selector.addItem(tr("English"), "en")

        saved_language = self.settings.value("language", SYSTEM_LOCALE, type=str)
        language_index = self.language_selector.findData(saved_language)
        self.language_selector.setCurrentIndex(max(language_index, 0))
        self.language_selector.currentIndexChanged.connect(self._on_language_changed)

        self.language_desc = QLabel(
            tr(
                "Follow System uses your desktop's language when Dendro supports it, "
                "otherwise English. Changes apply immediately."
            )
        )
        self.language_desc.setWordWrap(True)
        self.language_desc.setObjectName("SubtleHelpLabel")

        language_layout.addWidget(language_title)
        language_layout.addWidget(self.language_selector)
        language_layout.addWidget(self.language_desc)
        prefs_layout.addWidget(language_group)

        # Epoch Display Setting
        epoch_group = QFrame()
        epoch_group.setObjectName("StatsFrame")
        epoch_layout = QVBoxLayout(epoch_group)
        epoch_layout.setContentsMargins(12, 12, 12, 12)
        epoch_layout.setSpacing(6)

        self.chk_show_epoch = QCheckBox(
            tr("Display RPM Epoch tag in package versions (%{epoch}:%{evr})")
        )
        self.chk_show_epoch.setStyleSheet("font-weight: bold;")
        self.chk_show_epoch.setChecked(
            self.settings.value("show_epoch", False, type=bool)
        )
        self.chk_show_epoch.toggled.connect(self._on_epoch_toggled)

        epoch_desc = QLabel(
            tr(
                "When enabled, versions display with their epoch prefix (e.g. <code>0:1.10.3-1.fc45</code>). "
                "Essential for RPM power users comparing version upgrades where epoch overrides standard semantic ordering."
            )
        )
        epoch_desc.setWordWrap(True)
        epoch_desc.setObjectName("SubtleHelpLabel")

        epoch_layout.addWidget(self.chk_show_epoch)
        epoch_layout.addWidget(epoch_desc)
        prefs_layout.addWidget(epoch_group)

        # Repository signing key trust
        key_group = QFrame()
        key_group.setObjectName("StatsFrame")
        key_layout = QVBoxLayout(key_group)
        key_layout.setContentsMargins(12, 12, 12, 12)
        key_layout.setSpacing(6)

        self.chk_auto_import_keys = QCheckBox(
            tr("Import repository signing keys without asking")
        )
        self.chk_auto_import_keys.setStyleSheet("font-weight: bold;")
        self.chk_auto_import_keys.setChecked(
            self.settings.value(AUTOMATIC_KEY_IMPORT_PREFERENCE, False, type=bool)
        )
        self.chk_auto_import_keys.toggled.connect(self._on_auto_import_keys_toggled)

        key_desc = QLabel(
            tr(
                "A repository's signing key is what makes every package it ships "
                "trusted. When this is off, Dendro shows the key's fingerprint and "
                "asks before trusting it, so you can accept one repository without "
                "trusting every future one. Turning this on skips that question for "
                "all repositories."
            )
        )
        key_desc.setWordWrap(True)
        key_desc.setObjectName("SubtleHelpLabel")

        key_layout.addWidget(self.chk_auto_import_keys)
        key_layout.addWidget(key_desc)
        prefs_layout.addWidget(key_group)
        prefs_layout.addStretch(1)

        self.tabs.addTab(tab_prefs, tr("Preferences"))

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
        # Nothing here is invented: a launcher that never set an application
        # version shows the name alone rather than "Dendro unknown".
        application_version = html.escape(
            QCoreApplication.applicationVersion().strip(), quote=True
        )
        title_lbl = QLabel(
            f"<b>Dendro</b> {application_version}"
            if application_version
            else "<b>Dendro</b>"
        )
        subtitle_lbl = QLabel(
            tr(
                "Visual Package Manager & Dependency Hierarchy Explorer for Fedora Linux"
            )
        )
        subtitle_lbl.setObjectName("SubtleHelpLabel")
        title_vbox.addWidget(title_lbl)
        title_vbox.addWidget(subtitle_lbl)
        h_layout.addLayout(title_vbox, stretch=1)
        about_layout.addWidget(header_card)

        # Metadata Grid
        grid = QGridLayout()
        grid.setSpacing(8)

        def field_name(caption_text: str) -> QLabel:
            """Bold caption for one metadata row.

            The emphasis is an object name rather than inline ``<b>`` markup so
            the label carries a plain word. Translators are also handed a word
            instead of a tag, and the extraction tool can see the ``tr`` call at
            the point where the caption is written.
            """

            caption = QLabel(caption_text)
            caption.setObjectName("AboutFieldName")
            return caption

        grid.addWidget(field_name(tr("License:")), 0, 0)
        grid.addWidget(
            QLabel("GNU General Public License v3.0 or later (GPL-3.0-or-later)"), 0, 1
        )

        grid.addWidget(field_name(tr("Runtime:")), 1, 0)
        grid.addWidget(QLabel(f"Python {sys.version.split()[0]} | PyQt6"), 1, 1)

        grid.addWidget(field_name(tr("Backend Bindings:")), 2, 0)
        grid.addWidget(QLabel(tr("Native librpm (C) & libdnf5 (C++)")), 2, 1)

        host_mode_label = QLabel(self._host_environment.display_label())
        host_mode_label.setObjectName("AboutHostEnvironmentLabel")
        host_mode_label.setTextFormat(Qt.TextFormat.PlainText)
        host_mode_label.setWordWrap(True)
        grid.addWidget(field_name(tr("Host Environment:")), 3, 0)
        grid.addWidget(host_mode_label, 3, 1)

        grid.addWidget(field_name(tr("Author:")), 4, 0)
        grid.addWidget(QLabel("Yashar &lt;yashar@duck.com&gt;"), 4, 1)

        about_layout.addLayout(grid)

        # Link Buttons
        links_box = QHBoxLayout()
        btn_github = QPushButton(tr("GitHub Repository"))
        btn_github.setIcon(theme_icon("applications-internet", "web-browser"))
        btn_github.clicked.connect(self._open_github_repository)

        btn_issues = QPushButton(tr("Issue Tracker"))
        btn_issues.setIcon(theme_icon("help-browser", "system-help"))
        btn_issues.clicked.connect(self._open_issue_tracker)

        links_box.addWidget(btn_github)
        links_box.addWidget(btn_issues)
        links_box.addStretch(1)
        about_layout.addLayout(links_box)
        about_layout.addStretch(1)

        # A single "&" is a mnemonic marker inside a tab label and renders as
        # a bare underscore ("ABOUT _LEGAL"), so it must be doubled.
        self.tabs.addTab(tab_about, tr("About && Legal"))

        # A page of wrapped help text does not have a single fixed height: the
        # same paragraph can need three lines in a narrower window or under a
        # larger desktop font. Wrapping the pages in a scroll area means the
        # controls always keep their natural height, and a window that is
        # dragged shorter scrolls instead of squeezing labels on top of their
        # neighbours.
        for page in (tab_prefs, tab_about):
            page.setMinimumHeight(page.sizeHint().height())
        self.tabs.setMinimumWidth(self.tabs.sizeHint().width())

        main_layout.addWidget(self.tabs, stretch=1)

        # Bottom Close Button
        btn_bar = QHBoxLayout()
        btn_bar.addStretch(1)
        btn_close = QPushButton(tr("Close"))
        btn_close.clicked.connect(self.accept)
        btn_bar.addWidget(btn_close)
        main_layout.addLayout(btn_bar)

    def _size_to_content(self) -> None:
        """Open at a size that fits the help text, capped to the screen.

        A fixed initial size made the layout over-constrained as soon as the
        appearance description wrapped to two lines: the combo kept its 32px
        height while the label below it was squeezed to a single line and drawn
        on top of it. Sizing from the layout's own hint keeps every control at
        its natural height, and the cap stops the dialog opening taller than a
        short screen can show.
        """
        hint = self.sizeHint()
        width = max(hint.width(), 560)
        height = max(hint.height(), 400)

        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            height = min(height, max(400, available.height() - 120))
            width = min(width, max(560, available.width() - 80))

        self.resize(width, height)

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

    def retranslate(self) -> None:
        """Rebuild the Preferences copy in the active language.

        The dialog is normally short-lived, but it is the place the language is
        chosen, so it outlives the change. Rather than translating sixty labels
        in place -- most of them are laid out inside local helpers and are not
        reachable afterwards -- the two pages are rebuilt. Geometry that the
        dialog manages itself (tab titles, the minimum width derived from the
        page's size hint) is refreshed with them.
        """

        current_tab = self.tabs.currentIndex()
        # Drop the whole previous page set, widget and all. Only the pages would
        # otherwise be replaced, leaving the old tab bar stacked in the layout.
        previous = self.tabs
        self.tabs = QTabWidget(self)
        previous.setParent(None)
        previous.deleteLater()
        # ``_init_ui`` installs a layout on the dialog, so the old one has to be
        # detached first. Handing it to a throwaway widget drops it without
        # touching the child widgets, which are being rebuilt anyway.
        stale_layout = self.layout()
        if stale_layout is not None:
            QWidget().setLayout(stale_layout)

        # Rebuilding restores each saved value, which would otherwise fire the
        # toggled/currentIndexChanged handlers as though the user had just
        # changed them -- writing settings and signalling the window in a loop.
        was_blocked = self.blockSignals(True)
        try:
            self._init_ui()
        finally:
            self.blockSignals(was_blocked)

        if 0 <= current_tab < self.tabs.count():
            self.tabs.setCurrentIndex(current_tab)

    def _on_language_changed(self, index: int) -> None:
        """Persist the language choice and apply it without a restart.

        The stored value is the raw preference (``system`` or a locale tag), not
        the resolved locale, so a user following the system keeps following it if
        they later change their desktop language.
        """

        language_key = self.language_selector.itemData(index)
        if not isinstance(language_key, str) or not language_key:
            return
        self.settings.setValue("language", language_key)
        self.language_changed.emit(language_key)
        # This dialog is already open, so nothing else will repaint it: the
        # window behind it redraws its own chrome, but the Preferences copy has
        # to be rebuilt here or the user watches the app change language around
        # an English dialog.
        self.retranslate()

    def _on_epoch_toggled(self, checked: bool):
        self.settings.setValue("show_epoch", checked)
        self.epoch_display_toggled.emit(checked)

    def _on_auto_import_keys_toggled(self, checked: bool):
        self.settings.setValue(AUTOMATIC_KEY_IMPORT_PREFERENCE, bool(checked))
