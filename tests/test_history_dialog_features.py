"""Qt integration and UI behavior tests for Task 1.4 history features."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from dendro.core.backend import HistoryEntry, HistoryPackageChange, PackageInfo
from dendro.core.dnf5daemon import (
    RecentChangesSummary,
    RecentPackageChange,
    TransactionRequest,
)
from dendro.core.host_environment import HostEnvironment, HostEnvironmentKind
from dendro.ui.history_dialog import DnfHistoryDialog
from dendro.ui.inspector_panel import PackageInspectorPanel
from dendro.ui.main_window_transactions import MainWindowTransactionsMixin


def test_history_dialog_tabs_scope_and_lazy_package_diffs(qtbot) -> None:
    """History dialog renders per-package diffs, lazy CLI fetch, and package scope."""
    dialog = DnfHistoryDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    QApplication.processEvents()

    requested_filters: list[str] = []
    requested_info: list[int] = []
    requested_nav: list[str] = []
    requested_reverse: list[HistoryEntry] = []
    dialog.package_filter_requested.connect(requested_filters.append)
    dialog.transaction_info_requested.connect(requested_info.append)
    dialog.package_navigate_requested.connect(requested_nav.append)
    dialog.reverse_preview_requested.connect(requested_reverse.append)

    dialog.set_package_scope("openssl")
    assert dialog.package_scope() == "openssl"
    assert not dialog.package_scope_label.isHidden()

    serial = dialog.set_history_loading()
    entry_with_packages = HistoryEntry(
        id=101,
        command_line="dnf5 upgrade openssl",
        date_time="2026-11-16 14:30",
        action="Upgrade",
        altered_count=1,
        return_code=0,
        user_id=1000,
        releasever="45",
        comment="CVE batch",
        status="Ok",
        packages=(
            HistoryPackageChange(
                name="openssl",
                arch="x86_64",
                evr="1:3.5.0-2.fc45",
                original_evr="1:3.5.0-1.fc45",
                repo_id="updates",
                action="Upgrade",
                reason="User",
            ),
        ),
    )
    entry_without_packages = HistoryEntry(
        id=100,
        command_line="dnf5 install htop",
        date_time="2026-11-15 09:00",
        action="Install",
        altered_count=2,
        return_code=0,
    )
    assert dialog.set_history_entries_if_current(
        serial, [entry_with_packages, entry_without_packages]
    )

    assert dialog.table.rowCount() == 2
    dialog.inspect_btn.click()
    QApplication.processEvents()
    assert not dialog.detail_panel.isHidden()
    assert dialog.diff_tree.topLevelItemCount() == 1
    group_item = dialog.diff_tree.topLevelItem(0)
    assert group_item is not None and group_item.childCount() == 1
    pkg_child = group_item.child(0)
    assert pkg_child is not None
    assert pkg_child.text(0) == "openssl.x86_64"
    assert pkg_child.text(1) == "1:3.5.0-1.fc45 → 1:3.5.0-2.fc45"
    assert pkg_child.text(2) == "updates"
    assert dialog.preview_reverse_btn.isEnabled()

    # Clicking Reverse emits reverse_preview_requested with selected HistoryEntry.
    dialog.preview_reverse_btn.click()
    assert requested_reverse == [entry_with_packages]

    # Selecting child enables Go to Package and clicking/activating emits navigate.
    dialog.diff_tree.setCurrentItem(pkg_child)
    assert dialog.go_to_pkg_btn.isEnabled()
    dialog.go_to_pkg_btn.click()
    dialog._emit_navigate_for_diff_item(pkg_child)
    assert requested_nav == ["openssl.x86_64", "openssl.x86_64"]

    # Selecting row 1 (altered_count=2, empty packages) emits lazy info lookup once.
    dialog.table.selectRow(1)
    assert requested_info == [100]
    dialog.set_transaction_packages_if_current(
        100,
        (
            HistoryPackageChange(
                name="htop",
                arch="x86_64",
                evr="3.3.0-1.fc45",
                repo_id="fedora",
                action="Install",
                reason="User",
            ),
        ),
    )
    assert dialog.diff_tree.topLevelItemCount() == 1
    assert not dialog.preview_reverse_btn.toolTip().startswith("No ")

    # Clicking Show All clears package scope and emits single package_filter_requested("").
    dialog.clear_scope_btn.click()
    assert dialog.package_scope() == ""
    assert requested_filters == [""]

    dialog.close()
    dialog.deleteLater()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def test_history_dialog_recent_changes_tab_filtering_and_navigation(qtbot) -> None:
    """Switching to Recently Changed loads summary, filters advisories, and navigates."""
    dialog = DnfHistoryDialog()
    qtbot.addWidget(dialog)

    recent_calls: list[tuple[int | None, bool]] = []
    nav_targets: list[str] = []
    dialog.recent_changes_requested.connect(
        lambda since, adv: recent_calls.append((since, adv))
    )
    dialog.package_navigate_requested.connect(nav_targets.append)

    # Tab 1 emits recent_changes_requested cleanly on first switch (default window: Last 7 Days).
    dialog.tabs.setCurrentIndex(1)
    assert len(recent_calls) == 1
    since_val, include_adv = recent_calls[0]
    assert isinstance(since_val, int) and since_val > 0
    assert include_adv is True

    summary = RecentChangesSummary(
        upgraded=(
            RecentPackageChange(
                name="openssl",
                arch="x86_64",
                evr="3.5.0-2.fc45",
                original_evr="3.5.0-1.fc45",
                summary="Cryptography library",
                repo_id="updates",
                transaction_time=1700001000,
                advisories=("FEDORA-2026-1234",),
            ),
        ),
        installed=(
            RecentPackageChange(
                name="ripgrep",
                arch="x86_64",
                evr="14.1.1-1.fc45",
                summary="Fast search tool",
                repo_id="fedora",
                transaction_time=1700001000,
            ),
        ),
    )
    serial = dialog.set_recent_changes_loading()
    assert dialog.set_recent_changes_if_current(serial, summary)
    assert dialog.recent_table.rowCount() == 2

    # Filtering by advisory ID or toggling Advisories-only retains only matching row.
    dialog.recent_search_input.setText("FEDORA-2026-1234")
    assert dialog.recent_table.rowCount() == 1
    pkg_item = dialog.recent_table.item(0, 0)
    assert pkg_item is not None
    assert pkg_item.data(Qt.ItemDataRole.UserRole) == "openssl.x86_64"
    dialog.recent_search_input.clear()
    assert dialog.recent_table.rowCount() == 2
    dialog.recent_advisory_only_cb.setChecked(True)
    assert dialog.recent_table.rowCount() == 1

    pkg_item = dialog.recent_table.item(0, 0)
    assert pkg_item is not None
    dialog._on_recent_item_double_clicked(pkg_item)
    assert nav_targets == ["openssl.x86_64"]

    # Selecting Latest Transaction re-emits recent_changes_requested with None.
    dialog.recent_scope_combo.setCurrentIndex(0)
    assert len(recent_calls) == 2
    assert recent_calls[1][0] is None

    # Selecting Last 30 Days re-emits recent_changes_requested with a non-None timestamp.
    dialog.recent_scope_combo.setCurrentIndex(2)
    assert len(recent_calls) == 3
    assert isinstance(recent_calls[2][0], int)

    dialog.deleteLater()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def test_inspector_history_button_emits_package_name(qtbot) -> None:
    """PackageInspectorPanel exposes a History action linked to the inspected package."""
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)

    emitted: list[str] = []
    panel.history_requested.connect(emitted.append)
    assert not panel.history_btn.isEnabled()

    pkg = PackageInfo(
        name="mesa-dri-drivers",
        version="25.1.2",
        release="1.fc45",
        arch="x86_64",
    )
    panel.set_package_info(pkg)
    assert panel.history_btn.isEnabled()
    panel.history_btn.click()
    assert emitted == ["mesa-dri-drivers"]

    panel.clear()
    assert not panel.history_btn.isEnabled()

    panel.deleteLater()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def test_history_dialog_tolerates_string_counts_on_legacy_entries(qtbot) -> None:
    """HistoryEntry instances constructed with string counts format cleanly."""
    dialog = DnfHistoryDialog()
    qtbot.addWidget(dialog)

    legacy = HistoryEntry(
        id=1,
        command_line="dnf upgrade",
        date_time="2026-11-17 10:00",
        action="Upgrade",
        altered_count="2",  # type: ignore[arg-type]
        return_code="0",  # type: ignore[arg-type]
    )
    dialog.set_history_entries([legacy])
    assert dialog.table.rowCount() == 1
    dialog.inspect_btn.click()
    assert "#1" in dialog.detail_title_label.text()
    assert "Altered: 2" in dialog.detail_meta_label.text()

    dialog.deleteLater()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def test_main_window_history_navigation_and_reverse_preview(
    monkeypatch: pytest.MonkeyPatch, qtbot
) -> None:
    """MainWindowTransactionsMixin wires navigation, CLI-info gating, and reverse plans."""
    started_resolutions: list[tuple[TransactionRequest, str]] = []
    navigated_pkgs: list[PackageInfo] = []
    status_messages: list[str] = []
    errors_seen: list[str] = []

    class _FakeDialog:
        def __init__(self) -> None:
            self.accepted = False
            self.status_label = SimpleNamespace(text="")
            self.status_label.setText = lambda t: setattr(self.status_label, "text", t)
            self.info_errors: list[tuple[int, str]] = []

        def accept(self) -> None:
            self.accepted = True

        def set_transaction_info_error(self, tx_id: int, detail: str) -> None:
            self.info_errors.append((tx_id, detail))

    class _Harness(MainWindowTransactionsMixin):
        def __init__(self) -> None:
            self.status_bar = SimpleNamespace(
                showMessage=lambda msg, *_a: status_messages.append(msg)
            )
            self.transaction_controller = SimpleNamespace(
                is_busy=False, has_ready_plan=False
            )
            self._env = HostEnvironment.traditional()
            self._lookup: dict[str, PackageInfo] = {
                "openssl.x86_64": PackageInfo(
                    name="openssl",
                    version="3.5.0",
                    release="2.fc45",
                    arch="x86_64",
                )
            }

        def _find_local_package_item(self, target: str):
            pkg = self._lookup.get(target)
            return SimpleNamespace(payload=pkg) if pkg is not None else None

        def _navigate_to_package(self, pkg: PackageInfo) -> None:
            navigated_pkgs.append(pkg)

        def _begin_transaction_resolution(
            self, request: TransactionRequest, kind: str
        ) -> None:
            started_resolutions.append((request, kind))

        def _show_transaction_error(self, title: str, detail: str) -> None:
            errors_seen.append(f"{title}: {detail}")

    harness = _Harness()
    monkeypatch.setattr(
        "dendro.ui.main_window_transactions._window_host_environment",
        lambda _w: harness._env,
    )

    # 1. Non-installed package keeps dialog open and reports status.
    dlg = _FakeDialog()
    harness._on_history_package_navigate(
        dlg,  # type: ignore[arg-type]
        "nonexistent.x86_64",
    )
    assert dlg.accepted is False
    assert "nonexistent.x86_64" in dlg.status_label.text
    assert navigated_pkgs == []

    # 2. Installed package closes dialog and navigates.
    harness._on_history_package_navigate(
        dlg,  # type: ignore[arg-type]
        "openssl.x86_64",
    )
    assert dlg.accepted is True
    assert len(navigated_pkgs) == 1 and navigated_pkgs[0].name == "openssl"

    # 3. CLI-info query cleanly explains unavailability on Flatpak / OSTree without worker.
    harness._env = HostEnvironment(kind=HostEnvironmentKind.ATOMIC_OSTREE)
    harness._load_history_entry_info(dlg, 55)  # type: ignore[arg-type]
    assert dlg.info_errors and dlg.info_errors[0][0] == 55

    # 4. Reverse preview opens QMessageBox confirmation then calls _begin_transaction_resolution.
    harness._env = HostEnvironment.traditional()
    dlg2 = _FakeDialog()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_a, **_kw: QMessageBox.StandardButton.Yes,
    )
    entry = HistoryEntry(
        id=88,
        command_line="dnf5 install htop",
        date_time="2026-11-16 15:00",
        action="Install",
        altered_count=1,
        return_code=0,
        packages=(
            HistoryPackageChange(
                name="htop",
                arch="x86_64",
                evr="3.3.0-1.fc45",
                action="Install",
                reason="User",
            ),
        ),
    )
    harness._on_history_reverse_preview_requested(dlg2, entry)  # type: ignore[arg-type]
    assert dlg2.accepted is True
    assert len(started_resolutions) == 1
    req, kind = started_resolutions[0]
    assert kind == "history_reverse"
    assert req.removals == ("htop.x86_64",)
    assert errors_seen == []
