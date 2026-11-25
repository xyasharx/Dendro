# dendro/ui/inspector_renderers.py
"""Pure HTML and text formatting helpers for the inspector detail tabs."""

from __future__ import annotations

import html
import re
from typing import Any, Final

from PyQt6.QtGui import QColor

from core.backend import FileVerificationResult, PackageChangelogEntry
from ui.styles import get_delegate_palette

# Precompiled regular expressions for changelog auto-linking
CHANGELOG_CVE_PATTERN: Final[re.Pattern] = re.compile(
    r"\b(CVE-\d{4}-\d{4,7})\b", re.IGNORECASE
)
CHANGELOG_BUGZILLA_PATTERN: Final[re.Pattern] = re.compile(
    r"\b(?:RHBZ|bug)\s*#?(\d{5,8})\b", re.IGNORECASE
)


def placeholder_document_html(message: str, colour: QColor) -> str:
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


def verification_summary(
    results: list[FileVerificationResult],
    theme: str,
    *,
    ignored_mtime: bool = False,
) -> tuple[str, str, str]:
    """Return ``(status_text, tooltip_text, color_hex)`` for an rpm -V result."""
    pal = get_delegate_palette(theme)
    mtime_tooltip = (
        "Verified with 'rpm -V --nomtime': OSTree normalizes immutable '/usr' "
        "file modification times to Unix epoch 0, while digests, sizes, modes, "
        "owners, links, and '/etc' changes remain fully checked."
        if ignored_mtime
        else ""
    )
    if not results:
        clean_text = (
            "Clean (No content/mode diffs; '--nomtime')"
            if ignored_mtime
            else "Clean (No files modified or missing)"
        )
        return clean_text, mtime_tooltip, pal["badge_fg_installed"].name()

    missing_count = sum(1 for r in results if r.is_missing)
    unreadable_count = sum(1 for r in results if r.is_unreadable and not r.is_missing)
    tampered_count = len(results) - missing_count - unreadable_count
    suffix = " ('--nomtime')" if ignored_mtime else ""
    tooltip_parts: list[str] = []
    if mtime_tooltip:
        tooltip_parts.append(mtime_tooltip)
    if unreadable_count > 0:
        tooltip_parts.append(
            "Some root-owned or permission-restricted paths could not be "
            "inspected during unprivileged 'rpm -V' verification."
        )
    tooltip_text = "\n\n".join(tooltip_parts)
    if tampered_count == 0 and missing_count == 0 and unreadable_count > 0:
        return (
            f"Unreadable (unprivileged): {unreadable_count}{suffix}",
            tooltip_text,
            pal["badge_fg_queued_in"].name(),
        )
    if unreadable_count > 0:
        return (
            f"Modified: {tampered_count}, Missing: {missing_count}, "
            f"Unreadable: {unreadable_count}{suffix}",
            tooltip_text,
            pal["badge_fg_missing"].name(),
        )
    return (
        f"Modified: {tampered_count}, Missing: {missing_count}{suffix}",
        tooltip_text,
        pal["badge_fg_missing"].name(),
    )


def scriptlets_html(scripts: Any, theme: str) -> str:
    """Render RPM maintainer scriptlets HTML for the inspector Scripts tab."""
    pal = get_delegate_palette(theme)
    if not scripts or not getattr(scripts, "has_any", False):
        return (
            f"<p style='color: {pal['text_dim'].name()};'>"
            "No maintainer scriptlets defined in this RPM.</p>"
        )

    blocks: list[str] = []
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
            blocks.append(
                f"""
                <div style="margin-bottom: 12px; border-bottom: 1px solid {pal["border"].name()}; padding-bottom: 8px;">
                    <div style="color: {pal["accent"].name()}; font-weight: bold; margin-bottom: 4px;">{esc_title}</div>
                    <pre style="white-space: pre-wrap; color: {pal["text_main"].name()}; margin: 0px;">{esc_code}</pre>
                </div>
                """
            )
    return "".join(blocks)


def scriptlets_error_html(detail: str, theme: str) -> str:
    """Render an error placeholder when RPM scriptlets fail to load."""
    pal = get_delegate_palette(theme)
    escaped_detail = html.escape(detail).replace("\n", "<br>")
    return (
        f"<p style='color: {pal['badge_fg_missing'].name()}; font-weight: bold;'>"
        "Could not load RPM scriptlets.</p>"
        f"<p style='color: {pal['text_dim'].name()};'>{escaped_detail}</p>"
    )


def changelog_html(entries: list[PackageChangelogEntry], theme: str) -> str:
    """Render HTML changelog entries with CVE/RHBZ auto-linking."""
    pal = get_delegate_palette(theme)
    text_main = pal["text_main"].name()
    text_dim = pal["text_dim"].name()
    accent = pal["accent"].name()
    badge_author = pal["badge_fg_queued_in"].name()
    divider = pal["border"].name()

    if not entries:
        return placeholder_document_html(
            "This package does not carry a changelog in its RPM header.",
            QColor(text_dim),
        )

    html_blocks: list[str] = []
    for entry in entries[:40]:
        header_text = (
            f"{entry.date_str} — {entry.author}"
            if entry.date_str and entry.date_str not in entry.author
            else entry.author
        )
        esc_header = html.escape(header_text, quote=True)
        body = html.escape(entry.text, quote=True)

        body = CHANGELOG_CVE_PATTERN.sub(
            rf'<a href="https://access.redhat.com/security/cve/\1" style="color: {pal["badge_fg_missing"].name()}; font-weight: bold; text-decoration: none;">\1</a>',
            body,
        )
        body = CHANGELOG_BUGZILLA_PATTERN.sub(
            rf'<a href="https://bugzilla.redhat.com/show_bug.cgi?id=\1" style="color: {accent}; text-decoration: none;">RHBZ#\1</a>',
            body,
        )

        html_blocks.append(
            f"""
            <div style="margin-bottom: 12px; border-bottom: 1px solid {divider}; padding-bottom: 8px;">
                <div style="color: {badge_author}; font-weight: bold;">{esc_header}</div>
                <pre style="margin-top: 4px; white-space: pre-wrap; color: {text_main}; font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;">{body}</pre>
            </div>
            """
        )
    return "".join(html_blocks)


def changelog_error_html(detail: str, theme: str) -> str:
    """Render an error placeholder when RPM changelog lookup fails."""
    pal = get_delegate_palette(theme)
    escaped_detail = html.escape(detail).replace("\n", "<br>")
    return (
        f"<p style='color: {pal['badge_fg_missing'].name()}; font-weight: bold;'>"
        "Could not load the package changelog.</p>"
        f"<p style='color: {pal['text_dim'].name()};'>{escaped_detail}</p>"
    )


__all__ = (
    "CHANGELOG_BUGZILLA_PATTERN",
    "CHANGELOG_CVE_PATTERN",
    "changelog_error_html",
    "changelog_html",
    "placeholder_document_html",
    "scriptlets_error_html",
    "scriptlets_html",
    "verification_summary",
)
