"""Serialized Qt lifecycle controller for daemon-backed package operations.

Only this controller crosses from the UI into ``Dnf5DaemonClient``.  It makes
resolve/preview/execute a state machine instead of an implicit queue of shell
commands, retains workers until a terminal signal, and never starts a second
mutating operation while one plan is active.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal, pyqtSlot

from dendro.core.dnf5daemon import (
    Dnf5DaemonClient,
    Dnf5DaemonError,
    Dnf5DaemonPlanError,
    Dnf5DaemonResolutionError,
    TransactionExecutionResult,
    TransactionPlan,
    TransactionRequest,
    _normalise_offline_finish_action,
)
from dendro.core.operation_gate import HostOperationGate
from dendro.core.worker_lifecycle import ShutdownSafeRunnable


class TransactionState(Enum):
    IDLE = auto()
    RESOLVING = auto()
    READY = auto()
    EXECUTING = auto()
    CANCELLING = auto()
    MAINTENANCE = auto()


class _TaskSignals(QObject):
    succeeded = pyqtSignal(int, object)
    failed = pyqtSignal(int, object)


class _DaemonTask(ShutdownSafeRunnable):
    """One non-UI daemon call with an explicit terminal success/failure signal."""

    def __init__(self, operation_id: int, operation: Callable[[], object]) -> None:
        super().__init__()
        self.operation_id = operation_id
        self.operation = operation
        self.signals = _TaskSignals()
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self.operation()
        except Dnf5DaemonError as exc:
            self.signals.failed.emit(self.operation_id, exc)
        except (
            Exception
        ) as exc:  # Defensive boundary: no worker exception may disappear.
            self.signals.failed.emit(
                self.operation_id,
                Dnf5DaemonError(
                    "Unexpected transaction error", f"{type(exc).__name__}: {exc}"
                ),
            )
        else:
            self.signals.succeeded.emit(self.operation_id, result)


@dataclass(frozen=True, slots=True)
class _Operation:
    kind: str
    label: str
    plan: TransactionPlan | None = None
    request: TransactionRequest | None = None
    allow_erasing: bool = False
    download_only: bool = False


@dataclass(frozen=True, slots=True)
class _ExecutionOutcome:
    result: TransactionExecutionResult
    cleanup_error: str = ""


class DnfTransactionController(QObject):
    """Owns the serialized lifecycle of all DNF5 daemon mutations."""

    state_changed = pyqtSignal(object)
    plan_ready = pyqtSignal(object)
    plan_discarded = pyqtSignal()
    log_received = pyqtSignal(str)
    progress_changed = pyqtSignal(int)
    transaction_finished = pyqtSignal(bool, str)
    maintenance_finished = pyqtSignal(str, bool, str)
    operation_failed = pyqtSignal(str, str)
    resolution_failed = pyqtSignal(object, object)

    def __init__(
        self,
        client: Dnf5DaemonClient,
        protected_packages_provider: Callable[[], set[str]],
        *,
        thread_pool: QThreadPool | None = None,
        host_operation_gate: HostOperationGate | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._protected_packages_provider = protected_packages_provider
        self._thread_pool = thread_pool or QThreadPool.globalInstance()
        self._host_operation_gate = host_operation_gate
        self._host_operation_owner = "dnf5-daemon"
        self._state = TransactionState.IDLE
        self._plan: TransactionPlan | None = None
        self._cancel_requested_while_resolving = False
        self._next_operation_id = 0
        self._operations: dict[int, _Operation] = {}
        self._tasks: dict[int, _DaemonTask] = {}

    @property
    def state(self) -> TransactionState:
        return self._state

    @property
    def is_busy(self) -> bool:
        return self._state in {
            TransactionState.RESOLVING,
            TransactionState.EXECUTING,
            TransactionState.CANCELLING,
            TransactionState.MAINTENANCE,
        }

    @property
    def has_ready_plan(self) -> bool:
        return self._state == TransactionState.READY and self._plan is not None

    def resolve(
        self, request: TransactionRequest, *, allow_erasing: bool = False
    ) -> None:
        self._require_idle("resolve a package transaction")
        self._claim_host_operation("resolve a package transaction")
        try:
            self._client.ensure_available()
        except Exception:
            self._release_host_operation()
            raise
        effective_allow_erasing = bool(allow_erasing or request.allow_erasing)
        self._cancel_requested_while_resolving = False
        self._set_state(TransactionState.RESOLVING)
        if allow_erasing:
            self.log_received.emit(
                "Retrying transaction resolution with package erasing enabled…\n"
            )
        elif effective_allow_erasing:
            self.log_received.emit(
                "Resolving package changes with package erasing enabled…\n"
            )
        else:
            self.log_received.emit(
                "Resolving package changes through Fedora's DNF5 daemon…\n"
            )
        try:
            self._start(
                _Operation(
                    "resolve",
                    "resolve package transaction",
                    request=request,
                    allow_erasing=effective_allow_erasing,
                ),
                (
                    lambda: self._client.resolve(
                        request,
                        self._protected_packages_provider(),
                        allow_erasing=True,
                    )
                )
                if effective_allow_erasing
                else (
                    lambda: self._client.resolve(
                        request, self._protected_packages_provider()
                    )
                ),
            )
        except Dnf5DaemonError:
            self._set_state(TransactionState.IDLE)
            raise

    def execute_ready_plan(self) -> None:
        if self._state != TransactionState.READY or self._plan is None:
            raise Dnf5DaemonPlanError(
                "No approved transaction plan",
                "Resolve and approve a package transaction before executing it.",
            )
        plan = self._plan
        if plan.request.is_system_upgrade:
            raise Dnf5DaemonPlanError(
                "System release upgrades must be staged offline",
                "Fedora major release upgrades cannot run inside the live desktop session. "
                "Stage the transaction offline and reboot to apply it safely.",
            )
        self._set_state(TransactionState.EXECUTING)
        self.progress_changed.emit(0)
        self.log_received.emit(
            "Requesting Polkit authorization from the DNF5 daemon…\n"
        )
        try:
            self._client.attach_progress(
                plan, self.log_received.emit, self.progress_changed.emit
            )
        except Exception as exc:
            # Subscription setup happens before the worker starts.  It must
            # never strand the controller in EXECUTING when an unexpected Qt
            # D-Bus or transport exception escapes the client boundary.
            cleanup_detail = ""
            try:
                self._client.detach_progress(plan)
            except Exception as cleanup_error:
                cleanup_detail = (
                    " Progress-subscription cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            self._set_state(TransactionState.READY)
            if isinstance(exc, Dnf5DaemonError):
                if cleanup_detail:
                    raise Dnf5DaemonError(
                        exc.title,
                        exc.detail + cleanup_detail,
                        dbus_name=exc.dbus_name,
                    ) from exc
                raise
            raise Dnf5DaemonError(
                "Could not prepare DNF5 transaction progress",
                f"{type(exc).__name__}: {exc}.{cleanup_detail}",
            ) from exc
        try:
            self._start(
                _Operation("execute", "execute package transaction", plan),
                lambda: self._execute_and_close(plan),
            )
        except Dnf5DaemonError as error:
            cleanup_detail = ""
            try:
                self._client.detach_progress(plan)
            except Exception as cleanup_error:
                cleanup_detail = (
                    " Progress-subscription cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            self._set_state(TransactionState.READY)
            if cleanup_detail:
                raise Dnf5DaemonError(
                    error.title,
                    error.detail + cleanup_detail,
                    dbus_name=error.dbus_name,
                ) from error
            raise

    def discard_ready_plan(self) -> None:
        if self._state != TransactionState.READY or self._plan is None:
            return
        plan = self._plan
        self._set_state(TransactionState.CANCELLING)
        self.log_received.emit("Discarding the resolved transaction plan…\n")
        try:
            self._start(
                _Operation("discard", "discard transaction plan", plan),
                lambda: self._client.discard(plan),
            )
        except Dnf5DaemonError as error:
            self._set_state(TransactionState.READY)
            self.log_received.emit(
                "Could not queue plan cleanup; the resolved plan remains available for retry.\n"
            )
            self.operation_failed.emit(error.title, error.detail)

    def request_cancellation(self) -> None:
        """Honor the daemon's cancellation boundary; never kill an RPM transaction."""
        if self._state == TransactionState.RESOLVING:
            self._cancel_requested_while_resolving = True
            self._set_state(TransactionState.CANCELLING)
            self.log_received.emit(
                "Resolve cancellation requested; Dendro will close the plan as soon as DNF returns it.\n"
            )
            return
        if self._state == TransactionState.READY:
            self.discard_ready_plan()
            return
        if self._state == TransactionState.CANCELLING:
            return
        if self._state != TransactionState.EXECUTING or self._plan is None:
            return
        if any(operation.kind == "cancel" for operation in self._operations.values()):
            return
        plan = self._plan
        self._set_state(TransactionState.CANCELLING)
        self.log_received.emit("Requesting DNF5 download cancellation…\n")
        try:
            self._start(
                _Operation("cancel", "cancel package transaction", plan),
                lambda: self._client.request_cancellation(plan),
            )
        except Dnf5DaemonError as error:
            # The transaction worker is still running. Do not release its host
            # operation reservation or present the still-active transaction as done.
            self._set_state(TransactionState.EXECUTING)
            self.log_received.emit(
                f"Could not queue the DNF cancellation request: {error.detail}\n"
            )

    def _start_maintenance(
        self,
        *,
        guard_action: str,
        operation_label: str,
        log_message: str,
        callable_operation: Callable[[], object],
    ) -> None:
        self._require_idle(guard_action)
        self._claim_host_operation(guard_action)
        try:
            self._client.ensure_available()
        except Exception:
            self._release_host_operation()
            raise
        self._set_state(TransactionState.MAINTENANCE)
        self.log_received.emit(log_message)
        try:
            self._start(_Operation("maintenance", operation_label), callable_operation)
        except Dnf5DaemonError:
            self._set_state(TransactionState.IDLE)
            raise

    def set_repository_enabled(self, repository_id: str, enabled: bool) -> None:
        action = "enable repository" if enabled else "disable repository"
        self._start_maintenance(
            guard_action=action,
            operation_label=action,
            log_message=(
                f"Requesting DNF5 daemon authorization to {action}: {repository_id}\n"
            ),
            callable_operation=lambda: self._client.set_repository_enabled(
                repository_id, enabled
            ),
        )

    def clean_cache(self) -> None:
        self._start_maintenance(
            guard_action="clean the DNF cache",
            operation_label="clean DNF cache",
            log_message="Requesting DNF5 daemon authorization to clean the package cache…\n",
            callable_operation=self._client.clean_cache,
        )

    def schedule_offline_transaction(self) -> None:
        """Schedule a pre-staged (``download-complete``) update for next boot."""
        self._start_maintenance(
            guard_action="schedule the pre-staged offline update",
            operation_label="schedule offline update",
            log_message=(
                "Requesting Polkit authorization to schedule the pre-staged update "
                "for the next boot…\n"
            ),
            callable_operation=self._client.schedule_offline_transaction,
        )

    def cancel_offline_transaction(self) -> None:
        """Unlink ``/system-update`` while keeping downloaded offline packages."""
        self._start_maintenance(
            guard_action="cancel the pending boot update",
            operation_label="cancel pending boot update",
            log_message=(
                "Requesting Polkit authorization to cancel the pending boot update…\n"
            ),
            callable_operation=self._client.cancel_offline_transaction,
        )

    def clean_offline_transaction(self) -> None:
        """Cancel any boot link and purge staged offline payloads and state."""
        self._start_maintenance(
            guard_action="clean the staged offline update cache",
            operation_label="clean staged update cache",
            log_message=(
                "Requesting Polkit authorization to clean the staged offline update cache…\n"
            ),
            callable_operation=self._client.clean_offline_transaction,
        )

    def set_offline_finish_action(self, action: str) -> None:
        """Configure whether the staged offline boot update finishes with reboot or poweroff."""
        normalized = _normalise_offline_finish_action(action)
        self._start_maintenance(
            guard_action=f"set offline finish action to '{normalized}'",
            operation_label="set offline finish action",
            log_message=(
                f"Requesting Polkit authorization to set offline finish action to '{normalized}'…\n"
            ),
            callable_operation=lambda: self._client.set_offline_finish_action(
                normalized
            ),
        )

    def stage_ready_plan(
        self,
        *,
        download_only: bool = False,
        finish_action: str = "reboot",
    ) -> None:
        """Download the approved plan for next-boot application or pre-staging.

        Shares the serialized host-operation gate and the daemon session with
        the normal path; only the final ``do_transaction`` options differ, so
        the staged package set is exactly the plan the user reviewed.
        """
        normalized_finish = _normalise_offline_finish_action(finish_action)
        if self._state != TransactionState.READY or self._plan is None:
            raise Dnf5DaemonPlanError(
                "No approved transaction plan",
                "Resolve and approve a package transaction before staging it.",
            )
        plan = self._plan
        self._set_state(TransactionState.EXECUTING)
        if download_only:
            self.log_received.emit(
                "Requesting Polkit authorization to download packages for offline pre-staging…\n"
            )
        else:
            self.log_received.emit(
                "Requesting Polkit authorization to stage this transaction for the next boot…\n"
            )
        try:
            self._start(
                _Operation(
                    "stage",
                    "pre-stage package transaction"
                    if download_only
                    else "stage package transaction",
                    plan,
                    download_only=bool(download_only),
                ),
                lambda: self._stage_and_close(
                    plan,
                    download_only=bool(download_only),
                    finish_action=normalized_finish,
                ),
            )
        except Dnf5DaemonError:
            self._set_state(TransactionState.READY)
            raise

    def _stage_and_close(
        self,
        plan: TransactionPlan,
        *,
        download_only: bool = False,
        finish_action: str = "reboot",
    ) -> _ExecutionOutcome:
        staging_error: Dnf5DaemonError | None = None
        try:
            if not download_only and finish_action == "reboot":
                result = self._client.execute_offline(plan)
            else:
                result = self._client.execute_offline(
                    plan,
                    download_only=download_only,
                    finish_action=finish_action,
                )
        except Dnf5DaemonError as exc:
            staging_error = exc
            result = None
        try:
            self._client.close_after_execution(plan)
        except Dnf5DaemonError as cleanup_error:
            if staging_error is not None:
                raise Dnf5DaemonError(
                    "Staging failed and daemon cleanup failed",
                    f"{staging_error.detail}\n\nSession cleanup also failed: {cleanup_error.detail}",
                ) from cleanup_error
            return _ExecutionOutcome(result, cleanup_error.detail)
        if staging_error is not None:
            raise staging_error
        return _ExecutionOutcome(result)

    def _execute_and_close(self, plan: TransactionPlan) -> _ExecutionOutcome:
        execution_error: Dnf5DaemonError | None = None
        try:
            result = self._client.execute(plan)
        except Dnf5DaemonError as exc:
            execution_error = exc
            result = None

        try:
            self._client.close_after_execution(plan)
        except Dnf5DaemonError as cleanup_error:
            if execution_error is not None:
                raise Dnf5DaemonError(
                    "Transaction failed and daemon cleanup failed",
                    f"{execution_error.detail}\n\nSession cleanup also failed: {cleanup_error.detail}",
                ) from cleanup_error
            if result is None:
                raise Dnf5DaemonError(
                    "DNF5 daemon returned an invalid execution result",
                    "The daemon returned no transaction result after execution. "
                    f"Session cleanup also failed: {cleanup_error.detail}",
                ) from cleanup_error
            return _ExecutionOutcome(result, cleanup_error.detail)

        if execution_error is not None:
            raise execution_error
        if result is None:
            raise Dnf5DaemonError(
                "DNF5 daemon returned an invalid execution result",
                "The daemon returned no transaction result after execution.",
            )
        return _ExecutionOutcome(result)

    def _require_idle(self, action: str) -> None:
        if self._state != TransactionState.IDLE:
            raise Dnf5DaemonPlanError(
                "Another package operation is active",
                f"Cannot {action} while Dendro is {self._state.name.lower()}.",
            )

    def _claim_host_operation(self, action: str) -> None:
        """Reserve the shared host-operation boundary for this lifecycle."""
        if self._host_operation_gate is None:
            return
        conflict = self._host_operation_gate.claim(
            self._host_operation_owner,
            "DNF5 daemon package operation",
        )
        if conflict is not None:
            raise Dnf5DaemonPlanError(
                "Another host package operation is active",
                f"Cannot {action} while {conflict.label} is active.",
            )

    def _release_host_operation(self) -> None:
        if self._host_operation_gate is not None:
            self._host_operation_gate.release(self._host_operation_owner)

    def _start(
        self, operation: _Operation, callable_operation: Callable[[], object]
    ) -> None:
        self._next_operation_id += 1
        operation_id = self._next_operation_id
        task = _DaemonTask(operation_id, callable_operation)
        self._operations[operation_id] = operation
        self._tasks[operation_id] = task
        task.signals.succeeded.connect(self._on_task_succeeded)
        task.signals.failed.connect(self._on_task_failed)
        try:
            self._thread_pool.start(task)
        except Exception as exc:
            self._operations.pop(operation_id, None)
            self._tasks.pop(operation_id, None)
            raise Dnf5DaemonError(
                "Could not start DNF5 operation",
                f"{operation.label}: {type(exc).__name__}: {exc}",
            ) from exc

    @pyqtSlot(int, object)
    def _on_task_succeeded(self, operation_id: int, result: object) -> None:
        operation = self._operations.pop(operation_id, None)
        self._tasks.pop(operation_id, None)
        if operation is None:
            return

        if operation.kind == "resolve":
            if not isinstance(result, TransactionPlan):
                self._fail_internal("Resolve returned an invalid result")
                return
            self._plan = result
            if self._cancel_requested_while_resolving:
                self._cancel_requested_while_resolving = False
                self.log_received.emit(
                    "DNF finished resolving; discarding the canceled plan.\n"
                )
                try:
                    self._start(
                        _Operation(
                            "discard", "discard canceled transaction plan", result
                        ),
                        lambda: self._client.discard(result),
                    )
                except Dnf5DaemonError as error:
                    # Keep the resolved plan visible and owned rather than
                    # losing a daemon session after a local scheduler failure.
                    self._set_state(TransactionState.READY)
                    self.log_received.emit(
                        "Could not queue cancellation cleanup; review or discard the resolved plan manually: "
                        f"{error.detail}\n"
                    )
                    self.plan_ready.emit(result)
                return
            self._set_state(TransactionState.READY)
            self.log_received.emit(
                "Transaction plan resolved. Review it before authenticating.\n"
            )
            self.plan_ready.emit(result)
            return

        if operation.kind == "execute":
            if not isinstance(result, _ExecutionOutcome):
                self._fail_internal("Execution returned an invalid result")
                return
            self._plan = None
            self._set_state(TransactionState.IDLE)
            if result.cleanup_error:
                self.log_received.emit(
                    f"Warning: DNF session cleanup failed after completion: {result.cleanup_error}\n"
                )
            self.transaction_finished.emit(True, result.cleanup_error)
            return

        if operation.kind == "stage":
            if not isinstance(result, _ExecutionOutcome):
                self._fail_internal("Staging returned an invalid result")
                return
            self._plan = None
            self._set_state(TransactionState.IDLE)
            if result.cleanup_error:
                self.log_received.emit(
                    "Warning: DNF session cleanup failed after staging: "
                    f"{result.cleanup_error}\n"
                )
            if operation.download_only:
                self.log_received.emit(
                    "Packages downloaded and verified for offline pre-staging "
                    "(not yet scheduled for next boot).\n"
                )
            else:
                self.log_received.emit(
                    "Transaction staged. It will be applied during the next boot.\n"
                )
            self.transaction_finished.emit(True, result.cleanup_error)
            return

        if operation.kind == "cancel":
            accepted, detail = (
                result
                if isinstance(result, tuple) and len(result) == 2
                else (False, "Invalid cancellation response")
            )
            if accepted:
                self.log_received.emit(
                    "DNF accepted cancellation; waiting for the transaction's terminal result.\n"
                )
            else:
                self.log_received.emit(
                    "DNF cannot cancel at its current phase"
                    + (f": {detail}" if detail else ".")
                    + "\n"
                )
                if self._state == TransactionState.CANCELLING:
                    self._set_state(TransactionState.EXECUTING)
            return

        if operation.kind == "discard":
            if self._plan is operation.plan:
                self._plan = None
            self._set_state(TransactionState.IDLE)
            self.plan_discarded.emit()
            self.log_received.emit("Resolved transaction plan discarded.\n")
            return

        if operation.kind == "maintenance":
            self._set_state(TransactionState.IDLE)
            self.maintenance_finished.emit(operation.label, True, "")
            self.log_received.emit(f"DNF5 daemon completed: {operation.label}.\n")
            return

        self._fail_internal(f"Unknown operation kind: {operation.kind}")

    @pyqtSlot(int, object)
    def _on_task_failed(self, operation_id: int, error: object) -> None:
        operation = self._operations.pop(operation_id, None)
        self._tasks.pop(operation_id, None)
        if operation is None:
            return
        if not isinstance(error, Dnf5DaemonError):
            error = Dnf5DaemonError("Unexpected transaction error", str(error))

        if operation.kind == "resolve" and self._cancel_requested_while_resolving:
            self._cancel_requested_while_resolving = False
            self._set_state(TransactionState.IDLE)
            self.plan_discarded.emit()
            return

        if operation.kind == "cancel":
            self.log_received.emit(f"DNF cancellation request failed: {error.detail}\n")
            if self._state == TransactionState.CANCELLING:
                self._set_state(TransactionState.EXECUTING)
            return

        if operation.kind == "discard":
            # Do not permit a failed cleanup to leave a plan executable in the UI.
            self._plan = None
            self._set_state(TransactionState.IDLE)
            self.plan_discarded.emit()
            self.operation_failed.emit(error.title, error.detail)
            return

        if operation.kind == "execute":
            self._plan = None
            self._set_state(TransactionState.IDLE)
            self.transaction_finished.emit(False, error.detail)
            self.operation_failed.emit(error.title, error.detail)
            return

        if operation.kind == "stage":
            self._plan = None
            self._set_state(TransactionState.IDLE)
            self.log_received.emit(f"{error.title}: {error.detail}\n")
            self.transaction_finished.emit(False, error.detail)
            self.operation_failed.emit(error.title, error.detail)
            return

        if operation.kind == "maintenance":
            self._set_state(TransactionState.IDLE)
            self.maintenance_finished.emit(operation.label, False, error.detail)
            self.operation_failed.emit(error.title, error.detail)
            return

        if (
            operation.kind == "resolve"
            and isinstance(error, Dnf5DaemonResolutionError)
            and self.receivers(self.resolution_failed) > 0
        ):
            self._set_state(TransactionState.IDLE)
            self.resolution_failed.emit(operation.request, error)
            return

        self._set_state(TransactionState.IDLE)
        self.operation_failed.emit(error.title, error.detail)

    def _fail_internal(self, detail: str) -> None:
        self._plan = None
        self._set_state(TransactionState.IDLE)
        self.operation_failed.emit("Internal transaction lifecycle error", detail)

    def _set_state(self, state: TransactionState) -> None:
        if state == self._state:
            return
        self._state = state
        if state == TransactionState.IDLE:
            self._release_host_operation()
        self.state_changed.emit(state)
