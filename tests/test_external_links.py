"""Regression coverage for visible failures when opening external web links."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWidgets import QPushButton, QWidget

from core.backend import PackageInfo
from core.dnf5daemon import (
    AUTOMATIC_KEY_IMPORT_PREFERENCE,
    repository_key_import_is_automatic,
)
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


@pytest.mark.parametrize(
    "raw_url",
    (
        # Embedded credentials would show a password in the URL bar and in the
        # desktop handler's own error messages.
        "https://user:secret@example.test/path",
        "https://user@example.test/path",
        # Control characters can smuggle a percent-encoded payload past the
        # URL parser and into the external handler's argv.
        "https://example.test/\x00",
        "https://example.test/a\nb",
        "https://example.test/a\tb",
        "https://example.test/a\x7f",
    ),
)
def test_normalized_web_url_rejects_credentials_and_control_characters(
    raw_url: str,
) -> None:
    assert normalized_web_url(raw_url) is None


def test_normalized_web_url_keeps_a_port_and_a_query_string() -> None:
    url = normalized_web_url("https://example.test:8443/a?b=c#frag")

    assert url is not None
    assert url.toString() == "https://example.test:8443/a?b=c#frag"
    assert url.port() == 8443


def test_linux_gio_adapter_uses_the_registered_default_application(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []
    fake_gi = ModuleType("gi")
    fake_repository = ModuleType("gi.repository")

    class _FakeAppInfo:
        @staticmethod
        def launch_default_for_uri(uri: str, context: object) -> bool:
            calls.append((uri, context))
            return True

    fake_gi.require_version = lambda namespace, version: calls.append(
        (namespace, version)
    )
    fake_repository.Gio = SimpleNamespace(AppInfo=_FakeAppInfo)
    monkeypatch.setattr(external_links.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repository)

    assert external_links._launch_uri_through_gio(QUrl("https://example.test")) is True
    assert calls == [("Gio", "2.0"), ("https://example.test", None)]


def test_linux_gio_adapter_turns_a_launch_error_into_failure(monkeypatch) -> None:
    fake_gi = ModuleType("gi")
    fake_repository = ModuleType("gi.repository")

    class _FailingAppInfo:
        @staticmethod
        def launch_default_for_uri(_uri: str, _context: object) -> bool:
            raise RuntimeError("no default handler")

    fake_gi.require_version = lambda _namespace, _version: None
    fake_repository.Gio = SimpleNamespace(AppInfo=_FailingAppInfo)
    monkeypatch.setattr(external_links.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repository)

    assert external_links._launch_uri_through_gio(QUrl("https://example.test")) is False


def test_external_web_link_reports_missing_desktop_handler(monkeypatch, qtbot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    launched: list[QUrl] = []
    warnings: list[tuple[object, str, str]] = []

    monkeypatch.setattr(external_links, "_launch_uri_through_gio", lambda _url: None)
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

    monkeypatch.setattr(external_links, "_launch_uri_through_gio", lambda _url: None)
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


def test_gio_rejected_handoff_is_reported_without_using_qt(monkeypatch, qtbot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    warnings: list[str] = []

    monkeypatch.setattr(external_links, "_launch_uri_through_gio", lambda _url: False)
    monkeypatch.setattr(
        external_links.QDesktopServices,
        "openUrl",
        lambda _url: pytest.fail("a rejected GIO handoff must not be retried blindly"),
    )
    monkeypatch.setattr(
        external_links.QMessageBox,
        "warning",
        lambda _parent, _title, text: warnings.append(text),
    )

    assert open_external_web_url(parent, "https://example.test") is False
    assert len(warnings) == 1
    assert "default web browser" in warnings[0]


def test_gio_success_is_accepted_without_using_qt(monkeypatch, qtbot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    launched: list[str] = []

    monkeypatch.setattr(
        external_links,
        "_launch_uri_through_gio",
        lambda url: launched.append(url.toString()) or True,
    )
    monkeypatch.setattr(
        external_links.QDesktopServices,
        "openUrl",
        lambda _url: pytest.fail("a successful GIO handoff must not be duplicated"),
    )

    assert open_external_web_url(parent, "github.com/xyasharx/Dendro") is True
    assert launched == ["https://github.com/xyasharx/Dendro"]


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

    monkeypatch.setattr(external_links, "_launch_uri_through_gio", lambda _url: None)
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


def test_repository_key_trust_option_is_off_by_default_and_persists(
    monkeypatch, qtbot
) -> None:
    """A new repository's signing key must not be trusted without the user.

    The default matters: ``Goal.resolve`` blocks the daemon for five minutes
    waiting for this answer, so an accidental default of "trust" would silently
    widen trust in every third-party repository a user enables.
    """
    settings = _Settings()
    monkeypatch.setattr(settings_dialog, "QSettings", lambda *_args: settings)
    monkeypatch.setattr(
        "core.dnf5daemon.QSettings", lambda *_args: settings, raising=False
    )
    dialog = DendroSettingsDialog()
    qtbot.addWidget(dialog)

    assert dialog.chk_auto_import_keys.isChecked() is False
    assert "without asking" in dialog.chk_auto_import_keys.text()
    assert not repository_key_import_is_automatic()

    dialog.chk_auto_import_keys.setChecked(True)
    assert settings.values[AUTOMATIC_KEY_IMPORT_PREFERENCE] is True
    assert repository_key_import_is_automatic()

    dialog.chk_auto_import_keys.setChecked(False)
    assert settings.values[AUTOMATIC_KEY_IMPORT_PREFERENCE] is False
    assert not repository_key_import_is_automatic()


def _run_apprun_in_temp_appdir(
    tmp_path: Path, base_env: dict[str, str]
) -> tuple[Path, dict[str, str]]:
    """Execute ``data/AppRun`` inside a minimal AppDir and capture the env."""
    appdir = tmp_path / "AppDir"
    site_packages = appdir / "usr" / "lib" / "python3.15" / "site-packages"
    qt_plugins = site_packages / "PyQt6" / "Qt6" / "plugins"
    (qt_plugins / "platforms").mkdir(parents=True)
    (site_packages / "PyQt6" / "Qt6" / "lib").mkdir(parents=True)
    (appdir / "usr" / "bin").mkdir(parents=True)
    (appdir / "usr" / "app").mkdir(parents=True)
    (appdir / "usr" / "app" / "main.py").write_text("# stub\n", encoding="utf-8")

    apprun_src = Path(__file__).resolve().parents[1] / "data" / "AppRun"
    apprun_dst = appdir / "AppRun"
    shutil.copy2(apprun_src, apprun_dst)
    apprun_dst.chmod(0o755)

    env_bin = shutil.which("env") or "/usr/bin/env"
    python_stub = appdir / "usr" / "bin" / "python3"
    python_stub.write_text(f"#!/bin/sh\nexec {env_bin} -0\n", encoding="utf-8")
    python_stub.chmod(0o755)

    completed = subprocess.run(
        [str(apprun_dst)],
        env=base_env,
        capture_output=True,
        check=True,
    )
    captured: dict[str, str] = {}
    for entry in completed.stdout.split(b"\x00"):
        if not entry:
            continue
        key, _, value = entry.partition(b"=")
        captured[key.decode("utf-8")] = value.decode("utf-8")
    return appdir, captured


def test_apprun_and_desktop_url_handoff_do_not_leak_bundled_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot
) -> None:
    caller_env = {
        "PATH": "/home/tester/.local/bin:/usr/bin:/bin",
        "DISPLAY": ":99",
        "XDG_CURRENT_DESKTOP": "KDE",
        "APPIMAGE": "/home/tester/Dendro-x86_64.AppImage",
        "ARGV0": "./Dendro-x86_64.AppImage",
        "OWD": "/home/tester",
    }
    appdir, bundled_env = _run_apprun_in_temp_appdir(tmp_path, caller_env)

    # AppRun exports bundled paths for the running Python/PyQt6 process while
    # recording the original caller environment for host child handoffs.
    assert bundled_env["DENDRO_APPRUN_ENV_SAVED"] == "1"
    assert bundled_env["DENDRO_ORIG_PATH"] == "/home/tester/.local/bin:/usr/bin:/bin"
    assert "DENDRO_ORIG_LD_LIBRARY_PATH" not in bundled_env
    assert "DENDRO_ORIG_QT_PLUGIN_PATH" not in bundled_env
    assert str(appdir / "usr" / "lib") in bundled_env["LD_LIBRARY_PATH"]
    assert str(appdir) in bundled_env["QT_PLUGIN_PATH"]

    monkeypatch.setattr(os, "environ", bundled_env.copy())
    seen_during_open: list[dict[str, str]] = []
    parent = QWidget()
    qtbot.addWidget(parent)

    monkeypatch.setattr(external_links, "_launch_uri_through_gio", lambda _url: None)
    monkeypatch.setattr(
        external_links.QDesktopServices,
        "openUrl",
        lambda _url: seen_during_open.append(dict(os.environ)) or True,
    )

    assert open_external_web_url(parent, "https://fedoraproject.org") is True
    assert len(seen_during_open) == 1
    child_env = seen_during_open[0]

    assert child_env["PATH"] == "/home/tester/.local/bin:/usr/bin:/bin"
    assert child_env["DISPLAY"] == ":99"
    assert child_env["XDG_CURRENT_DESKTOP"] == "KDE"
    for leaked in (
        "LD_LIBRARY_PATH",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "APPDIR",
        "APPIMAGE",
        "ARGV0",
        "OWD",
        "DENDRO_APPRUN_ENV_SAVED",
        "DENDRO_APPDIR",
        "DENDRO_ORIG_PATH",
    ):
        assert leaked not in child_env

    # The running GUI process keeps its AppImage runtime paths intact afterward.
    assert os.environ["LD_LIBRARY_PATH"] == bundled_env["LD_LIBRARY_PATH"]
    assert os.environ["QT_PLUGIN_PATH"] == bundled_env["QT_PLUGIN_PATH"]
    assert os.environ["PYTHONHOME"] == bundled_env["PYTHONHOME"]


def test_apprun_preserves_caller_ld_and_qt_variables_when_originally_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot
) -> None:
    caller_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LD_LIBRARY_PATH": "/opt/host-custom/lib",
        "QT_PLUGIN_PATH": "/opt/host-qt/plugins",
        "QT_QPA_PLATFORM_PLUGIN_PATH": "/opt/host-qt/plugins/platforms",
        "PYTHONPATH": "/opt/host-python",
        "PYTHONHOME": "/opt/host-pyhome",
    }
    _appdir, bundled_env = _run_apprun_in_temp_appdir(tmp_path, caller_env)

    monkeypatch.setattr(os, "environ", bundled_env.copy())
    seen_during_open: list[dict[str, str]] = []
    parent = QWidget()
    qtbot.addWidget(parent)

    monkeypatch.setattr(external_links, "_launch_uri_through_gio", lambda _url: None)
    monkeypatch.setattr(
        external_links.QDesktopServices,
        "openUrl",
        lambda _url: seen_during_open.append(dict(os.environ)) or True,
    )

    assert open_external_web_url(parent, "https://fedoraproject.org") is True
    child_env = seen_during_open[0]
    assert child_env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert child_env["LD_LIBRARY_PATH"] == "/opt/host-custom/lib"
    assert child_env["QT_PLUGIN_PATH"] == "/opt/host-qt/plugins"
    assert child_env["QT_QPA_PLATFORM_PLUGIN_PATH"] == "/opt/host-qt/plugins/platforms"
    assert child_env["PYTHONPATH"] == "/opt/host-python"
    assert child_env["PYTHONHOME"] == "/opt/host-pyhome"


def test_linux_gio_adapter_sanitizes_launch_context_in_bundled_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appdir = "/tmp/.mount_DendroTest"
    bundled_env = {
        "PATH": f"{appdir}/usr/bin:/usr/bin:/bin",
        "LD_LIBRARY_PATH": f"{appdir}/usr/lib",
        "QT_PLUGIN_PATH": f"{appdir}/usr/plugins",
        "PYTHONHOME": f"{appdir}/usr",
        "PYTHONPATH": f"{appdir}/usr/app",
        "APPDIR": appdir,
        "APPIMAGE": "/home/user/Dendro.AppImage",
        "DENDRO_APPRUN_ENV_SAVED": "1",
        "DENDRO_APPDIR": appdir,
        "DENDRO_ORIG_PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    monkeypatch.setattr(os, "environ", bundled_env.copy())

    context_ops: list[tuple[str, ...]] = []
    seen_envs: list[dict[str, str]] = []

    class _FakeLaunchContext:
        @classmethod
        def new(cls) -> _FakeLaunchContext:
            return cls()

        def unsetenv(self, key: str) -> None:
            context_ops.append(("unset", key))

        def setenv(self, key: str, value: str) -> None:
            context_ops.append(("set", key, value))

    class _FakeAppInfo:
        @staticmethod
        def launch_default_for_uri(uri: str, context: object) -> bool:
            assert isinstance(context, _FakeLaunchContext)
            seen_envs.append(dict(os.environ))
            return uri == "https://example.test"

    fake_gi = ModuleType("gi")
    fake_repository = ModuleType("gi.repository")
    fake_gi.require_version = lambda _namespace, _version: None
    fake_repository.Gio = SimpleNamespace(
        AppInfo=_FakeAppInfo, AppLaunchContext=_FakeLaunchContext
    )
    monkeypatch.setattr(external_links.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repository)

    assert external_links._launch_uri_through_gio(QUrl("https://example.test")) is True
    assert ("set", "PATH", "/usr/local/bin:/usr/bin:/bin") in context_ops
    assert ("unset", "LD_LIBRARY_PATH") in context_ops
    assert ("unset", "QT_PLUGIN_PATH") in context_ops
    assert ("unset", "APPIMAGE") in context_ops
    assert seen_envs[0]["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "LD_LIBRARY_PATH" not in seen_envs[0]
    assert os.environ["LD_LIBRARY_PATH"] == f"{appdir}/usr/lib"
