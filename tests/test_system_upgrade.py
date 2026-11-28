"""Comprehensive unit and headless Qt tests for 2.1 Fedora System Release Upgrade Assistant."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QDialog

import dendro.core.fedora_releases as fedora_releases_mod
import dendro.core.i18n as core_i18n
from dendro.core.dnf5daemon import (
    GOAL_INTERFACE,
    OFFLINE_INTERFACE,
    ROOT_PATH,
    RPM_INTERFACE,
    SESSION_MANAGER_INTERFACE,
    Dnf5DaemonClient,
    Dnf5DaemonPlanError,
    Dnf5DaemonUnsupportedError,
    Dnf5DaemonValidationError,
    OfflineTransactionStatus,
    TransactionItem,
    TransactionPlan,
    TransactionRequest,
)
from dendro.core.dnf5daemon_parsers import _offline_rpm_diagnostic_guidance
from dendro.core.fedora_releases import (
    CurrentFedoraRelease,
    FedoraReleaseDiscoveryResult,
    FedoraReleasesQueryWorker,
    FedoraTargetRelease,
    _fetch_https_json,
    _HTTPSOnlyRedirectHandler,
    build_local_fallback_targets,
    detect_current_fedora_release,
    discover_fedora_releases,
    parse_bodhi_releases,
    parse_fedora_releases_json,
    validate_target_releasever,
)
from dendro.core.host_environment import HostEnvironment, HostEnvironmentKind
from dendro.core.transaction_controller import (
    DnfTransactionController,
    TransactionState,
)
from dendro.ui.header import HeaderBar
from dendro.ui.main_window import MainWindow
from dendro.ui.main_window_transactions import MainWindowTransactionsMixin
from dendro.ui.system_upgrade_dialog import SystemReleaseUpgradeDialog
from dendro.ui.transaction_plan_dialog import TransactionPlanReviewDialog
from tests.test_host_environment import FakeHostProbe


@pytest.fixture(autouse=True)
def _restore_locale():
    core_i18n.clear_cache()
    core_i18n.set_active_locale("en")
    yield
    core_i18n.clear_cache()
    core_i18n.set_active_locale("en")


@dataclass
class _SystemUpgradeTransport:
    """In-memory D-Bus transport stub tracking open_session & Rpm.system_upgrade."""

    unsupported_system_upgrade: bool = False
    emit_key_prompt: bool = False
    calls: list[tuple[str, str, str, tuple[str, ...], tuple[Any, ...]]] = field(
        default_factory=list
    )
    callbacks: dict[object, Any] = field(default_factory=dict)
    _next_token: int = 0

    def call(
        self,
        path: str,
        interface: str,
        method: str,
        signatures: tuple[str, ...],
        arguments: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        self.calls.append(
            (path, interface, method, tuple(signatures), tuple(arguments))
        )
        if interface == SESSION_MANAGER_INTERFACE and method == "open_session":
            return (f"{ROOT_PATH}/session/sysup1",)
        if interface == SESSION_MANAGER_INTERFACE and method == "close_session":
            return (True,)
        if interface == RPM_INTERFACE and method == "system_upgrade":
            if self.unsupported_system_upgrade:
                raise Dnf5DaemonUnsupportedError(
                    "DNF5 daemon interface unsupported",
                    "UnknownMethod: org.rpm.dnf.v0.rpm.Rpm.system_upgrade",
                    dbus_name="org.freedesktop.DBus.Error.UnknownMethod",
                )
            return ()
        if interface == GOAL_INTERFACE and method == "resolve":
            if self.emit_key_prompt:
                for (_s_path, _s_iface, signal_name), cb in tuple(
                    self.callbacks.items()
                ):
                    if signal_name == "repo_key_import_request":
                        cb(
                            (
                                path,
                                "FEDORA45-KEY",
                                ["Fedora (45) <fedora-45-primary@fedoraproject.org>"],
                                "AABBCCDDEEFF00112233445566778899",
                                "https://fedoraproject.org/fedora.gpg",
                                1730000000,
                            )
                        )
            items = [
                (
                    "Package",
                    "Upgrade",
                    "User",
                    {},
                    {
                        "name": "fedora-release",
                        "version": "45",
                        "release": "1",
                        "arch": "noarch",
                        "download_size": 102400,
                        "install_size": 50000,
                    },
                ),
                (
                    "Package",
                    "Install",
                    "Dependency",
                    {},
                    {
                        "name": "kernel-core",
                        "version": "6.17.1",
                        "release": "100.fc45",
                        "arch": "x86_64",
                        "download_size": 25_000_000,
                        "install_size": 95_000_000,
                    },
                ),
                (
                    "Package",
                    "Downgrade",
                    "External User",
                    {},
                    {
                        "name": "mesa-dri-drivers",
                        "version": "25.2.0",
                        "release": "1.fc45",
                        "arch": "x86_64",
                    },
                ),
                (
                    "Package",
                    "Remove",
                    "Dependency",
                    {"obsolete": True},
                    {
                        "name": "legacy-shim",
                        "version": "1.0",
                        "release": "1.fc44",
                        "arch": "x86_64",
                    },
                ),
                (
                    "Package",
                    "Removed",
                    "Dependency",
                    {"replaced_by": ["fedora-release-45-1.noarch"]},
                    {
                        "name": "fedora-release",
                        "version": "44",
                        "release": "1",
                        "arch": "noarch",
                    },
                ),
                (
                    "Group",
                    "Upgrade",
                    "User",
                    {},
                    {"id": "workstation-product", "name": "Fedora Workstation"},
                ),
            ]
            return (items, 0)
        if interface == GOAL_INTERFACE and method == "do_transaction":
            return ()
        if interface == OFFLINE_INTERFACE and method in {
            "clean",
            "set_finish_action",
            "set_finish_action_with_options",
        }:
            return (True, "")
        return ()

    def subscribe(
        self, path: str, interface: str, signal_name: str, callback
    ) -> object:
        self._next_token += 1
        token = (path, interface, signal_name, self._next_token)
        # Keep simple match key for key-import trigger lookup
        self.callbacks[(path, interface, signal_name)] = callback
        return token

    def unsubscribe(self, token: object) -> None:
        return None


def test_transaction_request_system_upgrade_validation_and_mutual_exclusion() -> None:
    req = TransactionRequest(
        system_upgrade_releasever="45",
        system_upgrade_mode="distrosync",
        allow_erasing=True,
    )
    req.validate()
    assert req.system_upgrade_releasever == "45"
    assert req.system_upgrade_mode == "distrosync"
    assert req.allow_erasing is True
    assert req.is_system_upgrade is True

    rawhide = TransactionRequest.from_values(
        system_upgrade_releasever="rawhide",
        system_upgrade_mode="upgrade",
    )
    assert rawhide.is_system_upgrade is True
    assert rawhide.system_upgrade_mode == "upgrade"

    for bad_rel in (" 45 ", "../etc", "45/beta", "45\\foo", "45\n46"):
        with pytest.raises(Dnf5DaemonValidationError):
            TransactionRequest.from_values(system_upgrade_releasever=bad_rel)

    with pytest.raises(Dnf5DaemonValidationError, match="distrosync"):
        TransactionRequest.from_values(
            system_upgrade_releasever="45", system_upgrade_mode="rebase"
        )

    with pytest.raises(Dnf5DaemonValidationError, match="combined"):
        TransactionRequest.from_values(
            installs=["htop"], system_upgrade_releasever="45"
        )

    with pytest.raises(Dnf5DaemonValidationError, match="combined"):
        TransactionRequest.from_values(upgrade_all=True, system_upgrade_releasever="45")


def test_transaction_plan_breakdown_and_preview_text_for_system_upgrade() -> None:
    req = TransactionRequest.from_values(
        system_upgrade_releasever="45", system_upgrade_mode="distrosync"
    )
    items = (
        TransactionItem(
            object_type="Package",
            action="Upgrade",
            reason="User",
            attributes={},
            package={
                "name": "bash",
                "version": "5.3",
                "release": "1.fc45",
                "arch": "x86_64",
            },
        ),
        TransactionItem(
            object_type="Package",
            action="Install",
            reason="Dependency",
            attributes={},
            package={
                "name": "kernel",
                "version": "6.17",
                "release": "1.fc45",
                "arch": "x86_64",
            },
        ),
        TransactionItem(
            object_type="Package",
            action="Downgrade",
            reason="User",
            attributes={},
            package={
                "name": "mesa",
                "version": "25.1",
                "release": "1.fc45",
                "arch": "x86_64",
            },
        ),
        TransactionItem(
            object_type="Package",
            action="Remove",
            reason="Dependency",
            attributes={},
            package={
                "name": "explicit-drop",
                "version": "1.0",
                "release": "1.fc44",
                "arch": "x86_64",
            },
        ),
        TransactionItem(
            object_type="Package",
            action="Replaced",
            reason="Dependency",
            attributes={},
            package={
                "name": "old-shim",
                "version": "1.0",
                "release": "1.fc44",
                "arch": "x86_64",
            },
        ),
        TransactionItem(
            object_type="Package",
            action="Replaced",
            reason="Dependency",
            attributes={},
            package={
                "name": "bash",
                "version": "5.2",
                "release": "1.fc44",
                "arch": "x86_64",
            },
        ),
        TransactionItem(
            object_type="Group",
            action="Upgrade",
            reason="User",
            attributes={},
            package={"id": "core", "name": "Core"},
        ),
        TransactionItem(
            object_type="Skipped",
            action="Skipped",
            reason="Unavailable",
            attributes={},
            package={"name": "ignored-pkg"},
        ),
    )
    plan = TransactionPlan(
        token="sysup-plan",
        session_path="/org/rpm/dnf/v0/session/sysup1",
        request=req,
        items=items,
        resolver_result=0,
        warnings=(),
        critical_removals=(),
    )

    breakdown = plan.summary_breakdown()
    assert breakdown.upgrades == 1
    assert breakdown.installs == 1
    assert breakdown.downgrades == 1
    assert breakdown.removals == 1
    assert breakdown.obsoletes == 1
    assert breakdown.replaced_paired == 1
    assert breakdown.groups == 1
    assert breakdown.skipped == 1
    assert plan.obsolete_names() == ["old-shim-1.0-1.fc44.x86_64"]

    preview = plan.preview_text()
    assert "Target Fedora release: 45 (mode: distrosync)" in preview
    assert "Upgrade summary:" in preview
    assert "Obsolete: old-shim-1.0-1.fc44.x86_64" in preview

    guidance = _offline_rpm_diagnostic_guidance(req)
    assert "dnf5 system-upgrade download --releasever=45" in guidance


def test_dnf5_client_system_upgrade_resolve_key_prompt_and_offline_only_guards() -> (
    None
):
    transport = _SystemUpgradeTransport(emit_key_prompt=True)
    accepted_prompts: list[str] = []

    def _confirm_key(prompt) -> bool:
        accepted_prompts.append(prompt.key_id)
        return True

    client = Dnf5DaemonClient(
        transport=transport,
        key_import_prompt=_confirm_key,
        environment_detector=HostEnvironment.traditional,
    )
    request = TransactionRequest.from_values(
        system_upgrade_releasever="45",
        system_upgrade_mode="distrosync",
        allow_erasing=True,
    )
    plan = client.resolve(request, {"systemd"})

    assert accepted_prompts == ["FEDORA45-KEY"]
    open_calls = [c for c in transport.calls if c[2] == "open_session"]
    assert len(open_calls) == 1
    assert open_calls[0][4][0].get("releasever") == "45"

    sysup_calls = [c for c in transport.calls if c[2] == "system_upgrade"]
    assert len(sysup_calls) == 1
    assert sysup_calls[0][1] == RPM_INTERFACE
    assert sysup_calls[0][4][0]["mode"] == "distrosync"
    assert sysup_calls[0][4][0]["interactive"] is True

    resolve_calls = [c for c in transport.calls if c[2] == "resolve"]
    assert len(resolve_calls) == 1
    assert resolve_calls[0][4][0]["allow_erasing"] is True

    # Live execute MUST be blocked cleanly.
    with pytest.raises(Dnf5DaemonPlanError, match="offline"):
        client.execute(plan)

    # Offline staging executes Goal.do_transaction({"offline": True}).
    client.execute_offline(plan, finish_action="poweroff")
    do_tx_calls = [c for c in transport.calls if c[2] == "do_transaction"]
    assert len(do_tx_calls) == 1
    assert do_tx_calls[0][4][0]["offline"] is True
    finish_calls = [
        c
        for c in transport.calls
        if c[2] in {"set_finish_action_with_options", "set_finish_action"}
    ]
    assert finish_calls and finish_calls[0][4][0] == "poweroff"


def test_dnf5_client_reports_unsupported_system_upgrade_for_old_daemons() -> None:
    transport = _SystemUpgradeTransport(unsupported_system_upgrade=True)
    client = Dnf5DaemonClient(
        transport=transport,
        environment_detector=HostEnvironment.traditional,
    )
    request = TransactionRequest.from_values(system_upgrade_releasever="45")
    with pytest.raises(Dnf5DaemonUnsupportedError, match=r"5\.2\.8"):
        client.resolve(request, set())


def test_transaction_controller_enforces_offline_execution_for_system_upgrade(
    qtbot,
) -> None:
    transport = _SystemUpgradeTransport()
    client = Dnf5DaemonClient(
        transport=transport,
        environment_detector=HostEnvironment.traditional,
    )
    controller = DnfTransactionController(
        client=client, protected_packages_provider=set
    )

    ready_plans: list[TransactionPlan] = []
    finished: list[tuple[bool, str]] = []
    controller.plan_ready.connect(ready_plans.append)
    controller.transaction_finished.connect(lambda ok, msg: finished.append((ok, msg)))

    req = TransactionRequest.from_values(
        system_upgrade_releasever="45", allow_erasing=True
    )
    controller.resolve(req)
    qtbot.waitUntil(lambda: len(ready_plans) == 1, timeout=4000)
    assert controller.state == TransactionState.READY

    with pytest.raises(Dnf5DaemonPlanError, match="offline"):
        controller.execute_ready_plan()

    controller.stage_ready_plan(download_only=False, finish_action="reboot")
    qtbot.waitUntil(lambda: len(finished) == 1, timeout=4000)
    assert finished == [(True, "")]


def test_fedora_release_detection_discovery_validation_and_security() -> None:
    # Flatpak prefers /run/host/os-release over container /etc/os-release.
    flatpak_probe = FakeHostProbe(
        files={
            "/run/host/os-release": 'ID=fedora\nVERSION_ID=43\nPRETTY_NAME="Fedora Linux 43 (Workstation Edition)"\n',
            "/etc/os-release": "ID=org.fedoraproject.Platform\nVERSION_ID=42\n",
        }
    )
    cur_fp = detect_current_fedora_release(flatpak_probe, in_flatpak=True)
    assert cur_fp.version_id == "43"
    assert cur_fp.is_fedora is True
    assert cur_fp.numeric_version == 43

    # Fallback to OfflineTransactionStatus.system_releasever if file missing.
    cur_fallback = detect_current_fedora_release(
        FakeHostProbe(),
        in_flatpak=False,
        offline_status=OfflineTransactionStatus(pending=False, system_releasever="44"),
    )
    assert cur_fallback.version_id == "44"

    # Bodhi parsing filters non-Fedora, archived, and <= current releases.
    current = CurrentFedoraRelease(
        distro_id="fedora", version_id="43", pretty_name="Fedora Linux 43"
    )
    bodhi_payload = {
        "releases": [
            {"id_prefix": "FEDORA", "version": "42", "state": "archived"},
            {"id_prefix": "FEDORA", "version": "43", "state": "current"},
            {
                "id_prefix": "FEDORA",
                "version": "44",
                "state": "current",
                "eol": "2027-06-01",
            },
            {"id_prefix": "FEDORA", "version": "45", "state": "frozen"},
            {"id_prefix": "FEDORA", "version": "46", "state": "pending"},
            {"id_prefix": "FEDORA", "version": "rawhide", "state": "pending"},
            {"id_prefix": "FEDORA-EPEL", "version": "10", "state": "current"},
        ]
    }
    parsed_bodhi = parse_bodhi_releases(bodhi_payload, current)
    assert [t.releasever for t in parsed_bodhi] == ["44", "45", "46", "rawhide"]
    assert parsed_bodhi[0].status == "stable" and parsed_bodhi[0].is_prerelease is False
    assert parsed_bodhi[1].status == "beta" and parsed_bodhi[1].is_prerelease is True
    assert (
        parsed_bodhi[2].status == "branched" and parsed_bodhi[2].is_prerelease is True
    )
    assert parsed_bodhi[3].status == "rawhide" and parsed_bodhi[3].is_prerelease is True

    # releases.json parsing prefers stable over beta for same numeric version.
    rel_json = parse_fedora_releases_json(
        [
            {"version": "44 Beta"},
            {"version": "44"},
            {"version": "45 Beta"},
            {"version": "rawhide"},
            {"version": "43"},
        ],
        current,
    )
    assert [t.releasever for t in rel_json] == ["44", "45", "rawhide"]
    assert rel_json[0].is_prerelease is False
    assert rel_json[1].is_prerelease is True

    # Local fallback N+1 / N+2 / rawhide
    fallback = build_local_fallback_targets(current)
    assert [t.releasever for t in fallback] == ["44", "45", "rawhide"]
    assert (
        build_local_fallback_targets(
            CurrentFedoraRelease(distro_id="fedora", version_id="rawhide")
        )
        == ()
    )

    # Validation & warnings
    assert validate_target_releasever("", current)[1] != ""
    assert validate_target_releasever("43", current)[1] != ""
    assert validate_target_releasever("41", current)[1] != ""
    norm, err, warn = validate_target_releasever("44", current)
    assert (norm, err, warn) == ("44", "", "")
    _, err_jump, warn_jump = validate_target_releasever("46", current)
    assert err_jump == "" and "sequentially" in warn_jump.lower()
    _, err_raw, warn_raw = validate_target_releasever("rawhide", current)
    assert err_raw == "" and "rawhide" in warn_raw.lower()

    # Discovery chain: Bodhi -> releases.json -> local fallback
    res_online = discover_fedora_releases(
        current, json_fetcher=lambda _u: bodhi_payload
    )
    assert res_online.source == "online" and len(res_online.targets) == 4

    def _only_releases_json(url: str):
        if "bodhi" in url:
            raise URLError("bodhi down")
        return [{"version": "44"}, {"version": "rawhide"}]

    res_secondary = discover_fedora_releases(current, json_fetcher=_only_releases_json)
    assert res_secondary.source == "online"
    assert [t.releasever for t in res_secondary.targets] == ["44", "rawhide"]

    res_offline = discover_fedora_releases(
        current, json_fetcher=lambda _u: (_ for _ in ()).throw(URLError("offline"))
    )
    assert res_offline.source == "local"
    assert [t.releasever for t in res_offline.targets] == ["44", "45", "rawhide"]

    # Security: HTTPS-only checks
    with pytest.raises(ValueError):
        _fetch_https_json("http://fedoraproject.org/releases.json")

    handler = _HTTPSOnlyRedirectHandler(
        fedora_releases_mod.threading.Event(),
        fedora_releases_mod._monotonic() + 30.0,
    )
    with pytest.raises(URLError, match="non-HTTPS"):
        handler.redirect_request(None, None, 302, "", {}, "http://evil.invalid/")
    with pytest.raises(URLError, match="credentials"):
        handler.redirect_request(
            None, None, 302, "", {}, "https://user:pass@evil.invalid/"
        )


def test_fedora_releases_query_worker_emits_and_respects_cancel(qtbot) -> None:
    current = CurrentFedoraRelease(distro_id="fedora", version_id="43")
    worker = FedoraReleasesQueryWorker(
        current,
        json_fetcher=lambda _u: {
            "releases": [{"id_prefix": "FEDORA", "version": "44", "state": "current"}]
        },
    )
    received: list[FedoraReleaseDiscoveryResult] = []
    worker.signals.loaded.connect(received.append)
    worker.run()
    assert len(received) == 1 and received[0].targets[0].releasever == "44"

    cancelled_worker = FedoraReleasesQueryWorker(
        current,
        json_fetcher=lambda _u: {
            "releases": [{"id_prefix": "FEDORA", "version": "44", "state": "current"}]
        },
    )
    received2: list[FedoraReleaseDiscoveryResult] = []
    cancelled_worker.signals.loaded.connect(received2.append)
    cancelled_worker.cancel()
    cancelled_worker.run()
    assert received2 == []


def test_system_upgrade_wizard_and_review_dialog_ui_flows(qtbot) -> None:
    current = CurrentFedoraRelease(
        distro_id="fedora", version_id="43", pretty_name="Fedora Linux 43"
    )
    dlg = SystemReleaseUpgradeDialog(current)
    qtbot.addWidget(dlg)

    assert dlg.current_step() == 0
    assert dlg.target_list.count() == 3
    assert dlg.target_input.text() == "44"
    assert dlg.next_button.isEnabled() is True

    # Invalid/matching release blocks progression with error tone
    dlg.target_input.setText("43")
    dlg._on_target_input_edited("43")
    assert dlg.next_button.isEnabled() is False
    assert dlg.step1_notice_card.property("noticeTone") == "error"

    # Manual custom edit is preserved when async discovery completes
    dlg.target_input.setText("46")
    dlg._on_target_input_edited("46")
    assert dlg.next_button.isEnabled() is True
    assert dlg.step1_notice_card.property("noticeTone") == "warn"

    dlg.set_discovery_result(
        FedoraReleaseDiscoveryResult(
            targets=(
                FedoraTargetRelease(
                    releasever="44",
                    label="Fedora Linux 44",
                    status="stable",
                    status_badge="Stable",
                    detail="Supported stable release.",
                ),
            ),
            source="online",
            notice="Loaded available Fedora releases from Bodhi metadata.",
        )
    )
    assert dlg.target_input.text() == "46"

    # Select row 0 -> updates target_input to 44 cleanly
    dlg.target_list.setCurrentRow(0)
    assert dlg.target_input.text() == "44"

    # Advance to Step 2
    dlg.next_button.click()
    assert dlg.current_step() == 1
    assert dlg.step_badge.text() == "Step 2 of 2"
    assert dlg.back_button.isHidden() is False
    assert dlg.resolve_button.isHidden() is False

    dlg.upgrade_radio.setChecked(True)
    dlg.allow_erasing_checkbox.setChecked(True)
    req = dlg.selected_request()
    assert req.system_upgrade_releasever == "44"
    assert req.system_upgrade_mode == "upgrade"
    assert req.allow_erasing is True

    # Review dialog shows breakdown and locks offline boot staging
    plan = TransactionPlan(
        token="plan-sysup-ui",
        session_path="/org/rpm/dnf/v0/session/ui",
        request=TransactionRequest.from_values(
            system_upgrade_releasever="44", system_upgrade_mode="distrosync"
        ),
        items=(
            TransactionItem(
                object_type="Package",
                action="Upgrade",
                reason="User",
                attributes={},
                package={
                    "name": "glibc",
                    "version": "2.41",
                    "release": "1.fc44",
                    "arch": "x86_64",
                },
            ),
            TransactionItem(
                object_type="Package",
                action="Remove",
                reason="Dependency",
                attributes={"obsolete": True},
                package={
                    "name": "compat-old",
                    "version": "1",
                    "release": "1.fc43",
                    "arch": "x86_64",
                },
            ),
        ),
        resolver_result=0,
        warnings=(),
        critical_removals=(),
    )
    review = TransactionPlanReviewDialog(plan)
    qtbot.addWidget(review)
    assert review.windowTitle() == "Review Fedora 44 System Upgrade Plan"
    assert review.stage_checkbox.isChecked() is True
    assert review.stage_checkbox.isEnabled() is False
    assert review.stage_for_next_boot() is True
    assert review.offline_options_frame.isHidden() is False
    assert review.execute_button.text() == "Stage for Next Boot"

    review.download_only_checkbox.setChecked(True)
    assert review.stage_download_only() is True
    assert review.execute_button.text() == "Download and Pre-Stage"


def test_header_button_gating_and_mainwindow_system_upgrade_wiring(
    qtbot, monkeypatch
) -> None:
    header = HeaderBar()
    qtbot.addWidget(header)
    emitted: list[bool] = []
    header.system_upgrade_requested.connect(lambda: emitted.append(True))

    header.configure_host_environment(HostEnvironment.traditional())
    assert header.system_upgrade_btn.isHidden() is False
    header.system_upgrade_btn.click()
    assert emitted == [True]

    header.configure_host_environment(HostEnvironment.flatpak())
    assert header.system_upgrade_btn.isHidden() is False

    header.configure_host_environment(
        HostEnvironment(kind=HostEnvironmentKind.ATOMIC_OSTREE, ostree_booted=True)
    )
    assert header.system_upgrade_btn.isHidden() is True

    # MainWindowTransactionsMixin gating & forced offline routing
    import dendro.ui.main_window as main_win_mod
    import dendro.ui.main_window_transactions as tx_mod

    monkeypatch.setattr(
        main_win_mod,
        "_window_host_environment",
        lambda _w: HostEnvironment(
            kind=HostEnvironmentKind.ATOMIC_OSTREE, ostree_booted=True
        ),
    )
    info_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tx_mod.QMessageBox,
        "information",
        lambda _p, title, detail: info_calls.append((title, detail)),
    )
    fake_win = SimpleNamespace(
        transaction_controller=SimpleNamespace(is_busy=False, has_ready_plan=False),
        status_bar=SimpleNamespace(showMessage=lambda *_a: None),
    )
    MainWindowTransactionsMixin._open_system_release_upgrade_dialog(fake_win)
    assert len(info_calls) == 1

    # Mutable host + stub dialog forcing stage_ready_plan even if dialog returns False
    staged_calls: list[tuple[bool, str]] = []

    class _DishonestDialog:
        def __init__(self, plan, parent=None) -> None:
            self.plan = plan

        def exec(self) -> int:
            return int(QDialog.DialogCode.Accepted)

        def stage_for_next_boot(self) -> bool:
            return False

        def stage_download_only(self) -> bool:
            return False

        def stage_finish_action(self) -> str:
            return "reboot"

    monkeypatch.setattr(
        main_win_mod,
        "TransactionPlanReviewDialog",
        _DishonestDialog,
        raising=False,
    )
    fake_tx_win = SimpleNamespace(
        _pending_transaction_kind="",
        transaction_drawer=SimpleNamespace(
            set_transaction_preview=lambda *_a: None,
            start_execution_mode=lambda: None,
            finish_execution_mode=lambda _ok: None,
        ),
        status_bar=SimpleNamespace(showMessage=lambda *_a: None),
        transaction_controller=SimpleNamespace(
            stage_ready_plan=lambda download_only=False, finish_action="reboot": (
                staged_calls.append((download_only, finish_action))
            ),
            execute_ready_plan=lambda: (_ for _ in ()).throw(
                AssertionError("Live execute must never run for system upgrades")
            ),
            discard_ready_plan=lambda: None,
        ),
    )
    plan = TransactionPlan(
        token="force-offline",
        session_path="/org/rpm/dnf/v0/session/force",
        request=TransactionRequest.from_values(system_upgrade_releasever="45"),
        items=(
            TransactionItem(
                object_type="Package",
                action="Upgrade",
                reason="User",
                attributes={},
                package={
                    "name": "bash",
                    "version": "5.3",
                    "release": "1.fc45",
                    "arch": "x86_64",
                },
            ),
        ),
        resolver_result=0,
        warnings=(),
        critical_removals=(),
    )
    MainWindowTransactionsMixin._on_dnf_plan_ready(fake_tx_win, plan)
    assert staged_calls == [(False, "reboot")]
    assert fake_tx_win._pending_transaction_kind == "stage_offline"


def test_mainwindow_real_system_upgrade_shortcut_and_wizard_launch(
    qtbot, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)
    window = MainWindow(host_environment=HostEnvironment.traditional())
    qtbot.addWidget(window)

    started_workers: list[object] = []
    resolved_requests: list[tuple[TransactionRequest, str]] = []

    class _FakeWizard:
        def __init__(self, current_release, parent=None) -> None:
            self.current_release = current_release
            self.refresh_releases_requested = SimpleNamespace(connect=lambda _cb: None)
            self.loading_calls = 0

        def set_discovery_loading(self) -> None:
            self.loading_calls += 1

        def set_discovery_result(self, _result) -> None:
            return None

        def exec(self) -> int:
            return int(QDialog.DialogCode.Accepted)

        def selected_request(self) -> TransactionRequest:
            return TransactionRequest.from_values(
                system_upgrade_releasever="45", system_upgrade_mode="distrosync"
            )

        def deleteLater(self) -> None:
            return None

    monkeypatch.setattr(
        window.thread_pool,
        "start",
        lambda worker: started_workers.append(worker),
    )
    import dendro.ui.main_window as main_win_mod

    monkeypatch.setattr(main_win_mod, "SystemReleaseUpgradeDialog", _FakeWizard)
    monkeypatch.setattr(
        window,
        "_begin_transaction_resolution",
        lambda req, kind: resolved_requests.append((req, kind)),
    )

    window._open_system_release_upgrade_dialog()
    assert len(started_workers) == 1
    assert isinstance(started_workers[0], FedoraReleasesQueryWorker)
    assert len(resolved_requests) == 1
    assert resolved_requests[0][0].system_upgrade_releasever == "45"
    assert resolved_requests[0][1] == "fedora_release_upgrade"
    assert window.current_fedora_releases_worker is None
    window.close()
