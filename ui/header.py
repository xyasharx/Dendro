# dendro/ui/header.py
"""
Top toolbar header container:
Features an integrated search bar with a shortcut hint chip, dedicated update review pill,
direct 1-click system upgrade action, orphan cleanup action, grouped secondary tool cluster,
Preferences access, inspector toggle, and primary CTA controls.
Uses Dendro-bundled SVG icons, falling back to FreeDesktop theme icons; no font emoji glyphs.
"""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QWidget,
)

from core.host_environment import HostEnvironment
from core.i18n import tr
from core.models import SIZE_CONSTRAINT_PATTERN
from core.taxonomy import sidebar_category_ids
from ui.icons import app_icon, theme_icon

# Valid search filter keys and canonical options for validation and autocompletion
VALID_FILTER_KEYS = {
    "status",
    "type",
    "interface",
    "arch",
    "cat",
    "category",
    "sub",
    "subcategory",
    "pillar",
    "parent",
    "tag",
    "size",
    "repo",
    "license",
    "evidence",
    "source",
    "scope",
    "metadata",
}

VALID_FILTER_VALUES = {
    "status": {
        "update",
        "upgradable",
        "upgrade",
        "orphan",
        "user",
        "userinstalled",
        "manual",
        "queued",
        "installed",
        "available",
        "remote",
        "layered",
        "override",
        "base",
    },
    "type": {
        "gui",
        "desktop",
        "app",
        "cli",
        "terminal",
        "tool",
        "service",
        "daemon",
        "systemd",
        "lib",
        "library",
        "c_lib",
    },
    "interface": {
        "gui",
        "desktop",
        "app",
        "cli",
        "terminal",
        "tool",
        "service",
        "daemon",
        "systemd",
        "lib",
        "library",
        "c_lib",
    },
    "pillar": {
        "apps",
        "cli",
        "hardware",
        "system",
        "libs",
        "maintenance",
        "pillar_apps",
        "pillar_cli",
        "pillar_hardware",
        "pillar_system",
        "pillar_libs",
        "pillar_maintenance",
    },
    "parent": {
        "apps",
        "cli",
        "hardware",
        "system",
        "libs",
        "maintenance",
        "pillar_apps",
        "pillar_cli",
        "pillar_hardware",
        "pillar_system",
        "pillar_libs",
        "pillar_maintenance",
    },
    "evidence": {
        "manual_override",
        "appstream",
        "desktop_entry",
        "rpm_manifest",
        "fedora_comps",
        "name_heuristic",
        "insufficient_metadata",
    },
    "source": {
        "manual_override",
        "appstream",
        "desktop_entry",
        "rpm_manifest",
        "fedora_comps",
        "name_heuristic",
        "insufficient_metadata",
    },
    "scope": {"installed_manifest", "local_rpm_manifest", "remote_metadata", "unknown"},
    "metadata": {
        "installed_manifest",
        "local_rpm_manifest",
        "remote_metadata",
        "unknown",
    },
}

FILTER_AUTOCOMPLETE_ITEMS = [
    "status:available",
    "status:installed",
    "status:update",
    "status:upgradable",
    "status:user",
    "status:manual",
    "status:layered",
    "status:override",
    "status:base",
    "status:orphan",
    "status:queued",
    "status:remote",
    "type:gui",
    "type:cli",
    "type:service",
    "type:lib",
    "interface:gui",
    "interface:cli",
    "interface:service",
    "interface:lib",
    # Common examples only; arch: accepts any exact RPM architecture.
    "arch:x86_64",
    "arch:noarch",
    "arch:aarch64",
    "pillar:apps",
    "pillar:cli",
    "pillar:hardware",
    "pillar:system",
    "pillar:libs",
    "pillar:maintenance",
    *(f"cat:{identifier}" for identifier in sidebar_category_ids()),
    "evidence:appstream",
    "evidence:rpm_manifest",
    "evidence:insufficient_metadata",
    "scope:remote_metadata",
    "scope:installed_manifest",
    "size:>100M",
    "size:<50K",
    "repo:copr",
    "repo:fusion",
    "license:gpl",
    "license:mit",
]

