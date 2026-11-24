# dendro/ui/inspector_manifest.py
"""Installed file manifest views, layout synchronization, and rpm -V rendering."""

from __future__ import annotations

from typing import Final

from PyQt6.QtCore import QRect, Qt, pyqtSlot
from PyQt6.QtGui import QFontDatabase, QIcon, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollBar,
    QSizePolicy,
    QSpacerItem,
    QStyle,
    QStyleOptionHeaderV2,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.backend import FileVerificationResult, PackageFileInfo, format_byte_size
from core.i18n import tr
from ui.fonts import DETAIL_PT_DELTA, style_text
from ui.icons import theme_icon
from ui.inspector_renderers import verification_summary
from ui.styles import get_delegate_palette

# The manifest's columns are named here so the extraction tool can see the
# message ids: a literal list handed to ``setHorizontalHeaderLabels`` is
# invisible to it, and these two shipped in English in all seven languages.
# Column order is fixed by the two views' width arithmetic below.
COLUMN_HEADERS: Final[tuple[str, ...]] = (
    "File Path",
    "Size",
)


class ManifestHeaderView(QHeaderView):
    """A header that centres each title inside the part of its section you see.

    The manifest's path column is deliberately as wide as its widest path, so
    the column is usually several times wider than the viewport that shows it.
    ``QHeaderView`` lays a title out against the whole section width, which puts
    a centred title hundreds of pixels past the right edge of the visible strip:
    the title was painted, but off-screen, which is what the caller reported
    after 2.18.0. Anchoring the title to the section's leading edge (the 2.19.0
    workaround) brought it back but lost the centring the caller had asked for.

    This view paints the title against the intersection of the section and the
    viewport instead. The visible strip moves with the scroll offset, so the
    title is centred in what is on screen at every column width and at every
    scroll position, which is what "centred" has to mean for a column that is
    wider than its viewport. A section that fits entirely -- the pinned Size
    column, or a short path -- is unaffected: its intersection with the viewport
    is the section itself, so the result is ordinary centring.

    ``paintSection`` is the supported extension point: Qt renders each header
    section itself and ignores ``setItemDelegate`` entirely, so a delegate
    cannot reach the title. The section's frame and background still come from
    the style, so the stylesheet keeps its say; only the rectangle the title is
    laid out in changes, from the whole section to the visible strip.
    """

    def sectionRect(self, logical_index: int) -> QRect:
        """The full rectangle of *logical_index*, in viewport coordinates."""
        return QRect(
            self.sectionViewportPosition(logical_index),
            0,
            self.sectionSize(logical_index),
            self.viewport().height(),
        )

    def visibleSectionRect(self, logical_index: int) -> QRect:
        """The part of :meth:`sectionRect` that is actually on screen."""
        return self.sectionRect(logical_index).intersected(self.viewport().rect())

    def paintSection(self, painter: QPainter, rect: QRect, logical_index: int) -> None:
        if not rect.isValid():
            return

        strip = self.visibleSectionRect(logical_index)
        if strip.isEmpty():
            # Scrolled completely past: there is nothing on screen to label.
            return

        option = QStyleOptionHeaderV2()
        self.initStyleOption(option)
        self.initStyleOptionForIndex(option, logical_index)
        option.rect = rect
        option.textAlignment = (
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )

        title = option.text
        option.text = ""
        painter.save()
        painter.setClipRect(rect)
        self.style().drawControl(QStyle.ControlElement.CE_Header, option, painter, self)
        painter.restore()

        if not title:
            return

        option.text = title
        option.rect = strip
        painter.save()
        painter.setClipRect(strip)
        self.style().drawControl(
            QStyle.ControlElement.CE_HeaderLabel, option, painter, self
        )
        painter.restore()


