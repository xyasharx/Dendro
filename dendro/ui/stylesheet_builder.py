# dendro/ui/stylesheet_builder.py
"""Dynamic QSS stylesheet and SVG form-indicator generator for Dendro."""

from __future__ import annotations

import os
from pathlib import Path

# Dynamic QSS Stylesheet Builder (Elevated Modern Desktop Theme)
# =============================================================================

# QSS can only paint a ::indicator marker from an image file, so the two glyphs
# the form controls need are written into a per-user cache directory once and
# referenced by path. They are generated rather than shipped so the marker
# colour always matches the resolved palette.
_CHECK_MARKER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
    'viewBox="0 0 16 16"><path d="M3.5 8.4 L6.4 11.2 L12.5 4.9" '
    'fill="none" stroke="{stroke}" stroke-width="2.1" stroke-linecap="round" '
    'stroke-linejoin="round"/></svg>'
)
_DOT_MARKER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
    'viewBox="0 0 16 16"><circle cx="8" cy="8" r="3.4" fill="{stroke}"/></svg>'
)
_CHEVRON_MARKER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" '
    'viewBox="0 0 12 12"><path d="M2.5 4.4 L6 8 L9.5 4.4" '
    'fill="none" stroke="{stroke}" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"/></svg>'
)


def _control_assets(
    on_accent_colour: str,
    muted_colour: str,
    on_input_colour: str,
    accent_colour: str,
) -> tuple[str, str, str, str, str]:
    """Return the marker paths ``(check, muted_check, radio, muted_radio, chevron)``.

    The three markers cannot share a colour: the checkmark sits on the accent
    fill, the disabled checkmark sits on the muted well, and the drop-down
    chevron sits on the input background. Using one stroke colour made the
    chevron vanish into the input on the dark palette.

    Written atomically under the user cache directory so repeated theme
    switches reuse the same small files instead of leaving a trail of them.
    Any failure returns five empty strings, which simply drops the marker
    images and leaves the solid indicator fills in place; callers can rely on
    the shape of the result no matter how hostile the cache directory is.
    """
    xdg_cache = (os.environ.get("XDG_CACHE_HOME") or "").strip()
    cache_root = (
        Path(xdg_cache)
        if xdg_cache and os.path.isabs(xdg_cache)
        else Path.home() / ".cache"
    )
    base = cache_root / "dendro" / "control-assets"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "", "", "", "", ""
    written: list[str] = []
    for name, template, stroke_colour in (
        ("check", _CHECK_MARKER_SVG, on_accent_colour),
        ("check-muted", _CHECK_MARKER_SVG, muted_colour),
        # The radio dot sits inside an input-coloured well, so it is drawn in
        # the accent itself; accent_text would disappear into that well.
        ("radio", _DOT_MARKER_SVG, accent_colour),
        ("radio-muted", _DOT_MARKER_SVG, muted_colour),
        ("chevron", _CHEVRON_MARKER_SVG, on_input_colour),
    ):
        target = base / f"{name}-{stroke_colour.lstrip('#')}.svg"
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            try:
                temporary.write_text(
                    template.format(stroke=stroke_colour), encoding="utf-8"
                )
                temporary.replace(target)
            except OSError:
                continue
        written.append(target.as_posix())
    if len(written) != 5:
        return "", "", "", "", ""
    return tuple(written)  # type: ignore[return-value]


