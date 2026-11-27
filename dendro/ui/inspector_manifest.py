# dendro/ui/inspector_manifest.py
"""Installed file manifest views, layout synchronization, and rpm -V rendering."""

from __future__ import annotations

import heapq
from typing import Final

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, QRect, Qt, pyqtSlot
from PyQt6.QtGui import QBrush, QIcon, QPainter
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
    QVBoxLayout,
    QWidget,
)

from dendro.core.backend import (
    FileVerificationResult,
    PackageFileInfo,
    format_byte_size,
)
from dendro.core.i18n import format_number, tr
from dendro.ui.fonts import DETAIL_PT_DELTA, scaled_fixed_font, style_text
from dendro.ui.icons import theme_icon
from dendro.ui.inspector_renderers import verification_summary
from dendro.ui.styles import get_delegate_palette

# The manifest's columns are named here so the extraction tool can see the
# message ids: a literal list handed to ``setHorizontalHeaderLabels`` is
# invisible to it, and these two shipped in English in all seven languages.
# Column order is fixed by the two views' width arithmetic below.
COLUMN_HEADERS: Final[tuple[str, ...]] = (
    "File Path",
    "Size",
)
_WIDTH_SAMPLE_THRESHOLD: Final[int] = 500


class _ManifestCellProxy:
    """Lightweight cell accessor for callers inspecting rows via ``table.item()``."""

    __slots__ = ("_column", "_model", "_row")

    def __init__(self, model: QAbstractTableModel, row: int, column: int) -> None:
        self._model = model
        self._row = row
        self._column = column

    def _index(self) -> QModelIndex:
        return self._model.index(self._row, self._column)

    def text(self) -> str:
        value = self._model.data(self._index(), Qt.ItemDataRole.DisplayRole)
        return "" if value is None else str(value)

    def data(self, role: int = Qt.ItemDataRole.UserRole):
        return self._model.data(self._index(), role)

    def icon(self) -> QIcon:
        value = self._model.data(self._index(), Qt.ItemDataRole.DecorationRole)
        return value if isinstance(value, QIcon) else QIcon()

    def toolTip(self) -> str:
        value = self._model.data(self._index(), Qt.ItemDataRole.ToolTipRole)
        return "" if value is None else str(value)

    def foreground(self) -> QBrush:
        value = self._model.data(self._index(), Qt.ItemDataRole.ForegroundRole)
        return value if isinstance(value, QBrush) else QBrush()

    def textAlignment(self) -> int:
        value = self._model.data(self._index(), Qt.ItemDataRole.TextAlignmentRole)
        return int(value) if value is not None else 0


