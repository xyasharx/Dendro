"""Contract tests for the typed DNF5 daemon client.

These tests use a transport seam instead of a live host daemon. They verify the
public D-Bus API shape and lifecycle deterministically; Fedora integration still
needs to exercise the installed service.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from dendro.core.dnf5daemon import (
    BASE_INTERFACE,
    GOAL_INTERFACE,
    GROUP_INTERFACE,
    HISTORY_INTERFACE,
    REPOSITORY_INTERFACE,
    ROOT_PATH,
    RPM_INTERFACE,
    SESSION_MANAGER_INTERFACE,
    WRITE_TO_FD_FINISHED_SIGNAL,
    DbusInt64,
    Dnf5DaemonCallError,
    Dnf5DaemonClient,
    Dnf5DaemonPlanError,
    Dnf5DaemonUnavailable,
    Dnf5DaemonUnsupportedError,
    Dnf5DaemonValidationError,
    DnfCompsGroup,
    DnfCompsPackage,
    DnfHistoryTransaction,
    RepositoryKeyPrompt,
    TransactionRequest,
    _key_import_arguments,
    _key_import_prompt,
    _marshal_argument,
    _marshal_variant_value,
    _single_line,
)
from dendro.core.host_environment import HostEnvironment


@dataclass
class RecordingTransport:
    """In-memory implementation of the documented D-Bus client seam."""

    resolve_items: list[Any] = field(
        default_factory=lambda: [
            (
                "Package",
                "Remove",
                "User",
                {},
                {
                    "name": "systemd",
                    "epoch": "0",
                    "version": "256",
                    "release": "1.fc44",
                    "arch": "x86_64",
                },
            ),
            (
                "Package",
                "Install",
                "User",
                {},
                {
                    "name": "café-🌳",
                    "epoch": "0",
                    "version": "1",
                    "release": "1",
                    "arch": "noarch",
                },
            ),
        ]
    )
    resolve_result: int = 0
    calls: list[tuple[str, str, str, tuple[str, ...], tuple[Any, ...]]] = field(
        default_factory=list
    )
    callbacks: dict[object, Callable[[tuple[Any, ...]], None]] = field(
        default_factory=dict
    )
    unsubscribed: list[object] = field(default_factory=list)
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
            return (f"{ROOT_PATH}/session/42",)
        if interface == SESSION_MANAGER_INTERFACE and method == "close_session":
            # A real transport drops a session's signal subscriptions with the
            # connection's object path; mirror that so escaped watches are
            # visible to callers that assert on leftover callbacks.
            closed_path = arguments[0] if arguments else ""
            for token in tuple(self.callbacks):
                if token[0] == closed_path:
                    self.callbacks.pop(token, None)
            return (True,)
        if interface == GOAL_INTERFACE and method == "resolve":
            return (self.resolve_items, self.resolve_result)
        if interface == GOAL_INTERFACE and method == "get_transaction_problems_string":
            return (["one optional package is unavailable"],)
        if interface == GOAL_INTERFACE and method == "cancel":
            return (False, "RPM transaction already started")
        if interface == RPM_INTERFACE and method == "list_fd":
            raise Dnf5DaemonUnsupportedError(
                "DNF5 daemon interface unsupported",
                "UnknownMethod: list_fd",
                dbus_name="org.freedesktop.DBus.Error.UnknownMethod",
            )
        if interface == RPM_INTERFACE and method == "list":
            return (
                [
                    {
                        "name": "bash",
                        "version": "5.2",
                        "release": "9.fc44",
                        "arch": "x86_64",
                        "repo_id": "updates",
                    },
                ],
            )
        if interface == REPOSITORY_INTERFACE and method == "list":
            return (
                [
                    {
                        "id": "fedora",
                        "name": "Fedora",
                        "enabled": True,
                        "baseurl": "https://example.invalid/fedora",
                        "repofile": "/etc/yum.repos.d/fedora.repo",
                    },
                    {
                        "id": "third-party",
                        "name": "Third party",
                        "enabled": False,
                        "metalink": "https://example.invalid/meta",
                        "repofile": "/etc/yum.repos.d/third.repo",
                    },
                ],
            )
        if interface == BASE_INTERFACE and method == "clean_with_options":
            return (True, "")
        return ()

    def subscribe(
        self,
        path: str,
        interface: str,
        signal: str,
        callback: Callable[[tuple[Any, ...]], None],
    ) -> object:
        self._next_token += 1
        token = (path, interface, signal, self._next_token)
        self.callbacks[token] = callback
        return token

    def unsubscribe(self, token: object) -> None:
        self.unsubscribed.append(token)
        self.callbacks.pop(token, None)

    def emit(
        self, path: str, interface: str, signal: str, values: tuple[Any, ...]
    ) -> None:
        for token, callback in tuple(self.callbacks.items()):
            token_path, token_interface, token_signal, _token_id = token
            if (token_path, token_interface, token_signal) == (path, interface, signal):
                callback(values)


def _methods(transport: RecordingTransport) -> list[str]:
    return [
        method for _path, _interface, method, _signatures, _arguments in transport.calls
    ]


def test_request_normalizes_unicode_and_rejects_ambiguous_specs() -> None:
    request = TransactionRequest.from_values(
        installs=["café-🌳", "café-🌳"], removals=["old-package"]
    )
    assert request.installs == ("café-🌳",)
    assert request.removals == ("old-package",)

    with pytest.raises(Dnf5DaemonValidationError, match="leading/trailing whitespace"):
        TransactionRequest.from_values(installs=[" package"])
    with pytest.raises(Dnf5DaemonValidationError, match="control characters"):
        TransactionRequest.from_values(installs=["package\nnext"])
    with pytest.raises(Dnf5DaemonValidationError, match="incompatible actions"):
        TransactionRequest.from_values(installs=["same"], removals=["same"])


def test_resolve_execute_and_close_preserve_one_typed_plan() -> None:
    transport = RecordingTransport()
    client = Dnf5DaemonClient(transport=transport, flatpak_detector=lambda: False)
    request = TransactionRequest.from_values(
        installs=["café-🌳"], removals=["systemd"], upgrades=["bash"]
    )
    plan = client.resolve(request, {"systemd", "kernel"})

    assert plan.has_critical_system_removal is True
    assert plan.critical_removals == ("systemd",)
    assert "Remove: systemd-256-1.fc44.x86_64 (User)" in plan.preview_text()
    assert _methods(transport)[:5] == [
        "open_session",
        "upgrade",
        "install",
        "remove",
        "resolve",
    ]

    open_call = transport.calls[0]
    assert open_call[0:4] == (
        ROOT_PATH,
        SESSION_MANAGER_INTERFACE,
        "open_session",
        ("a{sv}",),
    )
    # ``locale`` is part of the session contract: the daemon runs each method
    # under ``newlocale()`` with it, so without it libdnf5's error messages come
    # back in the server's language rather than the user's.
    assert open_call[4] == (
        {
            "load_system_repo": True,
            "load_available_repos": True,
            "locale": "C.UTF-8",
        },
    )
    assert transport.calls[1][3:] == (("as", "a{sv}"), (("bash",), {}))
    assert transport.calls[2][3:] == (("as", "a{sv}"), (("café-🌳",), {}))
    assert transport.calls[3][3:] == (("as", "a{sv}"), (("systemd",), {}))

    logs: list[str] = []
    progress: list[int] = []
    client.attach_progress(plan, logs.append, progress.append)
    transport.emit(
        plan.session_path,
        RPM_INTERFACE,
        "transaction_before_begin",
        (plan.session_path, 2),
    )
    transport.emit(
        plan.session_path,
        RPM_INTERFACE,
        "transaction_after_complete",
        (plan.session_path, True),
    )
    result = client.execute(plan)
    assert result.completed_signal_received is True
    assert result.completed_successfully is True
    client.close_after_execution(plan)

    assert "do_transaction" in _methods(transport)
    assert _methods(transport)[-1] == "close_session"
    assert progress == [30, 100]
    assert any("Applying RPM transaction" in line for line in logs)
    # Ten progress callbacks plus the two repository key-prompt watches that
    # belong to the resolved session.
    assert len(transport.unsubscribed) == 12
    assert transport.callbacks == {}
    with pytest.raises(Dnf5DaemonPlanError, match="already been discarded"):
        client.execute(plan)


def test_partial_progress_subscription_failure_is_typed_and_cleans_registered_callbacks() -> (
    None
):
    class FailingSubscribeTransport(RecordingTransport):
        def subscribe(self, path, interface, signal, callback):
            if signal == "download_progress":
                raise RuntimeError("D-Bus signal subscription rejected")
            return super().subscribe(path, interface, signal, callback)

    transport = FailingSubscribeTransport()
    client = Dnf5DaemonClient(transport=transport)
    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())

    with pytest.raises(Dnf5DaemonCallError, match="D-Bus signal subscription rejected"):
        client.attach_progress(plan, lambda _line: None, lambda _percent: None)

    assert len(transport.unsubscribed) == 1
    # The failed progress subscription was rolled back; only the resolved
    # session's key-prompt watches remain, because the plan is still usable.
    assert {token[2] for token in transport.callbacks} == {
        "repo_key_import_request",
        "repo_key_imported",
    }
    client.discard(plan)
    assert transport.callbacks == {}
    assert _methods(transport)[-1] == "close_session"


def test_progress_unsubscribe_failure_is_retained_for_a_safe_retry() -> None:
    class RetryUnsubscribeTransport(RecordingTransport):
        def __init__(self) -> None:
            super().__init__()
            self.fail_once = True

        def unsubscribe(self, token: object) -> None:
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("temporary transport disconnect failure")
            super().unsubscribe(token)

    transport = RetryUnsubscribeTransport()
    client = Dnf5DaemonClient(transport=transport)
    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())
    client.attach_progress(plan, lambda _line: None, lambda _percent: None)

    with pytest.raises(
        Dnf5DaemonCallError, match="temporary transport disconnect failure"
    ):
        client.detach_progress(plan)

    client.detach_progress(plan)
    assert len(transport.unsubscribed) == 10
    # Progress callbacks are all gone; the resolved session's key-prompt watches
    # belong to the plan and are released by the discard below.
    assert {token[2] for token in transport.callbacks} == {
        "repo_key_import_request",
        "repo_key_imported",
    }
    client.discard(plan)
    assert transport.callbacks == {}


def _plan_item(action: str, name: str, *, arch: str = "x86_64") -> tuple[Any, ...]:
    return (
        "Package",
        action,
        "Dependency",
        {},
        {
            "name": name,
            "epoch": "0",
            "version": "1",
            "release": "1.fc44",
            "arch": arch,
        },
    )


def test_paired_replaced_actions_are_not_reported_as_removals() -> None:
    """``libdnf5`` emits ``Replaced`` for every routine upgrade.

    Counting those as removals made a normal ``upgrade`` look like a mass
    deletion, including of protected packages.
    """
    transport = RecordingTransport()
    transport.resolve_items = [
        _plan_item("Upgrade", "bash"),
        _plan_item("Replaced", "bash"),
        _plan_item("Upgrade", "kernel-core"),
        _plan_item("Replaced", "kernel-core"),
        _plan_item("Install", "htop"),
    ]
    client = Dnf5DaemonClient(transport=transport, flatpak_detector=lambda: False)
    plan = client.resolve(
        TransactionRequest.from_values(upgrades=["bash", "kernel-core", "htop"]),
        {"bash", "kernel-core"},
    )

    installs, removals, upgrades = plan.categorized_names()
    assert removals == []
    assert plan.critical_removals == ()
    assert plan.has_critical_system_removal is False
    assert len(upgrades) == 2
    assert installs == ["htop-1-1.fc44.x86_64"]
    assert "Remove:" not in plan.preview_text()


def test_cross_package_obsoletes_still_count_as_a_removal() -> None:
    transport = RecordingTransport()
    transport.resolve_items = [
        _plan_item("Install", "new-tool"),
        # No inbound action keeps "old-tool" installed: a real Obsoletes removal.
        _plan_item("Replaced", "old-tool"),
        _plan_item("Remove", "systemd"),
    ]
    client = Dnf5DaemonClient(transport=transport, flatpak_detector=lambda: False)
    plan = client.resolve(
        TransactionRequest.from_values(installs=["new-tool"], removals=["systemd"]),
        {"systemd"},
    )

    _installs, removals, _upgrades = plan.categorized_names()
    assert "old-tool-1-1.fc44.x86_64" in removals
    assert plan.critical_removals == ("systemd",)


def test_replaced_arch_sibling_is_not_escalated_into_a_critical_removal() -> None:
    """Multilib cleanup keeps the package name installed, so it is not a removal.

    An explicit removal of one architecture still arrives as a ``Remove`` item,
    so that case stays covered while a routine ``Replaced`` cannot raise a
    false "protected package will be removed" alarm.
    """
    transport = RecordingTransport()
    transport.resolve_items = [
        _plan_item("Upgrade", "glibc", arch="x86_64"),
        _plan_item("Replaced", "glibc", arch="i686"),
    ]
    client = Dnf5DaemonClient(transport=transport, flatpak_detector=lambda: False)
    plan = client.resolve(TransactionRequest.from_values(upgrades=["glibc"]), {"glibc"})

    _installs, removals, upgrades = plan.categorized_names()
    assert removals == []
    assert upgrades == ["glibc-1-1.fc44.x86_64"]
    assert plan.critical_removals == ()

    # The same package removed for real is still reported and still protected.
    transport.resolve_items = [_plan_item("Remove", "glibc", arch="i686")]
    plan = client.resolve(TransactionRequest.from_values(removals=["glibc"]), {"glibc"})
    _installs, removals, _upgrades = plan.categorized_names()
    assert removals == ["glibc-1-1.fc44.i686"]
    assert plan.critical_removals == ("glibc",)


def test_cleanup_failure_invalidates_a_plan_before_reporting_the_failure() -> None:
    class CloseFailureTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == SESSION_MANAGER_INTERFACE and method == "close_session":
                self.calls.append(
                    (path, interface, method, tuple(signatures), tuple(arguments))
                )
                return (False,)
            return super().call(path, interface, method, signatures, arguments)

    transport = CloseFailureTransport()
    client = Dnf5DaemonClient(transport=transport)
    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())

    with pytest.raises(Dnf5DaemonCallError, match="refused to close"):
        client.close_after_execution(plan)
    with pytest.raises(Dnf5DaemonPlanError, match="already been discarded"):
        client.execute(plan)


def test_warning_resolution_closes_session_and_retains_notices() -> None:
    transport = RecordingTransport(resolve_items=[], resolve_result=1)
    client = Dnf5DaemonClient(transport=transport)
    plan = client.resolve(
        TransactionRequest.from_values(installs=["optional-package"]), set()
    )

    assert plan.warnings == ("one optional package is unavailable",)
    assert plan.has_changes is False
    assert _methods(transport) == [
        "open_session",
        "install",
        "resolve",
        "get_transaction_problems_string",
    ]
    client.discard(plan)
    assert _methods(transport)[-1] == "close_session"


def test_resolve_rejects_a_transaction_item_without_a_package_identity() -> None:
    transport = RecordingTransport(
        resolve_items=[("Package", "Remove", "User", {}, {})]
    )
    client = Dnf5DaemonClient(transport=transport)

    with pytest.raises(
        Dnf5DaemonCallError, match="missing its package or group identity"
    ):
        client.resolve(
            TransactionRequest.from_values(removals=["systemd"]), {"systemd"}
        )

    assert _methods(transport)[-1] == "close_session"


def test_invalid_resolve_output_closes_session_before_raising() -> None:
    transport = RecordingTransport(resolve_items=[], resolve_result=9)
    client = Dnf5DaemonClient(transport=transport)
    with pytest.raises(Exception, match="unsupported result code 9"):
        client.resolve(TransactionRequest.from_values(installs=["broken"]), set())
    assert _methods(transport)[-1] == "close_session"


def test_available_updates_request_every_attribute_the_parser_reads() -> None:
    """``package_attrs`` is a projection, not a hint.

    ``Rpm.list`` builds each package map from exactly the attributes named in
    ``package_attrs``, so an attribute the parser reads but the request omits is
    silently absent from every real reply. Because the parser falls back to
    empty values, that failure is invisible: ``summary`` and both size fields
    just became ``""``/``0`` for every upgrade candidate. This pins the request
    to the parser so the drift cannot recur.
    """
    transport = RecordingTransport()

    Dnf5DaemonClient(transport=transport).list_available_updates()

    request = next(
        arguments[0]
        for _path, interface, method, _signatures, arguments in transport.calls
        if interface == RPM_INTERFACE and method == "list"
    )
    requested = set(request["package_attrs"])

    # Each key _parse_package_records dereferences.
    for attribute in (
        "name",
        "epoch",
        "version",
        "release",
        "arch",
        "repo_id",
        "summary",
        "install_size",
        "download_size",
    ):
        assert attribute in requested, f"{attribute} is read but never requested"


def test_available_updates_reject_a_negative_daemon_package_size() -> None:
    class NegativeSizeTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == RPM_INTERFACE and method == "list":
                self.calls.append(
                    (path, interface, method, tuple(signatures), tuple(arguments))
                )
                return (
                    [
                        {
                            "name": "bash",
                            "version": "5.2",
                            "release": "9.fc44",
                            "arch": "x86_64",
                            "install_size": -1,
                        }
                    ],
                )
            return super().call(path, interface, method, signatures, arguments)

    transport = NegativeSizeTransport()
    client = Dnf5DaemonClient(transport=transport)

    with pytest.raises(Dnf5DaemonCallError, match="negative package size"):
        client.list_available_updates()

    assert _methods(transport)[-1] == "close_session"


def test_repository_listing_rejects_a_non_boolean_enabled_state() -> None:
    class InvalidRepositoryTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == REPOSITORY_INTERFACE and method == "list":
                self.calls.append(
                    (path, interface, method, tuple(signatures), tuple(arguments))
                )
                return (
                    [
                        {
                            "id": "fedora",
                            "name": "Fedora",
                            "enabled": "false",
                        }
                    ],
                )
            return super().call(path, interface, method, signatures, arguments)

    transport = InvalidRepositoryTransport()
    client = Dnf5DaemonClient(transport=transport)

    with pytest.raises(Dnf5DaemonCallError, match="enabled state that is not boolean"):
        client.list_repositories()

    assert _methods(transport)[-1] == "close_session"


def test_repository_methods_use_documented_options_and_close_each_session() -> None:
    transport = RecordingTransport()
    client = Dnf5DaemonClient(transport=transport)
    repositories = client.list_repositories()
    assert [repository.identifier for repository in repositories] == [
        "fedora",
        "third-party",
    ]
    assert repositories[1].baseurl == "https://example.invalid/meta"

    client.set_repository_enabled("third-party", True)
    client.set_repository_enabled("third-party", False)
    client.clean_cache()
    calls = transport.calls
    repo_list = next(
        call for call in calls if call[1:3] == (REPOSITORY_INTERFACE, "list")
    )
    assert repo_list[4][0]["enable_disable"] == "all"
    assert repo_list[4][0]["repo_attrs"] == (
        "name",
        "enabled",
        "baseurl",
        "metalink",
        "mirrorlist",
        "repofile",
    )
    assert any(
        call[2] == "enable_with_options"
        and call[4] == (("third-party",), {"interactive": True})
        for call in calls
    )
    assert any(
        call[2] == "disable_with_options"
        and call[4] == (("third-party",), {"interactive": True})
        for call in calls
    )
    assert any(
        call[2] == "clean_with_options" and call[4] == ("all", {"interactive": True})
        for call in calls
    )
    assert (
        _methods(transport).count("open_session")
        == _methods(transport).count("close_session")
        == 4
    )


def test_update_query_uses_daemon_package_list_and_daemon_cache_expiry() -> None:
    transport = RecordingTransport()
    client = Dnf5DaemonClient(transport=transport)

    updates = client.list_available_updates(refresh=True)

    assert [
        (update.name, update.version, update.release, update.repository)
        for update in updates
    ] == [
        ("bash", "5.2", "9.fc44", "updates"),
    ]
    list_call = next(
        call for call in transport.calls if call[1:3] == (RPM_INTERFACE, "list")
    )
    assert list_call[4] == (
        {
            "scope": "upgrades",
            "latest-limit": 1,
            "package_attrs": (
                "name",
                "epoch",
                "version",
                "release",
                "arch",
                "repo_id",
                "summary",
                "install_size",
                "download_size",
            ),
            "interactive": False,
        },
    )
    expiry_call = next(
        call
        for call in transport.calls
        if call[1:3] == (BASE_INTERFACE, "clean_with_options")
    )
    assert expiry_call[4] == ("expire-cache", {"interactive": False})
    assert (
        _methods(transport).count("open_session")
        == _methods(transport).count("close_session")
        == 2
    )


def test_update_query_preserves_a_nonzero_epoch_from_the_daemon() -> None:
    class EpochTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == RPM_INTERFACE and method == "list":
                self.calls.append(
                    (path, interface, method, tuple(signatures), tuple(arguments))
                )
                return (
                    [
                        {
                            "name": "epoch-test",
                            "epoch": "7",
                            "version": "1.2",
                            "release": "3.fc44",
                            "arch": "x86_64",
                            "repo_id": "updates",
                        }
                    ],
                )
            return super().call(path, interface, method, signatures, arguments)

    updates = Dnf5DaemonClient(transport=EpochTransport()).list_available_updates()

    assert updates[0].epoch == "7"
    assert updates[0].name_arch == "epoch-test.x86_64"


def test_local_rpm_requires_readable_host_file_and_is_rejected_in_flatpak(
    tmp_path: Path,
) -> None:
    rpm = tmp_path / "local package.rpm"
    rpm.write_bytes(b"not an RPM; daemon validates the content")
    request = TransactionRequest.from_values(installs=[str(rpm)])
    transport = RecordingTransport()
    client = Dnf5DaemonClient(transport=transport, flatpak_detector=lambda: False)
    client.resolve(request, set())
    install_call = next(
        call for call in transport.calls if call[1:3] == (RPM_INTERFACE, "install")
    )
    assert install_call[4][0] == (str(rpm.resolve()),)

    flatpak_client = Dnf5DaemonClient(
        transport=RecordingTransport(), flatpak_detector=lambda: True
    )
    with pytest.raises(
        Dnf5DaemonValidationError, match="does not expose the host filesystem"
    ):
        flatpak_client.resolve(request, set())


def test_client_construction_is_read_only_until_daemon_capability_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingTransport:
        def __init__(self) -> None:
            raise Dnf5DaemonUnavailable("DNF5 daemon is unavailable", "No system bus")

    monkeypatch.setattr("dendro.core.dnf5daemon.QtDbusTransport", MissingTransport)
    client = Dnf5DaemonClient(environment_detector=HostEnvironment.traditional)
    with pytest.raises(Dnf5DaemonUnavailable, match="No system bus"):
        client.ensure_available()


def test_repository_list_worker_emits_one_terminal_result(qtbot) -> None:
    from dendro.core.dnf5daemon import DnfRepository, RepositoryListWorker

    class RepositoryClient:
        def list_repositories(self):
            return (
                DnfRepository(
                    "fedora", "Fedora", True, "", "/etc/yum.repos.d/fedora.repo"
                ),
            )

    worker = RepositoryListWorker(RepositoryClient())
    received: list[object] = []
    failures: list[tuple[str, str]] = []
    worker.signals.loaded.connect(received.append)
    worker.signals.failed.connect(
        lambda title, detail: failures.append((title, detail))
    )
    worker.run()

    assert len(received) == 1
    assert failures == []
    assert received[0][0].identifier == "fedora"


def test_repository_list_worker_translates_daemon_failure(qtbot) -> None:
    from dendro.core.dnf5daemon import RepositoryListWorker

    class FailingRepositoryClient:
        def list_repositories(self):
            raise Dnf5DaemonUnavailable(
                "DNF5 daemon is unavailable", "System bus denied access"
            )

    worker = RepositoryListWorker(FailingRepositoryClient())
    received: list[object] = []
    failures: list[tuple[str, str]] = []
    worker.signals.loaded.connect(received.append)
    worker.signals.failed.connect(
        lambda title, detail: failures.append((title, detail))
    )
    worker.run()

    assert received == []
    assert failures == [("DNF5 daemon is unavailable", "System bus denied access")]


def test_malformed_progress_signals_are_diagnostic_and_do_not_break_execution(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = RecordingTransport()
    client = Dnf5DaemonClient(transport=transport)
    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())
    logs: list[str] = []
    progress: list[int] = []
    client.attach_progress(plan, logs.append, progress.append)

    with caplog.at_level("WARNING", logger="dendro.core.dnf5daemon"):
        transport.emit(
            plan.session_path,
            BASE_INTERFACE,
            "download_add_new",
            (plan.session_path, "id", "htop", -1),
        )
        transport.emit(
            plan.session_path,
            RPM_INTERFACE,
            "transaction_after_complete",
            (plan.session_path, "false"),
        )

    assert logs == []
    assert progress == []
    assert "download total is negative" in caplog.text
    assert "success is not boolean" in caplog.text

    transport.emit(
        plan.session_path,
        RPM_INTERFACE,
        "transaction_after_complete",
        (plan.session_path, False),
    )
    with pytest.raises(
        Dnf5DaemonCallError, match="completed the transaction with a failure status"
    ):
        client.execute(plan)
    client.close_after_execution(plan)


class KeyPromptTransport(RecordingTransport):
    """Transport that asks for an OpenPGP key decision while a call is open.

    The real daemon emits ``repo_key_import_request`` from inside the method that
    loads repository metadata and blocks until ``Repo.confirm_key_with_options``
    arrives.  This double reproduces that by invoking the prompt from inside the
    ``open_session`` call, exactly where a client has to already be listening.
    """

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[tuple[str, str]] = []
        self.confirmations: list[tuple[str, str, dict[str, Any]]] = []
        self.prompt_once = True
        self.fail_confirmation = False

    def call(
        self,
        path: str,
        interface: str,
        method: str,
        signatures: tuple[str, ...],
        arguments: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        # ``open_session`` performs no libdnf5 work, so the prompt fires in the
        # first call that loads repository metadata (``Goal.resolve`` does that
        # through ``Session::fill_sack``).
        if interface == GOAL_INTERFACE and method == "resolve" and self.prompt_once:
            self.prompt_once = False
            self.emit(
                path,
                BASE_INTERFACE,
                "repo_key_import_request",
                (path, "ABCDEF12", ["1000"], "FINGERPRINT", "https://k", 0),
            )
        if interface == REPOSITORY_INTERFACE and method == "confirm_key_with_options":
            key_id, confirmed, options = arguments
            self.confirmations.append((key_id, confirmed, options))
            if self.fail_confirmation:
                raise RuntimeError("D-Bus refused the key confirmation")
            return ()
        return super().call(path, interface, method, signatures, arguments)


def test_key_prompt_is_declined_by_default_without_blocking_the_caller() -> None:
    """An unanswered prompt stalls the daemon for five minutes, so Dendro answers."""
    transport = KeyPromptTransport()
    client = Dnf5DaemonClient(transport=transport, key_import_policy=lambda: False)

    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())

    assert transport.confirmations == [("ABCDEF12", False, {"interactive": True})]
    # The decline is explained on the plan instead of silently shrinking it.
    assert any("was not trusted" in warning for warning in plan.warnings)
    assert any("ABCDEF12" in warning for warning in plan.warnings)
    client.discard(plan)
    assert client.take_declined_key_notices() == ()


def test_automatic_key_import_confirms_without_shrinking_the_plan() -> None:
    transport = KeyPromptTransport()
    client = Dnf5DaemonClient(transport=transport, key_import_policy=lambda: True)

    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())

    assert transport.confirmations == [("ABCDEF12", True, {"interactive": True})]
    assert plan.warnings == ()
    client.discard(plan)


def test_failed_key_confirmation_is_reported_rather_than_swallowed() -> None:
    transport = KeyPromptTransport()
    transport.fail_confirmation = True
    client = Dnf5DaemonClient(transport=transport, key_import_policy=lambda: True)

    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())

    reason = next(warning for warning in plan.warnings if "ABCDEF12" in warning)
    assert "was not trusted" in reason
    client.discard(plan)


def test_key_watches_are_released_with_the_session_and_skip_read_only_sessions() -> (
    None
):
    transport = RecordingTransport()
    client = Dnf5DaemonClient(transport=transport)

    repositories = client.list_repositories()
    assert repositories
    # Repository listing loads no metadata, so nothing is watched and nothing leaks.
    assert transport.callbacks == {}
    assert "confirm_key_with_options" not in _methods(transport)

    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())
    assert {token[2] for token in transport.callbacks} == {
        "repo_key_import_request",
        "repo_key_imported",
    }
    client.discard(plan)
    assert transport.callbacks == {}
    # No prompt appeared in this run, so nothing needed confirming.
    assert "confirm_key_with_options" not in _methods(transport)


def test_key_import_prompt_decoding_accepts_both_signal_shapes() -> None:
    """Older bindings emit a single user id where current ones emit a list."""
    current = _key_import_arguments(
        ("/session/1", "KEY", ["1000"], "FPR", "https://key", 0)
    )
    legacy = _key_import_arguments(
        ("/session/1", "KEY", "1000", "FPR", "https://key", 0)
    )
    assert current == legacy == ("/session/1", "KEY", "FPR")

    with pytest.raises(ValueError, match="at least five"):
        _key_import_arguments(("/session/1", "KEY"))
    with pytest.raises(ValueError, match="session object path"):
        _key_import_arguments(("", "KEY", ["1000"], "FPR", "https://key", 0))


def test_declined_key_notices_do_not_leak_into_a_later_resolve() -> None:
    transport = KeyPromptTransport()
    client = Dnf5DaemonClient(transport=transport, key_import_policy=lambda: False)

    first = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())
    assert any("not trusted" in warning for warning in first.warnings)
    client.discard(first)

    transport.prompt_once = False
    second = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())
    assert second.warnings == ()
    client.discard(second)


def test_daemon_text_cannot_forge_extra_console_lines() -> None:
    """Package and download text reaches the console, so newlines are flattened.

    A ``\\n`` in a package name would otherwise let the daemon print a line that
    looks like Dendro's own status output (for example a success message), and an
    unbounded value would let it flood the widget.
    """
    assert _single_line("evil\nFake: transaction succeeded") == (
        "evil Fake: transaction succeeded"
    )
    assert _single_line("a\tb\r\nc") == "a b c"
    assert _single_line("a\x00b\x1bc") == "a b c"
    assert len(_single_line("x" * 5000)) == 1024
    assert _single_line(None) == ""

    transport = RecordingTransport()
    client = Dnf5DaemonClient(transport=transport, flatpak_detector=lambda: False)
    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())
    lines: list[str] = []
    client.attach_progress(plan, lines.append, lambda _percent: None)

    transport.emit(
        plan.session_path,
        RPM_INTERFACE,
        "transaction_action_start",
        (plan.session_path, "pkg\nFake: transaction succeeded", "", 0),
    )
    transport.emit(
        plan.session_path,
        RPM_INTERFACE,
        "transaction_script_error",
        (plan.session_path, "pkg\nFALSE LOG", "", 1),
    )
    client.detach_progress(plan)
    client.discard(plan)

    assert lines == [
        "Processing: pkg Fake: transaction succeeded\n",
        "Scriptlet error for pkg FALSE LOG (exit 1).\n",
    ]


def test_interactive_prompt_trusts_the_key_when_the_user_accepts() -> None:
    """Without this the only way to accept a key was a global setting."""
    transport = KeyPromptTransport()
    asked: list[RepositoryKeyPrompt] = []

    def ask(prompt: RepositoryKeyPrompt) -> bool:
        asked.append(prompt)
        return True

    client = Dnf5DaemonClient(
        transport=transport,
        key_import_policy=lambda: False,
        key_import_prompt=ask,
    )

    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())

    assert transport.confirmations == [("ABCDEF12", True, {"interactive": True})]
    assert plan.warnings == ()
    assert len(asked) == 1
    prompt = asked[0]
    assert prompt.key_id == "ABCDEF12"
    assert prompt.fingerprint == "FINGERPRINT"
    assert prompt.key_url == "https://k"
    assert prompt.user_ids == ("1000",)
    client.discard(plan)


def test_interactive_prompt_declines_when_the_user_refuses() -> None:
    transport = KeyPromptTransport()
    client = Dnf5DaemonClient(
        transport=transport,
        key_import_policy=lambda: False,
        key_import_prompt=lambda prompt: False,
    )

    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())

    assert transport.confirmations == [("ABCDEF12", False, {"interactive": True})]
    assert any("was not trusted" in warning for warning in plan.warnings)
    client.discard(plan)


def test_stored_preference_wins_over_asking_the_user() -> None:
    """An automatic policy must not raise a dialog on every repository."""
    transport = KeyPromptTransport()
    asked: list[object] = []
    client = Dnf5DaemonClient(
        transport=transport,
        key_import_policy=lambda: True,
        key_import_prompt=lambda prompt: asked.append(prompt) or False,
    )

    client.resolve(TransactionRequest.from_values(installs=["htop"]), set())

    assert transport.confirmations == [("ABCDEF12", True, {"interactive": True})]
    assert asked == []


def test_a_prompt_that_raises_declines_the_key_instead_of_stranding_the_daemon() -> (
    None
):
    transport = KeyPromptTransport()

    def exploding_prompt(prompt: RepositoryKeyPrompt) -> bool:
        raise RuntimeError("the dialog could not be shown")

    client = Dnf5DaemonClient(
        transport=transport,
        key_import_policy=lambda: False,
        key_import_prompt=exploding_prompt,
    )

    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())

    # The daemon still got an answer, and it was the safe one.
    assert transport.confirmations == [("ABCDEF12", False, {"interactive": True})]
    assert any("was not trusted" in warning for warning in plan.warnings)
    client.discard(plan)


def test_key_prompt_carries_multiple_signed_identities() -> None:
    prompt = _key_import_prompt(
        ("/s/1", "KEY", ["Alice <a@example.org>", "Bob <b@example.org>"], "FPR", "u", 0)
    )
    assert prompt.user_ids == ("Alice <a@example.org>", "Bob <b@example.org>")

    single = _key_import_prompt(("/s/1", "KEY", "Alice <a@example.org>", "FPR", "u", 0))
    assert single.user_ids == ("Alice <a@example.org>",)


def test_key_confirmation_uses_primary_connection_to_preserve_session_sender() -> None:
    """``dnf5daemon-server`` rejects session calls from another bus sender."""

    class NestedSpyTransport(KeyPromptTransport):
        def __init__(self) -> None:
            super().__init__()
            self.nested_calls: list[str] = []

        def call_nested(
            self,
            path: str,
            interface: str,
            method: str,
            signatures: tuple[str, ...],
            arguments: tuple[Any, ...],
        ) -> tuple[Any, ...]:
            self.nested_calls.append(method)
            raise AssertionError("Confirmation must use the session's primary sender")

    transport = NestedSpyTransport()
    client = Dnf5DaemonClient(transport=transport, key_import_policy=lambda: True)

    plan = client.resolve(TransactionRequest.from_values(installs=["htop"]), set())

    assert transport.nested_calls == []
    assert transport.confirmations == [("ABCDEF12", True, {"interactive": True})]
    client.discard(plan)


def test_qt_dbus_transport_does_not_open_secondary_bus_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    QtDBus = pytest.importorskip("PyQt6.QtDBus")
    from dendro.core.dnf5daemon import QtDbusTransport

    assert not hasattr(QtDbusTransport, "_connection_for_nested")
    assert not hasattr(QtDbusTransport, "call_nested")

    class _FakeReply:
        def type(self):
            return QtDBus.QDBusMessage.MessageType.ReplyMessage

        def arguments(self) -> list[Any]:
            return []

    class _FakeConnection:
        def __init__(self) -> None:
            self.sent: list[Any] = []

        def isConnected(self) -> bool:
            return True

        def call(self, message, timeout: int = -1):
            self.sent.append(
                (message.path(), message.interface(), message.member(), timeout)
            )
            return _FakeReply()

    def forbid_connect_to_bus(*_args, **_kwargs):
        raise AssertionError("QtDbusTransport must never open a second bus connection")

    monkeypatch.setattr(QtDBus.QDBusConnection, "connectToBus", forbid_connect_to_bus)
    # Ensure QtDBus registers built-in meta-types (e.g. QDBusVariant) even when
    # the test injects a fake connection instead of calling systemBus() first.
    QtDBus.QDBusConnection.systemBus()
    connection = _FakeConnection()
    transport = QtDbusTransport(connection=connection)  # type: ignore[arg-type]

    result = transport.call(
        "/org/rpm/dnf/v0/session/1",
        REPOSITORY_INTERFACE,
        "confirm_key_with_options",
        ("s", "b", "a{sv}"),
        ("ABCDEF12", True, {"interactive": True}),
    )

    assert result == ()
    assert len(connection.sent) == 1
    assert connection.sent[0][:3] == (
        "/org/rpm/dnf/v0/session/1",
        REPOSITORY_INTERFACE,
        "confirm_key_with_options",
    )


def test_qt_dbus_transport_marshals_unix_fd_and_maps_unsupported_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from PyQt6 import QtDBus

    from dendro.core.dnf5daemon import QtDbusTransport

    monkeypatch.setattr(
        QtDBus.QDBusConnection,
        "connectToBus",
        lambda *_args, **_kwargs: pytest.fail(
            "QtDbusTransport must never open a second bus connection"
        ),
    )
    QtDBus.QDBusConnection.systemBus()

    r_fd, w_fd = os.pipe()
    try:
        marshalled = _marshal_argument("h", w_fd)
    finally:
        os.close(r_fd)
        os.close(w_fd)
    assert isinstance(marshalled, QtDBus.QDBusUnixFileDescriptor)
    assert marshalled.isValid()

    class _FakeErrorReply:
        def __init__(self, error_name: str) -> None:
            self._error_name = error_name

        def type(self):
            return QtDBus.QDBusMessage.MessageType.ErrorMessage

        def errorName(self) -> str:
            return self._error_name

        def errorMessage(self) -> str:
            return "interface or method unsupported"

    class _FakeConnection:
        def __init__(self, error_name: str) -> None:
            self._error_name = error_name

        def isConnected(self) -> bool:
            return True

        def call(self, _message, timeout: int = -1):
            return _FakeErrorReply(self._error_name)

    for dbus_error in (
        "org.freedesktop.DBus.Error.UnknownInterface",
        "org.freedesktop.DBus.Error.UnknownMethod",
        "org.freedesktop.DBus.Error.UnknownObject",
    ):
        transport = QtDbusTransport(connection=_FakeConnection(dbus_error))  # type: ignore[arg-type]
        with pytest.raises(Dnf5DaemonUnsupportedError) as exc_info:
            transport.call(
                "/org/rpm/dnf/v0/session/1",
                HISTORY_INTERFACE,
                "list",
                (),
                (),
            )
        assert isinstance(exc_info.value, Dnf5DaemonCallError)
        assert exc_info.value.dbus_name == dbus_error


class _FdAndHistoryTransport(RecordingTransport):
    """Transport double supporting Rpm.list_fd streaming and History.list."""

    def __init__(
        self,
        *,
        fd_chunks: list[bytes] = (),  # type: ignore[assignment]
        fd_ok: bool = True,
        fd_error: str = "",
        history_items: list[dict[str, Any]] = (),  # type: ignore[assignment]
        history_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.fd_chunks = list(fd_chunks)
        self.fd_ok = fd_ok
        self.fd_error = fd_error
        self.history_items = list(history_items)
        self.history_error = history_error
        self._callbacks: list[
            tuple[str, str, str, Callable[[tuple[Any, ...]], None]]
        ] = []

    def subscribe(
        self,
        path: str,
        interface: str,
        signal_name: str,
        callback: Callable[[tuple[Any, ...]], None],
    ) -> object:
        token = (path, interface, signal_name, callback)
        self._callbacks.append(token)
        return token

    def unsubscribe(self, handle: object) -> None:
        if handle in self._callbacks:
            self._callbacks.remove(handle)  # type: ignore[arg-type]

    def call(
        self,
        path: str,
        interface: str,
        method: str,
        signatures: Any,
        arguments: Any,
    ) -> tuple[Any, ...]:
        import os

        if interface == RPM_INTERFACE and method == "list_fd":
            self.calls.append(
                (path, interface, method, tuple(signatures), tuple(arguments))
            )
            transfer_id = "transfer-42"
            dup_fd = os.dup(int(arguments[1]))
            ok = self.fd_ok
            err = self.fd_error
            try:
                for chunk in self.fd_chunks:
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(dup_fd, chunk[offset:])
            except BrokenPipeError:
                ok = False
                err = "Broken pipe"
            finally:
                os.close(dup_fd)
            for _p, sig_iface, sig_name, cb in list(self._callbacks):
                if (
                    sig_iface == RPM_INTERFACE
                    and sig_name == WRITE_TO_FD_FINISHED_SIGNAL
                ):
                    cb((ok, transfer_id, err))
            return (transfer_id,)

        if interface == HISTORY_INTERFACE and method == "list":
            self.calls.append(
                (path, interface, method, tuple(signatures), tuple(arguments))
            )
            if self.history_error is not None:
                raise self.history_error
            return (self.history_items,)

        return super().call(path, interface, method, signatures, arguments)


def test_list_packages_fd_streams_chunked_ndjson_records() -> None:
    import json

    records_payload = [
        {
            "name": f"pkg-{idx}",
            "version": "1.2.3",
            "release": "1.fc44",
            "arch": "x86_64",
            "repo_id": "fedora",
            "install_size": 1024 + idx,
            "summary": f"Package number {idx} " + ("x" * 200),
            "provides": [f"pkg-{idx}(x86-64) = 1.2.3-1.fc44"],
        }
        for idx in range(320)
    ]
    raw_bytes = "".join(json.dumps(item) + "\n" for item in records_payload).encode(
        "utf-8"
    )
    assert len(raw_bytes) > 70_000
    # Split mid-UTF-8/line to prove buffering across pipe reads.
    chunks = [raw_bytes[:30_001], raw_bytes[30_001:65_537], raw_bytes[65_537:]]

    transport = _FdAndHistoryTransport(fd_chunks=chunks, fd_ok=True, fd_error="")
    client = Dnf5DaemonClient(
        transport=transport,
        flatpak_detector=lambda: True,
        environment_detector=HostEnvironment.flatpak,
    )

    loaded = client.list_packages_fd(
        scope="available",
        patterns=("*pkg*",),
        package_attrs=("name", "version", "release", "arch", "repo_id"),
        latest_limit=1,
        load_system_repo=False,
        load_available_repos=True,
    )

    assert len(loaded) == 320
    assert loaded[0]["name"] == "pkg-0"
    assert loaded[-1]["name"] == "pkg-319"
    fd_calls = [
        c for c in transport.calls if c[1] == RPM_INTERFACE and c[2] == "list_fd"
    ]
    assert len(fd_calls) == 1
    assert fd_calls[0][3] == ("a{sv}", "h")
    assert fd_calls[0][4][0]["scope"] == "available"
    assert fd_calls[0][4][0]["patterns"] == ("*pkg*",)
    assert fd_calls[0][4][0]["latest-limit"] == 1


def test_list_packages_fd_surfaces_daemon_write_failure() -> None:
    transport = _FdAndHistoryTransport(
        fd_chunks=[b'{"name": "partial"}\n'],
        fd_ok=False,
        fd_error="pipe write failed on host",
    )
    client = Dnf5DaemonClient(
        transport=transport,
        flatpak_detector=lambda: False,
        environment_detector=HostEnvironment.traditional,
    )

    with pytest.raises(Dnf5DaemonCallError) as exc_info:
        client.list_packages_fd(scope="installed")

    assert "pipe write failed on host" in exc_info.value.detail


def test_list_packages_fd_respects_cancel_event() -> None:
    import threading

    cancel = threading.Event()
    cancel.set()
    transport = _FdAndHistoryTransport(
        fd_chunks=[b'{"name": "ignored"}\n'],
        fd_ok=True,
    )
    client = Dnf5DaemonClient(
        transport=transport,
        flatpak_detector=lambda: False,
        environment_detector=HostEnvironment.traditional,
    )

    loaded = client.list_packages_fd(scope="installed", cancel_event=cancel)
    assert loaded == ()
    assert transport.calls == []

    # Mid-stream cancellation also handles pipe EPIPE / write_to_fd_finished cleanly.
    mid_cancel = threading.Event()

    class _CancellingTransport(_FdAndHistoryTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == RPM_INTERFACE and method == "list_fd":
                mid_cancel.set()
            return super().call(path, interface, method, signatures, arguments)

    mid_transport = _CancellingTransport(
        fd_chunks=[b'{"name": "pkg-1"}\n', b'{"name": "pkg-2"}\n'],
        fd_ok=True,
    )
    mid_client = Dnf5DaemonClient(
        transport=mid_transport,
        flatpak_detector=lambda: False,
        environment_detector=HostEnvironment.traditional,
    )
    assert mid_client.list_packages_fd(scope="installed", cancel_event=mid_cancel) == ()


def test_list_history_parses_transactions_and_actions() -> None:
    transport = _FdAndHistoryTransport(
        history_items=[
            {
                "id": 10,
                "start": 1761642000,
                "end": 1761642015,
                "description": "dnf5 install htop",
                "status": "Ok",
                "installed": [{"name": "htop"}],
                "upgraded": [],
                "removed": [],
                "downgraded": [],
                "reinstalled": [],
            },
            {
                "id": 12,
                "start": 1761643000,
                "end": 1761643050,
                "description": "dnf5 upgrade",
                "status": "Ok",
                "installed": [{"name": "kernel-core"}],
                "upgraded": [{"name": "bash"}, {"name": "glibc"}],
                "removed": [],
                "downgraded": [],
                "reinstalled": [],
            },
        ]
    )
    client = Dnf5DaemonClient(
        transport=transport,
        flatpak_detector=lambda: True,
        environment_detector=HostEnvironment.flatpak,
    )

    entries = client.list_history(limit=25)

    assert len(entries) == 2
    assert isinstance(entries[0], DnfHistoryTransaction)
    assert entries[0].transaction_id == 12
    assert entries[0].command_line == "dnf5 upgrade"
    assert entries[0].altered_count == 3
    assert entries[0].actions == ("Install", "Upgrade")
    assert entries[1].transaction_id == 10
    assert entries[1].actions == ("Install",)
    history_calls = [
        c for c in transport.calls if c[1] == HISTORY_INTERFACE and c[2] == "list"
    ]
    assert len(history_calls) == 1
    assert history_calls[0][4][0]["limit"] == DbusInt64(25)


def test_unwrap_dbus_value_handles_qt_containers_bounds_depth_and_rejects_qdbusargument() -> (
    None
):
    from PyQt6.QtCore import QByteArray
    from PyQt6.QtDBus import (
        QDBusArgument,
        QDBusObjectPath,
        QDBusSignature,
        QDBusVariant,
    )

    from dendro.core.dnf5daemon import _unwrap_dbus_value
    from dendro.core.dnf5daemon_transport import _MAX_UNWRAP_DEPTH

    unwrapped = _unwrap_dbus_value(
        {
            QDBusObjectPath("/org/rpm/dnf/v0/session/1"): QDBusVariant(
                (
                    QDBusSignature("a(uas)"),
                    [QByteArray(b"\x00dnf\xff"), QByteArray(b"")],
                )
            )
        }
    )
    assert unwrapped == {
        "/org/rpm/dnf/v0/session/1": (
            "a(uas)",
            [b"\x00dnf\xff", b""],
        )
    }

    with pytest.raises(TypeError):
        _unwrap_dbus_value(QDBusArgument())

    nested: Any = "leaf"
    for _ in range(_MAX_UNWRAP_DEPTH + 2):
        nested = [nested]
    with pytest.raises(ValueError, match="maximum nesting depth"):
        _unwrap_dbus_value(nested)


def test_qt_dbus_transport_maps_unreadable_replies_and_ignores_bad_signal_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from PyQt6 import QtDBus

    from dendro.core.dnf5daemon import QtDbusTransport
    from dendro.core.dnf5daemon_transport import _QtDbusSignalSink

    monkeypatch.setattr(
        QtDBus.QDBusConnection,
        "connectToBus",
        lambda *_args, **_kwargs: pytest.fail(
            "QtDbusTransport must never open a second bus connection"
        ),
    )
    QtDBus.QDBusConnection.systemBus()

    class _BadReply:
        def __init__(self, args_fn: Any) -> None:
            self._args_fn = args_fn

        def type(self):
            return QtDBus.QDBusMessage.MessageType.ReplyMessage

        def arguments(self) -> list[Any]:
            return self._args_fn()

    class _FakeConnection:
        def __init__(self, args_fn: Any) -> None:
            self._args_fn = args_fn

        def isConnected(self) -> bool:
            return True

        def call(self, _message: Any, timeout: int = -1):
            del timeout
            return _BadReply(self._args_fn)

    def _raise_unsupported() -> list[Any]:
        raise TypeError("unsupported DBus argument type -1")

    transport = QtDbusTransport(connection=_FakeConnection(_raise_unsupported))  # type: ignore[arg-type]
    with pytest.raises(Dnf5DaemonCallError) as exc_info:
        transport.call("/org/rpm/dnf/v0/session/1", GOAL_INTERFACE, "resolve", (), ())
    assert "unsupported DBus argument type -1" in exc_info.value.detail

    opaque_transport = QtDbusTransport(
        connection=_FakeConnection(lambda: [QtDBus.QDBusArgument()])  # type: ignore[arg-type]
    )
    with pytest.raises(Dnf5DaemonCallError):
        opaque_transport.call(
            "/org/rpm/dnf/v0/session/1", GOAL_INTERFACE, "resolve", (), ()
        )

    sink = _QtDbusSignalSink()
    received: list[tuple[Any, ...]] = []
    sink.callbacks[("/org/rpm/dnf/v0/session/1", BASE_INTERFACE, "download_end")] = {
        1: received.append
    }
    bad_signal = SimpleNamespace(
        path=lambda: "/org/rpm/dnf/v0/session/1",
        interface=lambda: BASE_INTERFACE,
        member=lambda: "download_end",
        arguments=lambda: [QtDBus.QDBusArgument()],
    )
    sink.receive(bad_signal)  # type: ignore[arg-type]
    assert received == []


def test_list_available_updates_uses_list_fd_and_falls_back_on_unsupported() -> None:
    import json

    from dendro.core.dnf5daemon import Dnf5DaemonCallError

    records = [
        {
            "name": "mesa-dri-drivers",
            "epoch": "0",
            "version": "25.0",
            "release": "2.fc44",
            "arch": "x86_64",
            "repo_id": "updates",
            "summary": "Mesa DRI drivers",
            "install_size": 1200,
            "download_size": 400,
        }
    ]
    payload = "".join(json.dumps(r) + "\n" for r in records).encode("utf-8")
    fd_transport = _FdAndHistoryTransport(fd_chunks=[payload])
    fd_client = Dnf5DaemonClient(
        transport=fd_transport,
        flatpak_detector=lambda: False,
        environment_detector=HostEnvironment.traditional,
    )

    updates = fd_client.list_available_updates()

    assert len(updates) == 1
    assert updates[0].name_arch == "mesa-dri-drivers.x86_64"
    assert updates[0].version == "25.0"
    assert any(c[1:3] == (RPM_INTERFACE, "list_fd") for c in fd_transport.calls)
    assert not any(c[1:3] == (RPM_INTERFACE, "list") for c in fd_transport.calls)

    # Refresh also validates expire-cache in list_security_advisories.
    class _BadExpiryTransport(RecordingTransport):
        def call(self, path, interface, method, signatures, arguments):
            if interface == BASE_INTERFACE and method == "clean_with_options":
                self.calls.append(
                    (path, interface, method, tuple(signatures), tuple(arguments))
                )
                return (False, "metadata locked")
            return super().call(path, interface, method, signatures, arguments)

    advisory_client = Dnf5DaemonClient(
        transport=_BadExpiryTransport(),
        flatpak_detector=lambda: False,
        environment_detector=HostEnvironment.traditional,
    )
    with pytest.raises(Dnf5DaemonCallError, match="metadata locked"):
        advisory_client.list_security_advisories(refresh=True)


def test_list_history_defaults_and_resolve_allow_erasing() -> None:
    from dendro.core.dnf5daemon import Dnf5DaemonResolutionError

    transport = _FdAndHistoryTransport(history_items=[])
    client = Dnf5DaemonClient(
        transport=transport,
        flatpak_detector=lambda: False,
        environment_detector=HostEnvironment.traditional,
    )
    client.list_history()
    client.list_history(limit=50, reverse=True, include_packages=False)
    client.list_history(limit=0)

    history_calls = [
        c for c in transport.calls if c[1:3] == (HISTORY_INTERFACE, "list")
    ]
    assert len(history_calls) == 3
    assert history_calls[0][4][0]["limit"] == DbusInt64(200)
    assert history_calls[0][4][0]["reverse"] is False
    assert history_calls[0][4][0]["include_packages"] is True
    assert history_calls[0][4][0]["package_attrs"] == ("name",)
    assert history_calls[1][4][0]["limit"] == DbusInt64(50)
    assert history_calls[1][4][0]["reverse"] is True
    assert history_calls[1][4][0]["include_packages"] is False
    assert "package_attrs" not in history_calls[1][4][0]
    assert "limit" not in history_calls[2][4][0]

    with pytest.raises(TypeError):
        client.list_history(limit=True)

    transport.resolve_result = 2
    request = TransactionRequest.from_values(installs=["htop"])
    with pytest.raises(Dnf5DaemonResolutionError) as first_exc:
        client.resolve(request, set())
    assert first_exc.value.allow_erasing_attempted is False

    with pytest.raises(Dnf5DaemonResolutionError) as second_exc:
        client.resolve(request, set(), allow_erasing=True)
    assert second_exc.value.allow_erasing_attempted is True

    resolve_calls = [
        c for c in transport.calls if c[1:3] == (GOAL_INTERFACE, "resolve")
    ]
    assert resolve_calls[0][4][0]["allow_erasing"] is False
    assert resolve_calls[1][4][0]["allow_erasing"] is True


def test_dbus_int64_validation_and_qt_marshalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DbusInt64(200).value == 200
    assert DbusInt64(2**63 - 1).value == 2**63 - 1
    assert DbusInt64(-(2**63)).value == -(2**63)
    with pytest.raises(TypeError):
        DbusInt64(True)
    with pytest.raises(ValueError, match="signed 64-bit"):
        DbusInt64(2**63)
    with pytest.raises(ValueError, match="signed 64-bit"):
        DbusInt64(-(2**63) - 1)

    QtCore = pytest.importorskip("PyQt6.QtCore")
    QtDBus = pytest.importorskip("PyQt6.QtDBus")

    QtDBus.QDBusConnection.systemBus()

    captured: list[tuple[Any, int]] = []
    real_qdbus_argument = QtDBus.QDBusArgument

    def _recording_qdbus_argument(*args: Any, **kwargs: Any) -> Any:
        if len(args) == 2 and not kwargs:
            captured.append((args[0], args[1]))
        return real_qdbus_argument(*args, **kwargs)

    monkeypatch.setattr(
        "dendro.core.dnf5daemon_transport.QDBusArgument", _recording_qdbus_argument
    )

    int64_variant = _marshal_variant_value(DbusInt64(200))
    assert isinstance(int64_variant, real_qdbus_argument)
    assert captured == [(200, QtCore.QMetaType.Type.LongLong.value)]

    assert _marshal_variant_value(1) == 1
    assert type(_marshal_variant_value(1)) is int
    assert _marshal_variant_value(True) is True
    with pytest.raises(ValueError, match="signed 32-bit"):
        _marshal_variant_value(2**31)

    marshalled_map = _marshal_argument(
        "a{sv}",
        {
            "limit": DbusInt64(200),
            "latest-limit": 1,
            "reverse": False,
        },
    )
    assert isinstance(marshalled_map, real_qdbus_argument)
    assert captured == [
        (200, QtCore.QMetaType.Type.LongLong.value),
        (200, QtCore.QMetaType.Type.LongLong.value),
    ]


def test_qt_dbus_transport_opens_named_connections_and_closes_cleanly(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PyQt6 import QtDBus

    from dendro.core.dnf5daemon import QtDbusTransport

    opened: list[str] = []
    disconnected: list[str] = []

    class _OwnedConnection:
        def __init__(self, name: str) -> None:
            self.name = name

        def isConnected(self) -> bool:
            return True

        def connect(self, *_args) -> bool:
            return True

        def disconnect(self, *_args) -> bool:
            return True

    def fake_connect(_bus_type, name: str):
        opened.append(name)
        return _OwnedConnection(name)

    monkeypatch.setattr(QtDBus.QDBusConnection, "connectToBus", fake_connect)
    monkeypatch.setattr(
        QtDBus.QDBusConnection, "disconnectFromBus", disconnected.append
    )

    t1 = QtDbusTransport()
    t2 = QtDbusTransport()
    assert len(opened) == 2
    assert opened[0] != opened[1]

    token = t1.subscribe_background(
        "/org/rpm/dnf/v0/session/1",
        RPM_INTERFACE,
        WRITE_TO_FD_FINISHED_SIGNAL,
        lambda _args: None,
    )
    assert t1._resources.bg_thread is not None
    assert t1._resources.bg_thread.isRunning()
    t1.unsubscribe(token)

    client = Dnf5DaemonClient(transport=t1)
    client.close()
    t2.close()

    assert t1._resources.bg_thread is None
    assert set(disconnected) == set(opened)


def test_list_groups_queries_comps_interface_and_parses_members() -> None:
    transport = RecordingTransport()
    original_call = transport.call

    def fake_call(path, interface, method, signatures, arguments):
        if interface == GROUP_INTERFACE and method == "list":
            transport.calls.append((path, interface, method, signatures, arguments))
            return (
                [
                    {
                        "groupid": "fonts",
                        "name": "Fonts",
                        "installed": True,
                        "uservisible": True,
                        "repos": ["@System", "fedora"],
                        "packages": [
                            {
                                "name": "default-fonts-core-sans",
                                "type": 1,
                                "condition": "",
                            },
                            {
                                "name": "google-noto-sans-vf-fonts",
                                "type": 2,
                                "condition": "",
                            },
                        ],
                    }
                ],
            )
        return original_call(path, interface, method, signatures, arguments)

    transport.call = fake_call  # type: ignore[method-assign]
    client = Dnf5DaemonClient(
        transport=transport,
        flatpak_detector=lambda: True,
        environment_detector=HostEnvironment.flatpak,
    )

    groups = client.list_groups(
        scope="all",
        with_hidden=True,
        patterns=("fonts",),
    )

    assert groups == (
        DnfCompsGroup(
            groupid="fonts",
            name="Fonts",
            installed=True,
            uservisible=True,
            repos=("@System", "fedora"),
            packages=(
                DnfCompsPackage(name="default-fonts-core-sans", package_type=1),
                DnfCompsPackage(name="google-noto-sans-vf-fonts", package_type=2),
            ),
        ),
    )
    group_calls = [c for c in transport.calls if c[1] == GROUP_INTERFACE]
    assert len(group_calls) == 1
    assert group_calls[0][2] == "list"
    assert group_calls[0][3] == ("a{sv}",)
    assert group_calls[0][4][0]["scope"] == "all"
    assert group_calls[0][4][0]["with_hidden"] is True
    assert group_calls[0][4][0]["patterns"] == ("fonts",)

    with pytest.raises(Dnf5DaemonValidationError, match="Unsupported Comps"):
        client.list_groups(scope="bogus")
