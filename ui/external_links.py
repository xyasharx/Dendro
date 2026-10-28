"""Reliable, user-visible opening of external HTTP(S) links.

Every Dendro UI path that asks the desktop to open a web page goes through this
module.  Qt can report whether the desktop accepted the request, but it cannot
report whether a browser later loads the remote page successfully.
"""

from __future__ import annotations

import sys
from typing import Final

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMessageBox, QWidget

_WEB_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


def _launch_uri_through_gio(url: QUrl) -> bool | None:
    """Use GLib's default-application API when it is available on Linux.

    ``QDesktopServices`` can report success after it merely starts a desktop
    helper; that helper can then fail because no browser is registered.  GIO
    asks the desktop's registered default-application service directly and
    reports a missing or unusable handler synchronously.  ``None`` means this
    optional Linux integration is not available, so the caller should retain
    Qt's cross-platform handoff.
    """
    if sys.platform != "linux":
        return None

    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    except (ImportError, ValueError):
        return None

    try:
        return bool(Gio.AppInfo.launch_default_for_uri(url.toString(), None))
    except Exception:
        # PyGObject exposes GLib launch errors as Python exceptions.  The
        # optional integration must never turn a failed desktop handoff into a
        # crash or an unreported false success.
        return False


def normalized_web_url(raw_url: str | QUrl) -> QUrl | None:
    """Return one valid HTTP(S) URL, accepting a scheme-less web address.

    Package metadata commonly supplies a hostname without ``https://``.  Keep
    that established convenience while rejecting empty, malformed, and
    non-web schemes before handing anything to the desktop service.
    """
    text = raw_url.toString().strip() if isinstance(raw_url, QUrl) else raw_url.strip()
    if not text:
        return None

    # ``fromUserInput`` recognizes a hostname with a port (for example,
    # ``docs.example.org:8443``) without mistaking the hostname for a custom
    # URL scheme.  It is only used to classify the input; Dendro deliberately
    # uses HTTPS for scheme-less web addresses.
    if text.startswith("//"):
        url = QUrl(f"https:{text}", QUrl.ParsingMode.TolerantMode)
    else:
        candidate = QUrl.fromUserInput(text)
        if candidate.scheme().lower() not in _WEB_SCHEMES:
            return None
        if text.lower().startswith(("http://", "https://")):
            url = QUrl(text, QUrl.ParsingMode.TolerantMode)
        else:
            url = QUrl(f"https://{text}", QUrl.ParsingMode.TolerantMode)

    if not url.isValid() or url.scheme().lower() not in _WEB_SCHEMES or not url.host():
        return None
    return url


def open_external_web_url(
    parent: QWidget | None,
    raw_url: str | QUrl,
    *,
    link_label: str = "web page",
) -> bool:
    """Ask the desktop to open an HTTP(S) URL and explain a failed handoff.

    ``True`` means the operating system accepted Dendro's open request.  It
    does not promise that the selected browser subsequently launches or that
    the remote server responds successfully; those outcomes are outside Qt's
    desktop-service contract.
    """
    url = normalized_web_url(raw_url)
    if url is None:
        QMessageBox.warning(
            parent,
            "Cannot Open Link",
            f"Dendro cannot open this {link_label} because its address is not a "
            "valid HTTP or HTTPS link.",
        )
        return False

    gio_opened = _launch_uri_through_gio(url)
    if gio_opened is None:
        try:
            opened = QDesktopServices.openUrl(url)
        except RuntimeError:
            opened = False
    else:
        opened = gio_opened

    if opened:
        return True

    QMessageBox.warning(
        parent,
        "Cannot Open Link",
        f"Dendro could not ask your desktop to open this {link_label}.\n\n"
        "Check that a default web browser is installed and configured for HTTP "
        "and HTTPS links, then try again.\n\n"
        f"Address:\n{url.toDisplayString()}",
    )
    return False
