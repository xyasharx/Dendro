"""Security advisor: which pending updates carry a published fix.

The DNF5 daemon's ``Advisory.list`` reports each advisory together with its own
comparison of the installed name-architecture against the version the advisory
covers.  Dendro shows only advisories whose fix is not installed yet, so the
list is a to-do list rather than an archive.

This is informational.  Installing the updates still goes through the same
resolve, review, and confirm path as any other transaction, and nothing here
touches the host.

Every link the panel can open is a row in a table of references rather than a
single "open the first CVE" action: an advisory commonly carries several CVEs,
and a Bugzilla-only advisory would otherwise have no reachable page at all.
"""

from __future__ import annotations

from typing import Final

from PyQt6.QtCore import QSettings, Qt, QUrl
from PyQt6.QtGui import (
    QFont,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.dnf5daemon import SecurityAdvisory
from core.i18n import tr, tr_plural
from ui.external_links import open_external_web_url
from ui.fonts import apply_labelled_fonts
from ui.styles import get_delegate_palette

# How a severity is coloured reuses the delegate palette so the badge stays
# consistent with the rest of the window instead of inventing new colours.
_SEVERITY_TONE: Final[dict[str, str]] = {
    "critical": "missing",
    "important": "queued_rm",
    "high": "queued_rm",
    "moderate": "update",
    "medium": "update",
    "low": "installed",
}

# The table's columns are named here so the extraction tool can see the message
# ids: a literal list handed to ``setHorizontalHeaderLabels`` is invisible to it
# and would ship in English in all seven languages.
COLUMN_HEADERS: Final[tuple[str, ...]] = (
    "Severity",
    "Advisory",
    "Affected package",
    "References",
)

# Red Hat's Bugzilla is reachable by bug number; the CVE viewer indexes Red Hat's
# own advisory database rather than the MITRE dictionary, which is the page that
# names the fix to install on Fedora.
_CVE_URL: Final[str] = "https://access.redhat.com/security/cve/{identifier}"
_BUGZILLA_URL: Final[str] = "https://bugzilla.redhat.com/show_bug.cgi?id={identifier}"


def _cve_browser_url(cve_id: str) -> str:
    """Return the Red Hat CVE page for one identifier."""
    return _CVE_URL.format(identifier=cve_id)


def _bugzilla_browser_url(bug_id: str) -> str:
    """Return the Bugzilla page for one bug number."""
    return _BUGZILLA_URL.format(identifier=bug_id)


def reference_url(kind: str, identifier: str) -> str:
    """Return the page for one reference, or an empty string if unknown."""
    if kind == "cve":
        return _cve_browser_url(identifier)
    if kind == "bugzilla":
        return _bugzilla_browser_url(identifier)
    return ""


def advisory_references(
    advisory: SecurityAdvisory,
) -> tuple[tuple[str, str], ...]:
    """Return ``(kind, identifier)`` for every reference the advisory carries.

    Both kinds are returned in a stable order -- CVEs first, then Bugzilla --
    because the daemon's own reference order is not specified and a keyboard
    user tabbing through the list needs it to hold still.
    """
    references: list[tuple[str, str]] = []
    for kind, identifiers in (
        ("cve", advisory.cve_ids),
        ("bugzilla", advisory.bugzilla_ids),
    ):
        references.extend(
            (kind, stripped)
            for identifier in identifiers
            if (stripped := identifier.strip())
        )
    return tuple(references)


class SecurityAdvisoryDialog(QDialog):
    """Lists security advisories that affect this system."""

    def __init__(
        self, advisories: tuple[SecurityAdvisory, ...], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Security Advisories"))
        self.resize(940, 640)
        self.setMinimumSize(720, 460)
        self._advisories = advisories
        self._references: tuple[tuple[str, str], ...] = ()
        # The count is kept on the widget so ``retranslate`` can rebuild the
        # label: a one-off ``tr()`` would lose the number, and an f-string would
        # never translate at all.
        self._total = len(advisories)
        self._init_ui()
        apply_labelled_fonts(self)
        self._populate()

    # -------------------------------------------------------------------------
    # Construction
    # -------------------------------------------------------------------------
    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        self.heading = QLabel()
        self.heading.setStyleSheet("font-weight: bold;")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        self.explanation = QLabel()
        self.explanation.setWordWrap(True)
        self.explanation.setObjectName("SubtleHelpLabel")
        layout.addWidget(self.explanation)
        self._write_summary()

        self.empty_hint = QLabel()
        self.empty_hint.setObjectName("SubtleHelpLabel")
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setText(
            tr(
                "Security updates appear here once a repository announces an "
                "advisory for a package installed on this system. Reload the "
                "package list, or refresh repository metadata, to check again."
            )
        )
        self.empty_hint.setVisible(not self._advisories)
        layout.addWidget(self.empty_hint, stretch=1)

        self.table = QTableWidget(0, len(COLUMN_HEADERS))
        self.table.setObjectName("AdvisoryTable")
        self.table.setHorizontalHeaderLabels([tr(text) for text in COLUMN_HEADERS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
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

        # A rich-text view so each reference in the detail pane is clickable in
        # place. The text itself is escaped before it is formatted; only the
        # link anchors this module writes are markup.
        self.detail = QTextBrowser()
        self.detail.setObjectName("AdvisoryDetail")
        self.detail.setOpenLinks(False)
        self.detail.setOpenExternalLinks(False)
        self.detail.anchorClicked.connect(self._on_anchor_clicked)
        self.detail.setPlaceholderText(
            tr("Select an advisory to read its description.")
        )
        layout.addWidget(self.detail, stretch=2)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.open_reference_button = QPushButton(tr("Open Security Page"))
        self.open_reference_button.setObjectName("AdvisoryOpenButton")
        # Enabled state and caption both depend on the selected advisory, so
        # neither is decided here.
        self.open_reference_button.clicked.connect(self._open_selected_reference)
        buttons.addWidget(self.open_reference_button)

        self.copy_button = QPushButton(tr("Copy Advisory ID"))
        self.copy_button.setObjectName("AdvisorySecondaryButton")
        self.copy_button.clicked.connect(self._copy_advisory_id)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)

        # With nothing to list there is no row to select, no page to open and no
        # identifier to copy; leaving three dead controls on screen is what makes
        # an empty panel look broken rather than empty.
        for widget in (
            self.table,
            self.detail,
            self.open_reference_button,
            self.copy_button,
        ):
            widget.setVisible(bool(self._advisories))

        close_button = QPushButton(tr("Close"))
        close_button.setObjectName("ApplyButton")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._on_selection_changed()

    def _write_summary(self) -> None:
        """Write the heading and the explanation for the current advisories."""
        self.heading.setText(tr("Security advisories awaiting installation"))
        if not self._advisories:
            self.explanation.setText(
                tr(
                    "No published security advisory affects an installed package "
                    "whose fix is still missing. Updates that only add features or "
                    "fix non-security bugs are not listed here."
                )
            )
            return

        counts: dict[str, int] = {}
        for advisory in self._advisories:
            severity = advisory.severity.strip() or tr("Unspecified")
            counts[severity] = counts.get(severity, 0) + 1

        summary = ", ".join(
            f"{count} {severity.casefold()}"
            for severity, count in sorted(
                counts.items(), key=lambda pair: pair[0].casefold()
            )
        )
        # English's "one vs many" rule is wrong for several supported languages,
        # so the noun comes from the catalog's own plural boundary.
        noun = tr_plural("advisory", "advisories", self._total)
        self.explanation.setText(
            tr(
                "{total} {noun} ({summary}) affect installed packages whose updated "
                "build is not installed yet. The fix arrives with the normal update; "
                "installing it still requires the usual review and confirmation."
            ).format(total=self._total, noun=noun, summary=summary)
        )

    def retranslate(self) -> None:
        """Re-apply every label after a language change.

        A dialog is normally created after a language switch and closed before
        the next one, but the sweep reaches any widget that is still visible.
        The table headers and the reference anchors are written once at
        construction, so they are the parts that would otherwise stay in the
        previous language.
        """
        self.setWindowTitle(tr("Security Advisories"))
        self._write_summary()
        self.table.setHorizontalHeaderLabels([tr(text) for text in COLUMN_HEADERS])
        self.detail.setPlaceholderText(
            tr("Select an advisory to read its description.")
        )
        self.copy_button.setText(tr("Copy Advisory ID"))
        self._on_selection_changed()

    # -------------------------------------------------------------------------
    # Population
    # -------------------------------------------------------------------------
    def _populate(self) -> None:
        palette = get_delegate_palette(
            QSettings("FedoraCommunity", "Dendro").value("theme", "auto", type=str)
        )
        self.table.setRowCount(len(self._advisories))
        for row, advisory in enumerate(self._advisories):
            severity = advisory.severity.strip() or tr("Unspecified")
            severity_item = QTableWidgetItem(severity)
            tone = _SEVERITY_TONE.get(severity.casefold())
            if tone:
                severity_item.setForeground(palette[f"badge_fg_{tone}"])
            severity_item.setToolTip(advisory.advisory_type or tr("security advisory"))
            self.table.setItem(row, 0, severity_item)

            title = advisory.title or advisory.advisory_id
            title_item = QTableWidgetItem(title)
            title_item.setToolTip(f"{advisory.advisory_id}\n{title}")
            title_item.setData(Qt.ItemDataRole.UserRole, advisory.advisory_id)
            self.table.setItem(row, 1, title_item)

            packages = ", ".join(advisory.affected_packages)
            package_item = QTableWidgetItem(packages)
            package_item.setToolTip(packages)
            self.table.setItem(row, 2, package_item)

            references = ", ".join(
                identifier for _kind, identifier in advisory_references(advisory)
            )
            reference_item = QTableWidgetItem(references)
            if references:
                # Say what a double-click does only where it does something.
                reference_item.setToolTip(
                    tr("Double-click to open this advisory's security page.")
                )
            self.table.setItem(row, 3, reference_item)

        if self._advisories:
            self.table.selectRow(0)

    # -------------------------------------------------------------------------
    # Selection
    # -------------------------------------------------------------------------
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
            self._references = ()
            self.detail.clear()
            self.open_reference_button.setEnabled(False)
            self.open_reference_button.setText(tr("Open Security Page"))
            self.copy_button.setEnabled(False)
            self.table.setToolTip("")
            return

        self._references = advisory_references(advisory)
        self.detail.setDocument(self._detail_document(advisory))
        # Reading a long description should start at the top rather than at
        # whatever offset the previously selected advisory was left at.
        self.detail.verticalScrollBar().setValue(0)
        self.copy_button.setEnabled(True)

        if not self._references:
            self.open_reference_button.setEnabled(False)
            self.open_reference_button.setText(tr("No Security Page"))
            self.open_reference_button.setToolTip(
                tr("This advisory publishes no CVE or Bugzilla reference.")
            )
            self.table.setToolTip("")
            return

        kind, identifier = self._references[0]
        self.open_reference_button.setEnabled(True)
        # The caption names the page it opens, so the button is not a generic
        # verb whose target the user has to guess. A CVE identifier is
        # self-describing; a Bugzilla number is not -- "Open 2311223" reads as
        # nothing at all -- so that kind is named.
        if kind == "bugzilla":
            self.open_reference_button.setText(
                tr("Open Bug {identifier}").format(identifier=identifier)
            )
        else:
            self.open_reference_button.setText(
                tr("Open {identifier}").format(identifier=identifier)
            )
        extra = len(self._references) - 1
        tooltip = tr("Opens {identifier} in your browser.").format(
            identifier=identifier
        )
        if extra:
            tooltip = tr(
                "Opens {identifier} in your browser. {extra} more references are "
                "listed below."
            ).format(identifier=identifier, extra=extra)
        self.open_reference_button.setToolTip(tooltip)
        self.table.setToolTip(
            tr("Double-click a row to open {identifier}.").format(identifier=identifier)
        )

    def _detail_document(self, advisory: SecurityAdvisory) -> QTextDocument:
        """Build the detail pane's document for one advisory.

        Built as a document rather than as HTML text because anchors are the
        one thing a Qt stylesheet cannot style: ``QTextBrowser`` paints every
        link in ``QPalette.link()``, and that default is pure ``#0000ff``, which
        measures 1.71:1 against the dark card. The theme's accent colour is set
        on each anchor fragment instead, and it is also what ``setDefaultStyleSheet``
        cannot reach.

        Every field is escaped: a description is repository metadata, so it is
        untrusted text as far as this widget is concerned. The only markup is the
        emphasis and the anchors this method writes.
        """
        palette = get_delegate_palette(
            QSettings("FedoraCommunity", "Dendro").value("theme", "auto", type=str)
        )
        accent = palette["accent"]
        dim = palette["text_dim"]

        document = QTextDocument()
        document.setDefaultFont(self.detail.font())
        cursor = QTextCursor(document)
        formatter = cursor.charFormat()

        def write(text: str, char_format: QTextCharFormat | None = None) -> None:
            cursor.insertText(text, char_format or formatter)

        def write_link(identifier: str, kind: str) -> None:
            link_format = QTextCharFormat()
            link_format.setAnchor(True)
            link_format.setAnchorHref(reference_url(kind, identifier))
            link_format.setForeground(accent)
            # Underline, because colour alone is not an accessible affordance.
            link_format.setFontUnderline(True)
            cursor.insertText(identifier, link_format)

        def write_dim(text: str, *, on_new_line: bool = False) -> None:
            dim_format = QTextCharFormat()
            dim_format.setForeground(dim)
            if on_new_line:
                cursor.insertBlock()
            cursor.insertText(text, dim_format)

        bold = QTextCharFormat()
        bold.setFontWeight(QFont.Weight.Bold)
        write(advisory.advisory_id, bold)
        write_dim(f"  ({advisory.severity.strip() or tr('Unspecified')})")
        cursor.insertBlock()

        if advisory.title:
            cursor.insertBlock()
            write(advisory.title)

        if advisory.affected_packages:
            cursor.insertBlock()
            cursor.insertBlock()
            write(tr("Installed packages this advisory updates:"))
            for nevra in advisory.affected_packages:
                cursor.insertBlock()
                write("    \u2022 ")
                write(nevra)

        if self._references:
            cursor.insertBlock()
            cursor.insertBlock()
            write(tr("References:"))
            for kind, identifier in self._references:
                cursor.insertBlock()
                write("    \u2022 ")
                write_link(identifier, kind)
                write_dim(f"  ({_kind_label(kind)})")

        if advisory.description:
            cursor.insertBlock()
            cursor.insertBlock()
            write(advisory.description)

        cursor.movePosition(QTextCursor.MoveOperation.Start)
        return document

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------
    def _on_anchor_clicked(self, url: QUrl) -> None:
        """Open one of the reference links rendered in the detail pane."""
        open_external_web_url(self, url.toString(), link_label=tr("security page"))

    def _on_reference_activated(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self._advisories)):
            return
        self.table.selectRow(row)
        self._open_selected_reference()

    def _open_selected_reference(self) -> None:
        """Open the first reference of the selected advisory."""
        if not self._references:
            return
        _kind, identifier = self._references[0]
        open_external_web_url(
            self,
            reference_url(*self._references[0]),
            link_label=tr("security page for {identifier}").format(
                identifier=identifier
            ),
        )

    def _copy_advisory_id(self) -> None:
        """Put the selected advisory's identifier on the clipboard."""
        advisory = self._selected_advisory()
        if advisory is None:
            return
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(advisory.advisory_id)


def _kind_label(kind: str) -> str:
    """Short human name for one reference kind."""
    if kind == "cve":
        return "CVE"
    if kind == "bugzilla":
        return "Bugzilla"
    return kind
