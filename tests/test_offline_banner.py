"""Headless Qt widget and window-wiring tests for OfflineUpdateBanner & reboot UX."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

import dendro.core.i18n as core_i18n
from dendro.core.dnf5daemon_types import (
    OfflineTransactionPhase,
    OfflineTransactionStatus,
)
from dendro.core.host_environment import HostEnvironment, HostEnvironmentKind
from dendro.core.system_reboot import RebootCapability
from dendro.ui.main_window import MainWindow
from dendro.ui.main_window_transactions import MainWindowTransactionsMixin
from dendro.ui.offline_banner import OfflineUpdateBanner


@pytest.fixture(autouse=True)
def _restore_locale():
    core_i18n.clear_cache()
    core_i18n.set_active_locale("en")
    yield
    core_i18n.clear_cache()
    core_i18n.set_active_locale("en")


def test_offline_banner_visibility_across_phases_and_environments(qtbot) -> None:
    banner = OfflineUpdateBanner()
    qtbot.addWidget(banner)

    assert banner.isHidden() is True

    idle = OfflineTransactionStatus(
        pending=False, status="", phase=OfflineTransactionPhase.NONE
    )
    banner.update_status(idle, HostEnvironment.traditional(), RebootCapability.YES)
    assert banner.isHidden() is True

    ready = OfflineTransactionStatus(
        pending=True,
        status="ready",
        phase=OfflineTransactionPhase.READY,
        poweroff_after=True,
        verb="upgrade",
        cmd_line="dnf5 upgrade --offline",
        target_releasever="45",
        system_releasever="44",
        cachedir="/var/cache/libdnf5",
    )
    banner.update_status(
        ready,
        HostEnvironment(kind=HostEnvironmentKind.ATOMIC_OSTREE, ostree_booted=True),
        RebootCapability.YES,
    )
    assert banner.isHidden() is True

    banner.update_status(ready, HostEnvironment.traditional(), RebootCapability.YES)
    assert banner.isHidden() is False
    assert banner.property("offlinePhase") == "ready"
    assert banner.phase_badge.text() == "SCHEDULED"
    assert banner.poweroff_after_checkbox.isChecked() is True
    assert banner.reboot_btn.isHidden() is False
    assert banner.reboot_btn.isEnabled() is True
    assert banner.cancel_btn.isHidden() is False
    assert banner.clean_btn.isHidden() is False
    assert banner.schedule_btn.isHidden() is True
    assert "Fedora 44 -> 45" in banner.meta_label.text()
    assert "After boot update: Power off" in banner.meta_label.text()

    banner.update_status(
        ready, HostEnvironment.traditional(), RebootCapability.INHIBITOR_BLOCKED
    )
    assert banner.reboot_btn.isEnabled() is False
    assert "inhibitor" in banner.detail_label.text().lower()

    banner.update_status(ready, HostEnvironment.flatpak(), RebootCapability.YES)
    assert banner.isHidden() is False
    assert banner.reboot_btn.isHidden() is True
    assert "desktop menu" in banner.detail_label.text()
    assert banner.cancel_btn.isHidden() is False
    assert banner.clean_btn.isHidden() is False

    pre_staged = OfflineTransactionStatus(
        pending=False,
        status="download-complete",
        phase=OfflineTransactionPhase.DOWNLOAD_COMPLETE,
        poweroff_after=False,
    )
    banner.update_status(
        pre_staged, HostEnvironment.traditional(), RebootCapability.UNAVAILABLE
    )
    assert banner.property("offlinePhase") == "download-complete"
    assert banner.phase_badge.text() == "PRE-STAGED"
    assert banner.schedule_btn.isHidden() is False
    assert banner.cancel_btn.isHidden() is True
    assert banner.reboot_btn.isHidden() is True
    assert banner.clean_btn.isHidden() is False
    assert banner.poweroff_after_checkbox.isChecked() is False

    incomplete = OfflineTransactionStatus(
        pending=False,
        status="transaction-incomplete",
        phase=OfflineTransactionPhase.TRANSACTION_INCOMPLETE,
    )
    banner.update_status(
        incomplete, HostEnvironment.traditional(), RebootCapability.UNAVAILABLE
    )
    assert banner.property("offlinePhase") == "warning"
    assert banner.phase_badge.text() == "INCOMPLETE"
    assert banner.clean_btn.isHidden() is False
    assert banner.schedule_btn.isHidden() is True
    assert banner.cancel_btn.isHidden() is True
    assert banner.reboot_btn.isHidden() is True
    assert banner.poweroff_after_checkbox.isHidden() is True


def test_offline_banner_signals_busy_state_retranslate_and_wrapping(qtbot) -> None:
    banner = OfflineUpdateBanner()
    qtbot.addWidget(banner)
    banner.show()
    QApplication.processEvents()

    emitted_finish: list[str] = []
    counts = {"schedule": 0, "cancel": 0, "clean": 0, "reboot": 0}
    banner.finish_action_requested.connect(emitted_finish.append)
    banner.schedule_requested.connect(
        lambda: counts.__setitem__("schedule", counts["schedule"] + 1)
    )
    banner.cancel_requested.connect(
        lambda: counts.__setitem__("cancel", counts["cancel"] + 1)
    )
    banner.clean_requested.connect(
        lambda: counts.__setitem__("clean", counts["clean"] + 1)
    )
    banner.reboot_requested.connect(
        lambda: counts.__setitem__("reboot", counts["reboot"] + 1)
    )

    # Programmatic update must not emit finish_action_requested.
    pre_staged = OfflineTransactionStatus(
        pending=False,
        status="download-complete",
        phase=OfflineTransactionPhase.DOWNLOAD_COMPLETE,
        poweroff_after=False,
    )
    banner.update_status(
        pre_staged, HostEnvironment.traditional(), RebootCapability.UNAVAILABLE
    )
    assert emitted_finish == []

    banner.poweroff_after_checkbox.click()
    banner.schedule_btn.click()
    banner.clean_btn.click()
    assert emitted_finish == ["poweroff"]
    assert counts["schedule"] == 1
    assert counts["clean"] == 1

    ready = OfflineTransactionStatus(
        pending=True,
        status="ready",
        phase=OfflineTransactionPhase.READY,
        poweroff_after=True,
    )
    banner.update_status(ready, HostEnvironment.traditional(), RebootCapability.YES)
    banner.reboot_btn.click()
    banner.cancel_btn.click()
    assert counts["reboot"] == 1
    assert counts["cancel"] == 1

    banner.set_busy(True)
    assert banner.reboot_btn.isEnabled() is False
    assert banner.cancel_btn.isEnabled() is False
    assert banner.clean_btn.isEnabled() is False
    assert banner.poweroff_after_checkbox.isEnabled() is False
    banner.set_busy(False)
    assert banner.reboot_btn.isEnabled() is True

    core_i18n.set_active_locale("de")
    banner.retranslate()
    assert banner.phase_badge.text() == "GEPLANT"

    actions_layout = banner.actions_widget.layout()
    assert actions_layout is not None
    assert actions_layout.hasHeightForWidth() is True
    assert actions_layout.heightForWidth(280) > actions_layout.heightForWidth(900)
    assert banner.minimumSizeHint().width() <= 400


def test_mainwindow_keeps_half_screen_tiling_when_offline_banner_visible(
    qtbot, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)
    app = QApplication.instance()
    original_font = QFont(app.font())
    try:
        app.setFont(QFont("DejaVu Sans", 12))
        window = MainWindow(host_environment=HostEnvironment.traditional())
        qtbot.addWidget(window)
        monkeypatch.setattr(window.thread_pool, "start", lambda *_a, **_kw: None)

        status = OfflineTransactionStatus(
            pending=True,
            status="ready",
            phase=OfflineTransactionPhase.READY,
            cmd_line="dnf5 upgrade --offline",
        )
        window.offline_banner.update_status(
            status, HostEnvironment.traditional(), RebootCapability.YES
        )
        QApplication.processEvents()
        assert window.minimumSizeHint().width() <= 640
        window.close()
    finally:
        app.setFont(original_font)


def test_main_window_transactions_mixin_offline_staging_and_reboot_flow(
    monkeypatch,
) -> None:
    import dendro.ui.main_window as main_win_mod
    import dendro.ui.main_window_transactions as tx_mod

    class _FakePlan:
        def categorized_names(self):
            return ([], [], ["bash"])

    class _FakeDialog:
        def __init__(self, *, plan, parent=None) -> None:
            self.plan = plan
            self.parent = parent

        def exec(self) -> int:
            return QDialog.DialogCode.Accepted

        def stage_for_next_boot(self) -> bool:
            return True

        def stage_download_only(self) -> bool:
            return True

        def stage_finish_action(self) -> str:
            return "poweroff"

    class _FakeController:
        def __init__(self) -> None:
            self.stage_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
            self.clean_calls = 0
            self.is_busy = False
            self.has_ready_plan = False

        def stage_ready_plan(self, *args, **kwargs) -> None:
            self.stage_calls.append((args, kwargs))

        def schedule_offline_transaction(self) -> None:
            pass

        def clean_offline_transaction(self) -> None:
            self.clean_calls += 1

    class _FakeTreeModel:
        def __init__(self) -> None:
            self.cleared = 0

        def clear_all_queued(self) -> None:
            self.cleared += 1

    class _FakeBanner:
        def __init__(self) -> None:
            self.busy_calls: list[bool] = []

        def set_busy(self, busy: bool) -> None:
            self.busy_calls.append(busy)

    class _Harness(MainWindowTransactionsMixin):
        def __init__(self) -> None:
            self.transaction_controller = _FakeController()
            self.transaction_drawer = SimpleNamespace(
                set_transaction_preview=lambda *_a: None,
                start_execution_mode=lambda: None,
                finish_execution_mode=lambda _ok: None,
            )
            self.tree_model = _FakeTreeModel()
            self.status_bar = SimpleNamespace(messages=[])
            self.status_bar.showMessage = self.status_bar.messages.append
            self.offline_banner = _FakeBanner()
            self._pending_updates_map = {"bash.x86_64": object()}
            self._last_transaction_upgrades = ["bash.x86_64"]
            self._last_transaction_removals = []
            self._orphan_cache = {"old-1-1.x86_64"}
            self._user_installed_cache = set()
            self._pending_transaction_kind = ""
            self._active_repo_dialog = None
            self._repo_dialog_op_kind = ""
            self.load_packages_calls = 0
            self.offline_refresh_calls: list[bool] = []
            self.reboot_started = 0
            self.sync_calls = 0
            self.errors: list[tuple[str, str]] = []

        def _sync_queue_states(self) -> None:
            self.sync_calls += 1

        def _load_packages(self) -> None:
            self.load_packages_calls += 1

        def _start_offline_status_check(
            self, *, prompt_reboot_if_ready: bool = False
        ) -> None:
            self.offline_refresh_calls.append(prompt_reboot_if_ready)

        def _show_transaction_error(self, title: str, detail: str) -> None:
            self.errors.append((title, detail))

        def _continue_close_if_requested(self) -> None:
            pass

        def _start_system_reboot(self) -> None:
            self.reboot_started += 1

    monkeypatch.setattr(main_win_mod, "TransactionPlanReviewDialog", _FakeDialog)
    h = _Harness()
    h._on_dnf_plan_ready(_FakePlan())
    assert h.transaction_controller.stage_calls == [
        ((), {"download_only": True, "finish_action": "poweroff"})
    ]
    assert h._pending_transaction_kind == "stage_download_only"

    h._on_transaction_finished(True, "")
    assert h.load_packages_calls == 0
    assert "bash.x86_64" in h._pending_updates_map
    assert "old-1-1.x86_64" in h._orphan_cache
    assert h.tree_model.cleared == 1
    assert h.offline_refresh_calls == [False]
    assert h._pending_transaction_kind == ""

    h._pending_transaction_kind = "stage_offline"
    h._on_transaction_finished(True, "")
    assert h.offline_refresh_calls == [False, True]

    h.offline_banner.set_busy(True)
    h._on_maintenance_finished("schedule offline update", True, "")
    assert h.offline_banner.busy_calls[-1] is False
    assert h.offline_refresh_calls == [False, True, False]

    questions: list[str] = []

    def _fake_question(_parent, title, text, *_args, **_kwargs):
        questions.append(f"{title}:{text}")
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(tx_mod.QMessageBox, "question", _fake_question)
    monkeypatch.setattr(
        main_win_mod,
        "_window_host_environment",
        lambda _self: HostEnvironment.traditional(),
    )
    h._on_offline_clean_requested()
    assert h.transaction_controller.clean_calls == 1

    h._prompt_reboot_to_apply_offline_update(
        OfflineTransactionStatus(
            pending=True,
            status="ready",
            phase=OfflineTransactionPhase.READY,
            poweroff_after=True,
        )
    )
    assert len(questions) == 2
    assert "power off automatically" in questions[1]
    assert h.reboot_started == 1

    # Direct call to _start_system_reboot under Flatpak refuses without spawning a worker.
    monkeypatch.setattr(
        main_win_mod,
        "_window_host_environment",
        lambda _self: HostEnvironment.flatpak(),
    )
    MainWindowTransactionsMixin._start_system_reboot(h)
    assert h.errors and "Flatpak" in h.errors[-1][0]