#: Search-filter reference, as rows rather than one block of markup. The rules
#: forbid translating markup, and one message id holding `<b>`/`<code>` could not
#: be translated accurately anyway: splitting it lets a locale choose its own
#: word order inside a row instead of being handed English order with tokens
#: dropped in. Each row is ``(message, shell, has_values)``; ``{}`` in the shell
#: is replaced by the translated text, and rows with values also substitute the
#: literal tokens from ``SEARCH_FILTER_VALUES``, which are what the user types
#: and therefore the same in every language.
SEARCH_FILTER_ROWS: tuple[tuple[str, str, bool], ...] = (
    ("Search filter reference", "<b>{}</b>", False),
    ("Use ordinary words to search package names and details.", "{}", False),
    ("Use filters as {grammar}.", "{}", True),
    (
        "Example: {example} uses {name} as the filter name and {value} as its value.",
        "{}",
        True,
    ),
    (
        "Separate filters with whitespace; every supplied filter must match.",
        "{}",
        False,
    ),
    ("Filter names and accepted values", "<b>{}</b>", False),
    (
        "status: accepts {update_aliases}; {user_aliases}; {atomic_aliases}; "
        "orphan, queued (Transaction Queue); installed; {remote_aliases}.",
        "<code>status:</code> {}",
        True,
    ),
    (
        "type: and interface: accept {gui_aliases}; {cli_aliases}; "
        "{service_aliases}; or {library_aliases}.",
        "<code>type:</code> / <code>interface:</code> {}",
        True,
    ),
    (
        "arch: accepts an exact RPM architecture, for example {x86_64}, {noarch}, "
        "or {aarch64}; other valid RPM architectures are accepted too.",
        "<code>arch:</code> {}",
        True,
    ),
    (
        "pillar: and parent: accept {pillars}.",
        "<code>pillar:</code> / <code>parent:</code> {}",
        True,
    ),
    (
        "cat: and category: accept a sidebar category ID such as {general} or "
        "{unclassified} and use the same matching rule as the sidebar; sub: and "
        "subcategory:, tag:, repo:, and license: accept matching text.",
        "<code>cat:</code> / <code>category:</code> {}",
        True,
    ),
    (
        "evidence: and source: accept {evidence} or {insufficient}; scope: and "
        "metadata: accept values such as {remote_metadata} or {installed_manifest}.",
        "<code>evidence:</code> / <code>source:</code> {}",
        True,
    ),
    (
        "size: accepts comparisons such as {gt}, {lt}, or {eq}.",
        "<code>size:</code> {}",
        True,
    ),
    ("Special file lookup", "<b>{}</b>", False),
    (
        "Use {file_token} by itself to select the package that owns that file.",
        "{}",
        True,
    ),
    ("Start typing a filter name or value to see supported completions.", "{}", False),
)

#: Literal tokens substituted into the reference above.
SEARCH_FILTER_VALUES: dict[str, str] = {
    "grammar": "<code>filter:value [filter:value ...]</code>",
    "example": "<code>status:update</code>",
    "name": "<code>status</code>",
    "value": "<code>update</code>",
    "update_aliases": "<code>update</code>/<code>upgradable</code>/<code>upgrade</code>",
    "user_aliases": "<code>user</code>/<code>userinstalled</code>/<code>manual</code>",
    "atomic_aliases": (
        "<code>layered</code>/<code>override</code>/<code>base</code> (Fedora Atomic)"
    ),
    "remote_aliases": "<code>available</code>/<code>remote</code>",
    "gui_aliases": "<code>gui</code>/<code>desktop</code>/<code>app</code>",
    "cli_aliases": "<code>cli</code>/<code>terminal</code>/<code>tool</code>",
    "service_aliases": "<code>service</code>/<code>daemon</code>/<code>systemd</code>",
    "library_aliases": "<code>lib</code>/<code>library</code>/<code>c_lib</code>",
    "x86_64": "<code>x86_64</code>",
    "noarch": "<code>noarch</code>",
    "aarch64": "<code>aarch64</code>",
    "pillars": (
        "<code>apps</code>, <code>cli</code>, <code>hardware</code>, "
        "<code>system</code>, <code>libs</code>, or <code>maintenance</code>"
    ),
    "general": "<code>cli_general</code>",
    "unclassified": "<code>unclassified</code>",
    "evidence": (
        "<code>appstream</code>, <code>desktop_entry</code>, "
        "<code>rpm_manifest</code>, <code>fedora_comps</code>, "
        "<code>name_heuristic</code>"
    ),
    "insufficient": "<code>insufficient_metadata</code>",
    "remote_metadata": "<code>remote_metadata</code>",
    "installed_manifest": "<code>installed_manifest</code>",
    "gt": "<code>size:&gt;100M</code>",
    "lt": "<code>size:&lt;50K</code>",
    "eq": "<code>size:=1G</code>",
    "file_token": "<code>file:/path/to/file</code>",
}


