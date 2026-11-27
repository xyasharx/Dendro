"""Event-driven lifecycle tests for DnfTransactionController."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event

import pytest
from PyQt6.QtCore import QThreadPool

from dendro.core.dnf5daemon import (
    Dnf5DaemonError,
    Dnf5DaemonPlanError,
    TransactionExecutionResult,
    TransactionItem,
    TransactionPlan,
    TransactionRequest,
)
from dendro.core.operation_gate import HostOperationGate
from dendro.core.transaction_controller import (
    DnfTransactionController,
    TransactionState,
)


def make_plan(
    request: TransactionRequest, *, protected: bool = False
) -> TransactionPlan:
    item = TransactionItem(
        object_type="Package",
        action="Remove" if protected else "Install",
        reason="User",
        attributes={},
        package={
            "name": "systemd" if protected else "htop",
            "version": "1",
            "release": "1.fc44",
            "arch": "x86_64",
        },
    )
    return TransactionPlan(
        token="test-plan",
        session_path="/org/rpm/dnf/v0/session/test",
        request=request,
        items=(item,),
        resolver_result=0,
        warnings=(),
        critical_removals=("systemd",) if protected else (),
    )


@dataclass
class LifecycleClient:
    """Thread-safe enough fake implementation used only by controller tests."""

    resolver: Callable[[TransactionRequest], TransactionPlan]
    # ``None`` models a malformed daemon reply for a defensive-boundary test.
    execute_result: TransactionExecutionResult | None = field(
        default_factory=lambda: TransactionExecutionResult(False, True)
    )
    resolve_started: Event | None = None
    resolve_release: Event | None = None
    execute_started: Event | None = None
    execute_release: Event | None = None
    execute_error: Dnf5DaemonError | None = None
    stage_error: Dnf5DaemonError | None = None
    attach_error: Exception | None = None
    detach_error: Exception | None = None
    close_error: Dnf5DaemonError | None = None
    discard_error: Dnf5DaemonError | None = None
    cancellation_reply: tuple[bool, str] = (False, "RPM transaction already started")

    def __post_init__(self) -> None:
        self.calls: list[str] = []
        self.cancel_calls = 0

    def ensure_available(self) -> None:
        self.calls.append("ensure_available")

    def resolve(
        self, request: TransactionRequest, _protected: set[str]
    ) -> TransactionPlan:
        self.calls.append("resolve")
        if self.resolve_started is not None:
            self.resolve_started.set()
        if self.resolve_release is not None:
            assert self.resolve_release.wait(2), "test did not release fake resolver"
        return self.resolver(request)

    def attach_progress(
        self,
        _plan: TransactionPlan,
        _log: Callable[[str], None],
        _progress: Callable[[int], None],
    ) -> None:
        self.calls.append("attach_progress")
        if self.attach_error is not None:
            raise self.attach_error

    def detach_progress(self, _plan: TransactionPlan) -> None:
        self.calls.append("detach_progress")
        if self.detach_error is not None:
            raise self.detach_error

    def execute(self, _plan: TransactionPlan) -> TransactionExecutionResult | None:
        self.calls.append("execute")
        if self.execute_started is not None:
            self.execute_started.set()
        if self.execute_release is not None:
            assert self.execute_release.wait(2), "test did not release fake execution"
        if self.execute_error is not None:
            raise self.execute_error
        return self.execute_result

    def execute_offline(self, _plan: TransactionPlan) -> TransactionExecutionResult:
        self.calls.append("execute_offline")
        if self.stage_error is not None:
            raise self.stage_error
        return TransactionExecutionResult(False, True)

    def close_after_execution(self, _plan: TransactionPlan) -> None:
        self.calls.append("close_after_execution")
        if self.close_error is not None:
            raise self.close_error

    def discard(self, _plan: TransactionPlan) -> None:
        self.calls.append("discard")
        if self.discard_error is not None:
            raise self.discard_error

    def request_cancellation(self, _plan: TransactionPlan) -> tuple[bool, str]:
        self.calls.append("request_cancellation")
        self.cancel_calls += 1
        return self.cancellation_reply

    def set_repository_enabled(self, _repository_id: str, _enabled: bool) -> None:
        self.calls.append("set_repository_enabled")

    def clean_cache(self) -> None:
        self.calls.append("clean_cache")


class RejectingThreadPool:
    """A deterministic QThreadPool substitute for scheduler-failure coverage."""

    def start(self, _task: object) -> None:
        raise RuntimeError("worker scheduler is unavailable")


@pytest.fixture
def controller() -> tuple[DnfTransactionController, LifecycleClient, QThreadPool]:
    client = LifecycleClient(resolver=lambda request: make_plan(request))
    pool = QThreadPool()
    pool.setMaxThreadCount(2)
    instance = DnfTransactionController(
        client, lambda: {"systemd", "kernel"}, thread_pool=pool
    )
    yield instance, client, pool
    pool.waitForDone(2_000)


def test_resolve_review_execute_has_one_ordered_terminal_result(
    qtbot, controller
) -> None:
    instance, client, _pool = controller
    request = TransactionRequest.from_values(installs=["htop"])
    states: list[TransactionState] = []
    finished: list[tuple[bool, str]] = []
    instance.state_changed.connect(states.append)
    instance.transaction_finished.connect(
        lambda success, detail: finished.append((success, detail))
    )

    with qtbot.waitSignal(instance.plan_ready, timeout=1_000) as ready:
        instance.resolve(request)
    plan = ready.args[0]
    assert isinstance(plan, TransactionPlan)
    assert instance.state is TransactionState.READY
    assert states == [TransactionState.RESOLVING, TransactionState.READY]

    with qtbot.waitSignal(instance.transaction_finished, timeout=1_000):
        instance.execute_ready_plan()
    assert instance.state is TransactionState.IDLE
    assert finished == [(True, "")]
    assert client.calls == [
        "ensure_available",
        "resolve",
        "attach_progress",
        "execute",
        "close_after_execution",
    ]


def test_unexpected_progress_subscription_failure_restores_ready_state(
    qtbot, controller
) -> None:
    instance, client, _pool = controller
    request = TransactionRequest.from_values(installs=["htop"])
    client.attach_error = RuntimeError("transport callback registration failed")

    with qtbot.waitSignal(instance.plan_ready, timeout=1_000):
        instance.resolve(request)

    with pytest.raises(Dnf5DaemonError, match="transport callback registration failed"):
        instance.execute_ready_plan()

    assert instance.state is TransactionState.READY
    assert instance.has_ready_plan is True
    assert client.calls[-2:] == ["attach_progress", "detach_progress"]
    assert "execute" not in client.calls


def test_execute_scheduler_failure_restores_ready_plan_and_cleans_progress(
    qtbot,
) -> None:
    request = TransactionRequest.from_values(installs=["htop"])
    client = LifecycleClient(resolver=lambda current: make_plan(current))
    pool = QThreadPool()
    gate = HostOperationGate()
    instance = DnfTransactionController(
        client,
        lambda: set(),
        thread_pool=pool,
        host_operation_gate=gate,
    )

    with qtbot.waitSignal(instance.plan_ready, timeout=1_000):
        instance.resolve(request)
    instance._thread_pool = RejectingThreadPool()  # type: ignore[assignment]

    with pytest.raises(Dnf5DaemonError, match="worker scheduler is unavailable"):
        instance.execute_ready_plan()

    assert instance.state is TransactionState.READY
    assert instance.has_ready_plan
    assert gate.active is not None
    assert client.calls[-2:] == ["attach_progress", "detach_progress"]
    assert instance._operations == {}
    pool.waitForDone(2_000)


def test_scheduler_failure_releases_a_not_started_resolution_gate() -> None:
    request = TransactionRequest.from_values(installs=["htop"])
    client = LifecycleClient(resolver=lambda current: make_plan(current))
    gate = HostOperationGate()
    instance = DnfTransactionController(
        client,
        lambda: set(),
        thread_pool=RejectingThreadPool(),  # type: ignore[arg-type]
        host_operation_gate=gate,
    )

    with pytest.raises(Dnf5DaemonError, match="worker scheduler is unavailable"):
        instance.resolve(request)

    assert instance.state is TransactionState.IDLE
    assert gate.active is None
    assert client.calls == ["ensure_available"]


def test_discard_scheduler_failure_keeps_the_plan_available_for_retry() -> None:
    request = TransactionRequest.from_values(installs=["htop"])
    plan = make_plan(request)
    client = LifecycleClient(resolver=lambda current: plan)
    gate = HostOperationGate()
    instance = DnfTransactionController(
        client,
        lambda: set(),
        thread_pool=RejectingThreadPool(),  # type: ignore[arg-type]
        host_operation_gate=gate,
    )
    assert gate.claim("dnf5-daemon", "DNF5 daemon package operation") is None
    instance._plan = plan
    instance._state = TransactionState.READY
    failures: list[tuple[str, str]] = []
    instance.operation_failed.connect(
        lambda title, detail: failures.append((title, detail))
    )

    instance.discard_ready_plan()

    assert instance.state is TransactionState.READY
    assert instance.has_ready_plan
    assert gate.active is not None
    assert failures and failures[0][0] == "Could not start DNF5 operation"
    assert client.calls == []


def test_cancellation_scheduler_failure_keeps_the_active_transaction_owned() -> None:
    request = TransactionRequest.from_values(installs=["htop"])
    plan = make_plan(request)
    client = LifecycleClient(resolver=lambda current: plan)
    gate = HostOperationGate()
    instance = DnfTransactionController(
        client,
        lambda: set(),
        thread_pool=RejectingThreadPool(),  # type: ignore[arg-type]
        host_operation_gate=gate,
    )
    assert gate.claim("dnf5-daemon", "DNF5 daemon package operation") is None
    instance._plan = plan
    instance._state = TransactionState.EXECUTING
    log: list[str] = []
    instance.log_received.connect(log.append)

    instance.request_cancellation()

    assert instance.state is TransactionState.EXECUTING
    assert gate.active is not None
    assert client.calls == []
    assert any(
        "Could not queue the DNF cancellation request" in message for message in log
    )


def test_cancel_during_resolve_discards_plan_after_the_single_resolve_returns(
    qtbot,
) -> None:
    request = TransactionRequest.from_values(installs=["htop"])
    resolve_started = Event()
    resolve_release = Event()
    client = LifecycleClient(
        resolver=lambda current: make_plan(current, protected=True),
        resolve_started=resolve_started,
        resolve_release=resolve_release,
    )
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    instance = DnfTransactionController(client, lambda: {"systemd"}, thread_pool=pool)
    discarded: list[bool] = []
    instance.plan_discarded.connect(lambda: discarded.append(True))

    instance.resolve(request)
    qtbot.waitUntil(resolve_started.is_set, timeout=1_000)
    instance.request_cancellation()
    assert instance.state is TransactionState.CANCELLING
    resolve_release.set()
    qtbot.waitUntil(lambda: discarded == [True], timeout=1_000)

    assert instance.state is TransactionState.IDLE
    assert client.calls == ["ensure_available", "resolve", "discard"]
    pool.waitForDone(2_000)


def test_cancel_request_during_execution_is_single_and_returns_to_execution_if_refused(
    qtbot,
) -> None:
    request = TransactionRequest.from_values(installs=["htop"])
    execution_started = Event()
    execution_release = Event()
    client = LifecycleClient(
        resolver=lambda current: make_plan(current),
        execute_started=execution_started,
        execute_release=execution_release,
    )
    pool = QThreadPool()
    pool.setMaxThreadCount(2)
    instance = DnfTransactionController(client, lambda: set(), thread_pool=pool)

    with qtbot.waitSignal(instance.plan_ready, timeout=1_000):
        instance.resolve(request)
    instance.execute_ready_plan()
    qtbot.waitUntil(execution_started.is_set, timeout=1_000)
    instance.request_cancellation()
    instance.request_cancellation()
    qtbot.waitUntil(
        lambda: (
            client.cancel_calls == 1 and instance.state is TransactionState.EXECUTING
        ),
        timeout=1_000,
    )

    with qtbot.waitSignal(instance.transaction_finished, timeout=1_000):
        execution_release.set()
    assert instance.state is TransactionState.IDLE
    assert client.calls.count("request_cancellation") == 1
    pool.waitForDone(2_000)


def test_execute_failure_closes_session_once_and_reports_one_failed_terminal_result(
    qtbot, controller
) -> None:
    instance, client, _pool = controller
    request = TransactionRequest.from_values(installs=["htop"])
    client.execute_error = Dnf5DaemonError(
        "RPM transaction failed", "The daemon rejected the transaction"
    )
    terminal: list[tuple[bool, str]] = []
    errors: list[tuple[str, str]] = []
    instance.transaction_finished.connect(
        lambda success, detail: terminal.append((success, detail))
    )
    instance.operation_failed.connect(
        lambda title, detail: errors.append((title, detail))
    )

    with qtbot.waitSignal(instance.plan_ready, timeout=1_000):
        instance.resolve(request)
    instance.execute_ready_plan()
    qtbot.waitUntil(lambda: len(terminal) == 1 and len(errors) == 1, timeout=1_000)

    assert terminal == [(False, "The daemon rejected the transaction")]
    assert errors == [("RPM transaction failed", "The daemon rejected the transaction")]
    assert client.calls[-2:] == ["execute", "close_after_execution"]
    assert instance.state is TransactionState.IDLE


def test_invalid_execution_result_is_reported_without_assertion(
    qtbot, controller
) -> None:
    """A malformed daemon reply must remain a user-visible failure under -O."""
    instance, client, _pool = controller
    client.execute_result = None
    request = TransactionRequest.from_values(installs=["htop"])
    terminal: list[tuple[bool, str]] = []
    errors: list[tuple[str, str]] = []
    instance.transaction_finished.connect(
        lambda success, detail: terminal.append((success, detail))
    )
    instance.operation_failed.connect(
        lambda title, detail: errors.append((title, detail))
    )

    with qtbot.waitSignal(instance.plan_ready, timeout=1_000):
        instance.resolve(request)
    instance.execute_ready_plan()
    qtbot.waitUntil(lambda: len(terminal) == 1 and len(errors) == 1, timeout=1_000)

    assert terminal == [
        (False, "The daemon returned no transaction result after execution.")
    ]
    assert errors == [
        (
            "DNF5 daemon returned an invalid execution result",
            "The daemon returned no transaction result after execution.",
        )
    ]
    assert client.calls[-2:] == ["execute", "close_after_execution"]
    assert instance.state is TransactionState.IDLE


def test_maintenance_is_serialized_and_restores_idle(qtbot, controller) -> None:
    instance, client, _pool = controller
    outcomes: list[tuple[str, bool, str]] = []
    instance.maintenance_finished.connect(
        lambda action, success, detail: outcomes.append((action, success, detail))
    )

    instance.clean_cache()
    with pytest.raises(Dnf5DaemonPlanError, match="Cannot disable repository"):
        instance.set_repository_enabled("fedora", False)
    qtbot.waitUntil(lambda: outcomes == [("clean DNF cache", True, "")], timeout=1_000)

    assert client.calls == ["ensure_available", "clean_cache"]
    assert instance.state is TransactionState.IDLE


def test_shared_host_gate_blocks_daemon_resolution_while_repository_deletion_is_active() -> (
    None
):
    request = TransactionRequest.from_values(installs=["htop"])
    client = LifecycleClient(resolver=lambda current: make_plan(current))
    gate = HostOperationGate()
    assert gate.claim("repository-file-deletion", "repository-file deletion") is None
    instance = DnfTransactionController(
        client,
        lambda: set(),
        host_operation_gate=gate,
    )

    with pytest.raises(Dnf5DaemonPlanError, match="repository-file deletion"):
        instance.resolve(request)

    assert client.calls == []
    assert instance.state is TransactionState.IDLE
    assert gate.active is not None
    gate.release("repository-file-deletion")


def test_shared_host_gate_is_retained_through_review_and_released_after_discard(
    qtbot,
) -> None:
    request = TransactionRequest.from_values(installs=["htop"])
    client = LifecycleClient(resolver=lambda current: make_plan(current))
    gate = HostOperationGate()
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    instance = DnfTransactionController(
        client,
        lambda: set(),
        thread_pool=pool,
        host_operation_gate=gate,
    )

    with qtbot.waitSignal(instance.plan_ready, timeout=1_000):
        instance.resolve(request)
    assert instance.state is TransactionState.READY
    assert gate.active is not None
    assert gate.active.owner == "dnf5-daemon"
    conflict = gate.claim("repository-file-deletion", "repository-file deletion")
    assert conflict is not None
    assert conflict.owner == "dnf5-daemon"

    with qtbot.waitSignal(instance.plan_discarded, timeout=1_000):
        instance.discard_ready_plan()
    assert instance.state is TransactionState.IDLE
    assert gate.active is None
    pool.waitForDone(2_000)


def test_staging_uses_the_offline_path_and_never_runs_rpm_now(
    qtbot, controller
) -> None:
    """Staging must not take the live transaction path in the running session."""
    instance, client, _pool = controller
    with qtbot.waitSignal(instance.plan_ready, timeout=1_000):
        instance.resolve(TransactionRequest.from_values(installs=["htop"]))

    finished: list[tuple[bool, str]] = []
    instance.transaction_finished.connect(
        lambda success, detail: finished.append((success, detail))
    )
    with qtbot.waitSignal(instance.transaction_finished, timeout=1_000):
        instance.stage_ready_plan()

    assert client.calls == [
        "ensure_available",
        "resolve",
        "execute_offline",
        "close_after_execution",
    ]
    assert "execute" not in client.calls
    # No live RPM run means no progress subscription either.
    assert "attach_progress" not in client.calls
    assert instance.state is TransactionState.IDLE
    assert finished == [(True, "")]


def test_staging_failure_restores_the_ready_plan(qtbot, controller) -> None:
    instance, client, _pool = controller
    with qtbot.waitSignal(instance.plan_ready, timeout=1_000):
        instance.resolve(TransactionRequest.from_values(installs=["htop"]))
    client.stage_error = Dnf5DaemonError("Could not stage", "the daemon refused")
    logs: list[str] = []
    instance.log_received.connect(logs.append)

    with qtbot.waitSignal(instance.transaction_finished, timeout=1_000) as done:
        instance.stage_ready_plan()

    assert done.args[0] is False
    assert "the daemon refused" in done.args[1]
    assert any("Polkit authorization" in entry for entry in logs)
    assert any("Could not stage: the daemon refused" in entry for entry in logs)
    # The plan was closed, so the user must resolve again rather than retry a
    # session that no longer exists.
    assert instance.has_ready_plan is False


def test_staging_is_refused_without_a_resolved_plan(qtbot, controller) -> None:
    instance, _client, _pool = controller
    with pytest.raises(Dnf5DaemonPlanError, match="before staging it"):
        instance.stage_ready_plan()


def test_resolution_failure_emits_allow_erasing_prompt_when_connected(
    qtbot,
) -> None:
    from PyQt6.QtCore import QThreadPool

    from dendro.core.dnf5daemon import Dnf5DaemonResolutionError, TransactionPlan

    class _ResolvingClient:
        def __init__(self) -> None:
            self.erasing_flags: list[bool] = []

        def ensure_available(self) -> None:
            return None

        def resolve(
            self,
            request: TransactionRequest,
            _protected: set[str],
            *,
            allow_erasing: bool = False,
        ) -> TransactionPlan:
            self.erasing_flags.append(allow_erasing)
            if not allow_erasing:
                raise Dnf5DaemonResolutionError(
                    "Conflict", ["conflict A"], allow_erasing_attempted=False
                )
            return TransactionPlan(
                token="test-token",
                session_path="/org/rpm/dnf/v0/session/9",
                request=request,
                items=(),
                resolver_result=0,
                warnings=(),
                critical_removals=(),
            )

    client = _ResolvingClient()
    pool = QThreadPool()
    ctrl = DnfTransactionController(
        client,
        set,
        thread_pool=pool,  # type: ignore[arg-type]
    )
    try:
        res_errors: list[tuple[object, object]] = []
        op_errors: list[tuple[str, str]] = []
        ctrl.resolution_failed.connect(lambda req, err: res_errors.append((req, err)))
        ctrl.operation_failed.connect(lambda t, d: op_errors.append((t, d)))

        req = TransactionRequest.from_values(installs=["htop"])
        with qtbot.waitSignal(ctrl.resolution_failed, timeout=1_000):
            ctrl.resolve(req)

        assert len(res_errors) == 1
        assert op_errors == []
        assert ctrl.state is TransactionState.IDLE
        assert isinstance(res_errors[0][1], Dnf5DaemonResolutionError)
        assert res_errors[0][1].allow_erasing_attempted is False

        with qtbot.waitSignal(ctrl.plan_ready, timeout=1_000):
            ctrl.resolve(req, allow_erasing=True)

        assert client.erasing_flags == [False, True]
        assert ctrl.state is TransactionState.READY
    finally:
        pool.waitForDone(2_000)


class _OfflineLifecycleClient(LifecycleClient):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.offline_exec_options: list[tuple[bool, str]] = []
        self.finish_actions: list[str] = []
        self.offline_error: Dnf5DaemonError | None = None

    def execute_offline(
        self,
        _plan: TransactionPlan,
        *,
        download_only: bool = False,
        finish_action: str = "reboot",
    ) -> TransactionExecutionResult:
        self.calls.append("execute_offline")
        self.offline_exec_options.append((download_only, finish_action))
        if self.stage_error is not None:
            raise self.stage_error
        return TransactionExecutionResult(False, True)

    def schedule_offline_transaction(self) -> None:
        self.calls.append("schedule_offline_transaction")
        if self.offline_error is not None:
            raise self.offline_error

    def cancel_offline_transaction(self) -> None:
        self.calls.append("cancel_offline_transaction")
        if self.offline_error is not None:
            raise self.offline_error

    def clean_offline_transaction(self) -> None:
        self.calls.append("clean_offline_transaction")
        if self.offline_error is not None:
            raise self.offline_error

    def set_offline_finish_action(self, action: str) -> None:
        self.calls.append("set_offline_finish_action")
        self.finish_actions.append(action)
        if self.offline_error is not None:
            raise self.offline_error


def test_stage_ready_plan_forwards_download_only_and_finish_action(qtbot) -> None:
    client = _OfflineLifecycleClient(resolver=lambda req: make_plan(req))
    pool = QThreadPool()
    pool.setMaxThreadCount(2)
    gate = HostOperationGate()
    ctrl = DnfTransactionController(
        client,
        lambda: {"systemd"},
        thread_pool=pool,
        host_operation_gate=gate,
    )
    try:
        logs: list[str] = []
        ctrl.log_received.connect(logs.append)
        with qtbot.waitSignal(ctrl.plan_ready, timeout=1_000):
            ctrl.resolve(TransactionRequest.from_values(installs=["htop"]))

        with qtbot.waitSignal(ctrl.transaction_finished, timeout=1_000) as done:
            ctrl.stage_ready_plan(download_only=True, finish_action="poweroff")

        assert done.args == [True, ""]
        assert client.offline_exec_options == [(True, "poweroff")]
        assert "pre-staging" in "".join(logs)
        assert gate.active is None
    finally:
        pool.waitForDone(2_000)


def test_offline_maintenance_operations_emit_results_and_release_gate(
    qtbot,
) -> None:
    from dendro.core.dnf5daemon_types import Dnf5DaemonValidationError

    client = _OfflineLifecycleClient(resolver=lambda req: make_plan(req))
    pool = QThreadPool()
    pool.setMaxThreadCount(2)
    gate = HostOperationGate()
    ctrl = DnfTransactionController(
        client,
        lambda: {"systemd"},
        thread_pool=pool,
        host_operation_gate=gate,
    )
    try:
        for trigger, expected_action, expected_call in (
            (
                ctrl.schedule_offline_transaction,
                "schedule offline update",
                "schedule_offline_transaction",
            ),
            (
                ctrl.cancel_offline_transaction,
                "cancel pending boot update",
                "cancel_offline_transaction",
            ),
            (
                ctrl.clean_offline_transaction,
                "clean staged update cache",
                "clean_offline_transaction",
            ),
            (
                lambda: ctrl.set_offline_finish_action("poweroff"),
                "set offline finish action",
                "set_offline_finish_action",
            ),
        ):
            with qtbot.waitSignal(ctrl.maintenance_finished, timeout=1_000) as sig:
                trigger()
            assert sig.args == [expected_action, True, ""]
            assert client.calls[-1] == expected_call
            assert gate.active is None

        assert client.finish_actions == ["poweroff"]

        with pytest.raises(Dnf5DaemonValidationError):
            ctrl.set_offline_finish_action("halt")
        assert gate.active is None

        client.offline_error = Dnf5DaemonError("Blocked", "Busy")
        with qtbot.waitSignal(ctrl.maintenance_finished, timeout=1_000) as sig:
            ctrl.schedule_offline_transaction()
        assert sig.args == ["schedule offline update", False, "Busy"]
        assert gate.active is None
    finally:
        pool.waitForDone(2_000)