class PackageFilesTableModel(QAbstractTableModel):
    """Virtual table model backing large RPM file manifests without item churn."""

    COL_PATH: Final[int] = 0
    COL_SIZE: Final[int] = 1

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._files: list[PackageFileInfo] = []
        self._verify_lookup: dict[str, FileVerificationResult] = {}
        self._theme_key: str = "auto"
        self._palette = get_delegate_palette(self._theme_key)
        self._headers: list[str] = [tr(text) for text in COLUMN_HEADERS]
        self._icon_cache: dict[tuple[str, str], QIcon] = {}

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._files)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return 2

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self._headers)
        ):
            return self._headers[section]
        return super().headerData(section, orientation, role)

    def set_headers(self, headers: list[str] | tuple[str, ...]) -> None:
        self._headers = list(headers)
        if self._headers:
            self.headerDataChanged.emit(
                Qt.Orientation.Horizontal, 0, len(self._headers) - 1
            )

    def set_theme(self, theme_key: str) -> None:
        self._theme_key = theme_key
        self._palette = get_delegate_palette(theme_key)
        if self._files and self._verify_lookup:
            self.dataChanged.emit(
                self.index(0, self.COL_PATH),
                self.index(len(self._files) - 1, self.COL_PATH),
                [Qt.ItemDataRole.ForegroundRole],
            )

    def set_files(
        self,
        files: list[PackageFileInfo],
        *,
        verify_lookup: dict[str, FileVerificationResult] | None = None,
        theme_key: str | None = None,
    ) -> None:
        self.beginResetModel()
        self._files = list(files)
        self._verify_lookup = dict(verify_lookup) if verify_lookup else {}
        if theme_key is not None and theme_key != self._theme_key:
            self._theme_key = theme_key
            self._palette = get_delegate_palette(theme_key)
        self.endResetModel()

    def clear(self) -> None:
        if not self._files and not self._verify_lookup:
            return
        self.beginResetModel()
        self._files = []
        self._verify_lookup = {}
        self.endResetModel()

    @staticmethod
    def icon_name_for_file(file_info: PackageFileInfo) -> str:
        if file_info.is_dir:
            return "folder"
        if file_info.is_executable:
            return "system-run"
        if file_info.is_config:
            return "text-x-generic"
        if file_info.path.endswith(".so") or ".so." in file_info.path:
            return "applications-development"
        return "text-plain"

    def icon_for_name(self, icon_name: str) -> QIcon:
        key = (QIcon.themeName(), icon_name)
        icon = self._icon_cache.get(key)
        if icon is None:
            icon = QIcon.fromTheme(icon_name)
            self._icon_cache[key] = icon
        return icon

    def has_any_icon(self) -> bool:
        if not self._files:
            return False
        seen_names: set[str] = set()
        for file_info in self._files:
            name = self.icon_name_for_file(file_info)
            if name in seen_names:
                continue
            seen_names.add(name)
            if not self.icon_for_name(name).isNull():
                return True
            if len(seen_names) == 5:
                break
        return False

    @staticmethod
    def format_display_path(path: str, diff: FileVerificationResult | None) -> str:
        if diff is None:
            return path
        state_only_diff = (
            bool(diff.annotation)
            and not diff.is_missing
            and not diff.is_unreadable
            and bool(diff.status_flags)
            and set(diff.status_flags) == {"."}
        )
        if diff.is_missing:
            tag = f"[{tr('MISSING')}] "
        elif diff.is_unreadable:
            tag = f"[{tr('UNREADABLE')}] "
        elif state_only_diff:
            tag = f"[{diff.annotation}] "
        else:
            tag = f"[{diff.status_flags}] "
        display_path = tag + path
        if diff.annotation and not state_only_diff:
            display_path += f" ({diff.annotation})"
        return display_path

    def sampled_path_texts(self) -> list[str]:
        """Return display paths to measure without scanning every row on huge RPMs."""
        total = len(self._files)
        if total == 0:
            return []
        verify = self._verify_lookup
        if total <= _WIDTH_SAMPLE_THRESHOLD:
            return [
                self.format_display_path(f.path, verify.get(f.path) if verify else None)
                for f in self._files
            ]
        indices: set[int] = set(range(40))
        indices.update(range(max(0, total - 40), total))
        if verify:
            for row, file_info in enumerate(self._files):
                if file_info.path in verify:
                    indices.add(row)
                    if len(indices) >= 280:
                        break
        indices.update(
            heapq.nlargest(
                200,
                range(total),
                key=lambda row: len(self._files[row].path),
            )
        )
        return [
            self.format_display_path(
                self._files[row].path,
                verify.get(self._files[row].path) if verify else None,
            )
            for row in indices
        ]

    def sampled_size_texts(self) -> list[str]:
        """Return representative size strings without formatting every row on huge RPMs."""
        total = len(self._files)
        if total == 0:
            return []
        if total <= _WIDTH_SAMPLE_THRESHOLD:
            return [format_byte_size(f.size_bytes) for f in self._files]
        sample_sizes: set[int] = set()
        for row in range(64):
            sample_sizes.add(self._files[row].size_bytes)
        for row in range(max(0, total - 32), total):
            sample_sizes.add(self._files[row].size_bytes)
        bucket_max: dict[int, int] = {}
        bucket_min: dict[int, int] = {}
        for file_info in self._files:
            size_val = file_info.size_bytes
            bits = size_val.bit_length()
            prev_max = bucket_max.get(bits)
            if prev_max is None or size_val > prev_max:
                bucket_max[bits] = size_val
            prev_min = bucket_min.get(bits)
            if prev_min is None or size_val < prev_min:
                bucket_min[bits] = size_val
        sample_sizes.update(bucket_max.values())
        sample_sizes.update(bucket_min.values())
        return [format_byte_size(size_val) for size_val in sample_sizes]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._files):
            return None
        file_info = self._files[row]
        col = index.column()

        if col == self.COL_PATH:
            if role == Qt.ItemDataRole.DisplayRole:
                diff = (
                    self._verify_lookup.get(file_info.path)
                    if self._verify_lookup
                    else None
                )
                return self.format_display_path(file_info.path, diff)
            if role == Qt.ItemDataRole.UserRole:
                return file_info.path
            if role == Qt.ItemDataRole.DecorationRole:
                icon = self.icon_for_name(self.icon_name_for_file(file_info))
                return icon if not icon.isNull() else None
            if role == Qt.ItemDataRole.ForegroundRole:
                diff = (
                    self._verify_lookup.get(file_info.path)
                    if self._verify_lookup
                    else None
                )
                if diff is not None:
                    return QBrush(
                        self._palette["badge_fg_queued_in"]
                        if diff.is_unreadable
                        else self._palette["badge_fg_missing"]
                    )
                return None
            if role == Qt.ItemDataRole.ToolTipRole:
                diff = (
                    self._verify_lookup.get(file_info.path)
                    if self._verify_lookup
                    else None
                )
                if diff is not None:
                    if diff.annotation:
                        return f"{file_info.path} ({diff.annotation})"
                    if diff.is_unreadable:
                        return tr(
                            "{path} (unreadable without elevated privileges: {flags})"
                        ).format(path=file_info.path, flags=diff.status_flags)
                return None
            return None

        if col == self.COL_SIZE:
            if role == Qt.ItemDataRole.DisplayRole:
                return format_byte_size(file_info.size_bytes)
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if role == Qt.ItemDataRole.UserRole:
                return file_info.size_bytes
            return None

        return None


