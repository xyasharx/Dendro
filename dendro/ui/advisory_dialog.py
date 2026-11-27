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

import re
from html import escape
from typing import Final
from urllib.parse import quote

from PyQt6.QtCore import Qt, QUrl
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

from dendro.core.daemon_locale import severity_message
from dendro.core.dnf5daemon import SecurityAdvisory
from dendro.core.i18n import format_number, tr, tr_plural
from dendro.core.settings import get_app_settings
from dendro.ui.external_links import open_external_web_url
from dendro.ui.fonts import apply_labelled_fonts
from dendro.ui.styles import get_delegate_palette

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
_CVE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^CVE-\d{4}-\d+$", re.IGNORECASE)
_BUGZILLA_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+$")


def _cve_browser_url(cve_id: str) -> str:
    """Return the Red Hat CVE page for a validated CVE identifier."""
    if not isinstance(cve_id, str):
        return ""
    normalized = cve_id.strip()
    if not _CVE_ID_PATTERN.fullmatch(normalized):
        return ""
    canonical = f"CVE-{normalized[4:]}"
    return _CVE_URL.format(identifier=quote(canonical, safe=""))


def _bugzilla_browser_url(bug_id: str) -> str:
    """Return the Bugzilla page for a validated numeric bug identifier."""
    if not isinstance(bug_id, str):
        return ""
    normalized = bug_id.strip()
    if not _BUGZILLA_ID_PATTERN.fullmatch(normalized):
        return ""
    return _BUGZILLA_URL.format(identifier=quote(normalized, safe=""))


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
        self.table.setAccessibleName(tr("Security advisories"))
        self.table.setHorizontalHeaderLabels([tr(text) for text in COLUMN_HEADERS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
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
        self.detail.setAccessibleName(tr("Advisory description and references"))
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

        self.close_button = QPushButton(tr("Close"))
        self.close_button.setObjectName("ApplyButton")
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.setTabOrder(self.table, self.detail)
        self.setTabOrder(self.detail, self.open_reference_button)
        self.setTabOrder(self.open_reference_button, self.copy_button)
        self.setTabOrder(self.copy_button, self.close_button)

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

        # The daemon's severity is an enumerator, not prose: it selects a
        # colour, orders the list, and lands inside a sentence Dendro builds.
        # Counting the raw word and casefolding it into the summary left it in
        # English ("3 Hinweise (1 important, 1 low)").
        #
        # The count and the severity are joined without a noun attached to the
        # adjective. "1 important" splices an attributive adjective onto a noun,
        # which German and Russian inflect ("1 wichtiger Hinweis") and French
        # agrees in gender ("1 avis importante" is wrong); translating the
        # adjective alone cannot express that. A bare "wichtig: 1" needs no
        # agreement in any supported language, and each catalogue writes its own
        # order, so nothing has to inherit English's word order.
        counts: dict[str, int] = {}
        for advisory in self._advisories:
            # Keyed on the daemon's own word, empty included: the empty case has
            # no severity, and counting it under a display string would make the
            # summary depend on the language that string happened to be in.
            severity = advisory.severity.strip()
            counts[severity] = counts.get(severity, 0) + 1

        summary = ", ".join(
            tr("{severity}: {count}").format(
                severity=self._severity_text(severity).casefold(),
                count=format_number(count),
            )
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
            ).format(total=format_number(self._total), noun=noun, summary=summary)
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
        self.empty_hint.setText(
            tr(
                "Security updates appear here once a repository announces an "
                "advisory for a package installed on this system. Reload the "
                "package list, or refresh repository metadata, to check again."
            )
        )
        self.table.setAccessibleName(tr("Security advisories"))
        self.table.setHorizontalHeaderLabels([tr(text) for text in COLUMN_HEADERS])
        self.detail.setAccessibleName(tr("Advisory description and references"))
        self.detail.setPlaceholderText(
            tr("Select an advisory to read its description.")
        )
        self.copy_button.setText(tr("Copy Advisory ID"))
        self.close_button.setText(tr("Close"))
        self._on_selection_changed()

    # -------------------------------------------------------------------------
    # Population
    # -------------------------------------------------------------------------
    def _populate(self) -> None:
        palette = get_delegate_palette(
            get_app_settings().value("theme", "auto", type=str)
        )
        self.table.setRowCount(len(self._advisories))
        for row, advisory in enumerate(self._advisories):
            severity = self._severity_text(advisory.severity)
            severity_item = QTableWidgetItem(severity)
            tone = _SEVERITY_TONE.get(advisory.severity.strip().casefold())
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
        self.detail.setHtml(self._detail_html(advisory))
        # Reading a long description should start at the top rather than at
        # whatever offset the previously selected advisory was left at.
        self.detail.verticalScrollBar().setValue(0)
        self.copy_button.setEnabled(True)

        openable = self._openable_references()
        if not openable:
            self.open_reference_button.setEnabled(False)
            self.open_reference_button.setText(tr("No Security Page"))
            self.open_reference_button.setToolTip(
                tr("This advisory publishes no CVE or Bugzilla reference.")
            )
            self.table.setToolTip("")
            return

        kind, identifier, _url = openable[0]
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
        extra = len(openable) - 1
        tooltip = tr("Opens {identifier} in your browser.").format(
            identifier=identifier
        )
        if extra:
            tooltip = tr(
                "Opens {identifier} in your browser. {extra} more references are "
                "listed below."
            ).format(identifier=identifier, extra=format_number(extra))
        self.open_reference_button.setToolTip(tooltip)
        self.table.setToolTip(
            tr("Double-click a row to open {identifier}.").format(identifier=identifier)
        )

    @staticmethod
    def _severity_text(severity: str) -> str:
        """Return a severity as the user should read it.

        The lookup belongs to :mod:`dendro.core.daemon_locale`, which decides where the
        data/prose line falls and owns the wording for a missing severity; this
        method only asks for the label.
        """

        return severity_message(severity)

    def _detail_html(self, advisory: SecurityAdvisory) -> str:
        """Render the detail pane's HTML for one advisory.

        Coloured with a document stylesheet rather than run-level formats: the
        stylesheet is the supported way to name an anchor colour, it avoids
        ``setDocument`` entirely (which does not take ownership, so a document
        built here would be a lifetime this class has to manage for a purely
        visual result), and it keeps the pane's model to one small document.

        Every field is escaped. An advisory description is repository metadata,
        so it is untrusted text as far as this widget is concerned; the only
        markup is the emphasis and anchors written here.
        """
        palette = get_delegate_palette(
            get_app_settings().value("theme", "auto", type=str)
        )
        self.detail.document().setDefaultStyleSheet(
            f"a {{ color: {palette['accent'].name()}; text-decoration: underline; }}"
        )

        parts: list[str] = []
        severity = escape(self._severity_text(advisory.severity))
        parts.append(
            f"<p><b>{escape(advisory.advisory_id)}</b> "
            f"<span style='color:{palette['text_dim'].name()}'>({severity})</span></p>"
        )
        if advisory.title:
            parts.append(f"<p>{escape(advisory.title)}</p>")
        if advisory.affected_packages:
            parts.append(
                f"<p>{escape(tr('Installed packages this advisory updates:'))}</p><ul>"
            )
            parts.extend(
                f"<li>{escape(nevra)}</li>" for nevra in advisory.affected_packages
            )
            parts.append("</ul>")
        if self._references:
            parts.append(f"<p>{escape(tr('References:'))}</p><ul>")
            for kind, identifier in self._references:
                url = reference_url(kind, identifier)
                if url:
                    href = escape(url, quote=True)
                    label_html = f"<a href='{href}'>{escape(identifier)}</a>"
                else:
                    label_html = escape(identifier)
                parts.append(
                    f"<li>{label_html} "
                    f"<span style='color:{palette['text_dim'].name()}'>"
                    f"({escape(_kind_label(kind))})</span></li>"
                )
            parts.append("</ul>")
        if advisory.description:
            parts.append(
                "<p>" + escape(advisory.description).replace("\n", "<br>") + "</p>"
            )
        return "".join(parts)

    def _openable_references(self) -> list[tuple[str, str, str]]:
        """Return ``(kind, identifier, url)`` tuples with a validated link."""
        openable: list[tuple[str, str, str]] = []
        for kind, identifier in self._references:
            url = reference_url(kind, identifier)
            if url:
                openable.append((kind, identifier, url))
        return openable

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
        openable = self._openable_references()
        if not openable:
            return
        _kind, identifier, url = openable[0]
        open_external_web_url(
            self,
            url,
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
