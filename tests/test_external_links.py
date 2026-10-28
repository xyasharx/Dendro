"""Regression coverage for visible failures when opening external web links."""

from __future__ import annotations

import ast
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWidgets import QPushButton, QWidget

from core.backend import PackageInfo
from ui import external_links, inspector_panel, settings_dialog
from ui.external_links import normalized_web_url, open_external_web_url
from ui.inspector_panel import PackageInspectorPanel
from ui.settings_dialog import DendroSettingsDialog


class _Settings:
    """Small in-memory settings substitute for the About-dialog UI test."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def value(
        self, key: str, default: object = None, *, type: type | None = None
    ) -> object:
        value = self.values.get(key, default)
        return type(value) if type is not None and value is not None else value

    def setValue(self, key: str, value: object) -> None:
        self.values[key] = value


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("github.com/xyasharx/Dendro", "https://github.com/xyasharx/Dendro"),
        (
            "HTTPS://crates.io/crates/rpm-sequoia",
            "https://crates.io/crates/rpm-sequoia",
        ),
        (
            QUrl("https://bugzilla.redhat.com/show_bug.cgi?id=123"),
            "https://bugzilla.redhat.com/show_bug.cgi?id=123",
        ),
        (
            QUrl("access.redhat.com/security/cve/CVE-2026-1234"),
            "https://access.redhat.com/security/cve/CVE-2026-1234",
        ),
        ("docs.example.org:8443/guide", "https://docs.example.org:8443/guide"),
        ("//example.org/changelog", "https://example.org/changelog"),
    ],
)
def test_normalized_web_url_preserves_valid_web_addresses(
    raw_url: str | QUrl, expected: str
) -> None:
    url = normalized_web_url(raw_url)

    assert url is not None
    assert url.toString() == expected


@pytest.mark.parametrize(
    "raw_url",
    (
        "",
        "https://",
        "ftp://example.test",
        "file:///etc/passwd",
        "mailto:maintainer@example.test",
        "javascript:alert(1)",
        "not a url",
    ),
)
def test_normalized_web_url_rejects_non_web_or_malformed_addresses(
    raw_url: str,
) -> None:
    assert normalized_web_url(raw_url) is None


def test_external_web_link_reports_missing_desktop_handler(monkeypatch, qtbot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    launched: list[QUrl] = []
    warnings: list[tuple[object, str, str]] = []

    monkeypatch.setattr(
        external_links.QDesktopServices,
        "openUrl",
        lambda url: launched.append(QUrl(url)) or False,
    )
    monkeypatch.setattr(
        external_links.QMessageBox,
        "warning",
        lambda dialog_parent, title, text: warnings.append(
            (dialog_parent, title, text)
        ),
    )

    assert (
        open_external_web_url(
            parent,
            "https://crates.io/crates/rpm-sequoia",
            link_label="package homepage",
        )
        is False
    )

    assert [url.toString() for url in launched] == [
        "https://crates.io/crates/rpm-sequoia"
    ]
    assert warnings == [
        (
            parent,
            "Cannot Open Link",
            "Dendro could not ask your desktop to open this package homepage.\n\n"
            "Check that a default web browser is installed and configured for HTTP "
            "and HTTPS links, then try again.\n\n"
            "Address:\nhttps://crates.io/crates/rpm-sequoia",
        )
    ]


def test_external_web_link_does_not_show_a_warning_after_success(
    monkeypatch, qtbot
) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    warnings: list[tuple[object, str, str]] = []

    monkeypatch.setattr(external_links.QDesktopServices, "openUrl", lambda _url: True)
    monkeypatch.setattr(
        external_links.QMessageBox,
        "warning",
        lambda dialog_parent, title, text: warnings.append(
            (dialog_parent, title, text)
        ),
    )

    assert open_external_web_url(parent, "github.com/xyasharx/Dendro") is True
    assert warnings == []


def test_invalid_package_url_is_explained_without_calling_the_desktop(
    monkeypatch, qtbot
) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    warnings: list[tuple[str, str]] = []

    monkeypatch.setattr(
        external_links.QDesktopServices,
        "openUrl",
        lambda _url: pytest.fail("invalid metadata must not reach the desktop"),
    )
    monkeypatch.setattr(
        external_links.QMessageBox,
        "warning",
        lambda _parent, title, text: warnings.append((title, text)),
    )

    assert (
        open_external_web_url(
            parent, "file:///etc/passwd", link_label="package homepage"
        )
        is False
    )
    assert warnings == [
        (
            "Cannot Open Link",
            "Dendro cannot open this package homepage because its address is not a "
            "valid HTTP or HTTPS link.",
        )
    ]


def test_runtime_error_from_desktop_service_is_reported(monkeypatch, qtbot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    warnings: list[str] = []

    def raise_runtime_error(_url: QUrl) -> bool:
        raise RuntimeError("desktop integration unavailable")

    monkeypatch.setattr(external_links.QDesktopServices, "openUrl", raise_runtime_error)
    monkeypatch.setattr(
        external_links.QMessageBox,
        "warning",
        lambda _parent, _title, text: warnings.append(text),
    )

    assert open_external_web_url(parent, "https://example.test") is False
    assert len(warnings) == 1
    assert "default web browser" in warnings[0]


def test_package_homepage_uses_the_shared_feedback_gateway(monkeypatch, qtbot) -> None:
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    panel._current_package = PackageInfo(
        name="rpm-sequoia",
        url="https://crates.io/crates/rpm-sequoia",
    )
    calls: list[tuple[object, object, str]] = []

    monkeypatch.setattr(
        inspector_panel,
        "open_external_web_url",
        lambda parent, raw_url, *, link_label: (
            calls.append((parent, raw_url, link_label)) or True
        ),
    )

    panel._open_project_url()

    assert calls == [
        (panel, "https://crates.io/crates/rpm-sequoia", "package homepage")
    ]


def test_changelog_links_use_the_shared_feedback_gateway(monkeypatch, qtbot) -> None:
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    calls: list[tuple[object, QUrl, str]] = []

    monkeypatch.setattr(
        inspector_panel,
        "open_external_web_url",
        lambda parent, raw_url, *, link_label: (
            calls.append((parent, raw_url, link_label)) or True
        ),
    )

    assert panel.changelog_browser.openLinks() is False
    assert panel.changelog_browser.openExternalLinks() is False
    panel.changelog_browser.anchorClicked.emit(
        QUrl("https://access.redhat.com/security/cve/CVE-2026-1234")
    )

    assert [(parent, url.toString(), label) for parent, url, label in calls] == [
        (
            panel,
            "https://access.redhat.com/security/cve/CVE-2026-1234",
            "changelog link",
        )
    ]


def test_about_links_use_the_shared_feedback_gateway(monkeypatch, qtbot) -> None:
    settings = _Settings()
    monkeypatch.setattr(settings_dialog, "QSettings", lambda *_args: settings)
    dialog = DendroSettingsDialog()
    qtbot.addWidget(dialog)
    calls: list[tuple[object, object, str]] = []

    monkeypatch.setattr(
        settings_dialog,
        "open_external_web_url",
        lambda parent, raw_url, *, link_label: (
            calls.append((parent, raw_url, link_label)) or True
        ),
    )

    buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
    qtbot.mouseClick(buttons["GitHub Repository"], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(buttons["Issue Tracker"], Qt.MouseButton.LeftButton)

    assert calls == [
        (dialog, "https://github.com/xyasharx/Dendro", "Dendro repository"),
        (
            dialog,
            "https://github.com/xyasharx/Dendro/issues",
            "Dendro issue tracker",
        ),
    ]


def test_runtime_code_cannot_bypass_the_shared_external_link_gateway() -> None:
    """Keep future runtime links from silently bypassing user-visible feedback."""
    project_root = Path(__file__).resolve().parents[1]
    runtime_paths = [
        project_root / "main.py",
        *sorted((project_root / "core").glob("*.py")),
        *sorted((project_root / "ui").glob("*.py")),
    ]
    violations: list[str] = []

    for path in runtime_paths:
        if path == project_root / "ui" / "external_links.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "PyQt6.QtGui"
                and any(alias.name == "QDesktopServices" for alias in node.names)
            ):
                violations.append(
                    f"{path.name}:{node.lineno} imports QDesktopServices directly"
                )
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr == "openUrl":
                violations.append(f"{path.name}:{node.lineno} calls openUrl directly")
            if node.func.attr == "setOpenExternalLinks":
                is_explicitly_disabled = (
                    len(node.args) == 1
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value is False
                )
                if not is_explicitly_disabled:
                    violations.append(
                        f"{path.name}:{node.lineno} enables or dynamically configures "
                        "automatic external links"
                    )

    assert violations == []


def test_shared_gateway_does_not_probe_or_spawn_a_browser() -> None:
    """Keep the feedback boundary local, fast, and truthful about its limits."""
    source_path = Path(__file__).resolve().parents[1] / "ui" / "external_links.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    forbidden_module_prefixes = (
        "http",
        "os",
        "requests",
        "socket",
        "subprocess",
        "urllib",
        "webbrowser",
        "PyQt6.QtNetwork",
    )
    violations: list[str] = []

    def is_forbidden_module(module: str) -> bool:
        return any(
            module == prefix or module.startswith(f"{prefix}.")
            for prefix in forbidden_module_prefixes
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(
                f"imports {alias.name}"
                for alias in node.names
                if is_forbidden_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module and is_forbidden_module(node.module):
                violations.append(f"imports from {node.module}")
            if node.module == "PyQt6.QtCore" and any(
                alias.name == "QProcess" for alias in node.names
            ):
                violations.append("imports QProcess")
            if node.module == "PyQt6" and any(
                alias.name == "QtNetwork" for alias in node.names
            ):
                violations.append("imports QtNetwork")

    assert violations == []