class ManifestTableView(QTableView):
    """QTableView preserving QTableWidget read accessors for test compatibility."""

    def rowCount(self) -> int:
        model = self.model()
        return model.rowCount() if model is not None else 0

    def columnCount(self) -> int:
        model = self.model()
        return model.columnCount() if model is not None else 0

    def item(self, row: int, column: int) -> _ManifestCellProxy | None:
        model = self.model()
        if (
            model is None
            or row < 0
            or column < 0
            or row >= model.rowCount()
            or column >= model.columnCount()
        ):
            return None
        return _ManifestCellProxy(model, row, column)

    def setRowCount(self, count: int) -> None:
        model = self.model()
        if count == 0 and isinstance(model, PackageFilesTableModel):
            model.clear()

    def setHorizontalHeaderLabels(self, labels: list[str] | tuple[str, ...]) -> None:
        model = self.model()
        if isinstance(model, PackageFilesTableModel):
            model.set_headers(labels)


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
        self.file_search_input.setAccessibleName(tr("Filter installed files"))
        self.file_search_input.setPlaceholderText(
            tr("Filter installed files (/bin, /etc, ...)")
        )
        self.file_search_input.setClearButtonEnabled(True)
        self.file_search_input.textChanged.connect(self._filter_files_view)
        layout.addWidget(self.file_search_input)

        self.files_model = PackageFilesTableModel(self)
        self.files_table = ManifestTableView()
        self.files_table.setObjectName("InspectorFilesTable")
        self.files_table.setAccessibleName(tr("Installed package files"))
        self.files_table.setFont(scaled_fixed_font())
        self.files_table.setModel(self.files_model)
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
        self.files_size_view.setModel(self.files_model)
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

        self._files_requested = True
        self._all_files = files
        self._files_loaded = True
        self._populate_files_table(files)
        if files:
            self.files_status_label.setText(
                tr("Loaded {count} installed files.").format(
                    count=format_number(len(files))
                )
            )
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
        self._files_requested = True
        self._all_files = []
        self._files_loaded = False
        self.files_model.clear()
        self._sync_empty_states()
        self.file_search_input.setEnabled(False)
        reason = " ".join(str(detail).split())
        prefix = tr("Could not load the installed file manifest.")
        self.files_status_label.setText(prefix + (f" {reason}" if reason else ""))
        style_text(
            self.files_status_label,
            delta=DETAIL_PT_DELTA,
            bold=True,
            color=pal["badge_fg_missing"].name(),
        )
        self.files_status_label.setToolTip(reason)

    def _populate_files_table(self, files: list[PackageFileInfo]):
        verify_lookup = (
            {r.path: r for r in self._last_verification[0]}
            if self._last_verification is not None
            else {}
        )
        self.files_table.setUpdatesEnabled(False)
        self.files_size_view.setUpdatesEnabled(False)
        try:
            self.files_model.set_files(
                files,
                verify_lookup=verify_lookup,
                theme_key=self._current_theme,
            )
            self._sync_empty_states()
            self._fit_files_columns()
        finally:
            self.files_table.setUpdatesEnabled(True)
            self.files_size_view.setUpdatesEnabled(True)

    def _size_column_width(self) -> int:
        """Width of the size gutter: wide enough for the header and sampled values."""
        metrics = self.files_table.fontMetrics()
        widest = metrics.horizontalAdvance(tr(COLUMN_HEADERS[1]))
        for text in self.files_model.sampled_size_texts():
            widest = max(widest, metrics.horizontalAdvance(text))
        return widest + 30 + self._PINNED_VIEW_SLACK

    def _fit_files_columns(self) -> None:
        """Size the file manifest's columns from its own content."""
        table = getattr(self, "files_table", None)
        model = getattr(self, "files_model", None)
        if table is None or model is None:
            return
        metrics = table.fontMetrics()
        padding = 24
        has_icon = model.has_any_icon()
        decoration = table.iconSize().width() + 8 if has_icon else 0
        max_content = max(0, self._MAX_PATH_COLUMN_WIDTH - decoration - padding)
        widest = 0
        for text in model.sampled_path_texts():
            widest = max(widest, metrics.horizontalAdvance(text))
            if widest >= max_content:
                widest = max_content
                break

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
        """Bring the manifest tables into the active scaled monospace font."""
        view = getattr(self, "files_size_view", None)
        table = getattr(self, "files_table", None)
        if view is None or table is None:
            return
        mono_font = scaled_fixed_font()
        if table.font() != mono_font:
            table.setFont(mono_font)
        if view.font() != table.font():
            view.setFont(table.font())
        path_rows = table.verticalHeader()
        size_rows = view.verticalHeader()
        height = path_rows.defaultSectionSize()
        if size_rows.defaultSectionSize() != height:
            size_rows.setDefaultSectionSize(height)
        if self.files_model.rowCount() > 0:
            self._fit_files_columns()

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
    "ManifestTableView",
    "PackageFilesTableModel",
    "PackageManifestMixin",
)
