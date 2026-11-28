# dendro/ui/fonts.py
"""Font sizing derived from the user's own font, never from the stylesheet.

A ``font-size`` in a Qt style sheet is absolute: it does not follow the font a
user configured through their desktop's accessibility or appearance settings.
A QSS-declared 11px label keeps rendering at 11px when the user raises their
font to 16pt, so the surrounding chrome grows while the text inside it does
not. That fails WCAG 2.1 SC 1.4.4 (Resize text), and it also desynchronizes
measurement from painting: Qt sizes a widget from the widget font while the
style sheet paints with its own, which is what elides labels that should fit.

Sizes are therefore expressed here as an offset from the application font and
applied as a real ``QFont``. The offset preserves a visual hierarchy while the
base size still comes from the user's setting.
"""

from __future__ import annotations

from typing import Final

from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import QApplication, QWidget

# Every size the interface uses, as a point offset from the application font.
#
# The previous style sheet expressed these in pixels at the nominal 96 dpi Qt
# assumed: a 16px title, a 12px body, 11px detail and a 10px overline. Pixels
# and points relate by 1px = 0.75pt, so the 12px body was 9pt and the title was
# 12pt. Expressing the body as an offset of zero is what keeps the type scale
# looking unchanged at the default font while letting every size grow with the
# user's setting, which the fixed pixels never did.
TITLE_PT_DELTA = 3
HEADING_PT_DELTA = 2
BODY_PT_DELTA = 0
DETAIL_PT_DELTA = -1
OVERLINE_PT_DELTA = -1

# Smallest point size worth rendering; below this text stops being legible.
MINIMUM_POINT_SIZE = 6

# The conventional body size, matching the type scale the interface was designed
# at. Only used when the application font reports neither points nor pixels.
DEFAULT_POINT_SIZE = 9.0


def application_font_base() -> QFont:
    """Return the font the application was configured with.

    ``QApplication.font()`` is what the desktop's font setting resolves to, so
    deriving every size from it is what makes the interface follow that setting.
    """
    app = QApplication.instance()
    if app is None:
        return QFont()
    return QFont(app.font())


def _point_size(base: QFont, delta: int) -> float:
    """Resolve one offset against the base font's point size.

    A platform may specify its application font in pixels instead of points. In
    that case ``pointSizeF()`` reports -1 and the pixel size is the only real
    measurement, so it becomes the baseline; substituting a fixed point size
    would guess larger than the user's own setting and overflow layouts that
    budget height for the default font.
    """
    size = base.pointSizeF()
    if size <= 0:
        pixels = base.pixelSize()
        # 96 dpi, the reference density Qt itself uses, relates px and pt by
        # 1px = 0.75pt. Only a starting point: ``scaled_font`` keeps the pixel
        # size authoritative when the base was pixel-measured.
        size = pixels * 0.75 if pixels > 0 else DEFAULT_POINT_SIZE
    return max(MINIMUM_POINT_SIZE, size + delta)


def scaled_font(
    delta: int = 0,
    *,
    weight: QFont.Weight | int | None = None,
    bold: bool = False,
    letter_spacing: float | None = None,
    base: QFont | None = None,
) -> QFont:
    """Return the application font resized by ``delta`` points.

    ``base`` lets a caller derive from a font it already resolved, so nested
    offsets stay relative to one another instead of compounding.
    """
    font = QFont(base if base is not None else application_font_base())
    if font.pointSizeF() > 0:
        font.setPointSizeF(_point_size(font, delta))
    else:
        # The base was measured in pixels. Stay in pixels so the size keeps
        # following the user's setting instead of being re-derived from an
        # assumed screen density: one point is 4/3 px at Qt's 96 dpi reference.
        pixels = font.pixelSize()
        baseline = pixels if pixels > 0 else DEFAULT_POINT_SIZE / 0.75
        font.setPixelSize(max(MINIMUM_POINT_SIZE, round(baseline + delta / 0.75)))
    if bold:
        font.setWeight(QFont.Weight.Bold)
    elif weight is not None:
        font.setWeight(QFont.Weight(weight))
    if letter_spacing is not None:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, float(letter_spacing))
    return font


