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

import html
import re
from typing import Any, Final, List, Optional
from PyQt6.QtCore import Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QGuiApplication,
    QIcon,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.backend import (
    DependencyNode,
    FileVerificationResult,
    PackageChangelogEntry,
    PackageFileInfo,
    PackageInfo,
    PackageState,
    ProductionTaxonomyEngine,
    RemovalRiskLevel,
    RemovalSafetyAssessment,
    format_byte_size,
)
from core.host_environment import HostEnvironment
from core.rpmostree_status import RpmOstreeOriginKind
from core.taxonomy import (
    category_label,
    classification_evidence_label,
    pillar_label,
    taxonomy_input_scope_label,
)
from ui.external_links import open_external_web_url
from ui.fonts import (
    DETAIL_PT_DELTA,
    OVERLINE_PT_DELTA,
    apply_labelled_fonts,
    scaled_font,
    style_text,
)
from ui.icons import theme_icon
from ui.styles import get_delegate_palette


# Precompiled regular expressions for changelog auto-linking
CHANGELOG_CVE_PATTERN: Final[re.Pattern] = re.compile(
    r"\b(CVE-\d{4}-\d{4,7})\b", re.IGNORECASE
)
CHANGELOG_BUGZILLA_PATTERN: Final[re.Pattern] = re.compile(
    r"\b(?:RHBZ|bug)\s*#?(\d{5,8})\b", re.IGNORECASE
)


