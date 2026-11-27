"""End-to-end tests for security advisory cross-referencing and remediation."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QMessageBox

from dendro.core.backend import AvailableUpdateInfo, PackageInfo, PackageState
from dendro.core.dnf5daemon import SecurityAdvisory
from dendro.core.host_environment import HostEnvironment, HostEnvironmentKind
from dendro.core.i18n import set_active_locale
from dendro.core.models import (
    CustomUserRoles,
    DependencyTreeModel,
    PackageFilterProxyModel,
)
from dendro.ui import main_window
from dendro.ui.delegates import PackageTreeItemDelegate
from dendro.ui.header import HeaderBar, search_filter_tooltip
from dendro.ui.main_window import MainWindow
from dendro.ui.styles import get_theme_stylesheet


def _pkg(
    name: str,
    version: str = "1.0",
    release: str = "1.fc44",
    *,
    has_update: bool = False,
    update_version: str = "",
    is_security: bool = False,
    advisory_id: str = "",
    advisory_severity: str = "",
    state: PackageState = PackageState.INSTALLED,
) -> PackageInfo:
    return PackageInfo(
        name=name,
        version=version,
        release=release,
        arch="x86_64",
        epoch="0",
        repository="updates",
        state=state,
        has_update=has_update,
        available_update_version=update_version,
        is_security_update=is_security,
        advisory_id=advisory_id,
        advisory_severity=advisory_severity,
    )


def test_tree_model_propagates_security_roles_tooltips_and_selective_queue(
    qapp,
) -> None:
    openssl = _pkg("openssl", "3.2.0")
    curl = _pkg("curl", "8.8.0")
    nano = _pkg("nano", "8.2")
    bash = _pkg("bash", "5.2.37", state=PackageState.QUEUED_REINSTALL)
    mesa = _pkg("mesa", "24.3.0", state=PackageState.QUEUED_DOWNGRADE)

    model = DependencyTreeModel()
    model.set_packages([openssl, curl, nano, bash, mesa])

    model.update_available_upgrades(
        {
            "openssl.x86_64": AvailableUpdateInfo(
                name="openssl",
                new_version="3.2.4",
                new_release="1.fc44",
                arch="x86_64",
                repository="updates",
                is_security=True,
                advisory_id="FEDORA-2026-SEC1",
                advisory_severity="Critical",
            ),
            "curl.x86_64": AvailableUpdateInfo(
                name="curl",
                new_version="8.9.0",
                new_release="1.fc44",
                arch="x86_64",
                repository="updates",
                is_security=True,
                advisory_id="FEDORA-2026-SEC2",
                advisory_severity="Moderate",
            ),
            "nano.x86_64": AvailableUpdateInfo(
                name="nano",
                new_version="8.3",
                new_release="1.fc44",
                arch="x86_64",
                repository="updates",
                is_security=False,
            ),
        },
        replace=True,
    )

    openssl_idx = model.index(0, model.COL_NAME)
    nano_idx = model.index(2, model.COL_NAME)
    assert openssl_idx.data(CustomUserRoles.IsSecurityUpdateRole) is True
    assert openssl_idx.data(CustomUserRoles.AdvisorySeverityRole) == "Critical"
    assert openssl_idx.data(CustomUserRoles.AdvisoryIdRole) == "FEDORA-2026-SEC1"
    tooltip = openssl_idx.data(Qt.ItemDataRole.ToolTipRole)
    assert "Critical" in tooltip and "FEDORA-2026-SEC1" in tooltip
    assert nano_idx.data(CustomUserRoles.IsSecurityUpdateRole) is False
    assert nano_idx.data(Qt.ItemDataRole.ToolTipRole) is None

    newly_queued, already_queued = model.queue_upgrade_packages(security_only=True)
    assert (newly_queued, already_queued) == (2, 0)
    assert openssl.state == PackageState.QUEUED_UPGRADE
    assert curl.state == PackageState.QUEUED_UPGRADE
    assert nano.state == PackageState.INSTALLED
    # Existing non-upgrade user actions must stay untouched.
    assert bash.state == PackageState.QUEUED_REINSTALL
    assert mesa.state == PackageState.QUEUED_DOWNGRADE

    # Re-running reports already_queued without double counting.
    assert model.queue_upgrade_packages(security_only=True) == (0, 2)

    # Replacing updates with non-security list clears security metadata on non-upgradable rows.
    model.update_available_upgrades(
        {
            "nano.x86_64": AvailableUpdateInfo(
                name="nano",
                new_version="8.3",
                new_release="1.fc44",
                arch="x86_64",
                repository="updates",
                is_security=False,
            )
        },
        replace=True,
    )
    assert openssl.is_security_update is False
    assert openssl.advisory_id == ""
    assert openssl.advisory_severity == ""


def test_proxy_model_filters_security_severity_and_advisory_id(qapp) -> None:
    openssl = _pkg(
        "openssl",
        has_update=True,
        update_version="3.2.4-1.fc44",
        is_security=True,
        advisory_id="FEDORA-2026-CRIT1",
        advisory_severity="Critical",
    )
    curl = _pkg(
        "curl",
        has_update=True,
        update_version="8.9.0-1.fc44",
        is_security=True,
        advisory_id="FEDORA-2026-MOD2",
        advisory_severity="Moderate",
    )
    nano = _pkg(
        "nano",
        has_update=True,
        update_version="8.3-1.fc44",
        is_security=False,
    )
    model = DependencyTreeModel()
    model.set_packages([openssl, curl, nano])
    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)

    def visible_names() -> list[str]:
        return [
            proxy.index(row, model.COL_NAME).data(Qt.ItemDataRole.DisplayRole)
            for row in range(proxy.rowCount())
        ]

    proxy.set_search_query("security:yes")
    assert visible_names() == ["openssl", "curl"]

    proxy.set_search_query("security:no")
    assert visible_names() == ["nano"]

    proxy.set_search_query("severity:critical")
    assert visible_names() == ["openssl"]

    # ``medium`` aliases DNF5's ``Moderate`` severity.
    proxy.set_search_query("severity:medium")
    assert visible_names() == ["curl"]

    proxy.set_search_query("FEDORA-2026-MOD2")
    assert visible_names() == ["curl"]

    proxy.set_search_query("security:invalid")
    assert visible_names() == []

    proxy.set_search_query("severity:bogus")
    assert visible_names() == []


def test_delegate_security_severity_pill_formats_tones_and_translations(qapp) -> None:
    pkg_crit = _pkg(
        "openssl",
        has_update=True,
        update_version="3.2.4-1.fc44",
        is_security=True,
        advisory_id="FEDORA-2026-1",
        advisory_severity="Critical",
    )
    pkg_none = _pkg(
        "curl",
        has_update=True,
        update_version="8.9.0-1.fc44",
        is_security=True,
        advisory_id="FEDORA-2026-2",
        advisory_severity="none",
    )
    pkg_bug = _pkg("nano", has_update=True, update_version="8.3-1.fc44")
    model = DependencyTreeModel()
    model.set_packages([pkg_crit, pkg_none, pkg_bug])

    delegate = PackageTreeItemDelegate()
    for theme in ("dark", "light", "dark_hc"):
        delegate.set_theme(theme)
        pill_crit = delegate._security_severity_pill(model.index(0, model.COL_NAME))
        pill_none = delegate._security_severity_pill(model.index(1, model.COL_NAME))
        pill_bug = delegate._security_severity_pill(model.index(2, model.COL_NAME))

        assert pill_crit is not None and pill_crit[0] == "CRITICAL"
        assert pill_none is not None and pill_none[0] == "SECURITY"
        assert pill_bug is None

    try:
        set_active_locale("de")
        pill_de = delegate._security_severity_pill(model.index(0, model.COL_NAME))
        assert pill_de is not None and pill_de[0] == "KRITISCH"
    finally:
        set_active_locale("en")


def test_header_security_button_visibility_and_search_help_tokens(qtbot) -> None:
    app = QApplication.instance()
    original_font = QFont(app.font())
    original_stylesheet = app.styleSheet()
    try:
        app.setStyleSheet(get_theme_stylesheet("dark"))
        app.setFont(QFont("DejaVu Sans", 10))
        header = HeaderBar()
        qtbot.addWidget(header)
        header.show()

        # Hidden until in updates view AND security updates exist on a mutable host.
        header.configure_host_environment(HostEnvironment.traditional())
        header.update_available_updates_badge(5, security_count=0)
        header.set_updates_view_active(True, 5, security_count=0)
        assert not header.queue_security_updates_btn.isVisibleTo(header)

        header.update_available_updates_badge(5, security_count=2)
        header.set_updates_view_active(False, 5, security_count=2)
        assert not header.queue_security_updates_btn.isVisibleTo(header)
        assert header.heightForWidth(1280) <= 58

        header.set_updates_view_active(True, 5, security_count=2)
        header.resize(1280, header.heightForWidth(1280))
        QApplication.processEvents()
        assert header.queue_security_updates_btn.isVisibleTo(header)
        assert "2" in header.queue_security_updates_btn.toolTip()
        btn_rect = header.queue_security_updates_btn.geometry()
        assert btn_rect.left() >= 0
        assert btn_rect.right() <= 1280
        assert btn_rect.bottom() <= header.height()

        # Hidden on read-only hosts even when updates view is active.
        header.configure_host_environment(
            HostEnvironment(kind=HostEnvironmentKind.ATOMIC_OSTREE, ostree_booted=True)
        )
        assert not header.queue_security_updates_btn.isVisibleTo(header)

        # Search filter validation & reference recognize security:/severity:.
        header.search_input.setText("security:yes severity:critical")
        assert header.search_input.property("filterState") == "valid"
        header.search_input.setText("severity:invalid")
        assert header.search_input.property("filterState") == "invalid"

        tooltip_en = search_filter_tooltip()
        assert "<code>security:</code> / <code>severity:</code>" in tooltip_en
        assert "<code>security:yes</code>" in tooltip_en
        assert "<code>critical</code>" in tooltip_en
    finally:
        app.setFont(original_font)
        app.setStyleSheet(original_stylesheet)


def test_main_window_updates_and_advisory_dialog_queue_security_fixes(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)
    window = MainWindow(host_environment=HostEnvironment.traditional())
    qtbot.addWidget(window)
    monkeypatch.setattr(window.thread_pool, "start", lambda *_args, **_kwargs: None)

    openssl = _pkg("openssl", "3.2.0")
    curl = _pkg("curl", "8.8.0")
    nano = _pkg("nano", "8.2")
    window._all_packages_cache = [openssl, curl, nano]
    window.tree_model.set_packages(window._all_packages_cache)

    worker = SimpleNamespace()
    window.current_updates_worker = worker
    window._on_updates_loaded(
        worker,
        window._update_request_serial,
        {
            "openssl.x86_64": AvailableUpdateInfo(
                name="openssl",
                new_version="3.2.4",
                new_release="1.fc44",
                arch="x86_64",
                repository="updates",
                is_security=True,
                advisory_id="FEDORA-2026-CRIT",
                advisory_severity="Critical",
            ),
            "curl.x86_64": AvailableUpdateInfo(
                name="curl",
                new_version="8.9.0",
                new_release="1.fc44",
                arch="x86_64",
                repository="updates",
                is_security=True,
                advisory_id="FEDORA-2026-IMP",
                advisory_severity="Important",
            ),
            "nano.x86_64": AvailableUpdateInfo(
                name="nano",
                new_version="8.3",
                new_release="1.fc44",
                arch="x86_64",
                repository="updates",
                is_security=False,
            ),
        },
        False,
    )

    assert openssl.is_security_update is True
    assert openssl.advisory_id == "FEDORA-2026-CRIT"
    assert nano.is_security_update is False

    # Switch to Updates view and click "Queue Security Updates Only".
    window._filter_to_updates()
    assert window.header.queue_security_updates_btn.isVisibleTo(window.header)
    window.header.queue_security_updates_btn.click()

    assert openssl.state == PackageState.QUEUED_UPGRADE
    assert curl.state == PackageState.QUEUED_UPGRADE
    assert nano.state == PackageState.INSTALLED
    assert window.tree_model.get_queued_transaction_specs().total_count == 2
    assert "2 security updates" in window.status_bar.currentMessage()

    # Reset queue and test remediation directly from SecurityAdvisoryDialog signals.
    window.tree_model.clear_all_queued()
    window._sync_queue_states()

    sec_one = SecurityAdvisory(
        advisory_id="FEDORA-2026-CRIT",
        title="Critical: openssl",
        advisory_type="security",
        severity="Critical",
        affected_packages=("openssl-3.2.4-1.fc44.x86_64",),
    )
    sec_two = SecurityAdvisory(
        advisory_id="FEDORA-2026-IMP",
        title="Important: curl",
        advisory_type="security",
        severity="Important",
        affected_packages=("curl-8.9.0-1.fc44.x86_64",),
    )

    class _FakeDialog:
        def __init__(self, entries, parent=None) -> None:
            from dendro.ui.advisory_dialog import SecurityAdvisoryDialog

            self.real = SecurityAdvisoryDialog(entries, parent)

        def __getattr__(self, name: str):
            return getattr(self.real, name)

        def exec(self) -> int:
            # 1) Queue only the selected advisory (keeps dialog open).
            self.real.queue_affected_button.click()
            assert openssl.state == PackageState.QUEUED_UPGRADE
            assert curl.state == PackageState.INSTALLED
            # 2) Queue all remaining security advisories.
            self.real.queue_all_button.click()
            return 0

        def deleteLater(self) -> None:
            self.real.deleteLater()

    monkeypatch.setattr(main_window, "SecurityAdvisoryDialog", _FakeDialog)
    window._on_advisories_loaded([sec_one, sec_two])

    assert openssl.state == PackageState.QUEUED_UPGRADE
    assert curl.state == PackageState.QUEUED_UPGRADE
    assert nano.state == PackageState.INSTALLED
    assert window.tree_model.get_queued_transaction_specs().total_count == 2

    # Read-only hosts gate queue_security_updates_only with an informative dialog.
    info_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, text: info_calls.append((title, text)),
    )
    ro_window = MainWindow(
        host_environment=HostEnvironment(
            kind=HostEnvironmentKind.ATOMIC_OSTREE, ostree_booted=True
        )
    )
    qtbot.addWidget(ro_window)
    monkeypatch.setattr(ro_window.thread_pool, "start", lambda *_args, **_kwargs: None)
    ro_window._on_queue_security_updates_only()
    assert len(info_calls) == 1
    ro_window.close()
    window.close()
