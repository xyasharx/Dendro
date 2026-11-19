"""Tests for host-environment detection, capability policy, and Atomic safety."""

from __future__ import annotations

import importlib.util
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from PyQt6.QtWidgets import QLabel

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import backend, package_metadata
from core.backend import (
    FileVerificationResult,
    LocalRpmInspectionResult,
    PackageInfo,
    PackageState,
)
from core.dnf5daemon import (
    Dnf5DaemonClient,
    Dnf5DaemonUnavailable,
    Dnf5DaemonValidationError,
    DnfRepository,
    RepositoryKeyPrompt,
    TransactionRequest,
)
from core.host_environment import (
    CONTAINERENV_PATH,
    DOCKERENV_PATH,
    FLATPAK_INFO_PATH,
    OS_RELEASE_PATHS,
    OSTREE_BOOTED_PATH,
    OSTREE_DIR_PATH,
    PID1_COMM_PATH,
    SYSROOT_PATH,
    SYSTEMD_RUNTIME_PATH,
    USR_PATH,
    HostEnvironment,
    HostEnvironmentKind,
    SystemHostEnvironmentProbe,
    detect_host_environment,
    parse_os_release,
    sanitize_display_text,
)
from core.models import DependencyTreeModel, PackageFilterProxyModel
from core.operation_gate import HostOperationGate
from core.repository_addition import (
    RepositoryAdditionRunner,
    repository_filename_for_url,
)
from core.repository_deletion import RepositoryDeletionRunner
from core.rpmostree_status import RpmOstreeOriginKind
from ui.header import HeaderBar
from ui.history_dialog import DnfHistoryDialog
from ui.inspector_panel import PackageInspectorPanel
from ui.main_window import HostModeBanner, LocalRpmInstallDialog, MainWindow
from ui.repo_dialog import RepoManagerDialog
from ui.settings_dialog import DendroSettingsDialog


@dataclass
class FakeHostProbe:
    """In-memory deterministic probe for exercising every host topology."""

    existing: set[str] = field(default_factory=set)
    directories: set[str] = field(default_factory=set)
    mountpoints: set[str] = field(default_factory=set)
    read_only_paths: set[str] = field(default_factory=set)
    files: dict[str, str] = field(default_factory=dict)

    def path_exists(self, path: str) -> bool:
        return path in self.existing or path in self.directories or path in self.files

    def is_dir(self, path: str) -> bool:
        return path in self.directories

    def is_mountpoint(self, path: str) -> bool:
        return path in self.mountpoints

    def is_path_read_only(self, path: str) -> bool:
        return path in self.read_only_paths

    def read_bounded_text(self, path: str, max_bytes: int) -> str:
        content = self.files.get(path, "")
        return content.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace")