class PackageInspectorPanel(QWidget):
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

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("InspectorPanel")
        self.setMinimumWidth(360)
        self._current_package: Optional[PackageInfo] = None
        self._all_files: List[PackageFileInfo] = []
        self._current_theme: str = "auto"
        self._current_changelog_entries: List[PackageChangelogEntry] = []
        self._current_scriptlets: Any = None
        self._last_verification: Optional[tuple[List[FileVerificationResult], bool]] = (
            None
        )
        self._host_environment: HostEnvironment = HostEnvironment.traditional()

        self._init_ui()
        # One sweep gives every labelled child the size its object name calls
        # for, so the sizes follow the user's font instead of the stylesheet.
        apply_labelled_fonts(self)

    def set_host_environment(self, environment: HostEnvironment) -> None:
        """Synchronize inspector labels and mutation affordances with host mode."""
        self._host_environment = environment
        if environment.normalizes_rpm_mtimes:
            self.btn_verify.setText("Verify Integrity (rpm -V --nomtime)")
            self.btn_verify.setToolTip(
                "Run 'rpm -V --nomtime' to verify digests, permissions, ownership, "
                "sizes, links, and configuration changes while ignoring OSTree's "
                "normalized epoch-0 '/usr' timestamps."
            )
        else:
            self.btn_verify.setText("Verify Integrity (rpm -V)")
            self.btn_verify.setToolTip(
                "Verify installed file digests, permissions, sizes, and timestamps with 'rpm -V'."
            )
        if self._current_package is not None:
            self.set_package_info(self._current_package)

    def _measure_stat_name_width(self) -> int:
        """Width the fixed field-name gutter needs at the current font.

        The names are a small overline step below body text, so their metrics
        are measured with that font rather than the panel's; a stylesheet alone
        would not tell us, because the size is applied per label.
        """
        font = scaled_font(OVERLINE_PT_DELTA, weight=QFont.Weight.DemiBold)
        metrics = QFontMetrics(font)
        # A couple of pixels of slack: the widest name must not sit flush against
        # the value column, and metrics differ slightly between paint engines.
        return (
            max(metrics.horizontalAdvance(name) for name in self._stat_name_labels) + 4
        )

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 12, 14, 12)
        main_layout.setSpacing(10)

        # ---------------------------------------------------------------------
        # 1. Header (Title, Summary, Close button)
        # ---------------------------------------------------------------------
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        self.pkg_name_label = QLabel("Package Details")
        self.pkg_name_label.setObjectName("InspectorPkgTitle")
        self.pkg_name_label.setTextFormat(Qt.TextFormat.PlainText)
        self.pkg_name_label.setWordWrap(True)
        self.pkg_name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.close_btn = QPushButton()
        self.close_btn.setObjectName("InspectorCloseBtn")
        self.close_btn.setFixedSize(26, 26)
        close_icon = theme_icon("window-close", "dialog-close")
        if not close_icon.isNull():
            self.close_btn.setIcon(close_icon)
        else:
            self.close_btn.setText("x")
        self.close_btn.setToolTip("Close panel")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.closed.emit)

        header_layout.addWidget(self.pkg_name_label, stretch=1)
        header_layout.addWidget(self.close_btn, alignment=Qt.AlignmentFlag.AlignTop)
        main_layout.addLayout(header_layout)

        self.summary_label = QLabel("Select a package to inspect full metadata.")
        self.summary_label.setObjectName("InspectorSummary")
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        main_layout.addWidget(self.summary_label)

        # ---------------------------------------------------------------------
        # 2. Action Toolbar
        # ---------------------------------------------------------------------
        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)

        self.queue_btn = QPushButton("Queue Action")
        self.queue_btn.setObjectName("InspectorQueueBtn")
        self.queue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.queue_btn.clicked.connect(self._on_queue_btn_clicked)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("InspectorCopyBtn")
        self.copy_btn.setIcon(QIcon.fromTheme("edit-copy"))
        self.copy_btn.setToolTip("Copy package name to clipboard")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.clicked.connect(self._copy_package_name)

        self.url_btn = QPushButton("Homepage")
        self.url_btn.setObjectName("InspectorHomepageBtn")
        self.url_btn.setIcon(theme_icon("applications-internet", "browser"))
        self.url_btn.setToolTip("Open official project website")
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

        self.lbl_size = QLabel("Size: -")
        self.lbl_arch = QLabel("Arch: -")
        self.lbl_license = QLabel("License: -")
        self.lbl_repo = QLabel("Repo: -")
        self.lbl_install_type = QLabel("Type: -")
        self.lbl_upgrade_status = QLabel("Update: None")
        self.lbl_installed_on = QLabel("Installed: -")

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
        # Measure with the font the names actually paint in. Measuring with the
        # panel's own font under-sized the gutter by a pixel or two and clipped
        # "Architecture", and the gutter also has to be recomputed whenever the
        # user's font changes size.
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
            name = QLabel(field_name)
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

            heading = QLabel(title)
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
        main_layout.addWidget(stats_frame)

        # ---------------------------------------------------------------------
        # 4. Detail Tabs
        # ---------------------------------------------------------------------
        self.tabs = QTabWidget()
        self.tabs.setObjectName("InspectorTabs")
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
        tab_font = self.tabs.tabBar().font()
        if tab_font.pixelSize() > 0:
            tab_font.setPixelSize(max(tab_font.pixelSize(), 12))
        else:
            tab_font.setPointSizeF(max(tab_font.pointSizeF(), 9.0))
        tab_font.setWeight(QFont.Weight.DemiBold)
        self.tabs.tabBar().setFont(tab_font)

        # Tab 1: Overview & Insights
        self.tab_overview = QWidget()
        self._init_overview_tab()
        self.tabs.addTab(self.tab_overview, "Overview")

        # Tab 2: Files Manifest & Verification
        self.tab_files = QWidget()
        self._init_files_tab()
        self.tabs.addTab(self.tab_files, "Files")

        # Tab 3: Reverse Dependents
        self.tab_reverse = QWidget()
        self._init_reverse_tab()
        self.tabs.addTab(self.tab_reverse, "Required By")

        # Tab 4: Native RPM Changelog & CVEs
        self.tab_changelog = QWidget()
        self._init_changelog_tab()
        self.tabs.addTab(self.tab_changelog, "Changelog")

        # Tab 5: Maintainer Scriptlets & Triggers (%pre, %post)
        self.tab_scriptlets = QWidget()
        self._init_scriptlets_tab()
        self.tabs.addTab(self.tab_scriptlets, "Scripts")

        # The five labels must stay whole, but their pixel width is a property
        # of the desktop font: at 11pt on Fedora's default font the row is wider
        # than the old fixed 360px dock minimum, so Qt elided every label to a
        # stub ("Overv...", "Fi...", "Require...").
        # Ask the bar how wide it needs to be and make that the dock's floor.
        self._sync_tab_bar_minimum_width()
        main_layout.addWidget(self.tabs, stretch=1)

        # Start on the empty states: nothing is selected yet, so no content
        # surface has anything to show. The two text surfaces would otherwise
        # present a completely blank card on first launch.
        self._sync_empty_states()
        self._show_text_empty_states()

    def _sync_tab_bar_minimum_width(self) -> None:
        """Keep the dock at least as wide as its five tab labels need.

        The bar's own ``sizeHint()`` is the one number that already accounts for
        the stylesheet's font weight, tab padding and per-tab margin, which is
        exactly the sum that used to be computed by hand and kept coming up a
        few pixels short. It also tracks the desktop font, so a larger system
        font raises the dock floor instead of eliding every label.
        """
        bar = self.tabs.tabBar()
        margins = self.layout().contentsMargins()
        # Plus the tab widget's own frame on either side.
        self.setMinimumWidth(
            max(360, bar.sizeHint().width() + margins.left() + margins.right() + 8)
        )

    def _init_overview_tab(self):
        layout = QVBoxLayout(self.tab_overview)
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
        classification_title = QLabel("Classification")
        classification_title.setObjectName("InspectorSectionTitle")
        card_layout.addWidget(classification_title)

        # Hierarchy Path (Pillar -> Subcategory)
        header_row = QHBoxLayout()
        self.taxonomy_badge = QLabel("Category: Unknown")
        self.taxonomy_badge.setObjectName("AICategoryBadge")
        self.taxonomy_badge.setTextFormat(Qt.TextFormat.PlainText)
        self.taxonomy_badge.setWordWrap(True)

        header_row.addWidget(self.taxonomy_badge, stretch=1)
        card_layout.addLayout(header_row)

        self.provenance_label = QLabel("Evidence: Unknown")
        self.provenance_label.setObjectName("AIProvenanceLabel")
        self.provenance_label.setTextFormat(Qt.TextFormat.PlainText)
        self.provenance_label.setWordWrap(True)
        card_layout.addWidget(self.provenance_label)

        # Removal Safety Status Indicator
        self.safety_status_badge = QLabel("Removal Risk: Assessment Pending")
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

        rat_title = QLabel("Why this classification")
        rat_title.setObjectName("InspectorBasisLabel")
        card_layout.addWidget(rat_title)

        self.rationale_label = QLabel("Classification rationale will appear here.")
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

        description_title = QLabel("Description")
        description_title.setObjectName("InspectorSectionTitle")
        description_layout.addWidget(description_title)

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
        layout.addWidget(self.description_card, stretch=1)

        # ---------------------------------------------------------------------
        # Footer: packager and build provenance
        # ---------------------------------------------------------------------
        self.packager_label = QLabel("Packager: -")
        self.packager_label.setObjectName("InspectorPackagerLabel")
        self.packager_label.setTextFormat(Qt.TextFormat.PlainText)
        self.packager_label.setWordWrap(True)
        layout.addWidget(self.packager_label)

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

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if hasattr(self, "desc_text"):
            self._sync_description_height()

    def clear(self):
        """Resets the inspector panel to its default empty state."""
        self._current_package = None
        self._all_files = []
        self._current_changelog_entries = []
        self._current_scriptlets = None
        self._last_verification = None

        self.pkg_name_label.setText("Package Details")
        self.summary_label.setText("Select a package to inspect full metadata.")

        self.taxonomy_badge.setText("Category: None")
        self.provenance_label.setText("Evidence: Unknown")
        self.safety_status_badge.setText("Removal Risk: Assessment Pending")
        style_text(
            self.safety_status_badge,
            delta=DETAIL_PT_DELTA,
            bold=True,
            padding="2px 0px",
        )
        self.rationale_label.setText("Classification rationale will appear here.")
        self.desc_text.clear()
        self.description_card.setVisible(False)

        self._set_stat("lbl_size", "Size", "")
        self._set_stat("lbl_arch", "Arch", "")
        self._set_stat("lbl_license", "License", "")
        self._set_stat("lbl_repo", "Repo", "")
        self._set_stat("lbl_install_type", "Type", "")
        self._set_stat("lbl_upgrade_status", "Update", "None")
        self._set_stat("lbl_installed_on", "Installed", "")
        self.packager_label.setText("Packager: -")

        self.queue_btn.setText("Queue Action")
        self.queue_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.url_btn.setEnabled(False)

        self.files_table.setRowCount(0)
        self._sync_empty_states()
        self.files_status_label.setText(
            "Select an installed package to inspect its files."
        )
        self.files_status_label.setStyleSheet("")
        self.file_search_input.clear()
        self.file_search_input.setEnabled(False)
        self.btn_verify.setEnabled(False)
        self.reverse_list.clear()
        self.reverse_empty_label.setText(
            "Select an installed package to inspect dependents."
        )
        self._sync_empty_states()
        self.rev_status_label.setText(
            "Select an installed package to inspect dependents."
        )
        self.btn_refresh_rev.setEnabled(False)
        self._show_text_empty_states()
        self.lbl_verify_status.setText("Audit: Not run")
        self.lbl_verify_status.setStyleSheet("")

    def _init_files_tab(self):
        layout = QVBoxLayout(self.tab_files)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        verify_bar = QHBoxLayout()
        self.btn_verify = QPushButton("Verify Integrity (rpm -V)")
        verify_icon = theme_icon("security-high", "system-run")
        if not verify_icon.isNull():
            self.btn_verify.setIcon(verify_icon)
        self.btn_verify.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_verify.clicked.connect(self._on_verify_clicked)

        self.lbl_verify_status = QLabel("Audit: Not run")
        self.lbl_verify_status.setObjectName("InspectorPackagerLabel")

        verify_bar.addWidget(self.btn_verify)
        verify_bar.addWidget(self.lbl_verify_status, stretch=1)
        layout.addLayout(verify_bar)

        self.files_status_label = QLabel(
            "Select an installed package to inspect its files."
        )
        self.files_status_label.setObjectName("InspectorPackagerLabel")
        self.files_status_label.setWordWrap(True)
        layout.addWidget(self.files_status_label)

        self.file_search_input = QLineEdit()
        self.file_search_input.setPlaceholderText(
            "Filter installed files (/bin, /etc, ...)"
        )
        self.file_search_input.setClearButtonEnabled(True)
        self.file_search_input.textChanged.connect(self._filter_files_view)
        layout.addWidget(self.file_search_input)

        self.files_table = QTableWidget(0, 2)
        self.files_table.setObjectName("InspectorFilesTable")
        self.files_table.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.files_table.setHorizontalHeaderLabels(["File Path", "Size"])
        files_header = self.files_table.horizontalHeader()
        # The path column is as wide as its widest path, not as wide as the dock.
        # A stretched path column elides the tail of every long path with no way
        # to reach it, and the only workaround was to widen the whole panel,
        # which costs the tree and the sidebar their space. Sizing the column to
        # its content gives the tab a horizontal scrollbar instead.
        files_header.setStretchLastSection(False)
        files_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        files_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        # Both axes scroll in pixels. The pinned Size view is a second view onto
        # this model, and the two scrollbars are kept in step by copying values
        # between them; those values only mean the same thing when both bars
        # measure pixels. Per-row scrolling here counted rows against a range
        # that counted pixels, so dragging this bar moved the Size column a few
        # pixels per row and it could never reach the bottom of the manifest.
        self.files_table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.files_table.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.files_table.setWordWrap(False)
        self.files_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.files_table.verticalHeader().setVisible(False)
        self.files_table.setShowGrid(False)
        self.files_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.files_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )

        # A scrolling path column would carry the Size column off the right edge
        # with it, which is the opposite of the point: the size must stay
        # readable while a long path is scrolled into view. The size column is
        # rendered by a second view onto the same model that never scrolls
        # horizontally and is pinned to the right edge.
        self.files_size_view = QTableView()
        self.files_size_view.setObjectName("InspectorFilesSizeView")
        self.files_size_view.setModel(self.files_table.model())
        self.files_size_view.setSelectionModel(self.files_table.selectionModel())
        self.files_size_view.setFont(self.files_table.font())
        self.files_size_view.setColumnHidden(0, True)
        self.files_size_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.files_size_view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.files_size_view.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.files_size_view.verticalHeader().setVisible(False)
        self.files_size_view.horizontalHeader().setStretchLastSection(False)
        self.files_size_view.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Fixed
        )
        self.files_size_view.setWordWrap(False)
        self.files_size_view.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.files_size_view.setShowGrid(False)
        self.files_size_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.files_size_view.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.files_size_view.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.files_size_view.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        # The path view keeps its own Size column in the model -- the data has to
        # live somewhere -- but hides it, or the reader sees two size columns.
        self.files_table.setColumnHidden(1, True)

        # The two views are one table to the reader, so a click anywhere selects
        # the row and both halves repaint.
        self.files_size_view.clicked.connect(
            lambda index: self.files_table.selectRow(index.row())
        )
        self.files_table.clicked.connect(self._sync_files_rows)
        self.files_table.verticalScrollBar().valueChanged.connect(
            self._sync_files_scroll
        )
        self.files_table.verticalScrollBar().rangeChanged.connect(
            lambda _min, _max: self._sync_files_scroll(
                self.files_table.verticalScrollBar().value()
            )
        )
        self.files_table.model().rowsInserted.connect(self._sync_files_rows)
        self.files_table.model().layoutChanged.connect(self._sync_files_rows)

        # The two views scroll the same model, so their vertical ranges only
        # agree while their viewports are the same height. A horizontal
        # scrollbar that appeared under the path column whenever a long path
        # needed it shortened just that viewport, and the two halves then drifted
        # apart a row at a time. Rather than reserving a phantom footer inside
        # both views -- which draws a visible groove under the Size column --
        # neither view carries a footer: the path column owns the horizontal
        # scrollbar and it sits in a strip of its own directly under that column.
        #
        # The strip is always there and always the same height. A scrollbar that
        # came and went as paths were filtered changed the path viewport's
        # height, which is exactly what desynchronised the two halves, so the
        # strip reserves its height permanently and only hides the bar itself
        # when every path already fits.
        self.files_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        )
        self.files_size_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.files_table.horizontalScrollBar().setObjectName("InspectorFilesHScroll")

        path_column = QWidget()
        path_column_layout = QVBoxLayout(path_column)
        path_column_layout.setContentsMargins(0, 0, 0, 0)
        path_column_layout.setSpacing(0)
        path_column_layout.addWidget(self.files_table, stretch=1)

        # The Size view starts at the path column's first row and stops at its
        # last, so the two halves are ruled by the same top and bottom edges and
        # the reserved strip under the path column cannot tilt them apart.
        size_column = QWidget()
        size_column_layout = QVBoxLayout(size_column)
        size_column_layout.setContentsMargins(0, 0, 0, 0)
        size_column_layout.setSpacing(0)
        size_column_layout.addWidget(self.files_size_view, stretch=1)
        self._files_footer = QWidget()
        self._files_footer.setObjectName("InspectorFilesFooter")
        self._files_footer.setFixedHeight(
            self.files_table.horizontalScrollBar().sizeHint().height()
        )
        size_column_layout.addWidget(self._files_footer)

        files_row = QWidget()
        files_row_layout = QHBoxLayout(files_row)
        files_row_layout.setContentsMargins(0, 0, 0, 0)
        files_row_layout.setSpacing(0)
        files_row_layout.addWidget(path_column, stretch=1)
        files_row_layout.addWidget(size_column)

        files_container = QWidget()
        files_stack = QVBoxLayout(files_container)
        files_stack.setContentsMargins(0, 0, 0, 0)
        files_stack.setSpacing(0)
        files_stack.addWidget(files_row, stretch=1)

        # A tall, empty table reads as a failed load. The label sits over the
        # card until the first row arrives.
        self.files_empty_label = QLabel("No files to list yet.")
        self.files_empty_label.setObjectName("InspectorEmptyState")
        self.files_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.files_empty_label.setWordWrap(True)
        # Only one of the two is ever visible, so both may take the whole page:
        # the visible one then fills it, and the note centres inside it.
        files_stack.addWidget(self.files_empty_label, stretch=1)

        layout.addWidget(files_container, stretch=1)

    def _init_reverse_tab(self):
        layout = QVBoxLayout(self.tab_reverse)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        btn_layout = QHBoxLayout()
        self.rev_status_label = QLabel("Packages requiring this package:")
        self.rev_status_label.setStyleSheet("")

        self.btn_refresh_rev = QPushButton("Re-Scan")
        self.btn_refresh_rev.setIcon(QIcon.fromTheme("view-refresh"))
        self.btn_refresh_rev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_rev.clicked.connect(self._request_reverse_deps)

        btn_layout.addWidget(self.rev_status_label, stretch=1)
        btn_layout.addWidget(self.btn_refresh_rev)
        layout.addLayout(btn_layout)

        self.reverse_list = QListWidget()
        self.reverse_list.setObjectName("InspectorReverseList")
        self.reverse_list.itemActivated.connect(self._on_reverse_item_activated)

        reverse_container = QWidget()
        reverse_stack = QVBoxLayout(reverse_container)
        reverse_stack.setContentsMargins(0, 0, 0, 0)
        reverse_stack.setSpacing(0)
        reverse_stack.addWidget(self.reverse_list, stretch=1)

        self.reverse_empty_label = QLabel("No reverse dependencies reported yet.")
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
                "Select a package to read its changelog.", pal["text_dim"]
            )
        )
        self.scriptlets_browser.setHtml(
            self._placeholder_document(
                "Select a package to inspect its maintainer scriptlets.",
                pal["text_dim"],
            )
        )

    def _sync_empty_states(self) -> None:
        """Show one centred note in place of an empty content surface.

        The surface itself is hidden rather than merely overlaid: an empty table
        still paints its header strip and card border, which reads as a broken
        widget behind the note.
        """
        files_empty = self.files_table.rowCount() == 0
        self.files_table.setVisible(not files_empty)
        # The pinned size column is part of the same surface: leaving it behind
        # would show a bare size slab next to the empty-state note.
        size_view = getattr(self, "files_size_view", None)
        if size_view is not None:
            size_view.setVisible(not files_empty)
        self.files_empty_label.setVisible(files_empty)

        reverse_empty = self.reverse_list.count() == 0
        self.reverse_list.setVisible(not reverse_empty)
        self.reverse_empty_label.setVisible(reverse_empty)

    @staticmethod
    def _placeholder_document(message: str, colour: QColor) -> str:
        """Render a centred, muted message for an empty content surface.

        A lone sentence in the top-left corner of a tall card reads as a broken
        page; a centred note reads as an intentional empty state.

        The note carries no font size of its own: the browser already inherits
        the application font, so leaving the size unset is what lets the note
        follow the user's font setting.
        """
        return (
            "<div style='text-align:center; margin-top:72px;'>"
            f"<p style='color:{colour.name()};'>{message}</p>"
            "</div>"
        )

    def _init_changelog_tab(self):
        layout = QVBoxLayout(self.tab_changelog)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        self.changelog_browser = QTextBrowser()
        self.changelog_browser.setObjectName("InspectorChangelogBrowser")
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
        from PyQt6.QtGui import QFontDatabase

        self.scriptlets_browser.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        layout.addWidget(self.scriptlets_browser, stretch=1)

    # -------------------------------------------------------------------------
    # Theme Coordination & Dynamic Re-rendering
    # -------------------------------------------------------------------------
    def set_theme(self, theme_choice: str):
        """Updates internal theme palette and re-renders active package views."""
        self._current_theme = theme_choice
        if self._current_package is not None:
            self.refresh_package_status(self._current_package)
        if self._all_files:
            self._filter_files_view(self.file_search_input.text())
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
        install_type = "Dependency (Auto)"
        if pkg.state == PackageState.INSTALLED:
            if pkg.ostree_origin == RpmOstreeOriginKind.OVERRIDE:
                install_type = "Base Override (OSTree)"
            elif pkg.ostree_origin == RpmOstreeOriginKind.LOCAL:
                install_type = "Local RPM (OSTree)"
            elif pkg.ostree_origin == RpmOstreeOriginKind.LAYERED:
                install_type = "Layered (OSTree)"
            elif pkg.ostree_origin == RpmOstreeOriginKind.BASE:
                install_type = "Base Image (OSTree)"
            elif self._host_environment.is_immutable_host:
                install_type = "Installed (Immutable)"
            elif pkg.is_user_installed:
                install_type = "Explicit (User)"
            elif pkg.is_orphan:
                install_type = "Orphan (Leaf)"
        elif pkg.state == PackageState.AVAILABLE:
            install_type = "Remote Package"
        elif pkg.state == PackageState.QUEUED_INSTALL:
            install_type = "Queued (Install)"
        elif pkg.state == PackageState.QUEUED_REMOVE:
            install_type = "Queued (Remove)"
        elif pkg.state == PackageState.QUEUED_UPGRADE:
            install_type = "Queued (Upgrade)"
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
                f"{pkg.available_update_version} available"
            )
            self._set_status_tone("update")
        elif self._host_environment.is_immutable_host:
            self._set_stat("lbl_upgrade_status", "Update", "Managed by OS Image")
            self._set_status_tone("unknown")
        elif not self._host_environment.supports_dnf5_daemon_queries:
            self._set_stat("lbl_upgrade_status", "Update", "Not Checked")
            self._set_status_tone("unknown")
        else:
            self._set_stat("lbl_upgrade_status", "Update", "Up to Date")
            self._set_status_tone("current")

        # Dynamic Action Button State
        if not self._host_environment.supports_dnf5_daemon_mutations:
            _title, reason = self._host_environment.dnf5_daemon_unavailable(
                "modify packages"
            )
            self.queue_btn.setText("Read-Only Mode")
            self.queue_btn.setProperty("queueState", "installed")
            self.queue_btn.setToolTip(reason)
            self.queue_btn.setEnabled(False)
        else:
            self.queue_btn.setToolTip("")
            if pkg.has_update and pkg.state == PackageState.INSTALLED:
                self.queue_btn.setText(
                    f"Queue Upgrade (→ {pkg.available_update_version})"
                )
                self.queue_btn.setProperty("queueState", "available")
            elif pkg.state == PackageState.QUEUED_UPGRADE:
                self.queue_btn.setText("Cancel Upgrade")
                self.queue_btn.setProperty("queueState", "queued_remove")
            elif pkg.state == PackageState.INSTALLED:
                self.queue_btn.setText("Queue Removal")
                self.queue_btn.setProperty("queueState", "installed")
            elif pkg.state == PackageState.QUEUED_REMOVE:
                self.queue_btn.setText("Cancel Removal")
                self.queue_btn.setProperty("queueState", "queued_remove")
            elif pkg.state == PackageState.AVAILABLE:
                self.queue_btn.setText("Queue Install")
                self.queue_btn.setProperty("queueState", "available")
            elif pkg.state == PackageState.QUEUED_INSTALL:
                self.queue_btn.setText("Cancel Install")
                self.queue_btn.setProperty("queueState", "queued_install")
            self.queue_btn.setEnabled(True)

        self.queue_btn.style().unpolish(self.queue_btn)
        self.queue_btn.style().polish(self.queue_btn)

    # -------------------------------------------------------------------------
    # Population & State Handlers
    # -------------------------------------------------------------------------
    def set_package_info(self, pkg: PackageInfo):
        """Populates the panel with metadata and native system safety verification."""
        self._current_package = pkg

        # Header titles
        self.pkg_name_label.setText(f"{pkg.name} {pkg.version}")
        self.summary_label.setText(pkg.summary or "No summary provided.")

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
            "Evidence: "
            f"{classification_evidence_label(pkg.classification_evidence)}"
            " · Input: "
            f"{taxonomy_input_scope_label(pkg.classification_input_scope)}"
        )

        pal = get_delegate_palette(self._current_theme)

        if pkg.classification_rationale:
            bullets = "\n".join(
                f"- {reason}" for reason in pkg.classification_rationale[:3]
            )
            self.rationale_label.setText(bullets)
        else:
            self.rationale_label.setText(
                "Classified via FHS structural manifest and FreeDesktop standards."
            )

        # Reflow RPM 70-column hard-wrapped descriptions to fit panel width cleanly
        raw_desc = (
            pkg.description or pkg.summary or "No detailed description available."
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
        self._set_stat("lbl_license", "License", pkg.license or "Unknown")
        self._set_stat("lbl_repo", "Repo", pkg.repository)

        self.packager_label.setText(
            f"Packager: {pkg.packager or pkg.vendor or 'Unknown'}"
            f"\nBuilt: {pkg.build_time or 'Unknown'}"
        )

        install_time = (pkg.install_time or "").strip()
        self._set_stat("lbl_installed_on", "Installed", install_time)

        self.copy_btn.setEnabled(True)
        self.url_btn.setEnabled(bool(pkg.url))
        self.refresh_package_status(pkg)

        # Installed-RPM inspection has a different data source from remote
        # repository metadata. Do not start commands that cannot answer for an
        # available-but-not-installed package.
        installed_for_inspection = pkg.state in {
            PackageState.INSTALLED,
            PackageState.QUEUED_REMOVE,
            PackageState.QUEUED_UPGRADE,
        }
        self._all_files = []
        self.files_table.setRowCount(0)
        self.file_search_input.clear()
        self.files_status_label.setToolTip("")
        self.reverse_list.clear()
        self.reverse_empty_label.setText("No reverse dependencies reported yet.")
        self.rev_status_label.setStyleSheet("")
        self.rev_status_label.setToolTip("")
        self.lbl_verify_status.setToolTip("")
        self._current_changelog_entries = []
        self._current_scriptlets = None
        self._last_verification = None
        # Both surfaces are now empty, so the empty states are accurate.
        self._sync_empty_states()

        if not installed_for_inspection:
            unavailable = "Available after this package is installed."
            self.files_status_label.setText(f"File manifest: {unavailable}")
            self.files_status_label.setStyleSheet("")
            self.file_search_input.setEnabled(False)
            self.btn_verify.setEnabled(False)
            self.lbl_verify_status.setText(f"Audit: {unavailable}")
            self.lbl_verify_status.setStyleSheet("")
            self.rev_status_label.setText(f"Reverse dependencies: {unavailable}")
            self.btn_refresh_rev.setEnabled(False)
            self.changelog_browser.setHtml(
                self._placeholder_document(
                    "Changelog inspection is available after this package is installed.",
                    pal["text_dim"],
                )
            )
            self.scriptlets_browser.setHtml(
                self._placeholder_document(
                    "Scriptlet inspection is available after this package is installed.",
                    pal["text_dim"],
                )
            )
            return

        self.files_status_label.setText("Loading installed file manifest…")
        self.files_status_label.setStyleSheet("")
        self.file_search_input.setEnabled(True)
        self.btn_verify.setEnabled(True)
        self.lbl_verify_status.setText("Audit: Not run")
        self.lbl_verify_status.setStyleSheet("")
        self.rev_status_label.setText("Click 'Re-Scan' to query dependents.")
        self.btn_refresh_rev.setEnabled(True)
        self.file_inspection_requested.emit(pkg.name)

        self.changelog_browser.setHtml(
            self._placeholder_document(
                "Extracting changelog from RPM header...", pal["text_dim"]
            )
        )
        self.changelog_requested.emit(pkg.name)

        self.scriptlets_browser.setHtml(
            self._placeholder_document(
                "Loading maintainer install scriptlets...", pal["text_dim"]
            )
        )
        self.scriptlets_requested.emit(pkg.name)

    @pyqtSlot(str, list)
    def set_package_files(self, pkg_name: str, files: List[PackageFileInfo]):
        if not self._current_package or self._current_package.name != pkg_name:
            return

        self._all_files = files
        self._populate_files_table(files)
        if files:
            self.files_status_label.setText(f"Loaded {len(files):,} installed files.")
        else:
            self.files_status_label.setText(
                "No installed files are recorded in this RPM manifest."
            )
        self.files_status_label.setStyleSheet("")
        self.files_status_label.setToolTip("")

    @pyqtSlot(str, str)
    def set_package_files_error(self, pkg_name: str, detail: str) -> None:
        """Render a failed manifest query instead of leaving the Files tab blank."""
        if not self._current_package or self._current_package.name != pkg_name:
            return
        pal = get_delegate_palette(self._current_theme)
        self._all_files = []
        self.files_table.setRowCount(0)
        self._sync_empty_states()
        self.file_search_input.setEnabled(False)
        # The status-bar message expires, so the reason has to live in the tab
        # that stayed empty. The label wraps, and the full text stays in the
        # tooltip for reasons that are longer than a couple of lines.
        reason = " ".join(str(detail).split())
        self.files_status_label.setText(
            "Could not load the installed file manifest."
            + (f" {reason}" if reason else "")
        )
        style_text(
            self.files_status_label,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=pal["badge_fg_missing"].name(),
        )
        self.files_status_label.setToolTip(reason)

    def _populate_files_table(self, files: List[PackageFileInfo]):
        pal = get_delegate_palette(self._current_theme)
        verify_lookup = (
            {r.path: r for r in self._last_verification[0]}
            if self._last_verification is not None
            else {}
        )
        self.files_table.setRowCount(len(files))
        self._sync_empty_states()
        for row, f in enumerate(files):
            if f.is_dir:
                icon = QIcon.fromTheme("folder")
            elif f.is_executable:
                icon = QIcon.fromTheme("system-run")
            elif f.is_config:
                icon = QIcon.fromTheme("text-x-generic")
            elif f.path.endswith(".so") or ".so." in f.path:
                icon = QIcon.fromTheme("applications-development")
            else:
                icon = QIcon.fromTheme("text-plain")

            display_path = f.path
            diff = verify_lookup.get(f.path)
            if diff is not None:
                tag = "[MISSING] " if diff.is_missing else f"[{diff.status_flags}] "
                display_path = tag + f.path

            path_item = QTableWidgetItem(display_path)
            path_item.setData(Qt.ItemDataRole.UserRole, f.path)
            if diff is not None:
                path_item.setForeground(pal["badge_fg_missing"])
            if not icon.isNull():
                path_item.setIcon(icon)

            # Empty files are real (lock files, placeholders) and used to
            # render as a blank SIZE cell that looked like a failed query.
            size_item = QTableWidgetItem(format_byte_size(f.size_bytes))
            size_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )

            self.files_table.setItem(row, 0, path_item)
            self.files_table.setItem(row, 1, size_item)
        self._fit_files_columns()

    # The size column is a fixed gutter so the horizontal scrollbar never covers
    # it: it stays put while the path column scrolls underneath. The path column
    # takes its content width, capped well above the dock width so one absurd
    # path cannot turn the whole tab into a two-screen-wide scroll.
    _MAX_PATH_COLUMN_WIDTH = 2400
    # Chrome the pinned size view keeps even with its scrollbars switched off.
    _PINNED_VIEW_SLACK = 8

    def _size_column_width(self) -> int:
        """Width of the size gutter: wide enough for the header and every value."""
        metrics = self.files_table.fontMetrics()
        widest = metrics.horizontalAdvance("Size")
        for row in range(self.files_table.rowCount()):
            item = self.files_table.item(row, 1)
            if item is not None:
                widest = max(widest, metrics.horizontalAdvance(item.text()))
        # Cell padding, plus the pixels the pinned view spends on its (hidden)
        # scrollbar and frame. Without the slack the column is a few pixels
        # wider than its own viewport and the text is clipped mid-value.
        return widest + 30 + self._PINNED_VIEW_SLACK

    def _fit_files_columns(self) -> None:
        """Size the file manifest's columns from its own content.

        Called after every repopulate, because filtering changes which paths are
        present and therefore how wide the path column needs to be.
        """
        table = getattr(self, "files_table", None)
        if table is None:
            return
        metrics = table.fontMetrics()
        widest = 0
        has_icon = False
        padding = 24
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is None:
                continue
            widest = max(widest, metrics.horizontalAdvance(item.text()))
            if not item.icon().isNull():
                has_icon = True

        # The path text starts after the icon and the cell's own padding.
        decoration = table.iconSize().width() + 8 if has_icon else 0
        width = min(widest + decoration + padding, self._MAX_PATH_COLUMN_WIDTH)
        header_width = metrics.horizontalAdvance("File Path") + padding
        table.setColumnWidth(0, max(width, header_width))
        size_width = self._size_column_width()
        table.setColumnWidth(1, size_width)
        view = self.files_size_view
        view.setColumnWidth(1, size_width)
        # The pinned view must be wide enough for the column *plus* whatever
        # frame it draws around it, or the column overflows its own viewport by
        # a pixel and the last glyph is clipped.
        chrome = view.width() - view.viewport().width()
        if chrome <= 0:
            chrome = 2 * view.frameWidth()
        view.setFixedWidth(size_width + chrome)
        self._sync_files_rows()

    def _sync_files_scroll(self, value: int) -> None:
        """Keep the pinned size column on the same rows as the path column.

        Both bars now measure pixels, so a value can be copied across directly.
        The range is copied too: the two views scroll the same model but compute
        their own extents, and leaving the destination range stale would clamp
        the copied value short of the last row.
        """
        view = getattr(self, "files_size_view", None)
        if view is None:
            return
        source = self.files_table.verticalScrollBar()
        bar = view.verticalScrollBar()
        if bar.maximum() != source.maximum():
            bar.setRange(source.minimum(), source.maximum())
        if bar.pageStep() != source.pageStep():
            bar.setPageStep(source.pageStep())
        if bar.value() != value:
            bar.setValue(value)

    def _sync_files_rows(self, *_args) -> None:
        """Match the pinned size column's row heights to the path column's.

        The two views share a model but not a layout, so a row that the path
        column renders taller (a wrapped tag, a larger icon) would drift the size
        column out of line a row at a time.
        """
        view = getattr(self, "files_size_view", None)
        table = getattr(self, "files_table", None)
        if view is None or table is None:
            return
        view.setFont(table.font())
        view.horizontalHeader().setFixedHeight(table.horizontalHeader().height())
        for row in range(table.rowCount()):
            height = table.rowHeight(row)
            if view.rowHeight(row) != height:
                view.setRowHeight(row, height)
        self._sync_files_scroll(table.verticalScrollBar().value())

    def _filter_files_view(self, query: str):
        query = query.strip().lower()
        if not query:
            self._populate_files_table(self._all_files)
            return

        filtered = [f for f in self._all_files if query in f.path.lower()]
        self._populate_files_table(filtered)

    @pyqtSlot(str, list)
    def set_package_verification(
        self,
        pkg_name: str,
        results: List[FileVerificationResult],
        *,
        ignored_mtime: bool = False,
    ):
        """Renders rpm -V verification results with visual diff badges."""
        if not self._current_package or self._current_package.name != pkg_name:
            return

        self._last_verification = (list(results), ignored_mtime)
        self._render_verification_status(results, ignored_mtime=ignored_mtime)
        if self._all_files:
            self._filter_files_view(self.file_search_input.text())

    def _render_verification_status(
        self,
        results: List[FileVerificationResult],
        *,
        ignored_mtime: bool = False,
    ) -> None:
        pal = get_delegate_palette(self._current_theme)
        mtime_tooltip = (
            "Verified with 'rpm -V --nomtime': OSTree normalizes immutable '/usr' "
            "file modification times to Unix epoch 0, while digests, sizes, modes, "
            "owners, links, and '/etc' changes remain fully checked."
            if ignored_mtime
            else ""
        )
        self.lbl_verify_status.setToolTip(mtime_tooltip)
        if not results:
            clean_text = (
                "Clean (No content/mode diffs; '--nomtime')"
                if ignored_mtime
                else "Clean (No files modified or missing)"
            )
            self.lbl_verify_status.setText(clean_text)
            style_text(
                self.lbl_verify_status,
                delta=DETAIL_PT_DELTA,
                bold=True,
                color=pal["badge_fg_installed"].name(),
            )
            return

        missing_count = sum(1 for r in results if r.is_missing)
        tampered_count = len(results) - missing_count

        suffix = " ('--nomtime')" if ignored_mtime else ""
        self.lbl_verify_status.setText(
            f"Modified: {tampered_count}, Missing: {missing_count}{suffix}"
        )
        style_text(
            self.lbl_verify_status,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=pal["badge_fg_missing"].name(),
        )

    @pyqtSlot(str, str)
    def set_package_verification_error(self, pkg_name: str, detail: str) -> None:
        """Show that rpm -V failed rather than implying a clean audit."""
        if not self._current_package or self._current_package.name != pkg_name:
            return
        self._last_verification = None
        pal = get_delegate_palette(self._current_theme)
        self.lbl_verify_status.setText("Audit: Could not complete")
        style_text(
            self.lbl_verify_status,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=pal["badge_fg_missing"].name(),
        )
        self.lbl_verify_status.setToolTip(detail)

    @pyqtSlot(str, object)
    def set_package_scriptlets(self, pkg_name: str, scripts: Any):
        """Displays maintainer install/uninstall scripts using active theme palette."""
        if not self._current_package or self._current_package.name != pkg_name:
            return

        self._current_scriptlets = scripts
        self._render_scriptlets(scripts)

    def _render_scriptlets(self, scripts: Any) -> None:
        pal = get_delegate_palette(self._current_theme)
        if not scripts or not getattr(scripts, "has_any", False):
            self.scriptlets_browser.setHtml(
                f"<p style='color: {pal['text_dim'].name()};'>No maintainer scriptlets defined in this RPM.</p>"
            )
            return

        blocks: List[str] = []
        sections = [
            ("Pre-Transaction Script (%pretrans)", getattr(scripts, "pretrans", "")),
            ("Pre-Install Script (%pre)", getattr(scripts, "prein", "")),
            ("Post-Install Script (%post)", getattr(scripts, "postin", "")),
            ("Pre-Uninstall Script (%preun)", getattr(scripts, "preun", "")),
            ("Post-Uninstall Script (%postun)", getattr(scripts, "postun", "")),
            ("Post-Transaction Script (%posttrans)", getattr(scripts, "posttrans", "")),
            (
                "Pre-Uninstall Transaction (%preuntrans)",
                getattr(scripts, "preuntrans", ""),
            ),
            (
                "Post-Uninstall Transaction (%postuntrans)",
                getattr(scripts, "postuntrans", ""),
            ),
            ("Verify Script (%verifyscript)", getattr(scripts, "verify", "")),
        ]
        for title, code in sections:
            if code:
                esc_title = html.escape(title, quote=True)
                esc_code = html.escape(str(code), quote=True)
                blocks.append(f"""
                <div style="margin-bottom: 12px; border-bottom: 1px solid {pal["border"].name()}; padding-bottom: 8px;">
                    <div style="color: {pal["accent"].name()}; font-weight: bold; margin-bottom: 4px;">{esc_title}</div>
                    <pre style="white-space: pre-wrap; color: {pal["text_main"].name()}; margin: 0px;">{esc_code}</pre>
                </div>
                """)

        self.scriptlets_browser.setHtml("".join(blocks))

    @pyqtSlot(str, str)
    def set_package_scriptlets_error(self, pkg_name: str, detail: str) -> None:
        """Replace the Scripts loading placeholder with a visible failure."""
        if not self._current_package or self._current_package.name != pkg_name:
            return
        self._current_scriptlets = None
        pal = get_delegate_palette(self._current_theme)
        escaped_detail = html.escape(detail).replace("\n", "<br>")
        self.scriptlets_browser.setHtml(
            f"<p style='color: {pal['badge_fg_missing'].name()}; font-weight: bold;'>"
            "Could not load RPM scriptlets.</p>"
            f"<p style='color: {pal['text_dim'].name()};'>{escaped_detail}</p>"
        )

    def _on_reverse_item_activated(self, item: QListWidgetItem):
        pkg_name = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(pkg_name, str) and pkg_name.strip():
            self.package_navigate_requested.emit(pkg_name.strip())

    @pyqtSlot(str, list)
    def set_package_changelog(
        self, pkg_name: str, entries: List[PackageChangelogEntry]
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
        pal = get_delegate_palette(self._current_theme)
        self._current_changelog_entries = []
        escaped_detail = html.escape(detail).replace("\n", "<br>")
        self.changelog_browser.setHtml(
            f"<p style='color: {pal['badge_fg_missing'].name()}; font-weight: bold;'>"
            "Could not load the package changelog.</p>"
            f"<p style='color: {pal['text_dim'].name()};'>{escaped_detail}</p>"
        )

    def _render_changelog(self):
        """Renders HTML changelog with palette-derived colors for active theme."""
        pal = get_delegate_palette(self._current_theme)
        text_main = pal["text_main"].name()
        text_dim = pal["text_dim"].name()
        accent = pal["accent"].name()
        badge_author = pal["badge_fg_queued_in"].name()
        divider = pal["border"].name()

        if not self._current_changelog_entries:
            self.changelog_browser.setHtml(
                self._placeholder_document(
                    "This package does not carry a changelog in its RPM header.",
                    QColor(text_dim),
                )
            )
            return

        html_blocks: List[str] = []
        for e in self._current_changelog_entries[:40]:
            header_text = (
                f"{e.date_str} — {e.author}"
                if e.date_str and e.date_str not in e.author
                else e.author
            )
            esc_header = html.escape(header_text, quote=True)
            body = html.escape(e.text, quote=True)

            # Auto-link CVE IDs to Red Hat Security Database
            body = CHANGELOG_CVE_PATTERN.sub(
                rf'<a href="https://access.redhat.com/security/cve/\1" style="color: {pal["badge_fg_missing"].name()}; font-weight: bold; text-decoration: none;">\1</a>',
                body,
            )

            # Auto-link Bugzilla issue identifiers
            body = CHANGELOG_BUGZILLA_PATTERN.sub(
                rf'<a href="https://bugzilla.redhat.com/show_bug.cgi?id=\1" style="color: {accent}; text-decoration: none;">RHBZ#\1</a>',
                body,
            )

            html_blocks.append(f"""
            <div style="margin-bottom: 12px; border-bottom: 1px solid {divider}; padding-bottom: 8px;">
                <div style="color: {badge_author}; font-weight: bold;">{esc_header}</div>
                <pre style="margin-top: 4px; white-space: pre-wrap; color: {text_main}; font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;">{body}</pre>
            </div>
            """)

        self.changelog_browser.setHtml("".join(html_blocks))

    @pyqtSlot(str, list)
    def set_reverse_dependencies(
        self, pkg_name: str, reverse_deps: List[DependencyNode]
    ):
        if not self._current_package or self._current_package.name != pkg_name:
            return

        self.reverse_list.clear()
        self.rev_status_label.setStyleSheet("")
        self.rev_status_label.setToolTip("")
        # A leaf package has no dependents. That is a result, not a sentinel row:
        # the list is left genuinely empty and the empty-state note carries the
        # message, so no caller sees a fake package in the manifest.
        self.reverse_empty_label.setText(
            "No other packages depend on this package.\nSafe to remove."
        )
        if reverse_deps:
            count = len(reverse_deps)
            self.rev_status_label.setText(
                f"Found {count} package{'s' if count != 1 else ''} depending on this:"
            )
            for dep in reverse_deps:
                item = QListWidgetItem(f" {dep.resolved_package_name}")
                item.setData(Qt.ItemDataRole.UserRole, dep.resolved_package_name)
                item.setIcon(theme_icon("package-x-generic", "system-software-install"))
                self.reverse_list.addItem(item)
        else:
            # An empty answer is still an answer: the status line says what the
            # query found, and the note underneath explains what it means.
            self.rev_status_label.setText(
                "No other packages depend on this package (Safe to remove)."
            )

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
        pal = get_delegate_palette(self._current_theme)
        self.reverse_list.clear()
        self.rev_status_label.setText("Could not determine reverse dependencies.")
        style_text(
            self.rev_status_label,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=pal["badge_fg_missing"].name(),
        )
        self.rev_status_label.setToolTip(detail)
        self.reverse_list.addItem("Dependency information could not be loaded.")
        self.reverse_empty_label.setText("No reverse dependencies reported yet.")
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
                link_label="package homepage",
            )

    @pyqtSlot(QUrl)
    def _open_changelog_url(self, url: QUrl) -> None:
        """Open an auto-linked CVE or Bugzilla page with visible failure feedback."""
        open_external_web_url(self, url, link_label="changelog link")

    def _request_reverse_deps(self):
        if self._current_package and self.btn_refresh_rev.isEnabled():
            self.rev_status_label.setText("Querying reverse dependencies...")
            self.rev_status_label.setStyleSheet("")
            self.rev_status_label.setToolTip("")
            self.reverse_deps_requested.emit(self._current_package.name)

    def _on_verify_clicked(self):
        if self._current_package and self.btn_verify.isEnabled():
            self.lbl_verify_status.setText("Auditing checksums & permissions...")
            self.lbl_verify_status.setStyleSheet("")
            self.lbl_verify_status.setToolTip("")
            self.file_verification_requested.emit(self._current_package.name)
