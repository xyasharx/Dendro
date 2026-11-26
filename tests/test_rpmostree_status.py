"""Tests for unprivileged read-only ``org.projectatomic.rpmostree1`` status."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QByteArray
from PyQt6.QtDBus import (
    QDBusArgument,
    QDBusMessage,
    QDBusObjectPath,
    QDBusSignature,
    QDBusVariant,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dendro.core import rpmostree_status as rpmostree_module
from dendro.core.backend import PackageInfo, PackageState
from dendro.core.host_environment import HostEnvironment, HostEnvironmentKind
from dendro.core.models import DependencyTreeModel, PackageFilterProxyModel
from dendro.core.rpmostree_status import (
    DEPLOYMENTS_PROPERTY,
    RPMOSTREE_SYSROOT_INTERFACE,
    RPMOSTREE_SYSROOT_PATH,
    GioRpmOstreePropertyTransport,
    QtRpmOstreePropertyTransport,
    RpmOstreeOriginKind,
    RpmOstreeStatusError,
    RpmOstreeStatusWorker,
    _unpack_glib_variant,
    _unwrap_dbus_value,
    parse_deployments_variant,
    query_rpmostree_status,
)
from dendro.ui.inspector_panel import PackageInspectorPanel
from dendro.ui.main_window import HostModeBanner, MainWindow
from dendro.ui.sidebar import CategorySidebar


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


def test_parses_upstream_six_field_gv_nevra_and_ignores_a_bool_epoch() -> None:
    """``rpm-ostreed`` ships ``gv_nevra`` as the 6-tuple ``(sstsss)``."""
    status = parse_deployments_variant(
        [
            {
                "id": "booted-6",
                "osname": "fedora",
                "version": "45.20261116.0",
                "booted": True,
                # ``base-removals`` is the resolved selector list (av of
                # gv_nevra); ``requested-base-removals`` is plain strings.
                "base-removals": [
                    # Upstream (nevra, name, epoch_u64, version, release, arch).
                    ("bash-5.3-1.fc45.x86_64", "bash", 0, "5.3", "1.fc45", "x86_64"),
                    # ``bool`` is an ``int`` subclass, so ``True`` must not be
                    # accepted as epoch 1.
                    ("zsh-5.9-2.fc45.x86_64", "zsh", True, "5.9", "2.fc45", "x86_64"),
                    # Short or non-string tuples are ignored, never guessed.
                    ("short", "short"),
                    ("missing-1-1.fc45.x86_64", "missing", 0, "1", None, "x86_64"),
                ],
            }
        ]
    )

    assert status.booted.removed_base_packages == frozenset(
        {"bash", "bash.x86_64", "bash-5.3-1.fc45.x86_64", "bash-0:5.3-1.fc45.x86_64"}
    )
    assert not any(
        "zsh" in selector for selector in status.booted.removed_base_packages
    )
    assert not any(
        "missing" in selector for selector in status.booted.removed_base_packages
    )


def test_renders_base_replacement_diffs_as_old_to_new() -> None:
    """Upstream ``a(vv)`` wire order is ``(new, old)``."""
    status = parse_deployments_variant(
        [
            {
                "id": "booted-diff",
                "osname": "fedora",
                "version": "45.20261116.0",
                "booted": True,
                "base-local-replacements": [
                    (
                        ("kernel", 0, "7.2.1", "300.fc45", "x86_64"),
                        ("kernel", 0, "7.2.0", "300.fc45", "x86_64"),
                    )
                ],
                "base-remote-replacements": {
                    "https://copr.fedorainfracloud.org": [
                        (
                            ("mesa-dri-drivers", 0, "26.2", "1.fc45", "x86_64"),
                            ("mesa-dri-drivers", 0, "26.1", "1.fc45", "x86_64"),
                        )
                    ]
                },
            }
        ]
    )

    diffs = status.booted.base_replacement_diffs
    assert "kernel-7.2.0-300.fc45.x86_64 -> kernel-7.2.1-300.fc45.x86_64" in diffs
    assert (
        "mesa-dri-drivers-26.1-1.fc45.x86_64 -> "
        "mesa-dri-drivers-26.2-1.fc45.x86_64" in diffs
    )
    assert "mesa-dri-drivers" in status.booted.overridden_packages


def test_live_replaced_and_in_progress_deployments_are_flagged() -> None:
    for key in ("live-replaced", "live-inprogress"):
        status = parse_deployments_variant(
            [
                {
                    "id": f"{key}-booted",
                    "osname": "fedora",
                    "version": "45.20261116.0",
                    "booted": True,
                    key: "f" * 64,
                }
            ]
        )
        assert status.booted.live_replaced is True, key


def test_pending_and_rollback_are_scoped_to_the_booted_osname() -> None:
    """A dual-boot host can expose deployments from another OSTree OS."""
    status = parse_deployments_variant(
        [
            {
                "id": "fedora-staged",
                "osname": "fedora",
                "version": "45.20261117.1",
                "staged": True,
            },
            {
                "id": "fedora-booted",
                "osname": "fedora",
                "version": "45.20261116.0",
                "booted": True,
            },
            {
                "id": "kinoite-booted",
                "osname": "kinoite",
                "version": "45.20261116.0",
                "booted": True,
            },
            {
                "id": "kinoite-rollback",
                "osname": "kinoite",
                "version": "45.20261115.0",
                "pinned": True,
            },
        ]
    )

    assert status.booted.osname == "fedora"
    assert status.pending is not None
    assert status.pending.osname == "fedora"
    assert status.rollback is None


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


def test_dbus_unwrapper_decodes_signature_and_qbytearray_and_rejects_qdbusargument() -> (
    None
):
    unwrapped = _unwrap_dbus_value(
        {
            "sig": QDBusSignature("aa{sv}"),
            "raw": QByteArray(b"\x00gpg\xff"),
            "empty": QByteArray(b""),
        }
    )
    assert unwrapped == {
        "sig": "aa{sv}",
        "raw": b"\x00gpg\xff",
        "empty": b"",
    }

    with pytest.raises(TypeError):
        _unwrap_dbus_value(QDBusArgument())


def test_qt_rpmostree_transport_maps_qdbusargument_and_reply_type_errors() -> None:
    class _FakeReply:
        def __init__(self, args_fn: Any) -> None:
            self._args_fn = args_fn

        def type(self) -> QDBusMessage.MessageType:
            return QDBusMessage.MessageType.ReplyMessage

        def arguments(self) -> list[Any]:
            return self._args_fn()

    class _FakeConnection:
        def __init__(self, args_fn: Any) -> None:
            self._args_fn = args_fn

        def isConnected(self) -> bool:
            return True

        def call(self, _message: Any, timeout: int = -1) -> _FakeReply:
            del timeout
            return _FakeReply(self._args_fn)

    def _raise_unsupported() -> list[Any]:
        raise TypeError("unsupported DBus argument type -1")

    transport = QtRpmOstreePropertyTransport(
        connection=_FakeConnection(_raise_unsupported)  # type: ignore[arg-type]
    )
    with pytest.raises(RpmOstreeStatusError) as exc_info:
        transport.get_property(
            RPMOSTREE_SYSROOT_PATH, RPMOSTREE_SYSROOT_INTERFACE, DEPLOYMENTS_PROPERTY
        )
    assert exc_info.value.title == "Invalid rpm-ostree reply"
    assert "unsupported DBus argument type -1" in exc_info.value.detail

    opaque_transport = QtRpmOstreePropertyTransport(
        connection=_FakeConnection(lambda: [QDBusArgument()])  # type: ignore[arg-type]
    )
    with pytest.raises(RpmOstreeStatusError) as opaque_exc:
        opaque_transport.get_property(
            RPMOSTREE_SYSROOT_PATH, RPMOSTREE_SYSROOT_INTERFACE, DEPLOYMENTS_PROPERTY
        )
    assert opaque_exc.value.title == "Invalid rpm-ostree reply"

    byte_transport = QtRpmOstreePropertyTransport(
        connection=_FakeConnection(
            lambda: [
                [
                    {
                        "id": "booted-0",
                        "booted": True,
                        "signatures": [QByteArray(b"\x00\x01")],
                    }
                ]
            ]
        )  # type: ignore[arg-type]
    )
    decoded = byte_transport.get_property(
        RPMOSTREE_SYSROOT_PATH, RPMOSTREE_SYSROOT_INTERFACE, DEPLOYMENTS_PROPERTY
    )
    assert decoded[0]["signatures"] == [b"\x00\x01"]
    assert parse_deployments_variant(decoded).booted.deployment_id == "booted-0"


def test_gio_rpmostree_transport_decodes_variants_and_maps_dbus_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeBytes:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def get_data(self) -> bytes:
            return self._data

    class _FakeVariant:
        def __init__(self, type_string: str, value: Any) -> None:
            self._type = type_string
            self._value = value

        def get_type_string(self) -> str:
            return self._type

        def get_size(self) -> int:
            return len(self._value)

        def get_data_as_bytes(self) -> _FakeBytes:
            return _FakeBytes(self._value)

        def get_variant(self) -> _FakeVariant:
            return self._value

        def n_children(self) -> int:
            return len(self._value)

        def get_child_value(self, index: int) -> _FakeVariant:
            return self._value[index]

        def unpack(self) -> Any:
            return self._value

    class _FakeGError(RuntimeError):
        def __init__(self, message: str, remote_name: str = "") -> None:
            super().__init__(message)
            self.message = message
            self.remote_name = remote_name

        def copy(self) -> _FakeGError:
            return _FakeGError(self.message, self.remote_name)

    class _FakeGLib:
        Error = _FakeGError

        @staticmethod
        def Variant(fmt: str, value: Any) -> tuple[str, Any]:
            return (fmt, value)

        @staticmethod
        def VariantType(fmt: str) -> str:
            return fmt

    class _FakeGio:
        class BusType:
            SYSTEM = "system"

        class DBusCallFlags:
            NONE = 0

        @staticmethod
        def dbus_error_get_remote_error(exc: _FakeGError) -> str:
            return exc.remote_name

        @staticmethod
        def dbus_error_strip_remote_error(exc: _FakeGError) -> bool:
            prefix = f"GDBus.Error:{exc.remote_name}: "
            if exc.remote_name and exc.message.startswith(prefix):
                exc.message = exc.message[len(prefix) :]
                return True
            return False

    monkeypatch.setattr(
        rpmostree_module, "_load_gio_modules", lambda: (_FakeGio, _FakeGLib)
    )

    deep = _FakeVariant("s", "leaf")
    for _ in range(25):
        deep = _FakeVariant(
            "v",
            _FakeVariant(
                "a{sv}", [_FakeVariant("{sv}", [_FakeVariant("s", "meta"), deep])]
            ),
        )

    reply_variant = _FakeVariant(
        "(v)",
        [
            _FakeVariant(
                "v",
                _FakeVariant(
                    "aa{sv}",
                    [
                        _FakeVariant(
                            "a{sv}",
                            [
                                _FakeVariant(
                                    "{sv}",
                                    [
                                        _FakeVariant("s", "id"),
                                        _FakeVariant(
                                            "v", _FakeVariant("s", "fedora-gio-0")
                                        ),
                                    ],
                                ),
                                _FakeVariant(
                                    "{sv}",
                                    [
                                        _FakeVariant("s", "booted"),
                                        _FakeVariant("v", _FakeVariant("b", True)),
                                    ],
                                ),
                                _FakeVariant(
                                    "{sv}",
                                    [
                                        _FakeVariant("s", "signatures"),
                                        _FakeVariant(
                                            "v",
                                            _FakeVariant(
                                                "av",
                                                [
                                                    _FakeVariant(
                                                        "v",
                                                        _FakeVariant(
                                                            "ay", b"\x00\x01\xff"
                                                        ),
                                                    )
                                                ],
                                            ),
                                        ),
                                    ],
                                ),
                                _FakeVariant(
                                    "{sv}",
                                    [
                                        _FakeVariant("s", "base-commit-meta"),
                                        _FakeVariant("v", deep),
                                    ],
                                ),
                            ],
                        )
                    ],
                ),
            )
        ],
    )

    class _FakeConnection:
        def __init__(self, *, error: Exception | None = None) -> None:
            self._error = error

        def is_closed(self) -> bool:
            return False

        def call_sync(self, *_args: Any) -> _FakeVariant:
            if self._error is not None:
                raise self._error
            return reply_variant

    transport = GioRpmOstreePropertyTransport(connection=_FakeConnection())
    decoded = transport.get_property(
        RPMOSTREE_SYSROOT_PATH, RPMOSTREE_SYSROOT_INTERFACE, DEPLOYMENTS_PROPERTY
    )
    assert decoded[0]["signatures"] == [b"\x00\x01\xff"]
    assert parse_deployments_variant(decoded).booted.deployment_id == "fedora-gio-0"

    assert _unpack_glib_variant(_FakeVariant("ms", [])) is None
    assert _unpack_glib_variant(_FakeVariant("ms", [_FakeVariant("s", "ok")])) == "ok"

    err_transport = GioRpmOstreePropertyTransport(
        connection=_FakeConnection(
            error=_FakeGError(
                "GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: The name is not activatable",
                "org.freedesktop.DBus.Error.ServiceUnknown",
            )
        )
    )
    with pytest.raises(RpmOstreeStatusError) as err_info:
        err_transport.get_property(
            RPMOSTREE_SYSROOT_PATH, RPMOSTREE_SYSROOT_INTERFACE, DEPLOYMENTS_PROPERTY
        )
    assert err_info.value.dbus_name == "org.freedesktop.DBus.Error.ServiceUnknown"
    assert err_info.value.detail == "The name is not activatable"


def test_query_prefers_gio_when_available_and_falls_back_to_qt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _FakeGioTransport:
        def get_property(self, path: str, interface: str, prop: str) -> Any:
            calls.append(f"gio:{path}:{interface}:{prop}")
            return _sample_silverblue_deployments()

    class _FakeQtTransport:
        def get_property(self, path: str, interface: str, prop: str) -> Any:
            calls.append(f"qt:{path}:{interface}:{prop}")
            return _sample_silverblue_deployments()

    monkeypatch.setattr(
        rpmostree_module, "_load_gio_modules", lambda: (object(), object())
    )
    monkeypatch.setattr(
        rpmostree_module, "GioRpmOstreePropertyTransport", _FakeGioTransport
    )
    monkeypatch.setattr(
        rpmostree_module, "QtRpmOstreePropertyTransport", _FakeQtTransport
    )
    status_gio = query_rpmostree_status(environment_detector=_atomic_environment)
    assert status_gio.booted.version == "45.20261116.0"
    assert calls == [
        f"gio:{RPMOSTREE_SYSROOT_PATH}:{RPMOSTREE_SYSROOT_INTERFACE}:{DEPLOYMENTS_PROPERTY}"
    ]

    calls.clear()
    monkeypatch.setattr(rpmostree_module, "_load_gio_modules", lambda: None)
    status_qt = query_rpmostree_status(environment_detector=_atomic_environment)
    assert status_qt.booted.version == "45.20261116.0"
    assert calls == [
        f"qt:{RPMOSTREE_SYSROOT_PATH}:{RPMOSTREE_SYSROOT_INTERFACE}:{DEPLOYMENTS_PROPERTY}"
    ]


def test_gio_rpmostree_transport_unpacks_real_glib_variants() -> None:
    gi_glib = pytest.importorskip("gi.repository.GLib")
    pytest.importorskip("gi.repository.Gio")

    nevra_tuple_sig = "(sstsss)"
    deployments_variant = gi_glib.Variant(
        "aa{sv}",
        [
            {
                "id": gi_glib.Variant("s", "fedora-real-gio"),
                "osname": gi_glib.Variant("s", "fedora"),
                "checksum": gi_glib.Variant("s", "c" * 64),
                "version": gi_glib.Variant("s", "45.20261117.0"),
                "booted": gi_glib.Variant("b", True),
                "staged": gi_glib.Variant("b", False),
                "pinned": gi_glib.Variant("b", False),
                "unlocked": gi_glib.Variant("s", "none"),
                "packages": gi_glib.Variant("as", ["htop"]),
                "requested-packages": gi_glib.Variant("as", ["htop"]),
                "base-removals": gi_glib.Variant(
                    "av",
                    [
                        gi_glib.Variant(
                            nevra_tuple_sig,
                            (
                                "bash-5.3-1.fc45.x86_64",
                                "bash",
                                0,
                                "5.3",
                                "1.fc45",
                                "x86_64",
                            ),
                        )
                    ],
                ),
                "base-local-replacements": gi_glib.Variant(
                    "a(vv)",
                    [
                        (
                            gi_glib.Variant(
                                nevra_tuple_sig,
                                (
                                    "kernel-7.2.1-300.fc45.x86_64",
                                    "kernel",
                                    0,
                                    "7.2.1",
                                    "300.fc45",
                                    "x86_64",
                                ),
                            ),
                            gi_glib.Variant(
                                nevra_tuple_sig,
                                (
                                    "kernel-7.2.0-300.fc45.x86_64",
                                    "kernel",
                                    0,
                                    "7.2.0",
                                    "300.fc45",
                                    "x86_64",
                                ),
                            ),
                        )
                    ],
                ),
                "signatures": gi_glib.Variant(
                    "av", [gi_glib.Variant("ay", b"\x00\xab\xcd")]
                ),
            }
        ],
    )
    reply = gi_glib.Variant("(v)", (deployments_variant,))

    class _FakeGioConnection:
        def is_closed(self) -> bool:
            return False

        def call_sync(self, *_args: Any) -> Any:
            return reply

    transport = GioRpmOstreePropertyTransport(connection=_FakeGioConnection())
    decoded = transport.get_property(
        RPMOSTREE_SYSROOT_PATH, RPMOSTREE_SYSROOT_INTERFACE, DEPLOYMENTS_PROPERTY
    )
    assert decoded[0]["signatures"] == [b"\x00\xab\xcd"]
    status = parse_deployments_variant(decoded)
    assert status.booted.deployment_id == "fedora-real-gio"
    assert "bash" in status.booted.removed_base_packages
    assert "kernel" in status.booted.overridden_packages
    assert (
        "kernel-7.2.0-300.fc45.x86_64 -> kernel-7.2.1-300.fc45.x86_64"
        in status.booted.base_replacement_diffs
    )