def search_filter_tooltip() -> str:
    """Build the search-filter reference in the active language.

    Rows are translated on read, never at import: a module-level constant would
    freeze the tooltip in whatever language was active at import, which is the
    exact failure a live language switch is supposed to avoid.

    A catalog that mistypes a placeholder falls back to the English row rather
    than raising inside a tooltip setter, where the exception would surface as a
    crash during a language change. ``tests/test_i18n.py`` asserts no catalog
    actually needs that fallback.
    """

    rows = []
    for message, shell, has_values in SEARCH_FILTER_ROWS:
        text = tr(message)
        if has_values:
            try:
                text = text.format(**SEARCH_FILTER_VALUES)
            except (KeyError, IndexError, ValueError):
                text = message.format(**SEARCH_FILTER_VALUES)
        rows.append(shell.format(text))
    return "<br>".join(rows)


def _active_token_bounds(text: str, cursor_position: int) -> tuple[int, int]:
    """Return the whitespace-delimited token surrounding the edit cursor."""
    cursor = max(0, min(cursor_position, len(text)))
    start = cursor
    end = cursor
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


class FilterCompleter(QCompleter):
    """Complete only the active filter token while preserving earlier filters."""

    def splitPath(self, path: str) -> list[str]:
        line_edit = self.widget()
        if isinstance(line_edit, QLineEdit):
            start, end = _active_token_bounds(path, line_edit.cursorPosition())
            return [path[start:end]]
        return [path.rsplit(maxsplit=1)[-1]] if path else [""]

    def pathFromIndex(self, index) -> str:
        completion = super().pathFromIndex(index)
        line_edit = self.widget()
        if not isinstance(line_edit, QLineEdit):
            return completion

        text = line_edit.text()
        start, end = _active_token_bounds(text, line_edit.cursorPosition())
        return f"{text[:start]}{completion}{text[end:]}"