def apply_scaled_font(
    widget: QWidget,
    delta: int = 0,
    *,
    weight: QFont.Weight | int | None = None,
    bold: bool = False,
    letter_spacing: float | None = None,
) -> QFont:
    """Apply :func:`scaled_font` to ``widget`` and return the font used."""
    font = scaled_font(delta, weight=weight, bold=bold, letter_spacing=letter_spacing)
    widget.setFont(font)
    return font


def scaled_fixed_font(
    delta: int = 0,
    *,
    weight: QFont.Weight | int | None = None,
    bold: bool = False,
    letter_spacing: float | None = None,
) -> QFont:
    """Return the system fixed-width font scaled with the application font."""
    from PyQt6.QtGui import QFontDatabase

    fixed = QFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    app_base = application_font_base()
    if app_base.pointSizeF() > 0:
        fixed.setPointSizeF(_point_size(app_base, delta))
    elif app_base.pixelSize() > 0:
        fixed.setPixelSize(
            max(MINIMUM_POINT_SIZE, round(app_base.pixelSize() + delta / 0.75))
        )
    elif fixed.pointSizeF() > 0:
        fixed.setPointSizeF(_point_size(fixed, delta))
    else:
        pixels = fixed.pixelSize()
        baseline = pixels if pixels > 0 else DEFAULT_POINT_SIZE / 0.75
        fixed.setPixelSize(max(MINIMUM_POINT_SIZE, round(baseline + delta / 0.75)))
    if bold:
        fixed.setWeight(QFont.Weight.Bold)
    elif weight is not None:
        fixed.setWeight(QFont.Weight(weight))
    if letter_spacing is not None:
        fixed.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, float(letter_spacing))
    return fixed


def style_text(
    widget: QWidget,
    *,
    delta: int = 0,
    weight: QFont.Weight | int | None = None,
    bold: bool = False,
    color: str | None = None,
    padding: str | None = None,
) -> QFont:
    """Size and weight ``widget``'s text from the application font, then tint it.

    Colour and padding stay in a style sheet because they are not metrics, but
    the size and weight deliberately do not: a style sheet's ``font-size`` is
    absolute and would stop tracking the user's font, which is the whole point
    of this module.
    """
    font = apply_scaled_font(widget, delta, weight=weight, bold=bold)
    declarations = []
    if color:
        declarations.append(f"color: {color};")
    if padding:
        declarations.append(f"padding: {padding};")
    widget.setStyleSheet(" ".join(declarations))
    return font