class _FailIfCalledTransport:
    """D-Bus transport stub that tracks any unauthorized call attempt."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def call(self, path, interface, method, _signatures, _arguments):
        self.calls.append((path, interface, method))
        raise AssertionError(f"Unexpected D-Bus call: {interface}.{method} on {path}")

    def subscribe(self, path, interface, signal, _callback):
        self.calls.append((path, interface, signal))
        return object()

    def unsubscribe(self, _token) -> None:
        return None


def _load_repo_helper() -> ModuleType:
    path = Path(__file__).parents[1] / "tools" / "dendro-repo-helper.py"
    spec = importlib.util.spec_from_file_location("dendro_repo_helper_atomic", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _atomic_env(
    pretty_name: str = "Fedora Linux 45 (Silverblue)",
    *,
    usr_read_only: bool = True,
) -> HostEnvironment:
    return HostEnvironment(
        kind=HostEnvironmentKind.ATOMIC_OSTREE,
        os_pretty_name=pretty_name,
        os_variant_id="silverblue",
        usr_read_only=usr_read_only,
        ostree_booted=True,
    )


def _readonly_usr_env() -> HostEnvironment:
    return HostEnvironment(
        kind=HostEnvironmentKind.READ_ONLY_USR,
        os_pretty_name="Fedora Linux 45 (Image Mode)",
        usr_read_only=True,
    )


def _container_env() -> HostEnvironment:
    return HostEnvironment(
        kind=HostEnvironmentKind.UNBOOTED_CONTAINER,
        os_pretty_name="Fedora Linux 45 (Container Image)",
        os_variant_id="container",
        in_container=True,
        container_booted_systemd=False,
    )


# ---------------------------------------------------------------------------
# Host classification & precedence tests
# ---------------------------------------------------------------------------


def test_detects_traditional_mutable_fedora_host() -> None:
    probe = FakeHostProbe(
        existing={SYSTEMD_RUNTIME_PATH},
        files={
            PID1_COMM_PATH: "systemd\n",
            OS_RELEASE_PATHS[0]: (
                'PRETTY_NAME="Fedora Linux 45 (Workstation Edition)"\n'
                "VARIANT_ID=workstation\n"
            ),
        },
    )

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.TRADITIONAL_FEDORA
    assert env.os_pretty_name == "Fedora Linux 45 (Workstation Edition)"
    assert env.os_variant_id == "workstation"
    assert env.supports_local_rpm_inspection is True
    assert env.supports_dnf5_daemon_mutations is True
    assert env.supports_dnf5_daemon_queries is True
    assert env.supports_repository_helper is True
    assert env.supports_dnf_cli_queries is True
    assert env.supports_orphan_detection is True
    assert env.supports_flatpak_management is True
    assert env.supports_rpmostree_readonly is False
    assert env.normalizes_rpm_mtimes is False
    assert env.mode_banner() is None


def test_flatpak_takes_precedence_over_container_ostree_and_readonly_usr() -> None:
    probe = FakeHostProbe(
        existing={FLATPAK_INFO_PATH, CONTAINERENV_PATH, OSTREE_BOOTED_PATH},
        read_only_paths={USR_PATH},
    )

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.FLATPAK_SANDBOX
    assert env.is_flatpak is True
    assert env.supports_dnf5_daemon_mutations is True
    assert env.supports_dnf5_daemon_queries is True
    assert env.supports_repository_helper is False
    assert env.supports_local_rpm_inspection is False
    assert env.supports_flatpak_management is False
    assert env.supports_rpmostree_readonly is False
    assert env.mode_banner() is None


@pytest.mark.parametrize(
    ("variant_name", "variant_id"),
    [
        ("Fedora Linux 45 (Silverblue)", "silverblue"),
        ("Fedora Linux 45 (Kinoite)", "kinoite"),
        ("Fedora Linux 45 (Sway Atomic)", "sway-atomic"),
        ("Fedora Linux 45 (Budgie Atomic)", "budgie-atomic"),
        ("Fedora Linux 45 (COSMIC Atomic)", "cosmic-atomic"),
        ("Fedora Linux 45 (IoT Edition)", "iot"),
    ],
)
def test_detects_fedora_atomic_ostree_variants(
    variant_name: str, variant_id: str
) -> None:
    probe = FakeHostProbe(
        existing={OSTREE_BOOTED_PATH, SYSTEMD_RUNTIME_PATH},
        read_only_paths={USR_PATH},
        files={
            PID1_COMM_PATH: "systemd\n",
            OS_RELEASE_PATHS[0]: (
                f'PRETTY_NAME="{variant_name}"\nVARIANT_ID={variant_id}\n'
            ),
        },
    )

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.ATOMIC_OSTREE
    assert env.is_atomic_ostree is True
    assert env.is_immutable_host is True
    assert env.ostree_booted is True
    assert env.usr_read_only is True
    assert env.supports_local_rpm_inspection is True
    assert env.supports_flatpak_management is True
    assert env.supports_rpmostree_readonly is True
    assert env.normalizes_rpm_mtimes is True
    assert env.supports_dnf5_daemon_mutations is False
    assert env.supports_dnf5_daemon_queries is False
    assert env.supports_repository_helper is False
    assert env.supports_dnf_cli_queries is False
    assert env.supports_orphan_detection is False
    banner = env.mode_banner()
    assert banner is not None
    assert variant_name in banner[0]


def test_detects_composefs_ostree_host_without_run_ostree_booted_marker() -> None:
    probe = FakeHostProbe(
        directories={OSTREE_DIR_PATH, SYSROOT_PATH},
        mountpoints={SYSROOT_PATH},
        read_only_paths={USR_PATH},
    )

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.ATOMIC_OSTREE
    assert env.ostree_booted is True
    assert env.supports_dnf5_daemon_mutations is False


def test_unlocked_usroverlay_still_classifies_as_atomic_ostree() -> None:
    """A volatile 'rpm-ostree usroverlay' makes /usr writable but stays immutable."""
    probe = FakeHostProbe(
        existing={OSTREE_BOOTED_PATH},
        read_only_paths=set(),
    )

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.ATOMIC_OSTREE
    assert env.usr_read_only is False
    assert env.supports_dnf5_daemon_mutations is False
    assert env.supports_repository_helper is False
    assert env.normalizes_rpm_mtimes is True


def test_ostree_directory_alone_without_sysroot_mount_stays_traditional() -> None:
    """Developer hosts building OSTree repos must not be misclassified as Atomic."""
    probe = FakeHostProbe(directories={OSTREE_DIR_PATH})

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.TRADITIONAL_FEDORA


def test_detects_non_ostree_readonly_usr_host() -> None:
    probe = FakeHostProbe(read_only_paths={USR_PATH})

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.READ_ONLY_USR
    assert env.is_immutable_host is True
    assert env.ostree_booted is False
    assert env.normalizes_rpm_mtimes is False
    assert env.supports_rpmostree_readonly is False
    assert env.supports_local_rpm_inspection is True
    assert env.supports_flatpak_management is True
    assert env.supports_dnf5_daemon_mutations is False
    assert env.supports_dnf5_daemon_queries is False
    assert env.supports_repository_helper is False
    assert env.supports_dnf_cli_queries is False


@pytest.mark.parametrize("marker", [CONTAINERENV_PATH, DOCKERENV_PATH])
def test_unbooted_toolbx_or_distrobox_container_blocks_host_bus_even_if_systemd_mounted(
    marker: str,
) -> None:
    probe = FakeHostProbe(
        existing={marker, SYSTEMD_RUNTIME_PATH},
        files={PID1_COMM_PATH: "bash\n"},
    )

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.UNBOOTED_CONTAINER
    assert env.in_container is True
    assert env.container_booted_systemd is False
    assert env.supports_local_rpm_inspection is True
    assert env.supports_dnf_cli_queries is True
    assert env.supports_orphan_detection is True
    assert env.supports_dnf5_daemon_queries is False
    assert env.supports_dnf5_daemon_mutations is False
    assert env.supports_repository_helper is False


def test_booted_systemd_container_allows_its_own_dnf5_daemon() -> None:
    probe = FakeHostProbe(
        existing={CONTAINERENV_PATH, SYSTEMD_RUNTIME_PATH},
        files={PID1_COMM_PATH: "systemd\n"},
    )

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.TRADITIONAL_FEDORA
    assert env.in_container is True
    assert env.container_booted_systemd is True
    assert env.supports_dnf5_daemon_mutations is True


def test_booted_systemd_container_with_readonly_usr_blocks_mutations() -> None:
    probe = FakeHostProbe(
        existing={CONTAINERENV_PATH, SYSTEMD_RUNTIME_PATH},
        read_only_paths={USR_PATH},
        files={PID1_COMM_PATH: "systemd\n"},
    )

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.READ_ONLY_USR
    assert env.in_container is True
    assert env.container_booted_systemd is True
    assert env.supports_dnf5_daemon_mutations is False


def test_environment_variables_and_os_release_never_drive_capability_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("container", "podman")
    monkeypatch.setenv("FLATPAK_ID", "io.github.xyasharx.Dendro")
    monkeypatch.setenv("OSTREE_REPO", "/ostree/repo")
    probe = FakeHostProbe(
        files={
            OS_RELEASE_PATHS[0]: (
                'PRETTY_NAME="Fedora\x1b[31m Silverblue\x9b"\nVARIANT_ID=silverblue\n'
            )
        }
    )

    env = detect_host_environment(probe)

    assert env.kind is HostEnvironmentKind.TRADITIONAL_FEDORA
    assert "\x1b" not in env.os_pretty_name
    assert "\x9b" not in env.os_pretty_name


def test_os_release_parser_and_bounded_probe_handle_escapes_and_non_files(
    tmp_path: Path,
) -> None:
    parsed = parse_os_release(
        '# Comment\nNAME="Fedora \\"Atomic\\""\nVARIANT_ID=\'kinoite\'\ninvalid=1\n'
    )
    assert parsed == {"NAME": 'Fedora "Atomic"', "VARIANT_ID": "kinoite"}
    assert sanitize_display_text("a\x00b\x7fc\x85d", max_length=20) == "a b c d"
    assert len(sanitize_display_text("x" * 200, max_length=20)) == 20

    sample = tmp_path / "os-release"
    sample.write_text("PRETTY_NAME=Fedora\n", encoding="utf-8")
    system_probe = SystemHostEnvironmentProbe()
    assert system_probe.read_bounded_text(str(sample), 6) == "PRETTY"
    assert system_probe.read_bounded_text(str(tmp_path), 64) == ""


# ---------------------------------------------------------------------------
# DNF5 daemon, repository runners, and root helper defense-in-depth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_factory",
    [
        _atomic_env,
        lambda: _atomic_env(usr_read_only=False),
        _readonly_usr_env,
        _container_env,
    ],
)
def test_dnf5_client_blocks_every_operation_before_touching_dbus(env_factory) -> None:
    transport = _FailIfCalledTransport()
    client = Dnf5DaemonClient(transport=transport, environment_detector=env_factory)
    request = TransactionRequest.from_values(installs=["htop"])

    for action in (
        client.ensure_available,
        lambda: client.resolve(request, set()),
        lambda: client.set_repository_enabled("fedora", True),
        client.clean_cache,
        client.list_available_updates,
        lambda: client.list_available_updates(refresh=True),
        client.list_repositories,
    ):
        with pytest.raises(Dnf5DaemonUnavailable):
            action()

    assert transport.calls == []


@pytest.mark.parametrize(
    "env_factory",
    [_atomic_env, _readonly_usr_env, _container_env],
)
def test_repository_runners_refuse_unsupported_hosts_before_download_or_gate(
    tmp_path: Path,
    env_factory,
) -> None:
    helper = tmp_path / "dendro-repo-helper"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    gate = HostOperationGate()

    add_runner = RepositoryAdditionRunner(
        helper_path=str(helper),
        environment_detector=env_factory,
        host_operation_gate=gate,
    )
    delete_runner = RepositoryDeletionRunner(
        helper_path=str(helper),
        environment_detector=env_factory,
        host_operation_gate=gate,
    )

    with pytest.raises(Dnf5DaemonValidationError):
        add_runner.add_from_url("https://repo.example.test/example.repo")
    assert add_runner.is_running is False
    assert gate.active is None

    with pytest.raises(Dnf5DaemonValidationError):
        delete_runner.delete("/etc/yum.repos.d/example.repo")
    assert delete_runner.is_running is False
    assert gate.active is None


@pytest.mark.parametrize(
    ("ostree_booted", "usr_readonly", "expected_phrase"),
    [
        (True, False, "Fedora Atomic and OSTree"),
        (False, True, "read-only"),
    ],
)
def test_root_repo_helper_rejects_atomic_and_readonly_usr_for_add_and_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ostree_booted: bool,
    usr_readonly: bool,
    expected_phrase: str,
) -> None:
    helper = _load_repo_helper()
    ostree_marker = tmp_path / "ostree-booted"
    if ostree_booted:
        ostree_marker.write_text("", encoding="utf-8")
    existing_repo = tmp_path / "existing.repo"
    existing_repo.write_text("[existing]\nname=Existing\n", encoding="utf-8")

    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    monkeypatch.setattr(helper, "_OSTREE_BOOTED_PATH", ostree_marker)
    monkeypatch.setattr(helper, "_OSTREE_DIR_PATH", tmp_path / "no-ostree")
    monkeypatch.setattr(helper, "_ALLOWED_DIRECTORIES", (str(tmp_path),))
    monkeypatch.setattr(helper, "_ADD_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(
        helper,
        "_statvfs",
        lambda _path: SimpleNamespace(f_flag=helper._ST_RDONLY if usr_readonly else 0),
    )

    payload = (
        b"[example]\n"
        b"name=Example\n"
        b"baseurl=https://repo.example.test/fc45/\n"
        b"enabled=1\n"
        b"gpgcheck=1\n"
        b"gpgkey=https://repo.example.test/RPM-GPG-KEY\n"
    )
    dest = repository_filename_for_url("https://repo.example.test/example.repo")

    assert helper.add_repository_file(dest, io.BytesIO(payload)) == 1
    assert not (tmp_path / dest).exists()
    assert expected_phrase in capsys.readouterr().err

    assert helper.delete_repository_file(str(existing_repo)) == 1
    assert existing_repo.exists()
    assert expected_phrase in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Backend worker & Comps catalog behavior on Fedora Atomic
# ---------------------------------------------------------------------------


def test_package_verify_worker_uses_nomtime_only_on_ostree_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        recorded_commands.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend.subprocess, "run", fake_run)

    monkeypatch.setattr(backend, "get_host_environment", _atomic_env)
    atomic_worker = backend.PackageVerifyWorker("bash")
    atomic_worker.run()
    assert atomic_worker.ignored_mtime is True
    assert recorded_commands[-1] == ["rpm", "-V", "--nomtime", "--", "bash"]

    monkeypatch.setattr(backend, "get_host_environment", _readonly_usr_env)
    ro_worker = backend.PackageVerifyWorker("bash")
    ro_worker.run()
    assert ro_worker.ignored_mtime is False
    assert recorded_commands[-1] == ["rpm", "-V", "--", "bash"]

    monkeypatch.setattr(backend, "get_host_environment", HostEnvironment.traditional)
    std_worker = backend.PackageVerifyWorker("bash")
    std_worker.run()
    assert std_worker.ignored_mtime is False
    assert recorded_commands[-1] == ["rpm", "-V", "--", "bash"]


@pytest.mark.parametrize("env_factory", [_atomic_env, _readonly_usr_env])
@pytest.mark.parametrize(
    "worker_factory",
    [
        backend.UserInstalledQueryWorker,
        backend.OrphanQueryWorker,
        backend.DnfHistoryWorker,
        lambda: backend.RemotePackageSearchWorker("htop"),
        backend.SystemUpdatesCheckWorker,
    ],
)
def test_dnf_workers_report_clear_capability_messages_without_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
    env_factory,
    worker_factory,
) -> None:
    def forbid_subprocess(*_args, **_kwargs):
        raise AssertionError("DNF subprocess must not execute on an immutable host")

    monkeypatch.setattr(backend, "is_running_in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "get_host_environment", env_factory)
    monkeypatch.setattr(backend.subprocess, "run", forbid_subprocess)

    errors: list[tuple[str, str]] = []
    worker = worker_factory()
    worker.signals.error_occurred.connect(
        lambda pkg, detail: errors.append((pkg, detail))
    )
    worker.run()

    assert len(errors) == 1
    assert errors[0][1]
    assert "requires an installed" not in errors[0][1]


def test_comps_catalog_includes_rpm_ostree_repomd_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    comps_xml = tmp_path / "fedora-comps.xml"
    comps_xml.write_text(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<comps><group><id>system-tools</id>"
        "<packagelist><packagereq type='default'>ostree-tool</packagereq></packagelist>"
        "</group></comps>",
        encoding="utf-8",
    )
    seen_patterns: list[str] = []

    def fake_glob(pattern: str, recursive: bool = False) -> list[str]:
        seen_patterns.append(pattern)
        if pattern.startswith("/var/cache/rpm-ostree/repomd/"):
            return [str(comps_xml)]
        return []

    monkeypatch.setattr(package_metadata.glob, "glob", fake_glob)
    catalog = package_metadata.FedoraCompsCatalog()

    assert any(p.startswith("/var/cache/rpm-ostree/repomd/") for p in seen_patterns)
    assert catalog.get_domain_for_package("ostree-tool") == "cli_tools"


# ---------------------------------------------------------------------------
# Headless Qt UI integration tests for Atomic, Read-Only /usr, & Containers
# ---------------------------------------------------------------------------


def test_host_mode_banner_visibility_and_flatpak_shortcut(qtbot) -> None:
    banner = HostModeBanner()
    qtbot.addWidget(banner)

    banner.configure(HostEnvironment.traditional())
    assert banner.isHidden() is True

    banner.configure(HostEnvironment.flatpak())
    assert banner.isHidden() is True

    banner.configure(_atomic_env())
    assert banner.isHidden() is False
    assert "Fedora Atomic" in banner.title_label.text()
    assert banner.flatpak_action_btn.isHidden() is False
    banner.set_deployment_summary("Booted deployment: 45.20261117.0.")
    assert banner.deployment_label.isHidden() is False

    banner.configure(_container_env())
    assert banner.isHidden() is False
    assert banner.flatpak_action_btn.isHidden() is True


def test_header_locks_out_mutations_on_atomic_and_keeps_container_search(qtbot) -> None:
    header = HeaderBar()
    qtbot.addWidget(header)

    header.configure_host_environment(_atomic_env())
    assert header.apply_btn.isEnabled() is False
    assert header.apply_btn.text() == "Read-Only"
    assert header.remote_search_action.isVisible() is False

    header.update_queue_badge(4)
    header.update_available_updates_badge(9)
    header.set_updates_view_active(True, 9)
    header.set_orphan_clean_visible(True, 3)
    assert header.apply_btn.isEnabled() is False
    assert header.discard_btn.isHidden() is True
    assert header.updates_btn.isHidden() is True
    assert header.upgrade_context_btn.isHidden() is True
    assert header.refresh_live_btn.isHidden() is True
    assert header.clean_orphans_btn.isHidden() is True

    header.configure_host_environment(_container_env())
    assert header.apply_btn.isEnabled() is False
    assert header.remote_search_action.isVisible() is True


def test_inspector_renders_ostree_provenance_and_nomtime_audit(qtbot) -> None:
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    panel.set_host_environment(_atomic_env())

    assert "--nomtime" in panel.btn_verify.text()

    for origin, expected_label in (
        (RpmOstreeOriginKind.BASE, "Type: Base Image (OSTree)"),
        (RpmOstreeOriginKind.LAYERED, "Type: Layered (OSTree)"),
        (RpmOstreeOriginKind.LOCAL, "Type: Local RPM (OSTree)"),
        (RpmOstreeOriginKind.OVERRIDE, "Type: Base Override (OSTree)"),
    ):
        pkg = PackageInfo(
            name="htop",
            version="3.4.0",
            release="1.fc45",
            arch="x86_64",
            state=PackageState.INSTALLED,
            ostree_origin=origin,
            is_user_installed=origin != RpmOstreeOriginKind.BASE,
        )
        panel.set_package_info(pkg)
        assert panel.lbl_install_type.text() == expected_label
        assert panel.lbl_upgrade_status.text() == "Update: Managed by OS Image"
        assert panel.queue_btn.isEnabled() is False
        assert panel.queue_btn.text() == "Read-Only Mode"

    panel.set_package_verification("htop", [], ignored_mtime=True)
    assert "--nomtime" in panel.lbl_verify_status.text()
    assert "--nomtime" in panel.lbl_verify_status.toolTip()

    panel.set_package_verification(
        "htop",
        [
            FileVerificationResult(
                path="/etc/htoprc",
                status_flags="S.5....",
                is_config=True,
                is_missing=False,
                size_differs=True,
                mode_differs=False,
                digest_differs=True,
                mtime_differs=False,
                raw_line="S.5....  c /etc/htoprc",
            )
        ],
        ignored_mtime=True,
    )
    assert panel.lbl_verify_status.text() == "Modified: 1, Missing: 0 ('--nomtime')"


def test_repo_history_and_local_rpm_dialogs_enforce_read_only_host_mode(qtbot) -> None:
    repo_dialog = RepoManagerDialog()
    qtbot.addWidget(repo_dialog)
    repo_dialog.set_repositories(
        [
            DnfRepository(
                identifier="fedora",
                name="Fedora 45",
                enabled=True,
                baseurl="https://mirrors.fedoraproject.org/metalink",
                repo_file="/usr/share/dnf5/repos.d/fedora.repo",
            )
        ]
    )
    repo_dialog.set_read_only_mode(True, note="Disabled on Fedora Atomic.")
    assert repo_dialog.btn_add_repo.isEnabled() is False
    assert repo_dialog.repo_address_input.isEnabled() is False
    assert repo_dialog.btn_remove_repo.isEnabled() is False
    assert repo_dialog.btn_clean_cache.isEnabled() is False
    assert repo_dialog.btn_refresh.isEnabled() is False

    history_dialog = DnfHistoryDialog()
    qtbot.addWidget(history_dialog)
    history_dialog.set_undo_supported(False, tooltip="Disabled on Fedora Atomic.")
    history_dialog.set_history_entries(
        [
            backend.HistoryEntry(
                id=1,
                command_line="dnf upgrade",
                date_time="2026-11-17 10:00",
                action="Upgrade",
                altered_count="2",
                return_code="0",
            )
        ]
    )
    assert history_dialog.undo_btn.isEnabled() is False

    rpm_info = LocalRpmInspectionResult(
        file_path="/tmp/example-1.0-1.fc45.x86_64.rpm",
        name="example",
        version="1.0",
        release="1.fc45",
        arch="x86_64",
        summary="Example RPM",
        description="Example local RPM archive.",
        url="https://example.test/example",
        size_bytes=4096,
        license="MIT",
        requires=["libc.so.6()(64bit)"],
        is_already_installed=False,
    )
    local_dialog = LocalRpmInstallDialog(rpm_info, host_environment=_atomic_env())
    qtbot.addWidget(local_dialog)
    emitted: list[str] = []
    local_dialog.install_requested.connect(emitted.append)
    assert local_dialog.windowTitle() == "Inspect Package: example"
    assert local_dialog.btn_install.isEnabled() is False
    local_dialog._on_install_clicked()
    assert emitted == []


def test_search_filter_supports_ostree_layered_override_and_base_statuses(
    qtbot,
) -> None:
    header = HeaderBar()
    qtbot.addWidget(header)
    for token in ("status:layered", "status:override", "status:base"):
        header.search_input.setText(token)
        assert header.search_input.property("filterState") == "valid"

    base_pkg = PackageInfo(
        name="systemd",
        version="258",
        release="1.fc45",
        arch="x86_64",
        state=PackageState.INSTALLED,
        ostree_origin=RpmOstreeOriginKind.BASE,
    )
    layered_pkg = PackageInfo(
        name="htop",
        version="3.4.0",
        release="1.fc45",
        arch="x86_64",
        state=PackageState.INSTALLED,
        ostree_origin=RpmOstreeOriginKind.LAYERED,
        is_user_installed=True,
    )
    local_pkg = PackageInfo(
        name="custom-cli",
        version="1.0",
        release="1.fc45",
        arch="x86_64",
        state=PackageState.INSTALLED,
        ostree_origin=RpmOstreeOriginKind.LOCAL,
        is_user_installed=True,
    )
    override_pkg = PackageInfo(
        name="mesa-dri-drivers",
        version="26.2",
        release="1.fc45",
        arch="x86_64",
        state=PackageState.INSTALLED,
        ostree_origin=RpmOstreeOriginKind.OVERRIDE,
        is_user_installed=True,
    )
    model = DependencyTreeModel()
    model.set_packages([base_pkg, layered_pkg, local_pkg, override_pkg])
    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)
    proxy.set_category_filter("all")

    def visible_names(query: str) -> set[str]:
        proxy.set_search_query(query)
        return {proxy.index(row, 0).data() for row in range(proxy.rowCount())}

    assert visible_names("status:layered") == {"htop", "custom-cli"}
    assert visible_names("status:override") == {"mesa-dri-drivers"}
    assert visible_names("status:base") == {"systemd"}
    assert visible_names("status:user") == {"htop", "custom-cli", "mesa-dri-drivers"}


def test_main_window_queue_action_is_refused_on_atomic_host() -> None:
    pkg = PackageInfo(
        name="bash",
        version="5.3",
        release="1.fc45",
        arch="x86_64",
        state=PackageState.INSTALLED,
    )
    model = DependencyTreeModel()
    model.set_packages([pkg])
    messages: list[str] = []
    window = SimpleNamespace(
        host_environment=_atomic_env(),
        tree_model=model,
        status_bar=SimpleNamespace(
            showMessage=lambda msg, *_args: messages.append(msg)
        ),
    )

    MainWindow._on_inspector_queue_action(window, pkg)

    assert pkg.state == PackageState.INSTALLED
    assert messages and "Fedora Atomic" in messages[0]


def test_root_repo_helper_rejects_composefs_ostree_with_mounted_sysroot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = _load_repo_helper()
    ostree_dir = tmp_path / "ostree"
    ostree_dir.mkdir()
    sysroot_dir = tmp_path / "sysroot"
    sysroot_dir.mkdir()
    existing_repo = tmp_path / "existing.repo"
    existing_repo.write_text("[existing]\nname=Existing\n", encoding="utf-8")

    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    monkeypatch.setattr(helper, "_OSTREE_BOOTED_PATH", tmp_path / "absent")
    monkeypatch.setattr(helper, "_OSTREE_DIR_PATH", ostree_dir)
    monkeypatch.setattr(helper, "_SYSROOT_PATH", sysroot_dir)
    monkeypatch.setattr(
        helper, "_is_distinct_mountpoint", lambda path: path == sysroot_dir
    )
    monkeypatch.setattr(helper, "_ALLOWED_DIRECTORIES", (str(tmp_path),))
    monkeypatch.setattr(helper, "_statvfs", lambda _path: SimpleNamespace(f_flag=0))

    assert helper.delete_repository_file(str(existing_repo)) == 1
    assert existing_repo.exists()
    assert "Fedora Atomic and OSTree" in capsys.readouterr().err


def test_settings_about_tab_displays_host_environment(qtbot) -> None:
    dialog = DendroSettingsDialog(host_environment=_atomic_env())
    qtbot.addWidget(dialog)

    label = dialog.findChild(QLabel, "AboutHostEnvironmentLabel")
    assert label is not None
    assert "Fedora Linux 45 (Silverblue)" in label.text()
    assert "Fedora Atomic Read-Only RPM Mode" in label.text()


@pytest.mark.parametrize(
    "env_factory", [_atomic_env, _readonly_usr_env, _container_env]
)
def test_main_window_constructs_cleanly_in_every_restricted_host_mode(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    env_factory,
) -> None:
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)
    env = env_factory()
    window = MainWindow(host_environment=env)
    qtbot.addWidget(window)

    assert window.host_environment == env
    assert window.host_mode_banner.isHidden() is False
    assert window.header.apply_btn.isEnabled() is False
    assert (
        window.header.remote_search_action.isVisible() is env.supports_dnf_cli_queries
    )
    assert window.inspector_panel._host_environment == env
    window.close()


def test_main_window_asks_about_a_repository_key_before_trusting_it(
    qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live window must prompt, not fall back to the stored preference."""
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)
    window = MainWindow(
        host_environment=HostEnvironment(kind=HostEnvironmentKind.TRADITIONAL_FEDORA)
    )
    qtbot.addWidget(window)

    # The client was constructed with an interactive prompt hook.
    assert window.dnf_client._key_import_prompt is not None

    prompt = RepositoryKeyPrompt(
        session_path="/org/rpm/dnf/v0/session/1",
        key_id="8ACFA0B7",
        fingerprint="FPR",
        key_url="https://example.invalid/key.asc",
        user_ids=("Example <releng@example.invalid>",),
    )
    seen: list[object] = []

    class _Accepted:
        def __init__(self, prompt, parent=None) -> None:
            seen.append(prompt)
            self.remembered = False

        def exec(self):
            from PyQt6.QtWidgets import QDialog

            return QDialog.DialogCode.Accepted

        def apply_remembered_choice(self) -> None:
            self.remembered = True

    monkeypatch.setattr("ui.main_window.RepositoryKeyImportDialog", _Accepted)
    assert window._ask_about_repository_key(prompt) is True
    assert seen == [prompt]

    class _Rejected(_Accepted):
        def exec(self):
            from PyQt6.QtWidgets import QDialog

            return QDialog.DialogCode.Rejected

    monkeypatch.setattr("ui.main_window.RepositoryKeyImportDialog", _Rejected)
    assert window._ask_about_repository_key(prompt) is False

    # A dialog that cannot even be built must decline, never trust.
    def _boom(prompt, parent=None):
        raise RuntimeError("no display")

    monkeypatch.setattr("ui.main_window.RepositoryKeyImportDialog", _boom)
    assert window._ask_about_repository_key(prompt) is False