class HeaderBar(QWidget):
    """
    Top toolbar control hub for Dendro:
    Houses search, repository management, update review, direct upgrade action,
    Preferences access, inspector toggling, and transaction execution controls.
    """

    search_changed = pyqtSignal(str)
    apply_clicked = pyqtSignal()
    toggle_inspector_clicked = pyqtSignal()
    history_clicked = pyqtSignal()
    advisories_clicked = pyqtSignal()
    flatpak_clicked = pyqtSignal()
    reload_clicked = pyqtSignal()
    repos_clicked = pyqtSignal()
    settings_clicked = pyqtSignal()
    updates_clicked = pyqtSignal()
    upgrade_system_clicked = pyqtSignal()
    clean_orphans_clicked = pyqtSignal()
    refresh_updates_clicked = pyqtSignal()
    toggle_sidebar_clicked = pyqtSignal()
    clear_filter_clicked = pyqtSignal()
    remote_search_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("HeaderContainer")
        self._host_environment: HostEnvironment = HostEnvironment.traditional()
        self._init_ui()
        #: Latest counts shown in the labels. Kept so a language change can
        #: recompute the label rather than leave the old language in place.
        self._queued_count = 0
        self._available_count = 0
        self._orphan_count = 0

        self._setup_debounce()

    def _init_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(14, 8, 14, 8)
        main_layout.setSpacing(8)

        # Sidebar Toggle Button (Left of Search Bar)
        self.sidebar_toggle_btn = QPushButton()
        self.sidebar_toggle_btn.setObjectName("HeaderIconBtn")
        self.sidebar_toggle_btn.setFixedWidth(30)
        sidebar_icon = app_icon(
            "header-sidebar", "view-left-pane", "sidebar-show", "format-indent-more"
        )
        if not sidebar_icon.isNull():
            self.sidebar_toggle_btn.setIcon(sidebar_icon)
        else:
            self.sidebar_toggle_btn.setText("||")
        self.sidebar_toggle_btn.setToolTip(tr("Toggle category sidebar (Ctrl+B)"))
        self.sidebar_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sidebar_toggle_btn.clicked.connect(self.toggle_sidebar_clicked.emit)
        main_layout.addWidget(self.sidebar_toggle_btn)

        # ---------------------------------------------------------------------
        # 1. Search Bar with Native Actions (Directly in main_layout)
        # ---------------------------------------------------------------------
        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchBar")
        self.search_input.setPlaceholderText(
            tr("Search packages, filters, or file paths (/usr/bin/git)... (Ctrl+F)")
        )
        self.search_input.setClearButtonEnabled(True)

        # Autocompletion operates on the token at the caret, not the whole query.
        # This keeps suggestions useful after a completed filter such as
        # "arch:noarch status:".
        self.filter_completer = FilterCompleter(
            FILTER_AUTOCOMPLETE_ITEMS, self.search_input
        )
        self.filter_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.filter_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.search_input.setCompleter(self.filter_completer)

        # Hook live syntax validation
        self.search_input.textChanged.connect(self._validate_search_syntax)

        search_icon = app_icon("header-search", "system-search", "edit-find")
        if not search_icon.isNull():
            self.search_input.addAction(
                search_icon, QLineEdit.ActionPosition.LeadingPosition
            )

        # Trailing action: Click or press Enter to search remote repositories on demand
        repo_search_icon = theme_icon("system-software-install", "system-search")
        self.remote_search_action = QAction(
            repo_search_icon, tr("Search remote repositories (Enter)"), self
        )
        self.remote_search_action.setToolTip(
            tr(
                "Search remote Fedora repositories to install new packages (Press Enter)"
            )
        )
        self.remote_search_action.triggered.connect(
            lambda: self.remote_search_requested.emit(self.search_input.text())
        )
        self.search_input.addAction(
            self.remote_search_action, QLineEdit.ActionPosition.TrailingPosition
        )
        self.search_input.returnPressed.connect(
            lambda: self.remote_search_requested.emit(self.search_input.text())
        )

        self.search_input.setToolTip(search_filter_tooltip())

        # Live validation replaces the input tooltip, so the static reference
        # remains available from a separate, always-visible help control.
        self.search_help_btn = QPushButton("?")
        self.search_help_btn.setObjectName("HeaderIconBtn")
        self.search_help_btn.setFixedWidth(30)
        self.search_help_btn.setToolTip(search_filter_tooltip())
        self.search_help_btn.setAccessibleName(tr("Search filter reference"))
        self.search_help_btn.setAccessibleDescription(
            tr(
                "Open the permanent reference for Dendro search filter names, values, and examples."
            )
        )
        self.search_help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_help_btn.clicked.connect(self._show_search_filter_help)

        # ---------------------------------------------------------------------
        # 2. Update Controls (Separate Review vs. Action)
        # ---------------------------------------------------------------------
        # Elegant Updates Indicator (Clicking toggles between Updates and All Packages)
        self.updates_btn = QPushButton(tr("Updates ({count})").format(count=0))
        self.updates_btn.setIcon(
            app_icon("header-updates", "software-update-available")
        )
        self.updates_btn.setObjectName("UpdatesIndicatorBtn")
        self.updates_btn.setToolTip(tr("View list of available package updates"))
        self.updates_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.updates_btn.setVisible(False)
        self.updates_btn.clicked.connect(self.updates_clicked.emit)

        # Contextual Action: "Upgrade System" (Only visible when viewing Available Updates)
        self.upgrade_context_btn = QPushButton(
            tr("Upgrade All ({count})").format(count=0)
        )
        upgrade_icon = app_icon(
            "header-upgrade", "system-software-update", "emblem-default"
        )
        if not upgrade_icon.isNull():
            self.upgrade_context_btn.setIcon(upgrade_icon)
        self.upgrade_context_btn.setObjectName("ApplyButton")
        self.upgrade_context_btn.setToolTip(
            tr("Perform a complete system upgrade (dnf upgrade)")
        )
        self.upgrade_context_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upgrade_context_btn.setVisible(False)
        self.upgrade_context_btn.clicked.connect(self.upgrade_system_clicked.emit)

        # Contextual Action: "Clean Orphans" (Only visible when viewing Orphan Packages)
        self.clean_orphans_btn = QPushButton(tr("Clean Leaf Orphans"))
        orphans_icon = app_icon("header-clean_orphans", "edit-clear", "user-trash")
        if not orphans_icon.isNull():
            self.clean_orphans_btn.setIcon(orphans_icon)
        self.clean_orphans_btn.setObjectName("UpdatesIndicatorBtn")
        self.clean_orphans_btn.setToolTip(
            tr("Remove all unneeded leaf dependencies (dnf autoremove)")
        )
        self.clean_orphans_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clean_orphans_btn.setVisible(False)
        self.clean_orphans_btn.clicked.connect(self.clean_orphans_clicked.emit)

        # Contextual Action: "Live Refresh" (Expires metadata via DNF5 daemon)
        self.refresh_live_btn = QPushButton(tr("Check Mirrors"))
        refresh_icon = app_icon("header-refresh", "view-refresh")
        if not refresh_icon.isNull():
            self.refresh_live_btn.setIcon(refresh_icon)
        self.refresh_live_btn.setObjectName("HeaderToolBtn")
        self.refresh_live_btn.setToolTip(
            tr(
                "Expire cached repository metadata and query mirrors for the latest updates"
            )
        )
        self.refresh_live_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_live_btn.setVisible(False)
        self.refresh_live_btn.clicked.connect(self.refresh_updates_clicked.emit)

        # ---------------------------------------------------------------------
        # 3. Secondary Tool Cluster (Unified Compact Action Bar)
        # ---------------------------------------------------------------------
        tool_cluster = QWidget()
        cluster_layout = QHBoxLayout(tool_cluster)
        cluster_layout.setContentsMargins(0, 0, 0, 0)
        cluster_layout.setSpacing(4)

        self.reload_btn = QPushButton(tr("Reload"))
        reload_icon = app_icon("header-reload", "view-refresh")
        if not reload_icon.isNull():
            self.reload_btn.setIcon(reload_icon)
        self.reload_btn.setObjectName("HeaderToolBtn")
        self.reload_btn.setToolTip(
            tr("Reload and re-index system RPM database (Ctrl+R)")
        )
        self.reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reload_btn.clicked.connect(self.reload_clicked.emit)

        self.history_btn = QPushButton(tr("History"))
        history_icon = app_icon(
            "header-history", "document-open-recent", "view-history"
        )
        if not history_icon.isNull():
            self.history_btn.setIcon(history_icon)
        self.history_btn.setObjectName("HeaderToolBtn")
        self.history_btn.setToolTip(tr("View past DNF package transactions (Ctrl+H)"))
        self.history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_btn.clicked.connect(self.history_clicked.emit)

        # Security advisories for installed packages with a pending fix.
        self.advisories_btn = QPushButton(tr("Security"))
        advisory_icon = app_icon("header-advisories", "security-high", "dialog-warning")
        if not advisory_icon.isNull():
            self.advisories_btn.setIcon(advisory_icon)
        self.advisories_btn.setObjectName("HeaderToolBtn")
        self.advisories_btn.setToolTip(
            tr("Show security advisories that affect installed packages")
        )
        self.advisories_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.advisories_btn.clicked.connect(self.advisories_clicked.emit)

        self.repos_btn = QPushButton(tr("Repos"))
        self.repos_btn.setIcon(
            app_icon("header-repos", "system-software-install", "software-properties")
        )
        self.repos_btn.setObjectName("HeaderToolBtn")
        self.repos_btn.setToolTip(
            tr("Manage software repositories, RPM Fusion & COPR channels")
        )
        self.repos_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.repos_btn.clicked.connect(self.repos_clicked.emit)

        self.flatpak_btn = QPushButton(tr("Flatpak"))
        self.flatpak_btn.setIcon(
            app_icon(
                "header-flatpak", "application-x-flatpak", "system-software-install"
            )
        )
        self.flatpak_btn.setObjectName("HeaderToolBtn")
        self.flatpak_btn.setToolTip(
            tr(
                "View Flatpak apps and runtimes (separate from RPM packages; update or uninstall user and system apps)"
            )
        )
        self.flatpak_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.flatpak_btn.clicked.connect(self.flatpak_clicked.emit)

        self.settings_btn = QPushButton(tr("Settings"))
        self.settings_btn.setIcon(
            app_icon("header-settings", "preferences-system", "emblem-system")
        )
        self.settings_btn.setObjectName("HeaderToolBtn")
        self.settings_btn.setToolTip(tr("Preferences and About Dendro (Ctrl+,)"))
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(self.settings_clicked.emit)

        self.inspector_btn = QPushButton(tr("Details"))
        inspector_icon = app_icon(
            "header-inspector", "document-properties", "dialog-information"
        )
        if not inspector_icon.isNull():
            self.inspector_btn.setIcon(inspector_icon)
        self.inspector_btn.setObjectName("HeaderToolBtn")
        self.inspector_btn.setToolTip(
            tr("Toggle package detail inspector panel (Ctrl+I)")
        )
        self.inspector_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inspector_btn.clicked.connect(self.toggle_inspector_clicked.emit)

        cluster_layout.addWidget(self.reload_btn)
        cluster_layout.addWidget(self.history_btn)
        cluster_layout.addWidget(self.advisories_btn)
        cluster_layout.addWidget(self.repos_btn)
        cluster_layout.addWidget(self.flatpak_btn)
        cluster_layout.addWidget(self.settings_btn)
        cluster_layout.addWidget(self.inspector_btn)

        # Hairline vertical separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(
            "background-color: transparent; border: none; border-left: 1px solid rgba(127,127,127,0.2);"
        )
        sep.setFixedHeight(24)

        # ---------------------------------------------------------------------
        # 4. Transaction Actions (Discard & Apply)
        # ---------------------------------------------------------------------
        # 1-Click Discard Queue Button (Only appears when changes are staged)
        self.discard_btn = QPushButton(tr("Discard"))
        discard_icon = app_icon("header-discard", "edit-undo", "dialog-cancel")
        if not discard_icon.isNull():
            self.discard_btn.setIcon(discard_icon)
        self.discard_btn.setObjectName("HeaderDangerBtn")
        self.discard_btn.setToolTip(tr("Clear all packages from the transaction queue"))
        self.discard_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discard_btn.setVisible(False)

        # Primary Apply Transaction Button
        self.apply_btn = QPushButton(tr("Apply Changes ({count})").format(count=0))
        apply_icon = app_icon("header-apply", "emblem-default", "dialog-ok-apply")
        if not apply_icon.isNull():
            self.apply_btn.setIcon(apply_icon)
        self.apply_btn.setObjectName("ApplyButton")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_btn.clicked.connect(self.apply_clicked.emit)

        # Compose Layout
        main_layout.addWidget(self.search_input, stretch=1)
        main_layout.addWidget(self.search_help_btn)
        main_layout.addWidget(self.updates_btn)
        main_layout.addWidget(self.upgrade_context_btn)
        main_layout.addWidget(self.refresh_live_btn)
        main_layout.addWidget(self.clean_orphans_btn)
        main_layout.addWidget(tool_cluster)
        main_layout.addWidget(sep)
        main_layout.addWidget(self.discard_btn)
        main_layout.addWidget(self.apply_btn)

    def _setup_debounce(self):
        """250ms debounce timer preventing rapid database queries while typing."""
        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(250)
        self.debounce_timer.timeout.connect(self._emit_search)
        self.search_input.textChanged.connect(self.debounce_timer.start)

    def _show_search_filter_help(self) -> None:
        """Display the static filter reference independently of validation feedback."""
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setWindowTitle(tr("Search filter reference"))
        dialog.setText(search_filter_tooltip())
        dialog.setStandardButtons(QMessageBox.StandardButton.Close)
        dialog.exec()

    def _validate_search_syntax(self, text: str):
        """Validate every whitespace-delimited filter in a multi-filter search."""
        clean = text.strip()
        tokens = [
            t for t in clean.split() if ":" in t and not t.lower().startswith("file:")
        ]

        if not tokens:
            self.search_input.setProperty("filterState", "neutral")
            self.search_input.setToolTip(search_filter_tooltip())
            self.search_input.style().unpolish(self.search_input)
            self.search_input.style().polish(self.search_input)
            return

        invalid_reasons = []
        incomplete_keys = []

        for token in tokens:
            key, val = token.split(":", 1)
            key_l = key.lower()
            val_l = val.lower()

            key_esc = html.escape(key, quote=True)
            val_esc = html.escape(val, quote=True)

            if key_l not in VALID_FILTER_KEYS:
                invalid_reasons.append(f"Unknown filter key '{key_esc}'")
            elif not val_l:
                # A recognised prefix is not an error while the completer is
                # offering values, but it must not receive a green 'valid' state.
                incomplete_keys.append(key_l)
            elif key_l == "size" and SIZE_CONSTRAINT_PATTERN.fullmatch(val_l) is None:
                invalid_reasons.append(
                    f"Invalid size comparison '{val_esc}' for '{key_esc}:'"
                )
            elif (
                key_l in VALID_FILTER_VALUES and val_l not in VALID_FILTER_VALUES[key_l]
            ):
                invalid_reasons.append(f"Invalid value '{val_esc}' for '{key_esc}:'")

        if invalid_reasons:
            self.search_input.setProperty("filterState", "invalid")
            self.search_input.setToolTip(
                "<b>Invalid filter syntax:</b><br>- " + "<br>- ".join(invalid_reasons)
            )
        elif incomplete_keys:
            self.search_input.setProperty("filterState", "neutral")
            prefixes = ", ".join(
                f"<code>{html.escape(key, quote=True)}:</code>"
                for key in incomplete_keys
            )
            guidance = []
            for incomplete_key in dict.fromkeys(incomplete_keys):
                esc_incomplete = html.escape(incomplete_key, quote=True)
                if incomplete_key == "arch":
                    guidance.append(
                        "<code>arch:</code> accepts an exact RPM architecture; "
                        "completion entries are examples, not a complete list."
                    )
                elif incomplete_key in VALID_FILTER_VALUES:
                    guidance.append(
                        f"Choose a supported value for <code>{esc_incomplete}:</code> "
                        "from the completion list."
                    )
                else:
                    guidance.append(
                        f"Enter a value for <code>{esc_incomplete}:</code>; "
                        "see the ? reference for its format."
                    )
            self.search_input.setToolTip(
                f"<b>Continue typing {prefixes}</b><br>" + "<br>".join(guidance)
            )
        else:
            self.search_input.setProperty("filterState", "valid")
            escaped_tokens = [html.escape(token, quote=True) for token in tokens]
            self.search_input.setToolTip(
                "<b>Valid filters:</b><br>- " + "<br>- ".join(escaped_tokens)
            )

        self.search_input.style().unpolish(self.search_input)
        self.search_input.style().polish(self.search_input)

    def _emit_search(self):
        self.search_changed.emit(self.search_input.text())

    def retranslate(self) -> None:
        """Refresh toolbar tooltips and placeholders in the active language.

        Text that depends on host capability (read-only state, remote search) is
        reapplied by the same method that owns it, so this method only re-runs
        the static assignments rather than duplicating that logic.
        """

        # Labels. Anything carrying a count is recomputed from the stored value
        # so the number and the language cannot disagree.
        self._apply_button_text()
        self._refresh_update_labels()
        self.clean_orphans_btn.setText(
            tr("Clean Orphans ({count})").format(count=self._orphan_count)
        )
        for button, message in (
            (self.upgrade_context_btn, None),
            (self.refresh_live_btn, "Check Mirrors"),
            (self.reload_btn, "Reload"),
            (self.history_btn, "History"),
            (self.advisories_btn, "Security"),
            (self.repos_btn, "Repos"),
            (self.flatpak_btn, "Flatpak"),
            (self.settings_btn, "Settings"),
            (self.inspector_btn, "Details"),
            (self.discard_btn, "Discard"),
        ):
            if message is not None:
                button.setText(tr(message))

        # Tooltips and the search field's own text.
        self.sidebar_toggle_btn.setToolTip(tr("Toggle category sidebar (Ctrl+B)"))
        self.updates_btn.setToolTip(tr("View list of available package updates"))
        self.upgrade_context_btn.setToolTip(
            tr("Perform a complete system upgrade (dnf upgrade)")
        )
        self.clean_orphans_btn.setToolTip(
            tr("Remove all unneeded leaf dependencies (dnf autoremove)")
        )
        self.refresh_live_btn.setToolTip(
            tr(
                "Expire cached repository metadata and query mirrors for the latest updates"
            )
        )
        self.reload_btn.setToolTip(
            tr("Reload and re-index system RPM database (Ctrl+R)")
        )
        self.history_btn.setToolTip(tr("View past DNF package transactions (Ctrl+H)"))
        self.advisories_btn.setToolTip(tr("Browse security advisories (Ctrl+Shift+S)"))
        self.repos_btn.setToolTip(tr("Manage software repositories and mirrors"))
        self.flatpak_btn.setToolTip(tr("Manage Flatpak applications"))
        self.settings_btn.setToolTip(tr("Preferences and About Dendro (Ctrl+,)"))
        self.inspector_btn.setToolTip(
            tr("Toggle package detail inspector panel (Ctrl+I)")
        )
        self.discard_btn.setToolTip(tr("Clear all packages from the transaction queue"))
        self.search_input.setToolTip(search_filter_tooltip())
        self.search_help_btn.setToolTip(search_filter_tooltip())
        self.search_help_btn.setAccessibleName(tr("Search filter reference"))
        # The search field's trailing action is a QAction, not a widget, so the
        # widget sweep cannot reach it and it has to be named here.
        self.remote_search_action.setText(tr("Search remote repositories (Enter)"))
        self.remote_search_action.setToolTip(
            tr(
                "Search remote Fedora repositories to install new packages (Press Enter)"
            )
        )

        if self._host_environment is not None:
            # Re-applies capability-dependent tooltips and placeholders, which
            # include translated text that must follow the active language.
            self.configure_host_environment(self._host_environment)

    def configure_host_environment(self, environment: HostEnvironment) -> None:
        """Adapt header actions and tooltips to the classified host runtime."""
        self._host_environment = environment

        supports_mutations = environment.supports_dnf5_daemon_mutations
        supports_remote_search = environment.supports_dnf_cli_queries

        self.remote_search_action.setVisible(supports_remote_search)
        self.remote_search_action.setEnabled(supports_remote_search)
        if supports_remote_search:
            self.search_input.setPlaceholderText(
                tr("Search packages, filters, or file paths (/usr/bin/git)... (Ctrl+F)")
            )
        else:
            self.search_input.setPlaceholderText(
                tr(
                    "Search installed RPMs, filters, or file paths (/usr/bin/git)... (Ctrl+F)"
                )
            )

        if not supports_mutations:
            _title, mutation_reason = environment.dnf5_daemon_unavailable(
                "apply package changes"
            )
            self.apply_btn.setText(tr("Read-Only"))
            self.apply_btn.setEnabled(False)
            self.apply_btn.setToolTip(mutation_reason)
            self.discard_btn.setVisible(False)
            self.updates_btn.setVisible(False)
            self.upgrade_context_btn.setVisible(False)
            self.refresh_live_btn.setVisible(False)
            self.clean_orphans_btn.setVisible(False)
        else:
            self.apply_btn.setToolTip(
                tr(
                    "Resolve, review, and apply queued package actions through the DNF5 daemon"
                )
            )

        if not environment.supports_dnf_cli_queries and not environment.is_flatpak:
            self.history_btn.setToolTip(environment.feature_unavailable("DNF history"))
        if not environment.supports_dnf5_daemon_queries:
            _title, repo_reason = environment.dnf5_daemon_unavailable(
                "manage DNF5 repositories"
            )
            self.repos_btn.setToolTip(repo_reason)
            _title, advisory_reason = environment.dnf5_daemon_unavailable(
                "read security advisories"
            )
            self.advisories_btn.setToolTip(advisory_reason)

    def update_queue_badge(self, count: int):
        self._queued_count = count
        self._apply_button_text()
        if not self._host_environment.supports_dnf5_daemon_mutations:
            self.apply_btn.setEnabled(False)
            self.discard_btn.setVisible(False)
            return
        self.apply_btn.setEnabled(count > 0)
        self.discard_btn.setVisible(count > 0)

    def _apply_button_text(self) -> None:
        """Write the apply button's label for the current counts and locale.

        The label carries a count, so it cannot be a plain ``tr()`` assignment
        made once in ``_init_ui``: the number and the language both change, and
        whichever moved last has to win. Keeping the count on the widget is what
        lets ``retranslate`` recompute it instead of restoring a stale string.
        """

        if not self._host_environment.supports_dnf5_daemon_mutations:
            self.apply_btn.setText(tr("Read-Only"))
            return
        self.apply_btn.setText(
            tr("Apply Changes ({count})").format(count=self._queued_count)
        )

    def update_available_updates_badge(self, count: int):
        """Shows or updates the subtle header updates indicator pill."""
        if not self._host_environment.supports_dnf5_daemon_queries:
            self.updates_btn.setVisible(False)
            self.upgrade_context_btn.setVisible(False)
            return
        self._available_count = count
        self._refresh_update_labels()
        visible = count > 0
        self.updates_btn.setVisible(visible)
        self.upgrade_context_btn.setVisible(visible)

    def _refresh_update_labels(self) -> None:
        """Write the update pill labels for the current count and locale."""

        count = self._available_count
        self.updates_btn.setText(tr("Updates ({count})").format(count=count))
        self.upgrade_context_btn.setText(
            tr("Upgrade All ({count})").format(count=count)
        )

    def set_updates_view_active(self, active: bool, count: int = 0):
        """Displays 'Upgrade All Packages' and 'Check Mirrors' when viewing updates."""
        if not self._host_environment.supports_dnf5_daemon_queries:
            self.upgrade_context_btn.setVisible(False)
            self.refresh_live_btn.setVisible(False)
            return
        self.upgrade_context_btn.setVisible(active and count > 0)
        self.refresh_live_btn.setVisible(active)
        if active:
            self.updates_btn.setToolTip(
                tr("Currently viewing updates. Click to return to all packages.")
            )
        else:
            self.updates_btn.setToolTip(tr("View list of available package updates"))

    def set_orphan_clean_visible(self, visible: bool, count: int = 0):
        """Displays the 'Clean Leaf Orphans' shortcut when viewing the orphans category."""
        if not self._host_environment.supports_dnf5_daemon_mutations:
            self.clean_orphans_btn.setVisible(False)
            return
        self._orphan_count = count
        self.clean_orphans_btn.setText(
            tr("Clean Orphans ({count})").format(count=self._orphan_count)
        )
        self.clean_orphans_btn.setVisible(visible and count > 0)
