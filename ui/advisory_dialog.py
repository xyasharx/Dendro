"""Security advisor: which pending updates carry a published fix.

The DNF5 daemon's ``Advisory.list`` reports each advisory together with its own
comparison of the installed name-architecture against the version the advisory
covers.  Dendro shows only advisories whose fix is not installed yet, so the
list is a to-do list rather than an archive.

This is informational.  Installing the updates still goes through the same
resolve, review, and confirm path as any other transaction, and nothing here
touches the host.
"""

from __future__ import annotations

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.dnf5daemon import SecurityAdvisory
from ui.external_links import open_external_web_url
from ui.fonts import apply_labelled_fonts
from ui.styles import get_delegate_palette

# How a severity is coloured reuses the delegate palette so the badge stays
# consistent with the rest of the window instead of inventing new colours.
_SEVERITY_TONE = {
    "critical": "missing",
    "important": "queued_rm",
    "high": "queued_rm",
    "moderate": "update",
    "medium": "update",
    "low": "installed",
}


def _cve_browser_url(cve_id: str) -> str:
    """Return the Red Hat CVE page for one identifier."""
    return f"https://access.redhat.com/security/cve/{cve_id}"


class SecurityAdvisoryDialog(QDialog):
    """Lists security advisories that affect this system."""

    def __init__(
        self, advisories: tuple[SecurityAdvisory, ...], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Security Advisories")
        self.resize(900, 560)
        self._advisories = advisories
        self._init_ui()
        apply_labelled_fonts(self)
        self._populate()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        counts: dict[str, int] = {}
        for advisory in self._advisories:
            severity = advisory.severity.strip() or "Unspecified"
            counts[severity] = counts.get(severity, 0) + 1

        heading = QLabel("Security advisories awaiting installation")
        heading.setStyleSheet("font-weight: bold;")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        if self._advisories:
            summary = ", ".join(
                f"{count} {severity.casefold()}"
                for severity, count in sorted(
                    counts.items(), key=lambda pair: pair[0].casefold()
                )
            )
            total = len(self._advisories)
            noun = "advisory" if total == 1 else "advisories"
            explanation = QLabel(
                f"{total} {noun} "
                f"({summary}) affect installed packages whose updated build is "
                "not installed yet. The fix arrives with the normal update; "
                "installing it still requires the usual review and confirmation."
            )
        else:
            explanation = QLabel(
                "No published security advisory affects an installed package "
                "whose fix is still missing. Updates that only add features or "
                "fix non-security bugs are not listed here."
            )
        explanation.setWordWrap(True)
        explanation.setObjectName("SubtleHelpLabel")
        layout.addWidget(explanation)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Severity", "Advisory", "Affected package", "References"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        # The title needs real width; the reference list can be elided and is
        # always available in full in the detail pane below.
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(2, 300)
        self.table.setColumnWidth(3, 250)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.cellDoubleClicked.connect(self._on_reference_activated)
        layout.addWidget(self.table, stretch=3)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setObjectName("TransactionPlanText")
        self.detail.setPlaceholderText("Select an advisory to read its description.")
        layout.addWidget(self.detail, stretch=2)

        buttons = QHBoxLayout()
        self.open_cve_button = QPushButton("Open CVE Page")
        self.open_cve_button.setEnabled(False)
        self.open_cve_button.clicked.connect(self._open_selected_cve)
        buttons.addWidget(self.open_cve_button)
        buttons.addStretch(1)
        close_button = QPushButton("Close")
        close_button.setObjectName("ApplyButton")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _populate(self) -> None:
        palette = get_delegate_palette(
            QSettings("FedoraCommunity", "Dendro").value("theme", "auto", type=str)
        )
        self.table.setRowCount(len(self._advisories))
        for row, advisory in enumerate(self._advisories):
            severity = advisory.severity.strip() or "Unspecified"
            severity_item = QTableWidgetItem(severity)
            tone = _SEVERITY_TONE.get(severity.casefold())
            if tone:
                severity_item.setForeground(palette[f"badge_fg_{tone}"])
            severity_item.setToolTip(advisory.advisory_type or "security advisory")
            self.table.setItem(row, 0, severity_item)

            title = advisory.title or advisory.advisory_id
            title_item = QTableWidgetItem(title)
            title_item.setToolTip(advisory.advisory_id)
            title_item.setData(Qt.ItemDataRole.UserRole, advisory.advisory_id)
            self.table.setItem(row, 1, title_item)

            packages = ", ".join(advisory.affected_packages)
            package_item = QTableWidgetItem(packages)
            package_item.setToolTip(packages)
            self.table.setItem(row, 2, package_item)

            references = ", ".join((*advisory.cve_ids, *advisory.bugzilla_ids))
            reference_item = QTableWidgetItem(references)
            reference_item.setToolTip(
                "Double-click to open a CVE page in your browser."
                if advisory.cve_ids
                else references
            )
            self.table.setItem(row, 3, reference_item)

    def _selected_advisory(self) -> SecurityAdvisory | None:
        selection_model = self.table.selectionModel()
        rows = selection_model.selectedRows() if selection_model else []
        if not rows:
            return None
        index = rows[0].row()
        if 0 <= index < len(self._advisories):
            return self._advisories[index]
        return None

    def _on_selection_changed(self) -> None:
        advisory = self._selected_advisory()
        if advisory is None:
            self.detail.clear()
            self.open_cve_button.setEnabled(False)
            return
        lines = [f"{advisory.advisory_id}  ({advisory.severity or 'Unspecified'})", ""]
        if advisory.title:
            lines.extend([advisory.title, ""])
        if advisory.affected_packages:
            lines.append("Installed packages this advisory updates:")
            lines.extend(f"  {nevra}" for nevra in advisory.affected_packages)
            lines.append("")
        if advisory.cve_ids:
            lines.append("CVE references: " + ", ".join(advisory.cve_ids))
        if advisory.bugzilla_ids:
            lines.append("Bugzilla references: " + ", ".join(advisory.bugzilla_ids))
        if advisory.cve_ids or advisory.bugzilla_ids:
            lines.append("")
        if advisory.description:
            lines.append(advisory.description)
        self.detail.setPlainText("\n".join(lines).strip())
        self.open_cve_button.setEnabled(bool(advisory.cve_ids))

    def _on_reference_activated(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self._advisories)):
            return
        self.table.selectRow(row)
        self._open_selected_cve()

    def _open_selected_cve(self) -> None:
        advisory = self._selected_advisory()
        if advisory is None or not advisory.cve_ids:
            return
        open_external_web_url(
            self, _cve_browser_url(advisory.cve_ids[0]), link_label="CVE page"
        )
