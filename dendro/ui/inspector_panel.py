# dendro/ui/inspector_panel.py
"""
Side inspector panel displaying comprehensive package details:
Features dynamic theme-aware styling, two-tier hierarchical taxonomy display,
native system removal risk analysis, metadata grids, user-installed provenance,
file manifests with rpm -V verification, clickable CVE changelogs,
and reverse dependency inspection.
Exclusively utilizes native FreeDesktop vector icons with zero font emoji glyphs.
"""

from __future__ import annotations

from typing import Any, Final

from PyQt6.QtCore import QCoreApplication, QEvent, QSize, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from dendro.core.backend import (
    DependencyNode,
    FileVerificationResult,
    PackageChangelogEntry,
    PackageFileInfo,
    PackageInfo,
    PackageState,
    ProductionTaxonomyEngine,
    RemovalRiskLevel,
    RemovalSafetyAssessment,
)
from dendro.core.host_environment import HostEnvironment
from dendro.core.i18n import format_number, tr, tr_plural
from dendro.core.rpmostree_status import RpmOstreeOriginKind
from dendro.core.taxonomy import (
    category_label,
    classification_evidence_label,
    pillar_label,
    taxonomy_input_scope_label,
)
from dendro.ui.external_links import open_external_web_url
from dendro.ui.fonts import (
    DETAIL_PT_DELTA,
    OVERLINE_PT_DELTA,
    apply_labelled_fonts,
    scaled_fixed_font,
    scaled_font,
    style_text,
)
from dendro.ui.icons import theme_icon
from dendro.ui.inspector_manifest import (
    COLUMN_HEADERS,
    ManifestHeaderView,
    PackageManifestMixin,
)
from dendro.ui.inspector_renderers import (
    CHANGELOG_BUGZILLA_PATTERN,
    CHANGELOG_CVE_PATTERN,
    changelog_error_html,
    changelog_html,
    placeholder_document_html,
    scriptlets_error_html,
    scriptlets_html,
)
from dendro.ui.styles import get_delegate_palette

#: Message ids for the metadata card. Declared once so the extraction tool can
#: see them; the card itself looks them up rather than repeating the literals
#: in three separate grids.
_STATS_CARD_MESSAGES: Final[tuple[str, ...]] = (
    # Group headings
    "Package",
    "Installation",
    "Update status",
    # Field names
    "Size",
    "Architecture",
    "License",
    "Repository",
    "Type",
    "Installed",
    "Status",
)


def _box_layout_min_height_for_width(layout: QVBoxLayout, width: int) -> int:
    """Wrapped minimum height of a tab page without inflating stretch items."""
    margins = layout.contentsMargins()
    inner_w = max(1, width - margins.left() - margins.right())
    spacing = max(0, layout.spacing())
    total = margins.top() + margins.bottom()
    visible = 0
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item is None or item.isEmpty():
            continue
        if visible:
            total += spacing
        visible += 1
        if layout.stretch(index) > 0:
            total += item.minimumSize().height()
        elif item.hasHeightForWidth():
            total += item.heightForWidth(inner_w)
        else:
            total += item.sizeHint().height()
    return total


class _InspectorTabWidget(QTabWidget):
    """Tab widget whose size hint stays at its minimum usable floor.

    Scrollable views inside the tabs report ~192 px default ``sizeHint()``
    heights that cause an enclosing ``QScrollArea`` (via ``heightForWidth``)
    to inflate the panel scroll threshold past 700 px and jitter whenever
    internal scrollbars toggle. Reporting the wrapped minimum height lets the
    outer scrollbar stay hidden whenever the dock has room while still keeping
    wrapped status banners and at least one manifest row intact when compact.
    """

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(hint.width(), self.minimumSizeHint().height())

    def hasHeightForWidth(self) -> bool:
        page = self.currentWidget()
        layout = page.layout() if page is not None else None
        return bool(isinstance(layout, QVBoxLayout) and layout.hasHeightForWidth())

    def heightForWidth(self, width: int) -> int:
        min_hint = self.minimumSizeHint().height()
        page = self.currentWidget()
        layout = page.layout() if page is not None else None
        if not isinstance(layout, QVBoxLayout) or not layout.hasHeightForWidth():
            return min_hint
        max_page_min = max(
            (self.widget(i).minimumSizeHint().height() for i in range(self.count())),
            default=0,
        )
        tab_chrome = max(0, min_hint - max_page_min)
        page_w = max(1, width - 4)
        page_h = _box_layout_min_height_for_width(layout, page_w)
        return max(min_hint, tab_chrome + page_h)


class _InspectorOuterLayout(QVBoxLayout):
    """Outer layout delegating wrapped host height only when externally scrolled."""

    def __init__(self, panel: PackageInspectorPanel) -> None:
        super().__init__(panel)
        self._panel = panel

    def hasHeightForWidth(self) -> bool:
        if self._panel._scroll_host is not None:
            return True
        return super().hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:
        if self._panel._scroll_host is not None:
            return self._panel._host_height_for_width(width)
        return super().heightForWidth(width)


class _PanelScrollArea(QScrollArea):
    """Inner scroll area that yields wheel/scroll keys when hosted externally."""

    def __init__(self, panel: PackageInspectorPanel) -> None:
        super().__init__()
        self._panel = panel

    def wheelEvent(self, event) -> None:
        if self._panel._scroll_host is not None:
            event.ignore()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        if self._panel._scroll_host is not None and event.key() in {
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
            Qt.Key.Key_PageUp,
            Qt.Key.Key_PageDown,
            Qt.Key.Key_Home,
            Qt.Key.Key_End,
        }:
            event.ignore()
            return
        super().keyPressEvent(event)