def build_stylesheet(c: dict[str, str]) -> str:
    """Generates the polished application-wide Qt stylesheet."""
    (
        check_svg,
        check_muted_svg,
        radio_svg,
        radio_muted_svg,
        chevron_svg,
    ) = _control_assets(
        c["accent_text"], c["text_dim"], c["text_secondary"], c["accent"]
    )

    # An ``image: url()`` with no path is invalid QSS and can stop the whole
    # indicator block applying, so each marker declaration is emitted as an
    # empty string when its asset could not be written.
    def marker(path: str) -> str:
        """Return a complete ``image:`` declaration, or nothing at all."""
        return f"    image: url({path});\n" if path else ""

    check_image = marker(check_svg)
    check_muted_image = marker(check_muted_svg)
    radio_image = marker(radio_svg)
    radio_muted_image = marker(radio_muted_svg)
    chevron_image = marker(chevron_svg)
    return f"""
/* Global Reset & Base Typography

   This rule must NOT set a background. A universal `QWidget` background makes
   Qt paint a filled rectangle behind every child label, checkbox and inline
   widget, including ones that should be transparent on a card. The result was
   text sitting on visibly darker "navy" patched rectangles wherever a card
   surface differed from the window surface. Containers opt into a background
   through their own named rules below. */
QWidget {{
    background: transparent;
    color: {c["text_primary"]};
    font-family: "Cantarell", "Inter", "Segoe UI", "system-ui", sans-serif;
    selection-background-color: {c["bg_selected"]};
    selection-color: {c["accent"]};
}}

/* Top-level surfaces paint the window colour; children stay transparent. */
QMainWindow, QDialog {{
    background-color: {c["bg_base"]};
}}

/* Dialog Windows */
QDialog {{
    background-color: {c["bg_base"]};
    color: {c["text_primary"]};
}}

/* Tooltips (Synchronized with Active Theme) */
QToolTip {{
    background-color: {c["bg_card"]};
    color: {c["text_primary"]};
    border: 1px solid {c["border"]};
    border-radius: 4px;
    padding: 5px 9px;
}}

/* --------------------------------------------------------------------------
   Global Button Sizing & Appearance (Dialogs, Drawers & Windows)
   -------------------------------------------------------------------------- */
QPushButton {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    color: {c["text_primary"]};
    padding: 6px 14px;
    min-height: 28px;
    font-weight: 600;
}}

QPushButton:hover {{
    background-color: {c["bg_hover"]};
    border-color: {c["border"]};
    color: {c["text_primary"]};
}}

/* Keyboard focus must be visible, including on the default button of a
   confirmation dialog: a user who tabs to an answer has to be able to see
   which button Enter will press. */
QPushButton:focus {{
    border: 2px solid {c["accent"]};
    padding: 5px 13px;
}}

QPushButton:pressed {{
    background-color: {c["bg_selected"]};
}}

QPushButton:disabled {{
    background-color: {c["bg_input"]};
    border-color: {c["border_subtle"]};
    color: {c["text_dim"]};
}}

/* Primary Accent Action Button (Apply, Commit, Proceed, Undo) */
QPushButton#ApplyButton {{
    background-color: {c["accent"]};
    color: {c["accent_text"]};
    border: 1px solid {c["accent"]};
    border-radius: 6px;
    padding: 5px 16px;
    min-height: 26px;
    font-weight: 700;
}}

/* A destructive confirmation must not wear the same blue as a routine
   "apply". Used for a DNF5 plan that removes protected system packages. */
QPushButton#DangerButton {{
    background-color: {c["danger_bg"]};
    color: {c["danger_text"]};
    border: 1px solid {c["danger_bg"]};
    border-radius: 6px;
    padding: 5px 16px;
    min-height: 26px;
    font-weight: 700;
}}

QPushButton#DangerButton:hover {{
    background-color: {c["danger_hover"]};
    border-color: {c["danger_hover"]};
    color: {c["danger_text"]};
}}

QPushButton#DangerButton:pressed {{
    background-color: {c["danger_pressed"]};
    border-color: {c["danger_pressed"]};
    color: {c["danger_text"]};
}}

QPushButton#DangerButton:focus {{
    border: 2px solid {c["focus_solid_border"]};
    padding: 4px 15px;
    color: {c["danger_text"]};
}}

QPushButton#DangerButton:hover:focus {{
    background-color: {c["danger_hover"]};
    border: 2px solid {c["focus_solid_border"]};
    padding: 4px 15px;
    color: {c["danger_text"]};
}}

QPushButton#DangerButton:disabled {{
    background-color: {c["bg_hover"]};
    border-color: {c["border_subtle"]};
    color: {c["text_dim"]};
}}

QPushButton#ApplyButton:hover {{
    background-color: {c["accent_hover"]};
    border-color: {c["accent_hover"]};
    color: {c["accent_text"]};
}}

QPushButton#ApplyButton:pressed {{
    background-color: {c["accent"]};
    border-color: {c["accent"]};
    color: {c["accent_text"]};
}}

QPushButton#ApplyButton:focus {{
    border: 2px solid {c["focus_solid_border"]};
    padding: 4px 15px;
    color: {c["accent_text"]};
}}

QPushButton#ApplyButton:hover:focus {{
    background-color: {c["accent_hover"]};
    border: 2px solid {c["focus_solid_border"]};
    padding: 4px 15px;
    color: {c["accent_text"]};
}}

QPushButton#ApplyButton:disabled {{
    background-color: {c["bg_hover"]};
    border-color: {c["border_subtle"]};
    color: {c["text_dim"]};
}}

/* --------------------------------------------------------------------------
   Top Header & Control Toolbar
   -------------------------------------------------------------------------- */
QWidget#HeaderContainer {{
    background-color: {c["bg_surface"]};
    border-bottom: 1px solid {c["border"]};
    min-height: 48px;
}}

/* Compact Toolbar Sizing Specifically for Header Bar */
QWidget#HeaderContainer QPushButton {{
    min-height: 26px;
    max-height: 26px;
    border-radius: 6px;
    padding: 0px 10px;
    font-weight: 600;
}}

QWidget#HeaderContainer QPushButton:focus {{
    border: 2px solid {c["accent"]};
    padding: 0px 9px;
}}

QWidget#HeaderContainer QPushButton#ApplyButton {{
    padding: 0px 14px;
    font-weight: 700;
}}

QWidget#HeaderContainer QPushButton#ApplyButton:focus,
QWidget#HeaderContainer QPushButton#ApplyButton:hover:focus {{
    border: 2px solid {c["focus_solid_border"]};
    padding: 0px 13px;
}}

QWidget#HeaderContainer QPushButton#HeaderIconBtn {{
    padding: 0px;
}}

QWidget#HeaderContainer QPushButton#HeaderIconBtn:focus {{
    border: 2px solid {c["accent"]};
    padding: 0px;
}}

QLineEdit#SearchBar {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    padding: 0px 10px;
    min-height: 26px;
    max-height: 26px;
    color: {c["text_primary"]};
    selection-background-color: {c["accent"]};
    selection-color: {c["accent_text"]};
}}

QLineEdit#SearchBar:hover {{
    border: 1px solid {c["text_dim"]};
}}

QLineEdit#SearchBar:focus {{
    border: 2px solid {c["accent"]};
    padding: 0px 9px;
    background-color: {c["bg_input"]};
}}

/* Real-Time Filter Syntax Validation Feedback */
QLineEdit#SearchBar[filterState="valid"] {{
    border: 1px solid {c["badge_fg_installed"]};
}}

QLineEdit#SearchBar[filterState="valid"]:focus {{
    border: 2px solid {c["badge_fg_installed"]};
    padding: 0px 9px;
}}

QLineEdit#SearchBar[filterState="invalid"] {{
    border: 1px solid {c["badge_fg_tag"]};
}}

QLineEdit#SearchBar[filterState="invalid"]:focus {{
    border: 2px solid {c["badge_fg_tag"]};
    padding: 0px 9px;
}}

/* Header Secondary Tool Buttons (Grouped Cluster) */
QPushButton#HeaderToolBtn {{
    background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    color: {c["text_secondary"]};
    padding: 0px 10px;
    font-weight: 600;
}}

/* Header Compact Icon-Only Buttons (Sidebar Toggle & Theme Toggle) */
QPushButton#HeaderIconBtn {{
    background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    color: {c["text_secondary"]};
    padding: 0px;
}}

QPushButton#HeaderToolBtn:hover,
QPushButton#HeaderIconBtn:hover {{
    background-color: {c["bg_hover"]};
    border: 1px solid {c["border"]};
    color: {c["text_primary"]};
}}

QPushButton#HeaderToolBtn:focus,
QWidget#HeaderContainer QPushButton#HeaderToolBtn:focus {{
    border: 2px solid {c["accent"]};
    color: {c["text_primary"]};
    padding: 0px 9px;
}}

QPushButton#HeaderIconBtn:focus {{
    border: 2px solid {c["accent"]};
    color: {c["text_primary"]};
    padding: 0px;
}}

QPushButton#HeaderToolBtn:pressed,
QPushButton#HeaderIconBtn:pressed {{
    background-color: {c["bg_selected"]};
}}

/* Dynamic Update Indicator Button */
QPushButton#UpdatesIndicatorBtn {{
    background-color: {c["badge_bg_queued_in"]};
    border: 1px solid {c["badge_border_queued_in"]};
    color: {c["badge_fg_queued_in"]};
    font-weight: 700;
    padding: 0px 12px;
}}

QPushButton#UpdatesIndicatorBtn:hover {{
    background-color: {c["badge_fg_queued_in"]};
    color: {c["bg_base"]};
}}

QWidget#HeaderContainer QPushButton#UpdatesIndicatorBtn {{
    padding: 0px 12px;
}}

QPushButton#UpdatesIndicatorBtn:focus,
QWidget#HeaderContainer QPushButton#UpdatesIndicatorBtn:focus {{
    border: 2px solid {c["accent"]};
    padding: 0px 11px;
}}

QPushButton#UpdatesIndicatorBtn:hover:focus,
QWidget#HeaderContainer QPushButton#UpdatesIndicatorBtn:hover:focus {{
    background-color: {c["badge_fg_queued_in"]};
    border: 2px solid {c["accent"]};
    color: {c["bg_base"]};
    padding: 0px 11px;
}}

/* --------------------------------------------------------------------------
   Host Mode Safety Banner (Fedora Atomic / Read-Only /usr / Container)
   -------------------------------------------------------------------------- */
QFrame#HostModeBanner {{
    background-color: {c["bg_card"]};
    border: none;
    border-bottom: 1px solid {c["border"]};
}}

QLabel#HostModeBannerTitle {{
    background: transparent;
    color: {c["text_primary"]};
    font-weight: 700;
}}

QLabel#HostModeBannerDetail {{
    background: transparent;
    color: {c["text_secondary"]};
}}

QLabel#HostModeBannerStatus {{
    background: transparent;
    color: {c["accent"]};
    font-weight: 600;
}}

/* --------------------------------------------------------------------------
   Sidebar Navigation
   -------------------------------------------------------------------------- */
QListWidget#SidebarList {{
    background-color: {c["bg_surface"]};
    border: 2px solid transparent;
    border-right: 1px solid {c["border"]};
    padding: 6px 4px;
    outline: none;
}}

QListWidget#SidebarList:focus {{
    border: 2px solid {c["accent"]};
    padding: 6px 4px;
}}

QListWidget#SidebarList::item {{
    border: none;
    border-radius: 6px;
}}

/* --------------------------------------------------------------------------
   Package Tree View (Explicit Viewport & Item Styling)
   -------------------------------------------------------------------------- */
QTreeView, QTreeView#PackageTreeView {{
    background-color: {c["bg_base"]};
    alternate-background-color: {c["bg_base"]};
    color: {c["text_primary"]};
    border: 2px solid transparent;
    outline: none;
    padding: 0px;
    show-decoration-selected: 1;
}}

QTreeView#PackageTreeView:focus {{
    border: 2px solid {c["accent"]};
}}

QTreeView::item {{
    background-color: transparent;
    color: {c["text_primary"]};
    border: none;
    border-radius: 4px;
}}

QTreeView::item:hover {{
    background-color: {c["bg_hover"]};
    color: {c["text_primary"]};
}}

QTreeView::item:selected {{
    background-color: {c["bg_selected"]};
    color: {c["accent"]};
}}

QHeaderView::section {{
    background-color: {c["bg_surface"]};
    color: {c["text_secondary"]};
    padding: 8px 12px;
    border: none;
    border-bottom: 1px solid {c["border"]};
    border-right: 1px solid {c["border_subtle"]};
    font-weight: 700;
    letter-spacing: 0.3px;
    text-transform: uppercase;
}}

QHeaderView::section:hover {{
    background-color: {c["bg_hover"]};
    color: {c["accent"]};
}}

/* --------------------------------------------------------------------------
   Inspector Side Panel & Detail Dashboard
   -------------------------------------------------------------------------- */
QWidget#InspectorPanel {{
    background-color: {c["bg_surface"]};
    border-left: 1px solid {c["border"]};
}}

QScrollArea#InspectorHostScroll,
QScrollArea#InspectorPanelScroll,
QScrollArea#InspectorOverviewScroll {{
    background: transparent;
    border: none;
}}

/* The transaction drawer is a plain QWidget inside the workspace splitter, so
   it is not a window and does not inherit a window surface. Without a rule of
   its own its background stayed unpainted. */
QWidget#TransactionDrawer {{
    background-color: {c["bg_surface"]};
    border-left: 1px solid {c["border"]};
}}

QLabel#InspectorPkgTitle {{
    font-weight: 800;
    color: {c["text_primary"]};
}}

/* --------------------------------------------------------------------------
   Inspector metadata card

   Three labelled groups of field/value cells. The field name is small, muted
   and uppercase; the value carries full contrast, so the card reads as a table
   rather than as one run of "Label: value" sentences.
   -------------------------------------------------------------------------- */
QFrame#StatGridCard {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QLabel#StatGroupTitle {{
    color: {c["text_dim"]};
    font-weight: 700;
    letter-spacing: 0.6px;
    text-transform: uppercase;
}}

QLabel#StatFieldName {{
    color: {c["text_dim"]};
    font-weight: 600;
    letter-spacing: 0.3px;
}}

QLabel#StatFieldValue {{
    color: {c["text_primary"]};
    font-weight: 600;
}}

/* A value the host did not report is muted, so an absent fact does not read as
   an emphatic one. */
QLabel#StatFieldValue[muted="true"] {{
    color: {c["text_dim"]};
    font-weight: 500;
}}

/* The update cell doubles as a status chip. The frame carries the fill and the
   outline; the text label inside it stays transparent. The chip's padding is
   layout margin in code, not stylesheet padding: padding here would not feed the
   label's size hint, so the text would measure one line and paint two. */
QFrame#StatStatusChip {{
    border: 1px solid transparent;
    border-radius: 6px;
    background: transparent;
}}

QFrame#StatStatusChip[tone="current"] {{
    background-color: {c["badge_bg_installed"]};
    border: 1px solid {c["badge_border_installed"]};
}}

QFrame#StatStatusChip[tone="current"] QLabel#StatFieldValue {{
    color: {c["badge_fg_installed"]};
}}

QFrame#StatStatusChip[tone="update"] {{
    background-color: {c["badge_bg_update"]};
    border: 1px solid {c["badge_border_update"]};
}}

QFrame#StatStatusChip[tone="update"] QLabel#StatFieldValue {{
    color: {c["badge_fg_update"]};
}}

/* The chip must stay outlined even when the tone resolves to no fill, or an
   unknown state would read as floating text rather than as a status. */
QFrame#StatStatusChip[tone="unknown"] {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
}}

QFrame#StatStatusChip[tone="unknown"] QLabel#StatFieldValue {{
    color: {c["text_dim"]};
}}

/* Rule between the card's groups. Matches InspectorCardDivider above: the
   subtle border is within 1.0 contrast of the card surface on dark and reads as
   nothing at all, so the regular border carries these hairlines. */
QFrame#StatSeparator {{
    background-color: {c["border"]};
    border: none;
}}

/* --------------------------------------------------------------------------
   Inspector section cards
   -------------------------------------------------------------------------- */
QFrame#InspectorSectionCard {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QLabel#InspectorSectionTitle {{
    color: {c["text_dim"]};
    font-weight: 700;
    letter-spacing: 0.5px;
    text-transform: uppercase;
}}

/* A hairline separator inside a card, used to set the assessment apart from
   the justification beneath it without introducing a second bold heading. */
QFrame#InspectorCardDivider {{
    background-color: {c["border"]};
    border: none;
}}

QLabel#InspectorBasisLabel {{
    color: {c["text_secondary"]};
    font-weight: 600;
}}

QLabel#InspectorPackagerLabel {{
    color: {c["text_dim"]};
}}

/* Centred note shown while a tab's content surface has nothing in it yet. */
QLabel#InspectorEmptyState {{
    color: {c["text_dim"]};
    padding: 24px 16px;
}}

QLabel#InspectorSummary {{
    color: {c["text_secondary"]};
    line-height: 1.4;
}}

QLabel#AIRationaleLabel {{
    color: {c["text_secondary"]};
    line-height: 1.4;
}}

/* Tabbed surfaces - the inspector dock and the settings dialog.

   Both are the same control and must look like it. The settings dialog's tab
   widget carried no object name, so it matched no rule here and fell back to
   Qt's native frame: an unstyled strip above the pane, and a 1px seam of the
   dialog background showing between the tabs and the content.

   The pane's top border is dropped because the selected tab sits directly on
   it and already draws that edge; keeping both left a hairline seam. */
QTabWidget#InspectorTabs::pane,
QTabWidget#SettingsTabs::pane {{
    border: 1px solid {c["border"]};
    border-top: none;
    border-bottom-left-radius: 6px;
    border-bottom-right-radius: 6px;
    background-color: {c["bg_card"]};
}}

/* Tabs are flat-topped.

   They used to round their top corners, while the selected tab drew a straight
   2px accent `border-top`. Qt cannot follow a rounded corner with a straight
   top border: it painted the accent as a thin arc floating above the tab and
   then drew the rounded body inside it, so the active tab read as two shapes.
   The same rounding also rounded the *bottom* corners, which sit on the pane
   and cannot show a corner at all -- that is what left notches between the tabs
   and the content below.

   Square tabs meet the pane in a straight line, and the accent border sits on
   the tab's own top edge where it belongs. */
QTabBar::tab {{
    background-color: {c["bg_input"]};
    color: {c["text_secondary"]};
    padding: 7px 12px;
    border: none;
    border-top: 2px solid transparent;
    border-left: 2px solid transparent;
    border-right: 2px solid transparent;
    margin-right: 2px;
    font-weight: 600;
    text-transform: none;
    letter-spacing: 0px;
}}

/* The inspector is a narrow, resizable dock. Its five tabs must keep their
   complete labels at the dock's 360px minimum width instead of collapsing
   into unreadable stubs behind scroll arrows. */
/* No font-size here on purpose. Pinning a point size in the stylesheet makes
   Qt size the tab strips from the stylesheet font while painting them with the
   application font, so the two disagree and the labels elide. The tab bar is
   given one concrete QFont in code instead, and the dock minimum width is
   derived from that same font's metrics. */
QTabWidget#InspectorTabs > QTabBar::tab {{
    padding: 6px 6px;
    margin-right: 2px;
    font-weight: 600;
    text-transform: none;
    letter-spacing: 0px;
}}

QTabBar::tab:selected {{
    background-color: {c["bg_card"]};
    color: {c["accent"]};
    border-top: 2px solid {c["accent"]};
}}

QTabBar::tab:hover:!selected {{
    background-color: {c["bg_hover"]};
    color: {c["text_primary"]};
}}

QTabBar::tab:focus {{
    background-color: {c["bg_hover"]};
    color: {c["text_primary"]};
    border-top: 2px solid {c["accent"]};
    border-left: 2px solid {c["accent"]};
    border-right: 2px solid {c["accent"]};
}}

QTabBar::tab:selected:focus {{
    background-color: {c["bg_card"]};
    color: {c["accent"]};
    border-top: 2px solid {c["accent"]};
    border-left: 2px solid {c["accent"]};
    border-right: 2px solid {c["accent"]};
}}

QTextEdit#InspectorDescText {{
    background-color: {c["bg_card"]};
    border: none;
    color: {c["text_primary"]};
    line-height: 1.5;
}}

/* Every content surface on a tab is a rounded card, so switching tabs does not
   change the visual language of the dock.

   The manifest is one card holding a scrolling path column and a pinned size
   column, plus the path column's bar, and the card draws the outline for all of
   them. The two halves used to draw their own half-frames and butt against each
   other -- the path table keeping its left, top and bottom edges, the Size view
   keeping its right edge -- and two separate frames can never produce one
   straight line: the band between them had to be filled and bordered by hand to
   hide the join. One container with one border is the same card with none of
   that bookkeeping, and it cannot come apart. */
QWidget#InspectorFilesCard {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QTableWidget#InspectorFilesTable {{
    background-color: {c["bg_card"]};
    alternate-background-color: {c["bg_surface"]};
    border: none;
    color: {c["text_primary"]};
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
    gridline-color: transparent;
}}

/* The manifest's own scrollbars. The generic 6px bar is too small and too
   close to the card colour to read against this card, and the file list is the
   one place where the reader is genuinely scrolling a long list, so both axes
   get a wider track and a brighter handle.

   Both orientations are spelled out here, and with identical geometry and
   colours, because a rule that names only one axis leaves the other on the
   generic 6px/`border` styling beside it. That is what the caller saw after
   2.19.0: a wide bright bar next to a thin dim one, on the same card. Each
   orientation needs its own declaration -- `width` sizes a vertical bar and
   `height` sizes a horizontal one -- so the pair is kept symmetrical by hand
   and pinned by a test.

   Each bar is named and matched by that name. The horizontal one is laid out by
   the card's grid rather than by the table, so a `#InspectorFilesTable
   QScrollBar` descendant selector would no longer reach it at all, and the bar
   would silently fall back to the generic 6px styling. */
QScrollBar#InspectorFilesHScroll {{
    background: {c["bg_card"]};
    height: 10px;
    margin: 0px;
    border-radius: 5px;
}}

QScrollBar#InspectorFilesVScroll {{
    background: {c["bg_card"]};
    width: 10px;
    margin: 0px;
    border-radius: 5px;
}}

QScrollBar#InspectorFilesHScroll::handle,
QScrollBar#InspectorFilesVScroll::handle {{
    background-color: {c["text_dim"]};
    min-height: 28px;
    min-width: 28px;
    border-radius: 4px;
    margin: 2px;
}}

QScrollBar#InspectorFilesHScroll::handle:hover,
QScrollBar#InspectorFilesVScroll::handle:hover {{
    background-color: {c["text_secondary"]};
}}

QScrollBar#InspectorFilesVScroll::add-line,
QScrollBar#InspectorFilesVScroll::sub-line {{
    height: 0px;
}}

QScrollBar#InspectorFilesHScroll::add-line,
QScrollBar#InspectorFilesHScroll::sub-line {{
    width: 0px;
}}

QTableView#InspectorFilesSizeView {{
    background-color: {c["bg_card"]};
    alternate-background-color: {c["bg_surface"]};
    border: none;
    color: {c["text_primary"]};
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
    gridline-color: transparent;
}}

QTableWidget#InspectorFilesTable QTableCornerButton::section,
QTableView#InspectorFilesSizeView QTableCornerButton::section {{
    background-color: {c["bg_card"]};
    border: none;
}}

QListWidget#InspectorReverseList {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    color: {c["text_primary"]};
    padding: 4px;
}}

/* The dependent list is the whole body of its tab, so its rows need to read as
   rows: separated, padded, and highlighted on hover rather than running together
   as one block of text. */
QListWidget#InspectorReverseList::item {{
    border-radius: 5px;
    padding: 6px 8px;
    margin: 1px 2px;
}}

QListWidget#InspectorReverseList::item:hover {{
    background-color: {c["bg_hover"]};
}}

QListWidget#InspectorReverseList::item:selected {{
    background-color: {c["bg_input"]};
    color: {c["accent"]};
}}

QTextBrowser#InspectorChangelogBrowser, QTextBrowser#InspectorScriptletsBrowser {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    color: {c["text_primary"]};
    font-family: "JetBrains Mono", "Fira Code", "Consolas", monospace;
    padding: 8px;
}}

/* The advisory detail pane renders a description and a list of clickable
   references. It is its own rule rather than joining the console group below
   for two reasons: a console is monospaced and this is prose, and the anchors
   need an explicit colour. Qt's default link colour is pure #0000ff, which is
   1.71:1 against the dark card -- the same failure as text that is barely
   there. A Qt stylesheet cannot colour document anchors, so the document
   format itself sets the link colour from the theme accent (6.99:1 dark,
   5.45:1 light against the same surface) -- see
   ``SecurityAdvisoryDialog._detail_document``. */
QTextBrowser#AdvisoryDetail {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    color: {c["text_primary"]};
    padding: 8px;
    selection-background-color: {c["bg_selected"]};
    selection-color: {c["text_primary"]};
}}

/* Severity is carried by the row's text colour, which the table paints from
   the delegate palette; these rules only keep the table itself consistent with
   the other data tables in the window. */
QTableWidget#AdvisoryTable {{
    background-color: {c["bg_card"]};
    alternate-background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    gridline-color: transparent;
}}

/* Dynamic Theme-Adaptive Consoles & Terminal Outputs */
QTextEdit#ConsoleOutput,
QTextEdit#LocalRpmReqBox,
QTextEdit#TransactionPlanText {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    color: {c["text_primary"]};
}}

QTextEdit#ConsoleOutput:focus,
QTextEdit#LocalRpmReqBox:focus,
QTextEdit#TransactionPlanText:focus,
QTextBrowser#AdvisoryDetail:focus,
QTextBrowser#InspectorChangelogBrowser:focus,
QTextBrowser#InspectorScriptletsBrowser:focus {{
    border: 2px solid {c["accent"]};
}}

/* Theme-Adaptive Dialog, Drawer, Header & Inspector Semantic Labels */
QPushButton#HeaderDangerBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 10px;
    color: {c["badge_fg_missing"]};
    font-weight: 600;
}}

QPushButton#HeaderDangerBtn:hover {{
    background-color: {c["badge_bg_missing"]};
    border: 1px solid {c["badge_border_missing"]};
    color: {c["badge_fg_missing"]};
}}

QPushButton#HeaderDangerBtn:focus {{
    border: 2px solid {c["accent"]};
    padding: 4px 9px;
}}

QWidget#HeaderContainer QPushButton#HeaderDangerBtn:focus,
QWidget#HeaderContainer QPushButton#HeaderDangerBtn:hover:focus {{
    border: 2px solid {c["accent"]};
    padding: 0px 9px;
}}

QLabel#AboutFieldName {{
    font-weight: bold;
}}

QLabel#DialogTitleLabel {{
    color: {c["accent"]};
    font-weight: bold;
}}

QLabel#DrawerTitleLabel {{
    color: {c["accent"]};
    font-weight: bold;
}}

QLabel#DrawerSummaryLabel {{
    color: {c["text_secondary"]};
}}

QLabel#SubtleHelpLabel, QLabel#AIProvenanceLabel {{
    color: {c["text_secondary"]};
}}

QLabel#LocalRpmFieldName {{
    font-weight: bold;
}}

QLabel#LocalRpmSummaryLabel {{
    color: {c["text_secondary"]};
}}

QLabel#LocalRpmStatusNotice[statusKind="installed"] {{
    background-color: {c["badge_bg_queued_in"]};
    border: 1px solid {c["badge_border_queued_in"]};
    color: {c["badge_fg_queued_in"]};
    border-radius: 6px;
    padding: 8px;
}}

QLabel#LocalRpmStatusNotice[statusKind="available"] {{
    background-color: {c["badge_bg_installed"]};
    border: 1px solid {c["badge_border_installed"]};
    color: {c["badge_fg_installed"]};
    border-radius: 6px;
    padding: 8px;
}}

/* Progress Bar */
QProgressBar {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 4px;
    text-align: center;
    color: {c["text_primary"]};
}}

QProgressBar::chunk {{
    background-color: {c["accent"]};
    border-radius: 3px;
}}

/* Cards & Frames */
QFrame#AICard, QFrame#StatsFrame {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

/* --------------------------------------------------------------------------
   Checkboxes and radio buttons

   The native Fusion indicator is drawn from palette roles Qt still resolves
   from a light scheme, so on a dark surface the unchecked box rendered as
   almost the same colour as its background (measured 1.05:1). The indicator is
   therefore drawn explicitly: an outlined well that reads at any font size,
   with a checkmark pushed in through an inline SVG image so the checked state
   cannot be confused with the unchecked one.
   -------------------------------------------------------------------------- */
QCheckBox, QRadioButton {{
    spacing: 8px;
    color: {c["text_primary"]};
    min-height: 18px;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 0px 2px;
}}

QCheckBox:focus, QRadioButton:focus {{
    border: 1px solid {c["accent"]};
    color: {c["accent"]};
}}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {c["border_strong"]};
    background-color: {c["bg_input"]};
}}

QCheckBox::indicator {{
    border-radius: 4px;
}}

QRadioButton::indicator {{
    border-radius: 8px;
}}

QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {c["accent"]};
    background-color: {c["bg_hover"]};
}}

QCheckBox::indicator:focus, QRadioButton::indicator:focus {{
    width: 14px;
    height: 14px;
    border: 2px solid {c["accent"]};
    background-color: {c["bg_hover"]};
}}

QCheckBox::indicator:checked {{
    background-color: {c["accent"]};
    border-color: {c["accent"]};
{check_image}}}

QCheckBox::indicator:checked:hover {{
    background-color: {c["accent_hover"]};
    border-color: {c["accent_hover"]};
}}

QCheckBox::indicator:checked:focus {{
    width: 14px;
    height: 14px;
    border: 2px solid {c["focus_solid_border"]};
    background-color: {c["accent"]};
{check_image}}}

/* A filled accent dot inside a ring, rather than a thick border which Qt
   renders as a square-cornered blob at this size. */
QRadioButton::indicator:checked {{
    border: 2px solid {c["accent"]};
    background-color: {c["bg_input"]};
{radio_image}}}

QRadioButton::indicator:checked:focus {{
    width: 14px;
    height: 14px;
    border: 2px solid {c["focus_solid_border"]};
    background-color: {c["bg_input"]};
{radio_image}}}

QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {c["border_subtle"]};
    background-color: {c["bg_base"]};
}}

QCheckBox::indicator:checked:disabled {{
    background-color: {c["bg_hover"]};
    border-color: {c["border_subtle"]};
{check_muted_image}}}

QRadioButton::indicator:checked:disabled {{
    border-color: {c["text_dim"]};
    background-color: {c["bg_input"]};
{radio_muted_image}}}

QCheckBox:disabled, QRadioButton:disabled {{
    color: {c["text_dim"]};
}}

/* --------------------------------------------------------------------------
   Drop-downs

   Without explicit sizing a QComboBox collapses to the font's own height
   (~22px) while every themed button is 34px, which read as a "too short"
   control. Fixed minimum height, explicit padding and a themed chevron keep it
   consistent with the buttons and inputs around it.
   -------------------------------------------------------------------------- */
QComboBox {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    color: {c["text_primary"]};
    padding: 0px 10px;
    min-height: 30px;
    min-width: 140px;
}}

QComboBox:hover {{
    border-color: {c["text_dim"]};
}}

QComboBox:focus {{
    border: 2px solid {c["accent"]};
    padding: 0px 9px;
}}

QComboBox:disabled {{
    background-color: {c["bg_base"]};
    border-color: {c["border_subtle"]};
    color: {c["text_dim"]};
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 26px;
    border-left: 1px solid {c["border"]};
}}

QComboBox::down-arrow {{
{chevron_image}    width: 12px;
    height: 12px;
}}

QComboBox QAbstractItemView {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    color: {c["text_primary"]};
    selection-background-color: {c["bg_selected"]};
    selection-color: {c["accent"]};
    padding: 4px;
    outline: none;
}}

/* Text inputs share the combo's height so a form never mixes tall and short
   controls on the same row. */
QLineEdit {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    color: {c["text_primary"]};
    padding: 0px 8px;
    min-height: 30px;
    selection-background-color: {c["accent"]};
    selection-color: {c["accent_text"]};
}}

QLineEdit:focus {{
    border: 2px solid {c["accent"]};
    padding: 0px 7px;
}}

QLineEdit:disabled {{
    background-color: {c["bg_base"]};
    border-color: {c["border_subtle"]};
    color: {c["text_dim"]};
}}

/* Repository Dialog capability and direct-configuration controls */
QFrame#CoprBox, QFrame#RepoAddBox, QFrame#RepoOperationPanel {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QLabel#RepoOperationStatus {{
    color: {c["text_primary"]};
    font-weight: 600;
}}

/* These fields sit in the same row as a button -- "+ Add Repo" is beside the
   address box -- so they must be as tall as one. They carried their own shorter
   min-height, which left the field 4px shorter than the button, so the button
   stood proud of the row on both edges.

   A styled field's height is its min-height plus its padding, independent of the
   font, so matching this value to the field rules above is all it takes. */
QLineEdit#RepoAddressInput, QLineEdit#RepoFilterInput {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    padding: 5px 12px;
    min-height: 30px;
    color: {c["text_primary"]};
    selection-background-color: {c["accent"]};
    selection-color: {c["accent_text"]};
}}

QLineEdit#RepoAddressInput:hover, QLineEdit#RepoFilterInput:hover {{
    border: 1px solid {c["text_dim"]};
}}

QLineEdit#RepoAddressInput:focus, QLineEdit#RepoFilterInput:focus {{
    border: 2px solid {c["accent"]};
    padding: 4px 11px;
    background-color: {c["bg_input"]};
}}

/* Compact Close Buttons */
QPushButton#InspectorCloseBtn, QPushButton#DrawerCloseBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    /* The generic QPushButton rule adds 14px horizontal padding and a 28px
       minimum height. Both would defeat the fixed 26-28px square these
       compact close buttons ask for, so the fallback glyph disappears. */
    padding: 0px;
    min-height: 0px;
    color: {c["text_dim"]};
    font-weight: 700;
}}

QPushButton#InspectorCloseBtn:hover, QPushButton#DrawerCloseBtn:hover {{
    background-color: {c["bg_hover"]};
    border: 1px solid {c["border"]};
    color: {c["text_primary"]};
}}

QPushButton#InspectorCloseBtn:focus, QPushButton#DrawerCloseBtn:focus {{
    background-color: {c["bg_hover"]};
    border: 2px solid {c["accent"]};
    color: {c["text_primary"]};
    padding: 0px;
}}

/* Dynamic Action Button in Inspector Panel */
QPushButton#InspectorQueueBtn[queueState="installed"] {{
    background-color: {c["badge_bg_queued_rm"]};
    color: {c["badge_fg_queued_rm"]};
    border: 1px solid {c["badge_border_queued_rm"]};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="queued_remove"] {{
    background-color: {c["bg_selected"]};
    color: {c["badge_fg_queued_in"]};
    border: 1px solid {c["border"]};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="queued_upgrade"] {{
    background-color: {c["bg_selected"]};
    color: {c["badge_fg_queued_in"]};
    border: 1px solid {c["border"]};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="available"] {{
    background-color: {c["badge_bg_installed"]};
    color: {c["badge_fg_installed"]};
    border: 1px solid {c["badge_border_installed"]};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn[queueState="queued_install"] {{
    background-color: {c["bg_selected"]};
    color: {c["badge_fg_queued_in"]};
    border: 1px solid {c["border"]};
    font-weight: 700;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton#InspectorQueueBtn:focus,
QPushButton#InspectorQueueBtn[queueState="installed"]:focus,
QPushButton#InspectorQueueBtn[queueState="queued_remove"]:focus,
QPushButton#InspectorQueueBtn[queueState="queued_upgrade"]:focus,
QPushButton#InspectorQueueBtn[queueState="available"]:focus,
QPushButton#InspectorQueueBtn[queueState="queued_install"]:focus {{
    border: 2px solid {c["accent"]};
    padding: 6px 13px;
}}

/* --------------------------------------------------------------------------
   Tables (Repo Dialog, History Dialog, Files)
   -------------------------------------------------------------------------- */
QTableWidget {{
    background-color: {c["bg_card"]};
    /* Qt's default alternate-row colour is a fixed light grey. Without this,
       alternating rows of a Dark-mode table render as light grey strips with
       near-white text. */
    alternate-background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    color: {c["text_primary"]};
    gridline-color: {c["border_subtle"]};
}}

QTableWidget:focus,
QTableWidget#AdvisoryTable:focus,
QListWidget#InspectorReverseList:focus {{
    border: 2px solid {c["accent"]};
}}

QTableWidget QTableCornerButton::section {{
    background-color: {c["bg_surface"]};
    border: none;
}}

/* Nine metadata columns must fit the Flatpak dialog's default width without
   cutting the last heading, so this table uses tighter cell padding. */
QTableWidget#FlatpakInventoryTable::item {{
    padding: 6px 7px;
}}

QTableWidget#FlatpakInventoryTable QHeaderView::section {{
    padding: 8px 7px;
}}

QTableWidget::item {{
    padding: 6px 10px;
}}

QTableWidget::item:selected {{
    background-color: {c["bg_selected"]};
    color: {c["accent"]};
}}

/* --------------------------------------------------------------------------
   Scrollbars (Minimal Floating Rounded Thumb)
   -------------------------------------------------------------------------- */
QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 2px 0px 2px 0px;
    border-radius: 3px;
}}

QScrollBar::handle:vertical {{
    background-color: {c["border"]};
    min-height: 28px;
    border-radius: 3px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {c["text_dim"]};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
    margin: 0px 2px 0px 2px;
    border-radius: 3px;
}}

QScrollBar::handle:horizontal {{
    background-color: {c["border"]};
    min-width: 28px;
    border-radius: 3px;
}}

QScrollBar::handle:horizontal:hover {{
    background-color: {c["text_dim"]};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

/* --------------------------------------------------------------------------
   Menus & Popup Overlays
   -------------------------------------------------------------------------- */
QMenu {{
    background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    padding: 6px;
}}

QMenu::item {{
    padding: 7px 24px 7px 12px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background-color: {c["bg_selected"]};
    color: {c["accent"]};
}}

/* --------------------------------------------------------------------------
   Status Bar & Splitters
   -------------------------------------------------------------------------- */
QStatusBar {{
    background-color: {c["bg_input"]};
    border-top: 1px solid {c["border"]};
    color: {c["text_dim"]};
    padding: 4px 8px;
}}

QSplitter::handle {{
    background-color: {c["border_subtle"]};
}}

QSplitter::handle:hover {{
    background-color: {c["accent"]};
}}
"""


# =============================================================================


__all__ = ("_control_assets", "build_stylesheet")
