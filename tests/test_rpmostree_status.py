"""Tests for unprivileged read-only ``org.projectatomic.rpmostree1`` status."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtDBus import QDBusObjectPath, QDBusVariant

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.backend import PackageInfo, PackageState
from core.host_environment import HostEnvironment, HostEnvironmentKind
from core.models import DependencyTreeModel, PackageFilterProxyModel
from core.rpmostree_status import (
    DEPLOYMENTS_PROPERTY,
    RPMOSTREE_SYSROOT_INTERFACE,
    RPMOSTREE_SYSROOT_PATH,
    RpmOstreeOriginKind,
    RpmOstreeStatusError,
    RpmOstreeStatusWorker,
    _unwrap_dbus_value,
    parse_deployments_variant,
    query_rpmostree_status,
)
from ui.inspector_panel import PackageInspectorPanel
from ui.main_window import HostModeBanner, MainWindow
from ui.sidebar import CategorySidebar


class _FakePropertyTransport:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, str]] = []

    def get_property(self, path: str, interface: str, property_name: str) -> Any:
        self.calls.append((path, interface, property_name))
        return self.payload


def _atomic_environment() -> HostEnvironment:
    return HostEnvironment(
        kind=HostEnvironmentKind.ATOMIC_OSTREE,
        os_pretty_name="Fedora Linux 45 (Silverblue)",
        os_variant_id="silverblue",
        usr_read_only=True,
        ostree_booted=True,
    )


def _sample_silverblue_deployments() -> list[dict[str, Any]]:
    return [
        {
            "id": "fedora-staged-1",
            "osname": "fedora",
            "checksum": "b" * 64,
            "version": "45.20261117.1",
            "origin": "fedora:fedora/45/x86_64/silverblue",
            "booted": False,
            "staged": True,
            "pinned": False,
            "unlocked": "none",
            "packages": ["htop", "ripgrep"],
            "requested-packages": ["htop", "ripgrep"],
        },
        {
            "id": "fedora-booted-0",
            "osname": "fedora",
            "checksum": "a" * 64,
            "version": "45.20261116.0",
            "origin": "fedora:fedora/45/x86_64/silverblue",
            "booted": True,
            "staged": False,
            "pinned": False,
            "unlocked": "none",
            "packages": ["htop", "libgit2.i686"],
            "requested-packages": ["htop"],
            "requested-local-packages": ["local-tool-1.2.0-1.fc45.x86_64"],
            "requested-local-fileoverride-packages": ["quirk-pkg-0.9-2.fc45.noarch"],
            "requested-base-remote-replacements": [
                ("https://copr.fedorainfracloud.org", ["mesa-dri-drivers"])
            ],
            "base-local-replacements": [
                (
                    ("kernel", "0", "7.2.1", "300.fc45", "x86_64"),
                    ("kernel", "0", "7.2.0", "300.fc45", "x86_64"),
                )
            ],
            "requested-base-removals": ["firefox"],
        },
        {
            "id": "fedora-rollback-0",
            "osname": "fedora",
            "checksum": "9" * 64,
            "version": "45.20261115.0",
            "origin": "fedora:fedora/45/x86_64/silverblue",
            "booted": False,
            "staged": False,
            "pinned": True,
            "unlocked": "none",
            "packages": [],
            "requested-packages": [],
        },
    ]


def test_query_reads_sysroot_deployments_property_and_classifies_packages() -> None:
    transport = _FakePropertyTransport(_sample_silverblue_deployments())

    status = query_rpmostree_status(transport, environment_detector=_atomic_environment)

    assert transport.calls == [
        (RPMOSTREE_SYSROOT_PATH, RPMOSTREE_SYSROOT_INTERFACE, DEPLOYMENTS_PROPERTY)
    ]
    assert status.deployment_count == 3
    assert status.booted.version == "45.20261116.0"
    assert status.has_pending_deployment is True
    assert status.pending is not None
    assert status.pending.version == "45.20261117.1"
    assert status.pending.staged is True
    assert status.rollback is not None
    assert status.rollback.version == "45.20261115.0"
    assert status.rollback.pinned is True
    assert status.booted.removed_base_packages == frozenset({"firefox"})
    assert "45.20261117.1" in status.pending_reboot_notice()
    assert status.unlocked_overlay_warning() == ""

    assert (
        status.classify_installed_package(
            name="bash",
            arch="x86_64",
            nevra="bash-5.3-1.fc45.x86_64",
            full_nevra="bash-0:5.3-1.fc45.x86_64",
        )
        == RpmOstreeOriginKind.BASE
    )
    assert (
        status.classify_installed_package(
            name="htop",
            arch="x86_64",
            nevra="htop-3.4.0-1.fc45.x86_64",
            full_nevra="htop-0:3.4.0-1.fc45.x86_64",
        )
        == RpmOstreeOriginKind.LAYERED
    )
    # Multilib selector only matches the layered architecture.
    assert (
        status.classify_installed_package(
            name="libgit2",
            arch="i686",
            nevra="libgit2-1.9-1.fc45.i686",
            full_nevra="libgit2-0:1.9-1.fc45.i686",
        )
        == RpmOstreeOriginKind.LAYERED
    )
    assert (
        status.classify_installed_package(
            name="libgit2",
            arch="x86_64",
            nevra="libgit2-1.9-1.fc45.x86_64",
            full_nevra="libgit2-0:1.9-1.fc45.x86_64",
        )
        == RpmOstreeOriginKind.BASE
    )
    assert (
        status.classify_installed_package(
            name="local-tool",
            arch="x86_64",
            nevra="local-tool-1.2.0-1.fc45.x86_64",
            full_nevra="local-tool-0:1.2.0-1.fc45.x86_64",
        )
        == RpmOstreeOriginKind.LOCAL
    )
    assert (
        status.classify_installed_package(
            name="mesa-dri-drivers",
            arch="x86_64",
            nevra="mesa-dri-drivers-26.2-1.fc45.x86_64",
            full_nevra="mesa-dri-drivers-0:26.2-1.fc45.x86_64",
        )
        == RpmOstreeOriginKind.OVERRIDE
    )
    assert (
        status.classify_installed_package(
            name="kernel",
            arch="x86_64",
            nevra="kernel-7.2.1-300.fc45.x86_64",
            full_nevra="kernel-0:7.2.1-300.fc45.x86_64",
        )
        == RpmOstreeOriginKind.OVERRIDE
    )


def test_parses_oci_bootc_container_deployment_and_unlocked_overlay_warning() -> None:
    status = parse_deployments_variant(
        [
            {
                "id": "fedora-next",
                "checksum": "1" * 64,
                "version": "45.20261117.0",
                "container-image-reference": (
                    "ostree-image-signed:docker://quay.io/fedora/fedora-kinoite:45"
                ),
                "booted": False,
                "staged": False,
            },
            {
                "id": "fedora-current",
                "checksum": "0" * 64,
                "version": "45.20261110.0\x1b[2J",
                "container-image-reference": (
                    "ostree-image-signed:docker://quay.io/fedora/fedora-kinoite:45"
                ),
                "booted": True,
                "staged": False,
                "unlocked": "development",
            },
        ]
    )

    assert status.booted.is_container_image is True
    assert "\x1b" not in status.booted.version
    assert status.pending is not None
    assert status.pending.deployment_id == "fedora-next"
    assert "pending deployment" in status.pending_reboot_notice()
    assert "development" in status.unlocked_overlay_warning()
    assert "Booted deployment:" in status.summary_line()


@pytest.mark.parametrize(
    "env",
    [
        HostEnvironment.flatpak(),
        HostEnvironment.traditional(),
        HostEnvironment(kind=HostEnvironmentKind.READ_ONLY_USR, usr_read_only=True),
        HostEnvironment(kind=HostEnvironmentKind.UNBOOTED_CONTAINER, in_container=True),
    ],
)
def test_query_refuses_non_atomic_and_flatpak_environments_before_dbus(
    env: HostEnvironment,
) -> None:
    transport = _FakePropertyTransport(_sample_silverblue_deployments())

    with pytest.raises(RpmOstreeStatusError):
        query_rpmostree_status(transport, environment_detector=lambda: env)

    assert transport.calls == []


@pytest.mark.parametrize(
    "invalid_payload",
    [
        "not-a-list",
        [],
        ["not-a-dict"],
        [{"id": "missing-booted", "booted": False}],
        [{"booted": True}],  # missing both id and checksum
        [{"id": "int-bool", "booted": 1}],  # int must not pass exact bool check
        [{"id": "int-staged", "booted": True, "staged": 0}],
        [{"id": "bad-pkgs", "booted": True, "packages": "htop"}],
        [{"id": "bad-pkg-item", "booted": True, "packages": [123]}],
        [{"id": "bad-version", "booted": True, "version": 45}],
        [{123: "non-string-key", "id": "x", "booted": True}],
    ],
)
def test_parser_rejects_malformed_dbus_deployment_payloads(
    invalid_payload: Any,
) -> None:
    with pytest.raises(RpmOstreeStatusError):
        parse_deployments_variant(invalid_payload)


def test_dbus_unwrapper_handles_qdbusvariant_and_bounds_deep_commit_metadata() -> None:
    nested: Any = "leaf"
    for _ in range(25):
        nested = {"meta": QDBusVariant(nested)}

    unwrapped = _unwrap_dbus_value(
        QDBusVariant(
            [
                {
                    "id": QDBusVariant("fedora-0"),
                    "booted": QDBusVariant(True),
                    "path": QDBusObjectPath("/org/projectatomic/rpmostree1/fedora"),
                    "base-commit-meta": nested,
                }
            ]
        )
    )
    status = parse_deployments_variant(unwrapped)
    assert status.booted.deployment_id == "fedora-0"


def test_worker_lifecycle_handles_success_error_and_cancellation() -> None:
    expected = parse_deployments_variant(_sample_silverblue_deployments())
    loaded: list[object] = []
    failed: list[str] = []

    ok_worker = RpmOstreeStatusWorker(loader=lambda: expected)
    ok_worker.signals.loaded.connect(loaded.append)
    ok_worker.run()
    assert loaded == [expected]

    err_worker = RpmOstreeStatusWorker(
        loader=lambda: (_ for _ in ()).throw(
            RpmOstreeStatusError("Unavailable", "Service not active")
        )
    )
    err_worker.signals.failed.connect(failed.append)
    err_worker.run()
    assert failed == ["Unavailable: Service not active"]

    cancelled_worker = RpmOstreeStatusWorker(loader=lambda: expected)
    cancelled_worker.signals.loaded.connect(loaded.append)
    cancelled_worker.cancel()
    cancelled_worker.run()
    assert len(loaded) == 1


def test_main_window_applies_rpmostree_status_to_tree_inspector_and_banner(
    qtbot,
) -> None:
    base_pkg = PackageInfo(
        name="bash",
        version="5.3",
        release="1.fc45",
        arch="x86_64",
        state=PackageState.INSTALLED,
    )
    layered_pkg = PackageInfo(
        name="htop",
        version="3.4.0",
        release="1.fc45",
        arch="x86_64",
        state=PackageState.INSTALLED,
    )
    model = DependencyTreeModel()
    model.set_packages([base_pkg, layered_pkg])
    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)
    sidebar = CategorySidebar()
    qtbot.addWidget(sidebar)
    inspector = PackageInspectorPanel()
    qtbot.addWidget(inspector)
    inspector.set_host_environment(_atomic_environment())
    inspector.set_package_info(layered_pkg)
    banner = HostModeBanner()
    qtbot.addWidget(banner)
    banner.configure(_atomic_environment())

    status = parse_deployments_variant(_sample_silverblue_deployments())
    worker = RpmOstreeStatusWorker(loader=lambda: status)
    status_messages: list[str] = []

    window = SimpleNamespace(
        host_environment=_atomic_environment(),
        current_rpmostree_worker=worker,
        _rpmostree_status=None,
        _orphan_cache={"stale"},
        _pending_updates_map={"bash.x86_64": object()},
        _all_packages_cache=[base_pkg, layered_pkg],
        _user_installed_cache=set(),
        tree_model=model,
        proxy_model=proxy,
        sidebar=sidebar,
        inspector_panel=inspector,
        host_mode_banner=banner,
        status_bar=SimpleNamespace(
            showMessage=lambda msg, *_args: status_messages.append(msg)
        ),
        _apply_rpmostree_status_to_packages=lambda pkgs, st: (
            MainWindow._apply_rpmostree_status_to_packages(window, pkgs, st)
        ),
        _update_sidebar_counts=lambda pkgs: MainWindow._update_sidebar_counts(
            window, pkgs
        ),
        _set_inspector_package_info=inspector.set_package_info,
    )

    MainWindow._on_rpmostree_status_loaded(window, worker, status)

    assert base_pkg.ostree_origin == RpmOstreeOriginKind.BASE
    assert base_pkg.is_user_installed is False
    assert layered_pkg.ostree_origin == RpmOstreeOriginKind.LAYERED
    assert layered_pkg.is_user_installed is True
    assert window._user_installed_cache == {layered_pkg.full_nevra}
    assert window._orphan_cache == set()
    assert inspector.lbl_install_type.text() == "Type: Layered (OSTree)"
    assert "45.20261117.1" in banner.deployment_label.text()
    assert status_messages and "1 layered/overridden RPM detected" in status_messages[0]


def test_flatpak_manifest_preserves_single_dnf5_bus_permission_invariant() -> None:
    manifest_path = Path(__file__).parents[1] / "io.github.xyasharx.Dendro.yml"
    finish_args: list[str] = []
    in_finish_args = False
    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "finish-args:":
            in_finish_args = True
            continue
        if in_finish_args:
            if raw_line.startswith("  - "):
                finish_args.append(raw_line.removeprefix("  - ").strip())
            elif not raw_line.startswith(" "):
                break

    assert finish_args
    bus_args = [arg for arg in finish_args if "talk-name" in arg]
    fs_args = [arg for arg in finish_args if arg.startswith("--filesystem")]

    assert bus_args == ["--system-talk-name=org.rpm.dnf.v0"]
    assert fs_args == []