class PackageInspectorPanel(PackageManifestMixin, QWidget):
    """
    Side panel displaying package metadata, two-tier taxonomy paths,
    native system removal safety analysis, file verification, and changelogs.
    """

    package_action_requested = pyqtSignal(object)
    package_navigate_requested = pyqtSignal(str)
    reverse_deps_requested = pyqtSignal(str)
    file_inspection_requested = pyqtSignal(str)
    file_verification_requested = pyqtSignal(str)
    changelog_requested = pyqtSignal(str)
    scriptlets_requested = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("InspectorPanel")
        self.setMinimumWidth(360)
        self._current_package: PackageInfo | None = None
        self._all_files: list[PackageFileInfo] = []
        self._files_loaded: bool = False
        self._current_theme: str = "auto"
        self._current_changelog_entries: list[PackageChangelogEntry] = []
        self._current_scriptlets: Any = None
        self._last_verification: tuple[list[FileVerificationResult], bool] | None = None
        self._current_reverse_deps: list[DependencyNode] | None = None
        self._host_environment: HostEnvironment = HostEnvironment.traditional()
        self._scroll_host: QScrollArea | None = None
        self._syncing_host_geometry: bool = False

        self._init_ui()
        # One sweep gives every labelled child the size its object name calls
        # for, so the sizes follow the user's font instead of the stylesheet.
        apply_labelled_fonts(self)

    def _apply_host_environment_verify_button(self) -> None:
        if self._host_environment.normalizes_rpm_mtimes:
            self.btn_verify.setText(tr("Verify Integrity (rpm -V --nomtime)"))
            self.btn_verify.setToolTip(
                tr(
                    "Run 'rpm -V --nomtime' to verify digests, permissions, ownership, "
                    "sizes, links, and configuration changes while ignoring OSTree's "
                    "normalized epoch-0 '/usr' timestamps."
                )
            )
        else:
            self.btn_verify.setText(tr("Verify Integrity (rpm -V)"))
            self.btn_verify.setToolTip(
                tr(
                    "Verify installed file digests, permissions, sizes, and timestamps with 'rpm -V'."
                )
            )

    def set_host_environment(self, environment: HostEnvironment) -> None:
        """Synchronize inspector labels and mutation affordances with host mode."""
        self._host_environment = environment
        self._apply_host_environment_verify_button()
        if self._current_package is not None:
            self._populate_package_overview(self._current_package)

    # Mirrors the ``letter-spacing`` in the ``StatFieldName`` stylesheet rule.
    # The two have to agree: this value is measured against the font, and a
    # stylesheet change that is not reflected here clips the widest name.
    STAT_NAME_TRACKING_PX = 0.3
    _STAT_NAME_GUTTER_PADDING = 4

    def eventFilter(self, watched, event) -> bool:
        """Re-fit the manifest on resize and propagate hosted body layouts.

        The card changes height when the splitter, the window or the tab strip
        moves it, and each of those can leave the views holding a fraction of a
        row. Snapping here means the row is never drawn cut in half.
        """
        event_type = event.type()
        if watched is getattr(self, "_panel_body", None):
            if (
                event_type == QEvent.Type.LayoutRequest
                and self._scroll_host is not None
            ):
                self._sync_host_scroll_geometry()
            return super().eventFilter(watched, event)
        if event_type not in (QEvent.Type.Resize, QEvent.Type.Show):
            return super().eventFilter(watched, event)
        if watched is getattr(self, "_files_card", None):
            self._snap_manifest_rows()
            self._fit_files_columns()
        elif watched is getattr(self, "_files_viewport", None):
            # The column is sized to the viewport, and the viewport narrows by
            # the vertical bar's width the moment the manifest starts to
            # overflow. Without this the column keeps its old width and the
            # strip beside the header reappears.
            self._fit_files_columns()
        return super().eventFilter(watched, event)

    def changeEvent(self, event) -> None:
        """Re-measure the name gutter and tab floor when font or style changes.

        The gutter is a fixed width derived from font metrics, so a font change
        leaves it stale: the names then either clip or drift away from their
        values. Qt delivers this event to every widget when the font changes,
        which is cheaper and more reliable than polling metrics on each paint.
        """
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            if event.type() == QEvent.Type.FontChange:
                if hasattr(self, "_stat_name_labels"):
                    self._stat_name_width = self._measure_stat_name_width()
                self._sync_close_button_size()
                if hasattr(self, "scriptlets_browser"):
                    self.scriptlets_browser.setFont(scaled_fixed_font())
                # The manifest's two halves are separate views; the pinned one does
                # not inherit a font from the path table, so it follows here. Row
                # and header heights need no equivalent hook: both views are
                # configured identically, so Qt derives the same geometry for both.
                self._apply_manifest_font()
                self._snap_manifest_rows()
                for label in getattr(self, "_stat_names", {}).values():
                    label.setFixedWidth(self._stat_name_width)
            if hasattr(self, "tabs"):
                self._apply_tab_font()
            if hasattr(self, "tabs") and hasattr(self, "_panel_scroll"):
                self._sync_tab_bar_minimum_width()
            self._sync_host_scroll_geometry()

    def _measure_stat_name_width(self) -> int:
        """Width the fixed field-name gutter needs at the current font.

        The names are a small overline step below body text, so their metrics
        are measured with that font rather than the panel's; a stylesheet alone
        would not tell us, because the size is applied per label.

        ``letter-spacing`` from the ``StatFieldName`` rule has to be applied to
        the font before measuring. ``QFontMetrics`` does not see stylesheet
        styling, so measuring the bare font understates the advance by roughly
        one tracking step per character. That is enough for the longest name
        (``Architecture``) to come out flush: it measured 77 px against an 81 px
        gutter, but needs 81 px once the 0.3 px tracking is included.
        """
        font = scaled_font(OVERLINE_PT_DELTA, weight=QFont.Weight.DemiBold)
        font.setLetterSpacing(
            QFont.SpacingType.AbsoluteSpacing, self.STAT_NAME_TRACKING_PX
        )
        metrics = QFontMetrics(font)
        # Measure the text that is actually painted. ``_stat_name_labels`` holds
        # the English message ids, which are narrower than the translation in
        # most locales (Russian "Архитектура" and German "Aktualisierungsstatus"
        # both overflow an English-sized gutter), so ask the built labels first
        # and fall back to the ids only before the card exists.
        painted = [
            label.text()
            for label in getattr(self, "_stat_names", {}).values()
            if label.text()
        ]
        candidates = painted or self._stat_name_labels
        # A couple of pixels of slack: the widest name must not sit flush against
        # the value column, and metrics differ slightly between paint engines.
        return (
            max(metrics.horizontalAdvance(name) for name in candidates)
            + self._STAT_NAME_GUTTER_PADDING
        )

    def _init_ui(self):
        outer_layout = _InspectorOuterLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self._panel_scroll = _PanelScrollArea(self)
        self._panel_scroll.setObjectName("InspectorPanelScroll")
        self._panel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._panel_scroll.setWidgetResizable(True)
        self._panel_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._panel_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._panel_scroll.viewport().setAutoFillBackground(False)

        self._panel_body = QWidget()
        self._panel_body.installEventFilter(self)
        main_layout = QVBoxLayout(self._panel_body)
        main_layout.setContentsMargins(14, 12, 14, 12)
        main_layout.setSpacing(10)

        # ---------------------------------------------------------------------
        # 1. Header (Title, Summary, Close button)
        # ---------------------------------------------------------------------
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        self.pkg_name_label = QLabel(tr("Package Details"))
        self.pkg_name_label.setProperty("messageId", "Package Details")
        self.pkg_name_label.setObjectName("InspectorPkgTitle")
        self.pkg_name_label.setTextFormat(Qt.TextFormat.PlainText)
        self.pkg_name_label.setWordWrap(True)
        self.pkg_name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.close_btn = QPushButton()
        self.close_btn.setObjectName("InspectorCloseBtn")
        close_icon = theme_icon("window-close", "dialog-close")
        if not close_icon.isNull():
            self.close_btn.setIcon(close_icon)
        else:
            self.close_btn.setText("x")
        self.close_btn.setAccessibleName(tr("Close package details"))
        self.close_btn.setToolTip(tr("Close panel"))
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.closed.emit)
        self._sync_close_button_size()

        header_layout.addWidget(self.pkg_name_label, stretch=1)
        header_layout.addWidget(self.close_btn, alignment=Qt.AlignmentFlag.AlignTop)
        main_layout.addLayout(header_layout)

        self.summary_label = QLabel(tr("Select a package to inspect full metadata."))
        self.summary_label.setProperty(
            "messageId", "Select a package to inspect full metadata."
        )
        self.summary_label.setObjectName("InspectorSummary")
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        main_layout.addWidget(self.summary_label)

        # ---------------------------------------------------------------------
        # 2. Action Toolbar
        # ---------------------------------------------------------------------
        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)

        self.queue_btn = QPushButton(tr("Queue Action"))
        self.queue_btn.setObjectName("InspectorQueueBtn")
        self.queue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.queue_btn.clicked.connect(self._on_queue_btn_clicked)

        self.copy_btn = QPushButton(tr("Copy"))
        self.copy_btn.setObjectName("InspectorCopyBtn")
        self.copy_btn.setIcon(QIcon.fromTheme("edit-copy"))
        self.copy_btn.setToolTip(tr("Copy package name to clipboard"))
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.clicked.connect(self._copy_package_name)

        self.url_btn = QPushButton(tr("Homepage"))
        self.url_btn.setObjectName("InspectorHomepageBtn")
        self.url_btn.setIcon(theme_icon("applications-internet", "browser"))
        self.url_btn.setToolTip(tr("Open official project website"))
        self.url_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.url_btn.clicked.connect(self._open_project_url)

        action_layout.addWidget(self.queue_btn, stretch=2)
        action_layout.addWidget(self.copy_btn, stretch=1)
        action_layout.addWidget(self.url_btn, stretch=1)
        main_layout.addLayout(action_layout)

        # ---------------------------------------------------------------------
        # 3. Quick Stats
        #
        # Three labelled groups of field/value cells instead of one flat column
        # of "Label: value" strings. Grouping, a small uppercase field name over
        # a full-contrast value, and a rule between groups give the eye somewhere
        # to land; none of it adds information that was not already shown.
        #
        # ``lbl_size`` and friends keep carrying the whole "Field: value" string,
        # because callers and tests read them directly. ``_set_stat`` updates the
        # attribute and the visible value label together so the two cannot drift.
        # ---------------------------------------------------------------------
        stats_frame = QFrame()
        stats_frame.setObjectName("StatGridCard")
        stats_outer = QVBoxLayout(stats_frame)
        stats_outer.setContentsMargins(12, 7, 12, 8)
        stats_outer.setSpacing(0)

        self.lbl_size = QLabel(tr("Size: -"))
        self.lbl_arch = QLabel(tr("Arch: -"))
        self.lbl_license = QLabel(tr("License: -"))
        self.lbl_repo = QLabel(tr("Repo: -"))
        self.lbl_install_type = QLabel(tr("Type: -"))
        self.lbl_upgrade_status = QLabel(tr("Update: None"))
        self.lbl_installed_on = QLabel(tr("Installed: -"))

        for stat_label in (
            self.lbl_size,
            self.lbl_arch,
            self.lbl_license,
            self.lbl_repo,
            self.lbl_install_type,
            self.lbl_upgrade_status,
            self.lbl_installed_on,
        ):
            stat_label.setTextFormat(Qt.TextFormat.PlainText)
            # The attribute is the data holder; the card renders its own value
            # label so the field name and the value can differ typographically.
            stat_label.setVisible(False)

        # Visible value labels, keyed by the attribute that owns the same text.
        self._stat_values: dict[str, QLabel] = {}
        self._stat_names: dict[str, QLabel] = {}
        self._stat_chips: dict[str, QFrame] = {}
        # One width for every name column keeps all values on a single left edge.
        #
        # Measure with the font the names actually paint in, including the
        # stylesheet's tracking, and re-measure on font changes (see
        # ``changeEvent``). Measuring the bare font left "Architecture" flush
        # against its value because stylesheet letter-spacing is invisible to
        # QFontMetrics.
        self._stat_name_labels = (
            "Size",
            "Architecture",
            "License",
            "Repository",
            "Type",
            "Installed",
            "Status",
        )
        self._stat_name_width = self._measure_stat_name_width()

        def field_cell(
            attribute: str,
            field_name: str,
            grid: QGridLayout,
            row: int,
            *,
            pill: bool = False,
        ) -> None:
            """One table row: a fixed-width field name, then its value."""
            name = QLabel(tr(field_name))
            # Remember the message id: ``retranslate`` rewrites every field name
            # and has to re-measure the gutter from the translated text.
            name.setProperty("fieldName", field_name)
            name.setObjectName("StatFieldName")
            name.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            # The name column is a fixed gutter. Without a fixed width the label
            # itself would grow to fill a sparse group's column.
            name.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            name.setFixedWidth(self._stat_name_width)
            grid.addWidget(name, row, 0)
            self._stat_names[attribute] = name

            value = QLabel("-")
            value.setObjectName("StatFieldValue")
            value.setTextFormat(Qt.TextFormat.PlainText)
            # Nothing is known yet, so the placeholder is shown in the muted
            # weight rather than looking like a real, emphatic value.
            value.setProperty("muted", True)
            value.setWordWrap(True)
            value.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            self._stat_values[attribute] = value

            if pill:
                # A status reads as a chip that hugs its own text: the chip is a
                # framed box and the text sits inside it as a separate label.
                # The chip's padding is layout margins rather than stylesheet
                # padding, because a stylesheet's padding does not feed a
                # QLabel's size hint -- the label then measures one line, paints
                # its text inside padding it never reserved, and wraps the text
                # to two lines that the box clips.
                value.setWordWrap(False)
                value.setAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                chip = QFrame()
                chip.setObjectName("StatStatusChip")
                chip_row = QHBoxLayout(chip)
                chip_row.setContentsMargins(10, 3, 10, 3)
                chip_row.setSpacing(0)
                chip_row.addWidget(value)
                chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
                # The holder row keeps the chip at the column's left edge instead
                # of letting a Maximum-policy widget float to the centre.
                holder = QWidget()
                holder_row = QHBoxLayout(holder)
                holder_row.setContentsMargins(0, 0, 0, 0)
                holder_row.setSpacing(0)
                holder_row.addWidget(chip)
                holder_row.addStretch(1)
                grid.addWidget(holder, row, 1)
                self._stat_chips[attribute] = chip
                return
            grid.addWidget(value, row, 1)

        def field_group(
            title: str, fields: list[tuple[str, str]], *, pill_first: bool = False
        ) -> QVBoxLayout:
            group = QVBoxLayout()
            group.setContentsMargins(0, 0, 0, 0)
            group.setSpacing(2)

            heading = QLabel(tr(title))
            heading.setProperty("fieldName", title)
            heading.setObjectName("StatGroupTitle")
            group.addWidget(heading)

            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(3)
            # The names hold a fixed gutter; the values take the remaining width,
            # so every value in the card starts on the same vertical line.
            grid.setColumnStretch(0, 0)
            grid.setColumnStretch(1, 1)
            grid.setColumnMinimumWidth(0, self._stat_name_width)
            for row, (attribute, field_name) in enumerate(fields):
                field_cell(
                    attribute,
                    field_name,
                    grid,
                    row,
                    pill=pill_first and row == 0,
                )
            group.addLayout(grid)
            return group

        def add_group(group: QVBoxLayout) -> QWidget:
            container = QWidget()
            container.setLayout(group)
            stats_outer.addWidget(container)
            return container

        def add_separator() -> None:
            rule = QFrame()
            rule.setObjectName("StatSeparator")
            rule.setFixedHeight(1)
            stats_outer.addSpacing(8)
            stats_outer.addWidget(rule)
            stats_outer.addSpacing(8)

        add_group(
            field_group(
                "Package",
                [
                    ("lbl_size", "Size"),
                    ("lbl_arch", "Architecture"),
                    ("lbl_license", "License"),
                    ("lbl_repo", "Repository"),
                ],
            )
        )
        add_separator()
        add_group(
            field_group(
                "Installation",
                [
                    ("lbl_install_type", "Type"),
                    ("lbl_installed_on", "Installed"),
                ],
            )
        )
        add_separator()
        add_group(
            field_group(
                "Update status",
                [("lbl_upgrade_status", "Status")],
                pill_first=True,
            )
        )

        self._stats_card = stats_frame
        # The labels now hold translated text, which is wider than the English
        # ids the gutter was first measured from; re-measure before showing.
        self._stat_name_width = self._measure_stat_name_width()
        for name_label in self._stat_names.values():
            name_label.setFixedWidth(self._stat_name_width)
        main_layout.addWidget(stats_frame)

        # ---------------------------------------------------------------------
        # 4. Detail Tabs
        # ---------------------------------------------------------------------
        self.tabs = _InspectorTabWidget()
        self.tabs.setObjectName("InspectorTabs")
        self.tabs.setAccessibleName(tr("Package inspector sections"))
        # Tab titles are kept as message ids so a language change can retitle
        # them without rebuilding the tab pages.
        self._tab_titles: list[str] = []
        # The inspector is a narrow dock by design. Keep every tab label
        # readable at the minimum panel width instead of hiding tabs behind
        # scrolling arrows that appear at ~500px of label text.
        self.tabs.setUsesScrollButtons(False)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabBar().setExpanding(False)
        # The stylesheet paints the tabs in a semibold weight, but Qt measures
        # a tab label with the bar's own QFont. When the two weights differ,
        # Qt reserves regular-width space for text it then draws 45px wider
        # across the row, and every label elides to a stub. Give the bar the
        # same weight the stylesheet paints with so measurement matches paint.
        self._apply_tab_font()

        # Tab 1: Overview & Insights
        self.tab_overview = QWidget()
        self._init_overview_tab()
        self._tab_titles.append("Overview")
        self.tabs.addTab(self.tab_overview, tr("Overview"))

        # Tab 2: Files Manifest & Verification
        self.tab_files = QWidget()
        self._init_files_tab()
        self._tab_titles.append("Files")
        self.tabs.addTab(self.tab_files, tr("Files"))

        # Tab 3: Reverse Dependents
        self.tab_reverse = QWidget()
        self._init_reverse_tab()
        self._tab_titles.append("Required By")
        self.tabs.addTab(self.tab_reverse, tr("Required By"))

        # Tab 4: Native RPM Changelog & CVEs
        self.tab_changelog = QWidget()
        self._init_changelog_tab()
        self._tab_titles.append("Changelog")
        self.tabs.addTab(self.tab_changelog, tr("Changelog"))

        # Tab 5: Maintainer Scriptlets & Triggers (%pre, %post)
        self.tab_scriptlets = QWidget()
        self._init_scriptlets_tab()
        self._tab_titles.append("Scripts")
        self.tabs.addTab(self.tab_scriptlets, tr("Scripts"))

        # The five labels must stay whole, but their pixel width is a property
        # of the desktop font: at 11pt on Fedora's default font the row is wider
        # than the old fixed 360px dock minimum, so Qt elided every label to a
        # stub ("Overv...", "Fi...", "Require...").
        # Ask the bar how wide it needs to be and make that the dock's floor.
        self._sync_tab_bar_minimum_width()
        main_layout.addWidget(self.tabs, stretch=1)
        self.tabs.currentChanged.connect(self._sync_host_scroll_geometry)

        self._panel_scroll.setWidget(self._panel_body)
        # QScrollArea::setWidget calls setAutoFillBackground(true) internally,
        # which would otherwise paint QPalette::Window behind transparent cards.
        self._panel_body.setAutoFillBackground(False)
        outer_layout.addWidget(self._panel_scroll)
        # Re-apply once the tabs sit inside their parent hierarchy so
        # stylesheet polishing during layout attachment cannot drop the bar's
        # DemiBold measurement weight before sizing runs.
        self._apply_tab_font()
        self._sync_tab_bar_minimum_width()

        # Start on the empty states: nothing is selected yet, so no content
        # surface has anything to show. The two text surfaces would otherwise
        # present a completely blank card on first launch.
        self._sync_empty_states()
        self._show_text_empty_states()

    def bind_scroll_host(self, scroll_host: QScrollArea | None) -> None:
        """Delegate outer scrolling to an enclosing dock QScrollArea when hosted.

        When the panel sits inside ``MainWindow._inspector_host``, delegating
        vertical overflow alongside horizontal overflow keeps the vertical
        scrollbar pinned to the visible dock edge instead of off-screen at the
        right boundary of a horizontally scrolled inner viewport. Standalone
        instantiations leave ``scroll_host`` unset and keep using
        ``_panel_scroll`` directly.
        """
        self._scroll_host = scroll_host
        self.setProperty("scrollHosted", scroll_host is not None)
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self._panel_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._panel_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            if scroll_host is not None
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._apply_tab_font()
        self._sync_tab_bar_minimum_width()
        self._sync_host_scroll_geometry()

    def preferred_dock_width(self) -> int:
        """Minimum width the enclosing dock needs so neither axis scrolls."""
        base_width = self.minimumWidth()
        host = self._scroll_host
        if host is None:
            return base_width
        vbar = host.verticalScrollBar()
        vbar_extent = vbar.sizeHint().width()
        if vbar_extent <= 0:
            vbar_extent = host.style().pixelMetric(
                QStyle.PixelMetric.PM_ScrollBarExtent,
                None,
                host,
            )
        return base_width + max(0, vbar_extent)

    def hasHeightForWidth(self) -> bool:
        if self._scroll_host is not None:
            return True
        return super().hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:
        if self._scroll_host is not None:
            return self._host_height_for_width(width)
        return super().heightForWidth(width)

    def _host_height_for_width(self, width: int) -> int:
        body = getattr(self, "_panel_body", None)
        body_layout = body.layout() if body is not None else None
        if body_layout is None:
            return self.minimumSizeHint().height()
        margins = self._panel_scroll.contentsMargins()
        inner_w = max(1, width - margins.left() - margins.right())
        if body_layout.hasHeightForWidth():
            body_h = body_layout.heightForWidth(inner_w)
        else:
            body_h = body_layout.minimumSize().height()
        return max(
            self._panel_scroll.minimumSizeHint().height(),
            body_h + margins.top() + margins.bottom(),
        )

    def _sync_host_scroll_geometry(self, *_args) -> None:
        host = self._scroll_host
        if host is None or self._syncing_host_geometry:
            return
        self._syncing_host_geometry = True
        try:
            outer = self.layout()
            if outer is not None:
                outer.invalidate()
            self.updateGeometry()
            QCoreApplication.postEvent(host, QEvent(QEvent.Type.LayoutRequest))
        finally:
            self._syncing_host_geometry = False

    def _reset_outer_scroll(self) -> None:
        self._panel_scroll.verticalScrollBar().setValue(0)
        if self._scroll_host is not None:
            self._scroll_host.verticalScrollBar().setValue(0)
        self._overview_scroll.verticalScrollBar().setValue(0)

    def _apply_tab_font(self) -> None:
        """Keep the tab bar's measurement weight synced with the panel font."""
        tab_font = QFont(self.font())
        if tab_font.pixelSize() > 0:
            tab_font.setPixelSize(max(tab_font.pixelSize(), 12))
        else:
            tab_font.setPointSizeF(max(tab_font.pointSizeF(), 9.0))
        tab_font.setWeight(QFont.Weight.DemiBold)
        self.tabs.tabBar().setFont(tab_font)

    def _sync_tab_bar_minimum_width(self) -> None:
        """Keep the dock at least as wide as its five tab labels need.

        The bar's own ``sizeHint()`` is the one number that already accounts for
        the stylesheet's font weight, tab padding and per-tab margin, which is
        exactly the sum that used to be computed by hand and kept coming up a
        few pixels short. It also tracks the desktop font, so a larger system
        font raises the dock floor instead of eliding every label. When
        standalone, reserve the inner scroll area's vertical scrollbar extent
        as well so compact windows never clip or elide tabs; when hosted, the
        enclosing scroll host owns the outer bar outside this panel's width.
        """
        bar = self.tabs.tabBar()
        margins = self._panel_body.layout().contentsMargins()
        vbar = self._panel_scroll.verticalScrollBar()
        vbar_extent = vbar.sizeHint().width()
        if vbar_extent <= 0:
            vbar_extent = self._panel_scroll.style().pixelMetric(
                QStyle.PixelMetric.PM_ScrollBarExtent,
                None,
                self._panel_scroll,
            )
        base_min_w = max(
            360,
            bar.sizeHint().width() + margins.left() + margins.right() + 8,
        )
        if self._scroll_host is None:
            self.setMinimumWidth(base_min_w + max(0, vbar_extent))
        else:
            self.setMinimumWidth(base_min_w)

    def _init_overview_tab(self):
        outer = QVBoxLayout(self.tab_overview)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._overview_scroll = QScrollArea()
        self._overview_scroll.setObjectName("InspectorOverviewScroll")
        self._overview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._overview_scroll.setWidgetResizable(True)
        self._overview_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._overview_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._overview_scroll.viewport().setAutoFillBackground(False)

        self._overview_content = QWidget()
        layout = QVBoxLayout(self._overview_content)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        # Native Taxonomy & System Safety Card
        self.safety_card = QFrame()
        self.safety_card.setObjectName("AICard")
        # The card contains three wrapped labels. Without an explicit
        # height-for-width policy Qt lays them out at their single-line minimum
        # and clips the provenance, risk and rationale text mid-line.
        card_policy = self.safety_card.sizePolicy()
        card_policy.setHeightForWidth(True)
        card_policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        self.safety_card.setSizePolicy(card_policy)
        card_layout = QVBoxLayout(self.safety_card)
        card_layout.setContentsMargins(10, 9, 10, 10)
        card_layout.setSpacing(6)

        # Section title, matching the Description card so the page reads as a
        # series of sections rather than a stack of unrelated boxes.
        self.classification_title_label = QLabel(tr("Classification"))
        self.classification_title_label.setProperty("messageId", "Classification")
        self.classification_title_label.setObjectName("InspectorSectionTitle")
        card_layout.addWidget(self.classification_title_label)

        # Hierarchy Path (Pillar -> Subcategory)
        header_row = QHBoxLayout()
        self.taxonomy_badge = QLabel(tr("Category: Unknown"))
        self.taxonomy_badge.setProperty("messageId", "Category: Unknown")
        self.taxonomy_badge.setObjectName("AICategoryBadge")
        self.taxonomy_badge.setTextFormat(Qt.TextFormat.PlainText)
        self.taxonomy_badge.setWordWrap(True)

        header_row.addWidget(self.taxonomy_badge, stretch=1)
        card_layout.addLayout(header_row)

        self.provenance_label = QLabel(tr("Evidence: Unknown"))
        self.provenance_label.setProperty("messageId", "Evidence: Unknown")
        self.provenance_label.setObjectName("AIProvenanceLabel")
        self.provenance_label.setTextFormat(Qt.TextFormat.PlainText)
        self.provenance_label.setWordWrap(True)
        card_layout.addWidget(self.provenance_label)

        # Removal Safety Status Indicator
        self.safety_status_badge = QLabel(tr("Removal Risk: Assessment Pending"))
        self.safety_status_badge.setProperty(
            "messageId", "Removal Risk: Assessment Pending"
        )
        self.safety_status_badge.setTextFormat(Qt.TextFormat.PlainText)
        self.safety_status_badge.setWordWrap(True)
        style_text(
            self.safety_status_badge,
            delta=DETAIL_PT_DELTA,
            bold=True,
            padding="2px 0px",
        )
        card_layout.addWidget(self.safety_status_badge)

        # A thin divider separates the assessment from its justification.
        divider = QFrame()
        divider.setObjectName("InspectorCardDivider")
        divider.setFixedHeight(1)
        card_layout.addWidget(divider)

        self.rationale_title_label = QLabel(tr("Why this classification"))
        self.rationale_title_label.setProperty("messageId", "Why this classification")
        self.rationale_title_label.setObjectName("InspectorBasisLabel")
        card_layout.addWidget(self.rationale_title_label)

        self.rationale_label = QLabel(tr("Classification rationale will appear here."))
        self.rationale_label.setProperty(
            "messageId", "Classification rationale will appear here."
        )
        self.rationale_label.setObjectName("AIRationaleLabel")
        self.rationale_label.setTextFormat(Qt.TextFormat.PlainText)
        self.rationale_label.setWordWrap(True)
        card_layout.addWidget(self.rationale_label)

        layout.addWidget(self.safety_card)

        # ---------------------------------------------------------------------
        # Description card
        # ---------------------------------------------------------------------
        # The description used to be the only widget with stretch, so it grew
        # into a large empty box on every package whose text was shorter than
        # the page. It now sizes to its own content inside a card and the page
        # keeps the slack at the end, where empty space reads as breathing room
        # rather than as a hole in the middle of the panel.
        self.description_card = QFrame()
        self.description_card.setObjectName("InspectorSectionCard")
        description_layout = QVBoxLayout(self.description_card)
        description_layout.setContentsMargins(10, 9, 10, 10)
        description_layout.setSpacing(6)

        self.description_title_label = QLabel(tr("Description"))
        self.description_title_label.setProperty("messageId", "Description")
        self.description_title_label.setObjectName("InspectorSectionTitle")
        description_layout.addWidget(self.description_title_label)

        self.desc_text = QTextEdit()
        self.desc_text.setObjectName("InspectorDescText")
        self.desc_text.setReadOnly(True)
        self.desc_text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.desc_text.setFrameShape(QFrame.Shape.NoFrame)
        self.desc_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.desc_text.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.desc_text.document().setDocumentMargin(0)
        # Follow the text height up to a comfortable ceiling; a long
        # description then scrolls inside its card instead of pushing the
        # packager footer off the page.
        self.desc_text.document().documentLayout().documentSizeChanged.connect(
            self._sync_description_height
        )
        description_layout.addWidget(self.desc_text)
        # Let the card absorb the panel's spare height instead of leaving an
        # unbounded gap below the content: the text stays top-aligned and the
        # card surface carries the rest, which reads as a section rather than
        # as empty space.
        description_layout.addStretch(1)
        # Nothing is selected yet, so there is no description to show.
        self.description_card.setVisible(False)
        layout.addWidget(self.description_card)

        # ---------------------------------------------------------------------
        # Footer: packager and build provenance
        # ---------------------------------------------------------------------
        self.packager_label = QLabel(tr("Packager: -"))
        self.packager_label.setProperty("messageId", "Packager: -")
        self.packager_label.setObjectName("InspectorPackagerLabel")
        self.packager_label.setTextFormat(Qt.TextFormat.PlainText)
        self.packager_label.setWordWrap(True)
        layout.addWidget(self.packager_label)
        layout.addStretch(1)

        self._overview_scroll.setWidget(self._overview_content)
        # QScrollArea::setWidget calls setAutoFillBackground(true) internally;
        # keep the content surface transparent so the tab pane stays uniform.
        self._overview_content.setAutoFillBackground(False)
        outer.addWidget(self._overview_scroll)

    def _sync_description_height(self, *args) -> None:
        """Grow the description box to its text, up to a sensible ceiling.

        A ``QTextEdit`` has no height-for-width of its own, so it is given an
        explicit height from the measured document instead. Without this it
        either collapses or claims the whole page.
        """
        document = self.desc_text.document()
        # ``document.size()`` is the laid-out text; the extra pixels are the
        # caret and the last line's descenders, so the final line is not clipped.
        text_height = int(document.size().height()) + 6
        ceiling = self._description_max_height()
        self.desc_text.setFixedHeight(max(30, min(text_height, ceiling)))

    def _description_max_height(self) -> int:
        """Roughly a third of the dock, so long text scrolls rather than dominates."""
        height = self.height() if self.height() > 0 else 720
        return max(120, int(height * 0.34))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "desc_text"):
            self._sync_description_height()

    def retranslate(self) -> None:
        """Repaint the panel in the newly active language.

        The panel is built once and then fed by selection events, so a language
        change has to walk the static chrome explicitly. Fields that are
        rewritten by ``_set_stat``/``clear`` pick the new language up on their
        next render, which is why only the labels created in ``__init__`` and the
        tab titles are revisited here. Field-name gutters are re-measured because
        a translation such as "Architektur" is wider than "Architecture" is tall
        in the original layout, and the fixed gutter would otherwise clip it.
        """

        for label in (
            self.classification_title_label,
            self.rationale_title_label,
            self.description_title_label,
            self.files_empty_label,
            self.reverse_empty_label,
        ):
            message = label.property("messageId")
            if message:
                label.setText(tr(message))

        if self._current_package is None:
            for label in (
                self.pkg_name_label,
                self.summary_label,
                self.taxonomy_badge,
                self.provenance_label,
                self.safety_status_badge,
                self.rationale_label,
                self.packager_label,
            ):
                message = label.property("messageId")
                if message:
                    label.setText(tr(message))

        for label in (*self._stat_names.values(),):
            message = label.property("fieldName")
            if message:
                label.setText(tr(message))

        # Group headings are the ``StatGroupTitle`` labels; they carry the same
        # property, so collect them from the stats card rather than tracking
        # them individually during construction.
        for heading in self._stats_card.findChildren(QLabel, "StatGroupTitle"):
            message = heading.property("fieldName")
            if message:
                heading.setText(tr(message))

        self.close_btn.setAccessibleName(tr("Close package details"))
        self.close_btn.setToolTip(tr("Close panel"))
        self.copy_btn.setText(tr("Copy"))
        self.copy_btn.setToolTip(tr("Copy package name to clipboard"))
        self.url_btn.setText(tr("Homepage"))
        self.url_btn.setToolTip(tr("Open official project website"))
        self.tabs.setAccessibleName(tr("Package inspector sections"))
        self.file_search_input.setAccessibleName(tr("Filter installed files"))
        self.file_search_input.setPlaceholderText(
            tr("Filter installed files (/bin, /etc, ...)")
        )
        self.files_table.setAccessibleName(tr("Installed package files"))
        self.reverse_list.setAccessibleName(tr("Packages requiring this package"))
        self.changelog_browser.setAccessibleName(tr("Package changelog"))
        self.scriptlets_browser.setAccessibleName(tr("RPM maintainer scripts"))
        self.btn_refresh_rev.setText(tr("Re-Scan"))
        self._apply_host_environment_verify_button()
        self.files_model.set_headers([tr(header) for header in COLUMN_HEADERS])

        self._stat_name_width = self._measure_stat_name_width()
        for label in self._stat_names.values():
            label.setFixedWidth(self._stat_name_width)

        for index, title in enumerate(self._tab_titles):
            self.tabs.setTabText(index, tr(title))

        if self._current_package is not None:
            self._populate_package_overview(self._current_package)
            installed_for_inspection = self._current_package.state in {
                PackageState.INSTALLED,
                PackageState.QUEUED_REMOVE,
                PackageState.QUEUED_UPGRADE,
            }
            if not installed_for_inspection:
                self._apply_unavailable_inspection_state()
            else:
                if self._all_files:
                    self.files_status_label.setText(
                        tr("Loaded {count} installed files.").format(
                            count=format_number(len(self._all_files))
                        )
                    )
                    self._fit_files_columns()
                elif self._files_loaded:
                    self.files_status_label.setText(
                        tr("No installed files are recorded in this RPM manifest.")
                    )
                if self._last_verification is not None:
                    results, ignored_mtime = self._last_verification
                    self._render_verification_status(
                        results, ignored_mtime=ignored_mtime
                    )
                else:
                    self.lbl_verify_status.setText(tr("Audit: Not run"))
                if self._current_reverse_deps is not None:
                    self._render_reverse_dependencies_status(self._current_reverse_deps)
                    self.reverse_empty_label.setText(
                        tr("No other packages depend on this package.")
                        + "\n"
                        + tr("Safe to remove.")
                    )
                else:
                    self.rev_status_label.setText(
                        tr("Click 'Re-Scan' to query dependents.")
                    )
                if self._current_changelog_entries:
                    self._render_changelog()
                if self._current_scriptlets is not None:
                    self._render_scriptlets(self._current_scriptlets)
        else:
            self.clear()

    def clear(self):
        """Resets the inspector panel to its default empty state."""
        self._current_package = None
        self._all_files = []
        self._files_loaded = False
        self._current_changelog_entries = []
        self._current_scriptlets = None
        self._last_verification = None
        self._current_reverse_deps = None

        self.pkg_name_label.setText(tr("Package Details"))
        self.summary_label.setText(tr("Select a package to inspect full metadata."))

        self.taxonomy_badge.setText(tr("Category: None"))
        self.provenance_label.setText(tr("Evidence: Unknown"))
        self.safety_status_badge.setText(tr("Removal Risk: Assessment Pending"))
        style_text(
            self.safety_status_badge,
            delta=DETAIL_PT_DELTA,
            bold=True,
            padding="2px 0px",
        )
        self.rationale_label.setText(tr("Classification rationale will appear here."))
        self.desc_text.clear()
        self.description_card.setVisible(False)

        self._set_stat("lbl_size", "Size", "")
        self._set_stat("lbl_arch", "Arch", "")
        self._set_stat("lbl_license", "License", "")
        self._set_stat("lbl_repo", "Repo", "")
        self._set_stat("lbl_install_type", "Type", "")
        self._set_stat("lbl_upgrade_status", "Update", "None")
        self._set_stat("lbl_installed_on", "Installed", "")
        self.packager_label.setText(tr("Packager: -"))

        self.queue_btn.setText(tr("Queue Action"))
        self.queue_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.url_btn.setEnabled(False)

        self.files_model.clear()
        self._sync_empty_states()
        self.files_status_label.setText(
            tr("Select an installed package to inspect its files.")
        )
        self.files_status_label.setStyleSheet("")
        self.file_search_input.clear()
        self.file_search_input.setEnabled(False)
        self.btn_verify.setEnabled(False)
        self.reverse_list.clear()
        self.reverse_empty_label.setText(
            tr("Select an installed package to inspect dependents.")
        )
        self._sync_empty_states()
        self.rev_status_label.setText(
            tr("Select an installed package to inspect dependents.")
        )
        self.btn_refresh_rev.setEnabled(False)
        self._show_text_empty_states()
        self.lbl_verify_status.setText(tr("Audit: Not run"))
        self.lbl_verify_status.setStyleSheet("")
        self._reset_outer_scroll()

    def _sync_close_button_size(self) -> None:
        btn = getattr(self, "close_btn", None)
        if btn is None:
            return
        side = max(26, self.fontMetrics().height() + 8)
        btn.setFixedSize(side, side)

    def _init_reverse_tab(self):
        layout = QVBoxLayout(self.tab_reverse)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        btn_layout = QHBoxLayout()
        self.rev_status_label = QLabel(tr("Packages requiring this package:"))
        self.rev_status_label.setWordWrap(True)
        self.rev_status_label.setStyleSheet("")

        self.btn_refresh_rev = QPushButton(tr("Re-Scan"))
        self.btn_refresh_rev.setIcon(QIcon.fromTheme("view-refresh"))
        self.btn_refresh_rev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_rev.clicked.connect(self._request_reverse_deps)

        btn_layout.addWidget(self.rev_status_label, stretch=1)
        btn_layout.addWidget(self.btn_refresh_rev)
        layout.addLayout(btn_layout)

        self.reverse_list = QListWidget()
        self.reverse_list.setObjectName("InspectorReverseList")
        self.reverse_list.setAccessibleName(tr("Packages requiring this package"))
        self.reverse_list.itemActivated.connect(self._on_reverse_item_activated)

        reverse_container = QWidget()
        reverse_stack = QVBoxLayout(reverse_container)
        reverse_stack.setContentsMargins(0, 0, 0, 0)
        reverse_stack.setSpacing(0)
        reverse_stack.addWidget(self.reverse_list, stretch=1)

        self.reverse_empty_label = QLabel(tr("No reverse dependencies reported yet."))
        self.reverse_empty_label.setProperty(
            "messageId", "No reverse dependencies reported yet."
        )
        self.reverse_empty_label.setObjectName("InspectorEmptyState")
        self.reverse_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reverse_empty_label.setWordWrap(True)
        reverse_stack.addWidget(self.reverse_empty_label, stretch=1)

        layout.addWidget(reverse_container, stretch=1)

    def _set_stat(self, attribute: str, prefix: str, value: str) -> None:
        """Update one metadata field, keeping its attribute and cell in step.

        ``prefix`` is the field name used in the plain-text form the attribute
        exposes ("Size: 7.2 MB"); the visible value cell shows only the value.
        """
        display = (value or "").strip()
        label = getattr(self, attribute, None)
        if label is not None:
            label.setText(f"{prefix}: {display}" if display else f"{prefix}: -")
        cell = getattr(self, "_stat_values", {}).get(attribute)
        if cell is not None:
            placeholder = "-" if not display or display == "-" else display
            cell.setText(placeholder)
            cell.setProperty("muted", not display or display == "-")
            cell.style().unpolish(cell)
            cell.style().polish(cell)

    def _set_status_tone(self, tone: str) -> None:
        """Tag the update cell with a status tone so it can be styled as a pill.

        ``tone`` is one of ``current``, ``update`` or ``unknown``. Styling lives
        in the theme, keyed off this property, so switching themes restyles it
        without the caller knowing any colours.
        """
        target = getattr(self, "_stat_chips", {}).get("lbl_upgrade_status")
        if target is None:
            return
        target.setProperty("tone", tone)
        target.style().unpolish(target)
        target.style().polish(target)

    def _show_text_empty_states(self) -> None:
        """Fill the text-browser tabs with a note while no package is selected."""
        pal = get_delegate_palette(self._current_theme)
        self.changelog_browser.setHtml(
            self._placeholder_document(
                tr("Select a package to read its changelog."), pal["text_dim"]
            )
        )
        self.scriptlets_browser.setHtml(
            self._placeholder_document(
                tr("Select a package to inspect its maintainer scriptlets."),
                pal["text_dim"],
            )
        )

    def _sync_empty_states(self) -> None:
        """Show one centred note in place of an empty content surface.

        The surface itself is hidden rather than merely overlaid: an empty table
        still paints its header strip and card border, which reads as a broken
        widget behind the note.
        """
        files_empty = self.files_model.rowCount() == 0
        if files_empty:
            self.files_table.setMinimumHeight(0)
            self.files_table.setMaximumHeight(16777215)
        self.files_table.setVisible(not files_empty)
        # The card's frame and fill are painted by the container now, not by the
        # views, so hiding the views alone leaves an outlined empty box behind
        # the note. The card goes with them.
        card = getattr(self, "_files_card", None)
        if card is not None:
            if files_empty:
                card.setMinimumHeight(0)
            card.setVisible(not files_empty)
        # The pinned size column is part of the same surface: leaving it behind
        # would show a bare size slab next to the empty-state note.
        size_view = getattr(self, "files_size_view", None)
        if size_view is not None:
            if files_empty:
                size_view.setMinimumHeight(0)
                size_view.setMaximumHeight(16777215)
            size_view.setVisible(not files_empty)
        vbar = getattr(self, "_files_vbar", None)
        if vbar is not None and files_empty:
            vbar.setMinimumHeight(0)
            vbar.setMaximumHeight(16777215)
        self.files_empty_label.setVisible(files_empty)

        reverse_empty = self.reverse_list.count() == 0
        self.reverse_list.setVisible(not reverse_empty)
        self.reverse_empty_label.setVisible(reverse_empty)

    @staticmethod
    def _placeholder_document(message: str, colour: QColor) -> str:
        """Render a centred, muted message for an empty content surface."""
        return placeholder_document_html(message, colour)

    def _init_changelog_tab(self):
        layout = QVBoxLayout(self.tab_changelog)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        self.changelog_browser = QTextBrowser()
        self.changelog_browser.setObjectName("InspectorChangelogBrowser")
        self.changelog_browser.setAccessibleName(tr("Package changelog"))
        # Route every changelog link through Dendro's common failure feedback,
        # rather than allowing QTextBrowser to fail silently in the desktop.
        self.changelog_browser.setOpenLinks(False)
        self.changelog_browser.setOpenExternalLinks(False)
        self.changelog_browser.anchorClicked.connect(self._open_changelog_url)
        layout.addWidget(self.changelog_browser, stretch=1)

    def _init_scriptlets_tab(self):
        layout = QVBoxLayout(self.tab_scriptlets)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        self.scriptlets_browser = QTextBrowser()
        self.scriptlets_browser.setObjectName("InspectorScriptletsBrowser")
        self.scriptlets_browser.setAccessibleName(tr("RPM maintainer scripts"))
        self.scriptlets_browser.setFont(scaled_fixed_font())
        layout.addWidget(self.scriptlets_browser, stretch=1)

    # -------------------------------------------------------------------------
    # Theme Coordination & Dynamic Re-rendering
    # -------------------------------------------------------------------------
    def set_theme(self, theme_choice: str):
        """Updates internal theme palette and re-renders active package views."""
        self._current_theme = theme_choice
        if hasattr(self, "files_model"):
            self.files_model.set_theme(theme_choice)
        if self._current_package is not None:
            self.refresh_package_status(self._current_package)
        if self._last_verification is not None and self._current_package is not None:
            results, ignored_mtime = self._last_verification
            self._render_verification_status(results, ignored_mtime=ignored_mtime)
        if self._current_changelog_entries:
            self._render_changelog()
        if self._current_scriptlets is not None:
            self._render_scriptlets(self._current_scriptlets)

    def refresh_package_status(self, pkg: PackageInfo) -> None:
        """Refresh dynamic status, safety, provenance, and queue controls in place."""
        self._current_package = pkg
        pal = get_delegate_palette(self._current_theme)
        safety: RemovalSafetyAssessment = (
            ProductionTaxonomyEngine.assess_removal_safety(pkg)
        )
        self.safety_status_badge.setText(safety.title)
        self.safety_status_badge.setToolTip(safety.description)

        if safety.level == RemovalRiskLevel.CRITICAL:
            badge_color = pal["badge_fg_missing"].name()
        elif safety.level == RemovalRiskLevel.HIGH:
            badge_color = pal["badge_fg_queued_in"].name()
        elif safety.level in (RemovalRiskLevel.MEDIUM, RemovalRiskLevel.REVIEW):
            badge_color = pal["badge_fg_tag"].name()
        elif safety.level == RemovalRiskLevel.SAFE:
            badge_color = pal["badge_fg_installed"].name()
        else:  # RemovalRiskLevel.LOW
            badge_color = pal["accent"].name()

        style_text(
            self.safety_status_badge,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=badge_color,
            padding="2px 0px",
        )

        # Provenance / User-Installed Status
        if pkg.state == PackageState.INSTALLED:
            if pkg.ostree_origin == RpmOstreeOriginKind.OVERRIDE:
                install_type = tr("Base Override (OSTree)")
            elif pkg.ostree_origin == RpmOstreeOriginKind.LOCAL:
                install_type = tr("Local RPM (OSTree)")
            elif pkg.ostree_origin == RpmOstreeOriginKind.LAYERED:
                install_type = tr("Layered (OSTree)")
            elif pkg.ostree_origin == RpmOstreeOriginKind.BASE:
                install_type = tr("Base Image (OSTree)")
            elif self._host_environment.is_immutable_host:
                install_type = tr("Installed (Immutable)")
            elif pkg.is_user_installed:
                install_type = tr("Explicit (User)")
            elif pkg.is_orphan:
                install_type = tr("Orphan (Leaf)")
            else:
                install_type = tr("Dependency (Auto)")
        elif pkg.state == PackageState.AVAILABLE:
            install_type = tr("Remote Package")
        elif pkg.state == PackageState.QUEUED_INSTALL:
            install_type = tr("Queued (Install)")
        elif pkg.state == PackageState.QUEUED_REMOVE:
            install_type = tr("Queued (Remove)")
        elif pkg.state == PackageState.QUEUED_UPGRADE:
            install_type = tr("Queued (Upgrade)")
        else:
            install_type = tr("Dependency (Auto)")
        self._set_stat("lbl_install_type", "Type", install_type)

        # Upgrade Target Status. The cell keeps a status tone so "up to date",
        # "update available" and "state unknown" are distinguishable at a glance
        # rather than only by reading the words.
        if pkg.has_update and pkg.available_update_version:
            # The attribute keeps the plain arrow form it has always used; the
            # chip shows the version alone, which is what the reader needs.
            self._set_stat(
                "lbl_upgrade_status", "Update", f"-> {pkg.available_update_version}"
            )
            self._stat_values["lbl_upgrade_status"].setText(
                tr("{version} available").format(version=pkg.available_update_version)
            )
            self._set_status_tone("update")
        elif self._host_environment.is_immutable_host:
            self._set_stat("lbl_upgrade_status", "Update", tr("Managed by OS Image"))
            self._set_status_tone("unknown")
        elif not self._host_environment.supports_dnf5_daemon_queries:
            self._set_stat("lbl_upgrade_status", "Update", tr("Not Checked"))
            self._set_status_tone("unknown")
        else:
            self._set_stat("lbl_upgrade_status", "Update", tr("Up to Date"))
            self._set_status_tone("current")

        # Dynamic Action Button State
        if not self._host_environment.supports_dnf5_daemon_mutations:
            _title, reason = self._host_environment.dnf5_daemon_unavailable(
                tr("modify packages")
            )
            self.queue_btn.setText(tr("Read-Only Mode"))
            self.queue_btn.setProperty("queueState", "installed")
            self.queue_btn.setToolTip(reason)
            self.queue_btn.setEnabled(False)
        else:
            self.queue_btn.setToolTip("")
            if pkg.has_update and pkg.state == PackageState.INSTALLED:
                self.queue_btn.setText(
                    tr("Queue Upgrade (→ {version})").format(
                        version=pkg.available_update_version
                    )
                )
                self.queue_btn.setProperty("queueState", "available")
            elif pkg.state == PackageState.QUEUED_UPGRADE:
                self.queue_btn.setText(tr("Cancel Upgrade"))
                self.queue_btn.setProperty("queueState", "queued_remove")
            elif pkg.state == PackageState.INSTALLED:
                self.queue_btn.setText(tr("Queue Removal"))
                self.queue_btn.setProperty("queueState", "installed")
            elif pkg.state == PackageState.QUEUED_REMOVE:
                self.queue_btn.setText(tr("Cancel Removal"))
                self.queue_btn.setProperty("queueState", "queued_remove")
            elif pkg.state == PackageState.AVAILABLE:
                self.queue_btn.setText(tr("Queue Install"))
                self.queue_btn.setProperty("queueState", "available")
            elif pkg.state == PackageState.QUEUED_INSTALL:
                self.queue_btn.setText(tr("Cancel Install"))
                self.queue_btn.setProperty("queueState", "queued_install")
            self.queue_btn.setEnabled(True)

        self.queue_btn.style().unpolish(self.queue_btn)
        self.queue_btn.style().polish(self.queue_btn)

    # -------------------------------------------------------------------------
    # Population & State Handlers
    # -------------------------------------------------------------------------
    def _populate_package_overview(self, pkg: PackageInfo) -> None:
        """Render package identity, classification, metadata card, and action state."""
        self._current_package = pkg
        self.pkg_name_label.setText(f"{pkg.name} {pkg.version}")
        self.summary_label.setText(pkg.summary or tr("No summary provided."))

        # Two-tier taxonomy path and provenance come from the shared registry.
        pillar_title = pillar_label(pkg.parent_pillar)
        sub_title = category_label(
            pkg.sub_category, category_label(pkg.primary_category)
        )
        hierarchy_path = f"{pillar_title} -> {sub_title}"
        if pkg.secondary_tags:
            hierarchy_path += f" [{', '.join(pkg.secondary_tags)}]"
        self.taxonomy_badge.setText(hierarchy_path)
        self.provenance_label.setText(
            tr("Evidence: {evidence} · Input: {scope}").format(
                evidence=classification_evidence_label(pkg.classification_evidence),
                scope=taxonomy_input_scope_label(pkg.classification_input_scope),
            )
        )

        if pkg.classification_rationale:
            bullets = "\n".join(
                f"- {reason}" for reason in pkg.classification_rationale[:3]
            )
            self.rationale_label.setText(bullets)
        else:
            self.rationale_label.setText(
                tr("Classified via FHS structural manifest and FreeDesktop standards.")
            )

        # Reflow RPM 70-column hard-wrapped descriptions to fit panel width cleanly
        raw_desc = (
            pkg.description or pkg.summary or tr("No detailed description available.")
        ).strip()
        reflowed_paragraphs = []
        for paragraph in raw_desc.split("\n\n"):
            lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
            if lines and any(
                lines[0].startswith(bullet) for bullet in ("-", "*", "•", "1.", "2.")
            ):
                reflowed_paragraphs.append("\n".join(lines))
            else:
                reflowed_paragraphs.append(" ".join(lines))
        description = "\n\n".join(reflowed_paragraphs)
        self.desc_text.setPlainText(description)
        # An empty card is just a box in the middle of the panel; only show it
        # once there is a description to read.
        self.description_card.setVisible(bool(description.strip()))

        # Stats Cards
        self._set_stat("lbl_size", "Size", pkg.human_size)
        self._set_stat("lbl_arch", "Arch", pkg.arch)
        self._set_stat("lbl_license", "License", pkg.license or tr("Unknown"))
        self._set_stat("lbl_repo", "Repo", pkg.repository)

        self.packager_label.setText(
            tr("Packager: {packager}").format(
                packager=pkg.packager or pkg.vendor or tr("Unknown")
            )
            + "\n"
            + tr("Built: {built}").format(built=pkg.build_time or tr("Unknown"))
        )

        install_time = (pkg.install_time or "").strip()
        self._set_stat("lbl_installed_on", "Installed", install_time)

        self.copy_btn.setEnabled(True)
        self.url_btn.setEnabled(bool(pkg.url))
        self.refresh_package_status(pkg)

    def _apply_unavailable_inspection_state(self) -> None:
        pal = get_delegate_palette(self._current_theme)
        self.files_status_label.setText(
            tr("File manifest: Available after this package is installed.")
        )
        self.files_status_label.setStyleSheet("")
        self.file_search_input.setEnabled(False)
        self.btn_verify.setEnabled(False)
        self.lbl_verify_status.setText(
            tr("Audit: Available after this package is installed.")
        )
        self.lbl_verify_status.setStyleSheet("")
        self.rev_status_label.setText(
            tr("Reverse dependencies: Available after this package is installed.")
        )
        self.btn_refresh_rev.setEnabled(False)
        self.changelog_browser.setHtml(
            self._placeholder_document(
                tr(
                    "Changelog inspection is available after this package is installed."
                ),
                pal["text_dim"],
            )
        )
        self.scriptlets_browser.setHtml(
            self._placeholder_document(
                tr(
                    "Scriptlet inspection is available after this package is installed."
                ),
                pal["text_dim"],
            )
        )

    def set_package_info(self, pkg: PackageInfo):
        """Populates the panel with metadata and native system safety verification."""
        self._reset_outer_scroll()
        self._populate_package_overview(pkg)
        pal = get_delegate_palette(self._current_theme)

        # Installed-RPM inspection has a different data source from remote
        # repository metadata. Do not start commands that cannot answer for an
        # available-but-not-installed package.
        installed_for_inspection = pkg.state in {
            PackageState.INSTALLED,
            PackageState.QUEUED_REMOVE,
            PackageState.QUEUED_UPGRADE,
        }
        self._all_files = []
        self._files_loaded = False
        self.files_model.clear()
        self.file_search_input.clear()
        self.files_status_label.setToolTip("")
        self.reverse_list.clear()
        self.reverse_empty_label.setText(tr("No reverse dependencies reported yet."))
        self.rev_status_label.setStyleSheet("")
        self.rev_status_label.setToolTip("")
        self.lbl_verify_status.setToolTip("")
        self._current_changelog_entries = []
        self._current_scriptlets = None
        self._last_verification = None
        self._current_reverse_deps = None
        # Both surfaces are now empty, so the empty states are accurate.
        self._sync_empty_states()

        if not installed_for_inspection:
            self._apply_unavailable_inspection_state()
            return

        self.files_status_label.setText(tr("Loading installed file manifest…"))
        self.files_status_label.setStyleSheet("")
        self.file_search_input.setEnabled(True)
        self.btn_verify.setEnabled(True)
        self.lbl_verify_status.setText(tr("Audit: Not run"))
        self.lbl_verify_status.setStyleSheet("")
        self.rev_status_label.setText(tr("Click 'Re-Scan' to query dependents."))
        self.btn_refresh_rev.setEnabled(True)
        self.file_inspection_requested.emit(pkg.name)

        self.changelog_browser.setHtml(
            self._placeholder_document(
                tr("Extracting changelog from RPM header..."), pal["text_dim"]
            )
        )
        self.changelog_requested.emit(pkg.name)

        self.scriptlets_browser.setHtml(
            self._placeholder_document(
                tr("Loading maintainer install scriptlets..."), pal["text_dim"]
            )
        )
        self.scriptlets_requested.emit(pkg.name)

    @pyqtSlot(str, object)
    def set_package_scriptlets(self, pkg_name: str, scripts: Any):
        """Displays maintainer install/uninstall scripts using active theme palette."""
        if not self._current_package or self._current_package.name != pkg_name:
            return

        self._current_scriptlets = scripts
        self._render_scriptlets(scripts)

    def _render_scriptlets(self, scripts: Any) -> None:
        self.scriptlets_browser.setHtml(scriptlets_html(scripts, self._current_theme))

    @pyqtSlot(str, str)
    def set_package_scriptlets_error(self, pkg_name: str, detail: str) -> None:
        """Replace the Scripts loading placeholder with a visible failure."""
        if not self._current_package or self._current_package.name != pkg_name:
            return
        self._current_scriptlets = None
        self.scriptlets_browser.setHtml(
            scriptlets_error_html(detail, self._current_theme)
        )

    def _on_reverse_item_activated(self, item: QListWidgetItem):
        pkg_name = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(pkg_name, str) and pkg_name.strip():
            self.package_navigate_requested.emit(pkg_name.strip())

    @pyqtSlot(str, list)
    def set_package_changelog(
        self, pkg_name: str, entries: list[PackageChangelogEntry]
    ):
        """Caches and renders changelog entries using active theme colors."""
        if not self._current_package or self._current_package.name != pkg_name:
            return

        self._current_changelog_entries = entries
        self._render_changelog()

    @pyqtSlot(str, str)
    def set_package_changelog_error(self, pkg_name: str, detail: str) -> None:
        """Replace the changelog loading placeholder with a visible failure."""
        if not self._current_package or self._current_package.name != pkg_name:
            return
        self._current_changelog_entries = []
        self.changelog_browser.setHtml(
            changelog_error_html(detail, self._current_theme)
        )

    def _render_changelog(self):
        """Renders HTML changelog with palette-derived colors for active theme."""
        self.changelog_browser.setHtml(
            changelog_html(self._current_changelog_entries, self._current_theme)
        )

    def _render_reverse_dependencies_status(
        self, reverse_deps: list[DependencyNode]
    ) -> None:
        if reverse_deps:
            count = len(reverse_deps)
            self.rev_status_label.setText(
                tr_plural(
                    "Found {count} package depending on this:",
                    "Found {count} packages depending on this:",
                    count,
                ).format(count=format_number(count))
            )
        else:
            self.rev_status_label.setText(
                tr("No other packages depend on this package (Safe to remove).")
            )

    @pyqtSlot(str, list)
    def set_reverse_dependencies(
        self, pkg_name: str, reverse_deps: list[DependencyNode]
    ):
        if not self._current_package or self._current_package.name != pkg_name:
            return

        self._current_reverse_deps = list(reverse_deps)
        self.reverse_list.clear()
        self.rev_status_label.setStyleSheet("")
        self.rev_status_label.setToolTip("")
        # A leaf package has no dependents. That is a result, not a sentinel row:
        # the list is left genuinely empty and the empty-state note carries the
        # message, so no caller sees a fake package in the manifest.
        self.reverse_empty_label.setText(
            tr("No other packages depend on this package.")
            + "\n"
            + tr("Safe to remove.")
        )
        if reverse_deps:
            for dep in reverse_deps:
                item = QListWidgetItem(f" {dep.resolved_package_name}")
                item.setData(Qt.ItemDataRole.UserRole, dep.resolved_package_name)
                item.setIcon(theme_icon("package-x-generic", "system-software-install"))
                self.reverse_list.addItem(item)
        self._render_reverse_dependencies_status(reverse_deps)

        # The list and the note are mutually exclusive surfaces. Every path that
        # changes the list has to re-evaluate which of them is shown: adding rows
        # without this call left a populated list hidden behind the empty-state
        # note, so a successful Re-Scan looked like it had found nothing.
        self._sync_empty_states()

    @pyqtSlot(str, str)
    def set_reverse_dependencies_error(self, pkg_name: str, detail: str) -> None:
        """Never present a failed reverse-dependency query as safe removal."""
        if not self._current_package or self._current_package.name != pkg_name:
            return
        self._current_reverse_deps = None
        pal = get_delegate_palette(self._current_theme)
        self.reverse_list.clear()
        self.rev_status_label.setText(tr("Could not determine reverse dependencies."))
        style_text(
            self.rev_status_label,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=pal["badge_fg_missing"].name(),
        )
        self.rev_status_label.setToolTip(detail)
        self.reverse_list.addItem(tr("Dependency information could not be loaded."))
        self.reverse_empty_label.setText(tr("No reverse dependencies reported yet."))
        self._sync_empty_states()

    # -------------------------------------------------------------------------
    # Interactions
    # -------------------------------------------------------------------------
    def _on_queue_btn_clicked(self):
        if self._current_package:
            # Keep the selected RPM record intact; a bare name is ambiguous on
            # multilib and install-only systems.
            self.package_action_requested.emit(self._current_package)

    def _copy_package_name(self):
        if self._current_package:
            clipboard = QGuiApplication.clipboard()
            if clipboard:
                clipboard.setText(self._current_package.name)

    def _open_project_url(self):
        if self._current_package and self._current_package.url:
            open_external_web_url(
                self,
                self._current_package.url,
                link_label=tr("package homepage"),
            )

    @pyqtSlot(QUrl)
    def _open_changelog_url(self, url: QUrl) -> None:
        """Open an auto-linked CVE or Bugzilla page with visible failure feedback."""
        open_external_web_url(self, url, link_label=tr("changelog link"))

    def _request_reverse_deps(self):
        if self._current_package and self.btn_refresh_rev.isEnabled():
            self.rev_status_label.setText(tr("Querying reverse dependencies..."))
            self.rev_status_label.setStyleSheet("")
            self.rev_status_label.setToolTip("")
            self.reverse_deps_requested.emit(self._current_package.name)

    def _on_verify_clicked(self):
        if self._current_package and self.btn_verify.isEnabled():
            self.lbl_verify_status.setText(tr("Auditing checksums & permissions..."))
            self.lbl_verify_status.setStyleSheet("")
            self.lbl_verify_status.setToolTip("")
            self.file_verification_requested.emit(self._current_package.name)


__all__ = (
    "CHANGELOG_BUGZILLA_PATTERN",
    "CHANGELOG_CVE_PATTERN",
    "COLUMN_HEADERS",
    "ManifestHeaderView",
    "PackageInspectorPanel",
)
