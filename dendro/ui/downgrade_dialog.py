"""Version chooser dialog for staging a per-package RPM downgrade.

When a user chooses **Downgrade…** for an installed package, this dialog queries
available repository builds via the DNF5 daemon and lists only versions strictly
older than the installed NEVRA (sorted newest-first). Picking one stores its
exact target NEVRA on the tree row without changing any system files until the
staged transaction is resolved, reviewed, and committed.
"""

from __future__ import annotations

from typing import Final

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dendro.core.dnf5daemon import DnfPackageRecord, PackageRepoVersions
from dendro.core.i18n import format_byte_size, tr
from dendro.core.package_types import PackageInfo
from dendro.ui.fonts import apply_labelled_fonts
from dendro.ui.window_placement import fit_size_to_available_geometry

COLUMN_HEADERS: Final[tuple[str, ...]] = (
    "Version",
    "Architecture",
    "Repository",
    "Download Size",
)


class DowngradePackageDialog(QDialog):
    """Modal picker listing older repository versions for one installed package."""

    def __init__(
        self,
        package: PackageInfo,
        parent: QWidget | None = None,
        *,
        versions: PackageRepoVersions | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DowngradePackageDialog")
        self.setModal(True)

        self.package = package
        self._versions: PackageRepoVersions | None = versions
        self._records: list[DnfPackageRecord] = []
        self._error_detail = ""
        self._loading = versions is None
        self._selected_record: DnfPackageRecord | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.heading = QLabel()
        self.heading.setObjectName("DialogTitleLabel")
        self.heading.setProperty("fontRole", "dialogTitle")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        self.summary_label = QLabel()
        self.summary_label.setObjectName("SubtleHelpLabel")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)

        self.table = QTableWidget(0, len(COLUMN_HEADERS))
        self.table.setObjectName("DowngradeVersionsTable")
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table, stretch=1)

        self.status_label = QLabel()
        self.status_label.setObjectName("SubtleHelpLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)

        self.cancel_button = QPushButton()
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)

        self.queue_button = QPushButton()
        self.queue_button.setObjectName("ApplyButton")
        self.queue_button.setDefault(True)
        self.queue_button.setEnabled(False)
        self.queue_button.clicked.connect(self._accept_selected)
        buttons.addWidget(self.queue_button)

        layout.addLayout(buttons)

        self.setTabOrder(self.table, self.cancel_button)
        self.setTabOrder(self.cancel_button, self.queue_button)

        apply_labelled_fonts(self)
        fit_size_to_available_geometry(self, 660, 420)

        if versions is not None:
            self.set_versions(versions)
        else:
            self.retranslate()

    def selected_record(self) -> DnfPackageRecord | None:
        """Return the chosen downgrade build, or ``None`` if nothing was chosen."""
        return self._selected_record

    def set_versions(self, versions: PackageRepoVersions) -> None:
        """Populate the table with sorted downgrade candidates from the daemon."""
        self._versions = versions
        self._loading = False
        self._error_detail = ""
        self._records = list(versions.downgrades)

        self.progress_bar.hide()
        self.table.setRowCount(len(self._records))
        for row, record in enumerate(self._records):
            ver_item = QTableWidgetItem(record.evr)
            ver_item.setData(Qt.ItemDataRole.UserRole, record)
            arch_item = QTableWidgetItem(record.arch)
            repo_item = QTableWidgetItem(record.repository or tr("Unknown"))
            size_bytes = record.download_size_bytes or record.size_bytes
            size_text = format_byte_size(size_bytes) if size_bytes > 0 else "-"
            size_item = QTableWidgetItem(size_text)
            size_item.setTextAlignment(
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            )
            self.table.setItem(row, 0, ver_item)
            self.table.setItem(row, 1, arch_item)
            self.table.setItem(row, 2, repo_item)
            self.table.setItem(row, 3, size_item)

        has_choices = bool(self._records)
        self.table.setVisible(has_choices)
        if has_choices:
            self.table.selectRow(0)
            self.table.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self._selected_record = None
            self.queue_button.setEnabled(False)
        self.retranslate()

    def set_error(self, detail: str) -> None:
        """Display an error returned while querying available repository versions."""
        self._loading = False
        self._versions = None
        self._records = []
        self._selected_record = None
        self._error_detail = detail.strip()
        self.progress_bar.hide()
        self.table.hide()
        self.queue_button.setEnabled(False)
        self.retranslate()

    def _current_row_record(self) -> DnfPackageRecord | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def _on_selection_changed(self) -> None:
        record = self._current_row_record()
        self._selected_record = record
        self.queue_button.setEnabled(record is not None)

    def _on_row_double_clicked(self, row: int, _column: int) -> None:
        if 0 <= row < len(self._records):
            self.table.selectRow(row)
            self._selected_record = self._records[row]
            self.accept()

    def _accept_selected(self) -> None:
        record = self._current_row_record()
        if record is None:
            return
        self._selected_record = record
        self.accept()

    def retranslate(self) -> None:
        """Refresh all dialog labels when initialized or after a locale switch."""
        self.setWindowTitle(tr("Downgrade {name}").format(name=self.package.name))
        self.heading.setText(
            tr("Select an older version of {name}").format(name=self.package.name)
        )
        installed_version = self.package.full_version
        installed_repo = self.package.repository or tr("Unknown")
        self.summary_label.setText(
            tr("Currently installed: {version} (from {repo})").format(
                version=installed_version,
                repo=installed_repo,
            )
        )
        self.table.setHorizontalHeaderLabels([tr(col) for col in COLUMN_HEADERS])
        self.cancel_button.setText(tr("Cancel"))
        self.queue_button.setText(tr("Queue Downgrade"))

        if self._loading:
            self.progress_bar.show()
            self.table.hide()
            self.status_label.setText(
                tr("Querying enabled repositories for older builds…")
            )
            self.status_label.show()
        elif self._error_detail:
            self.status_label.setText(
                tr("Could not inspect repository versions: {detail}").format(
                    detail=self._error_detail
                )
            )
            self.status_label.show()
        elif not self._records:
            self.status_label.setText(
                tr(
                    "No older versions of {name} are available in the enabled repositories."
                ).format(name=self.package.name)
            )
            self.status_label.show()
        else:
            self.status_label.setText(
                tr(
                    "Staging a downgrade records your target build in the queue; "
                    "DNF5 still resolves dependencies before making system changes."
                )
            )
            self.status_label.show()

    def changeEvent(self, event: QEvent | None) -> None:
        super().changeEvent(event)
        if event is not None and event.type() == QEvent.Type.FontChange:
            apply_labelled_fonts(self)
        elif event is not None and event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


__all__ = ("COLUMN_HEADERS", "DowngradePackageDialog")