class PackageManifestMixin:
    """Mixin implementing the inspector's pinned-size file manifest & rpm -V UI."""

    _MAX_PATH_COLUMN_WIDTH = 2400
    _PINNED_VIEW_SLACK = 8

    def _init_files_tab(self):
        layout = QVBoxLayout(self.tab_files)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        verify_bar = QHBoxLayout()
        self.btn_verify = QPushButton(tr("Verify Integrity (rpm -V)"))
        verify_icon = theme_icon("security-high", "system-run")
        if not verify_icon.isNull():
            self.btn_verify.setIcon(verify_icon)
        self.btn_verify.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_verify.clicked.connect(self._on_verify_clicked)

        self.lbl_verify_status = QLabel(tr("Audit: Not run"))
        self.lbl_verify_status.setObjectName("InspectorPackagerLabel")
        self.lbl_verify_status.setWordWrap(True)

        verify_bar.addWidget(self.btn_verify)
        verify_bar.addWidget(self.lbl_verify_status, stretch=1)
        layout.addLayout(verify_bar)

        self.files_status_label = QLabel(
            tr("Select an installed package to inspect its files.")
        )
        self.files_status_label.setObjectName("InspectorPackagerLabel")
        self.files_status_label.setWordWrap(True)
        layout.addWidget(self.files_status_label)

        self.file_search_input = QLineEdit()
        self.file_search_input.setPlaceholderText(
            tr("Filter installed files (/bin, /etc, ...)")
        )
        self.file_search_input.setClearButtonEnabled(True)
        self.file_search_input.textChanged.connect(self._filter_files_view)
        layout.addWidget(self.file_search_input)

        self.files_table = QTableWidget(0, 2)
        self.files_table.setObjectName("InspectorFilesTable")
        self.files_table.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.files_table.setHorizontalHeaderLabels(
            [tr(text) for text in COLUMN_HEADERS]
        )
        files_header = ManifestHeaderView(Qt.Orientation.Horizontal, self.files_table)
        self.files_table.setHorizontalHeader(files_header)
        files_header.setStretchLastSection(False)
        files_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        files_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        files_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
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

        self.files_size_view = QTableView()
        self.files_size_view.setObjectName("InspectorFilesSizeView")
        self.files_size_view.setModel(self.files_table.model())
        self.files_size_view.setSelectionModel(self.files_table.selectionModel())
        self.files_size_view.setFont(self.files_table.font())
        self.files_size_view.setColumnHidden(0, True)
        self.files_size_view.setHorizontalScrollBarPolicy(
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
        self.files_table.setColumnHidden(1, True)

        self.files_size_view.clicked.connect(
            lambda index: self.files_table.selectRow(index.row())
        )
        self._files_vbar = QScrollBar(Qt.Orientation.Vertical)
        self._files_vbar.setObjectName("InspectorFilesVScroll")
        # Vertical policy Ignored prevents QScrollBar's ~52 px minimumSizeHint
        # from competing with the card's row-aligned minimumHeight floor.
        self._files_vbar.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Ignored
        )
        self.files_table.setVerticalScrollBar(self._files_vbar)
        self.files_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.files_size_view.setVerticalScrollBar(self._files_vbar)
        self.files_size_view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.files_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.files_size_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._files_hbar = QScrollBar(Qt.Orientation.Horizontal)
        self._files_hbar.setObjectName("InspectorFilesHScroll")
        self.files_table.setHorizontalScrollBar(self._files_hbar)

        self._files_card = QWidget()
        self._files_card.setObjectName("InspectorFilesCard")
        card_grid = QGridLayout(self._files_card)
        card_grid.setContentsMargins(0, 0, 0, 0)
        card_grid.setHorizontalSpacing(0)
        card_grid.setVerticalSpacing(0)
        card_grid.addWidget(self.files_table, 0, 0)
        card_grid.addWidget(self.files_size_view, 0, 1)
        card_grid.addWidget(self._files_vbar, 0, 2)
        card_grid.addWidget(self._files_hbar, 1, 0, 1, 2)
        card_grid.addItem(
            QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding),
            2,
            0,
        )
        card_grid.setColumnStretch(0, 1)
        card_grid.setRowStretch(0, 10000)
        card_grid.setRowStretch(2, 1)
        self._files_grid = card_grid
        self._files_card.installEventFilter(self)
        self._files_viewport = self.files_table.viewport()
        self._files_viewport.installEventFilter(self)
        self._files_hbar.setVisible(False)
        self._files_hbar.rangeChanged.connect(self._sync_files_hbar_visibility)
        self.files_table.horizontalScrollBar().rangeChanged.connect(
            self._sync_files_hbar_visibility
        )
        self._files_vbar.setVisible(False)
        self._files_vbar.rangeChanged.connect(self._sync_files_vbar_visibility)

        files_container = QWidget()
        files_stack = QVBoxLayout(files_container)
        files_stack.setContentsMargins(0, 0, 0, 0)
        files_stack.setSpacing(0)
        files_stack.addWidget(self._files_card, stretch=1)

        self.files_empty_label = QLabel(tr("No files to list yet."))
        self.files_empty_label.setProperty("messageId", "No files to list yet.")
        self.files_empty_label.setObjectName("InspectorEmptyState")
        self.files_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.files_empty_label.setWordWrap(True)
        files_stack.addWidget(self.files_empty_label, stretch=1)

        layout.addWidget(files_container, stretch=1)

    @pyqtSlot(str, list)
    def set_package_files(self, pkg_name: str, files: list[PackageFileInfo]):
        if not self._current_package or self._current_package.name != pkg_name:
            return

        self._all_files = files
        self._populate_files_table(files)
        if files:
            self.files_status_label.setText(f"Loaded {len(files):,} installed files.")
        else:
            self.files_status_label.setText(
                tr("No installed files are recorded in this RPM manifest.")
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

    def _populate_files_table(self, files: list[PackageFileInfo]):
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

            size_item = QTableWidgetItem(format_byte_size(f.size_bytes))
            size_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )

            self.files_table.setItem(row, 0, path_item)
            self.files_table.setItem(row, 1, size_item)
        self._fit_files_columns()

    def _size_column_width(self) -> int:
        """Width of the size gutter: wide enough for the header and every value."""
        metrics = self.files_table.fontMetrics()
        widest = metrics.horizontalAdvance("Size")
        for row in range(self.files_table.rowCount()):
            item = self.files_table.item(row, 1)
            if item is not None:
                widest = max(widest, metrics.horizontalAdvance(item.text()))
        return widest + 30 + self._PINNED_VIEW_SLACK

    def _fit_files_columns(self) -> None:
        """Size the file manifest's columns from its own content."""
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

        decoration = table.iconSize().width() + 8 if has_icon else 0
        width = min(widest + decoration + padding, self._MAX_PATH_COLUMN_WIDTH)
        header_width = metrics.horizontalAdvance(tr(COLUMN_HEADERS[0])) + padding
        width = max(width, header_width, table.viewport().width())
        table.setColumnWidth(0, width)
        size_width = self._size_column_width()
        table.setColumnWidth(1, size_width)
        view = self.files_size_view
        view.setColumnWidth(1, size_width)
        chrome = view.width() - view.viewport().width()
        if chrome <= 0:
            chrome = 2 * view.frameWidth()
        view.setFixedWidth(size_width + chrome)

    def _snap_manifest_rows(self) -> None:
        """Size the views to a whole number of rows without raising min-height.

        Cap ``maximumHeight`` instead of calling ``setFixedHeight``: raising
        ``minimumHeight`` to the current snapped height locks the inspector and
        ``MainWindow`` against shrinking once a package's files arrive. With
        row 0 stretch ``10000`` vs. the bottom spacer's ``1``, the grid gives
        the views the exact ``target`` height whenever space permits while
        still letting compact windows stay compact.
        """
        table = getattr(self, "files_table", None)
        card = getattr(self, "_files_card", None)
        if table is None or card is None:
            return
        row_height = table.rowHeight(0)
        if row_height <= 0:
            return
        bar = self._files_hbar
        bar_height = bar.sizeHint().height() if bar.isVisible() else 0
        # Always reserve the horizontal bar's extent in the card's floor so
        # toggling _files_hbar never changes the outer scroll area's height
        # threshold or compresses the manifest below one whole uncut row.
        bar_floor = max(0, bar.sizeHint().height())
        if bar_floor <= 0:
            bar_floor = card.style().pixelMetric(
                QStyle.PixelMetric.PM_ScrollBarExtent, None, bar
            )
        header = table.horizontalHeader().height()
        if header <= 0:
            header = table.horizontalHeader().sizeHint().height()
        min_card = header + row_height + bar_floor
        if card.minimumHeight() != min_card:
            card.setMinimumHeight(min_card)
        available = card.contentsRect().height() - bar_height
        whole = max(1, (available - header) // row_height)
        target = whole * row_height + header
        for widget in (table, self.files_size_view, self._files_vbar):
            if widget.minimumHeight() != 0:
                widget.setMinimumHeight(0)
            if widget.maximumHeight() != target:
                widget.setMaximumHeight(target)

    def _sync_files_vbar_visibility(self, *_args) -> None:
        """Show the vertical bar only when the manifest overflows."""
        bar = getattr(self, "_files_vbar", None)
        if bar is None:
            return
        wanted = bar.maximum() > 0
        if bar.isVisible() != wanted:
            bar.setVisible(wanted)
            self._snap_manifest_rows()

    def _sync_files_hbar_visibility(self, *_args) -> None:
        """Show the horizontal bar only when the paths overflow the column."""
        bar = getattr(self, "_files_hbar", None)
        if bar is None:
            return
        wanted = bar.maximum() > 0
        if bar.isVisible() != wanted:
            bar.setVisible(wanted)
            self._snap_manifest_rows()

    def _apply_manifest_font(self) -> None:
        """Bring the pinned Size view back into the path table's typography."""
        view = getattr(self, "files_size_view", None)
        table = getattr(self, "files_table", None)
        if view is None or table is None:
            return
        if view.font() != table.font():
            view.setFont(table.font())
        path_rows = table.verticalHeader()
        size_rows = view.verticalHeader()
        height = path_rows.defaultSectionSize()
        if size_rows.defaultSectionSize() != height:
            size_rows.setDefaultSectionSize(height)

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
        results: list[FileVerificationResult],
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
        results: list[FileVerificationResult],
        *,
        ignored_mtime: bool = False,
    ) -> None:
        text, tooltip, color_hex = verification_summary(
            results,
            self._current_theme,
            ignored_mtime=ignored_mtime,
        )
        self.lbl_verify_status.setToolTip(tooltip)
        self.lbl_verify_status.setText(text)
        style_text(
            self.lbl_verify_status,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=color_hex,
        )

    @pyqtSlot(str, str)
    def set_package_verification_error(self, pkg_name: str, detail: str) -> None:
        """Show that rpm -V failed rather than implying a clean audit."""
        if not self._current_package or self._current_package.name != pkg_name:
            return
        self._last_verification = None
        pal = get_delegate_palette(self._current_theme)
        self.lbl_verify_status.setText(tr("Audit: Could not complete"))
        style_text(
            self.lbl_verify_status,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=pal["badge_fg_missing"].name(),
        )
        self.lbl_verify_status.setToolTip(detail)


__all__ = (
    "COLUMN_HEADERS",
    "ManifestHeaderView",
    "PackageManifestMixin",
)
