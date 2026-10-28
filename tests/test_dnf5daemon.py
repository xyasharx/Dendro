"""Contract tests for the typed DNF5 daemon client.

These tests use a transport seam instead of a live host daemon. They verify the
public D-Bus API shape and lifecycle deterministically; Fedora integration still
needs to exercise the installed service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from core.dnf5daemon import (
    BASE_INTERFACE,
    GOAL_INTERFACE,
    REPOSITORY_INTERFACE,
    ROOT_PATH,
    RPM_INTERFACE,
    SESSION_MANAGER_INTERFACE,
    Dnf5DaemonCallError,
    Dnf5DaemonClient,
    Dnf5DaemonPlanError,
    Dnf5DaemonUnavailable,
    Dnf5DaemonValidationError,
    TransactionRequest,
)


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
            return (True,)
        if interface == GOAL_INTERFACE and method == "resolve":
            return (self.resolve_items, self.resolve_result)
        if interface == GOAL_INTERFACE and method == "get_transaction_problems_string":
            return (["one optional package is unavailable"],)
        if interface == GOAL_INTERFACE and method == "cancel":
            return (False, "RPM transaction already started")
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
    assert open_call[4] == ({"load_system_repo": True, "load_available_repos": True},)
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
    assert len(transport.unsubscribed) == 10
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
    assert transport.callbacks == {}
    client.discard(plan)
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
    assert transport.callbacks == {}
    client.discard(plan)


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
            "package_attrs": ("name", "version", "release", "arch", "repo_id"),
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

    monkeypatch.setattr("core.dnf5daemon.QtDbusTransport", MissingTransport)
    client = Dnf5DaemonClient()
    with pytest.raises(Dnf5DaemonUnavailable, match="No system bus"):
        client.ensure_available()


def test_repository_list_worker_emits_one_terminal_result(qtbot) -> None:
    from core.dnf5daemon import DnfRepository, RepositoryListWorker

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
    from core.dnf5daemon import RepositoryListWorker

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

    with caplog.at_level("WARNING", logger="core.dnf5daemon"):
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