# Object names whose text the style sheet used to size in pixels. The size,
# weight and tracking now live here instead, so they follow the user's font.
# Keeping them in one table rather than at each construction site means a widget
# cannot inherit the right colour and the wrong, inaccessible size by omission.
_LABEL_STYLES: Final[dict[str, tuple[int, QFont.Weight, float | None]]] = {
    "HostModeBannerTitle": (BODY_PT_DELTA, QFont.Weight.Bold, None),
    "HostModeBannerDetail": (DETAIL_PT_DELTA, QFont.Weight.Normal, None),
    "HostModeBannerStatus": (DETAIL_PT_DELTA, QFont.Weight.DemiBold, None),
    "OfflineBannerTitle": (BODY_PT_DELTA, QFont.Weight.Bold, None),
    "OfflineBannerDetail": (DETAIL_PT_DELTA, QFont.Weight.Normal, None),
    "OfflineBannerMeta": (DETAIL_PT_DELTA, QFont.Weight.Normal, None),
    "OfflineBannerBadge": (DETAIL_PT_DELTA, QFont.Weight.Bold, None),
    "InspectorPkgTitle": (TITLE_PT_DELTA, QFont.Weight.ExtraBold, None),
    "StatGroupTitle": (OVERLINE_PT_DELTA, QFont.Weight.Bold, 0.6),
    "StatFieldName": (OVERLINE_PT_DELTA, QFont.Weight.DemiBold, 0.3),
    "StatFieldValue": (BODY_PT_DELTA, QFont.Weight.DemiBold, None),
    "InspectorSectionTitle": (DETAIL_PT_DELTA, QFont.Weight.Bold, 0.5),
    "InspectorBasisLabel": (DETAIL_PT_DELTA, QFont.Weight.DemiBold, None),
    "InspectorPackagerLabel": (DETAIL_PT_DELTA, QFont.Weight.Normal, None),
    "InspectorEmptyState": (BODY_PT_DELTA, QFont.Weight.Normal, None),
    "InspectorSummary": (BODY_PT_DELTA, QFont.Weight.Normal, None),
    "AIRationaleLabel": (DETAIL_PT_DELTA, QFont.Weight.Normal, None),
    "AIProvenanceLabel": (DETAIL_PT_DELTA, QFont.Weight.Normal, None),
    "SubtleHelpLabel": (DETAIL_PT_DELTA, QFont.Weight.Normal, None),
    "LocalRpmSummaryLabel": (BODY_PT_DELTA, QFont.Weight.Normal, None),
    "LocalRpmFieldName": (BODY_PT_DELTA, QFont.Weight.Bold, None),
    "AboutFieldName": (BODY_PT_DELTA, QFont.Weight.Bold, None),
    "SettingsAboutTitle": (TITLE_PT_DELTA, QFont.Weight.Bold, None),
    "SectionHeadingLabel": (BODY_PT_DELTA, QFont.Weight.Bold, None),
    "RepoOperationStatus": (BODY_PT_DELTA, QFont.Weight.DemiBold, None),
    "PlanWarningTitle": (BODY_PT_DELTA, QFont.Weight.ExtraBold, None),
    "PlanSafeLabel": (BODY_PT_DELTA, QFont.Weight.Bold, None),
    "SystemUpgradeStepBadge": (DETAIL_PT_DELTA, QFont.Weight.Bold, None),
    "SystemUpgradeCurrentTitle": (BODY_PT_DELTA, QFont.Weight.Bold, None),
    "SystemUpgradeNoticeText": (BODY_PT_DELTA, QFont.Weight.DemiBold, None),
    "DialogTitleLabel": (HEADING_PT_DELTA, QFont.Weight.Bold, None),
    "DrawerTitleLabel": (HEADING_PT_DELTA, QFont.Weight.Bold, None),
    "DrawerSummaryLabel": (BODY_PT_DELTA, QFont.Weight.Normal, None),
}


def apply_labelled_font(label: QWidget) -> bool:
    """Give ``label`` the size and weight its object name calls for.

    Returns whether the object name had an entry. A widget that is not in the
    table keeps the application font, which is already the accessible default.
    """
    style = _LABEL_STYLES.get(label.objectName())
    if style is None:
        return False
    delta, weight, spacing = style
    apply_scaled_font(label, delta, weight=weight, letter_spacing=spacing)
    return True


def apply_labelled_fonts(root: QWidget) -> None:
    """Apply the table to every label below ``root``, including ``root`` itself.

    Called once per window and dialog after construction: a single sweep cannot
    miss a label the way per-site calls can, and it keeps the table in step with
    widgets built by helper functions.
    """
    from PyQt6.QtWidgets import QLabel

    apply_labelled_font(root)
    for label in root.findChildren(QLabel):
        apply_labelled_font(label)


def text_width(widget: QWidget, text: str) -> int:
    """Measure ``text`` as ``widget`` will paint it.

    Measurement has to use the widget's own resolved font. Measuring with the
    application font while a style sheet paints something larger is exactly the
    disagreement that elides labels which should fit.
    """
    return QFontMetrics(widget.font()).horizontalAdvance(text)
