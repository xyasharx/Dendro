# dendro/ui/main_window_transactions.py
"""MainWindow mixin handling DNF5 transactions, repositories, history, advisories, and Flatpaks."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from PyQt6.QtWidgets import QDialog, QMessageBox

from dendro.core.backend import (
    AppStreamCatalog,
    DnfHistoryWorker,
    FedoraCompsCatalog,
    HistoryEntry,
    LocalRpmInspectionResult,
    LocalRpmInspectionWorker,
    SecurityAdvisoryWorker,
    SQLiteCapabilityCache,
    SystemDesktopIndex,
    SystemRebootWorker,
    is_running_in_flatpak,
)
from dendro.core.dnf5daemon import (
    Dnf5DaemonError,
    Dnf5DaemonResolutionError,
    OfflineTransactionStatus,
    RepositoryKeyPrompt,
    RepositoryListWorker,
    SecurityAdvisory,
    TransactionPlan,
    TransactionRequest,
)
from dendro.core.i18n import format_number, tr, tr_plural
from dendro.ui.advisory_dialog import SecurityAdvisoryDialog
from dendro.ui.flatpak_dialog import FlatpakInventoryDialog, start_flatpak_refresh
from dendro.ui.history_dialog import DnfHistoryDialog
from dendro.ui.host_dialogs import LocalRpmInstallDialog
from dendro.ui.key_import_dialog import RepositoryKeyImportDialog
from dendro.ui.repo_dialog import RepoManagerDialog
from dendro.ui.transaction_plan_dialog import TransactionPlanReviewDialog
from dendro.ui.window_presentation import (
    window_host_environment as _window_host_environment,
)

LOGGER = logging.getLogger(__name__)

_OFFLINE_MAINTENANCE_ACTIONS = frozenset(
    {
        "schedule offline update",
        "cancel pending boot update",
        "clean staged update cache",
        "set offline finish action",
    }
)


def _mw_hook(name: str, default: Any) -> Any:
    """Resolve optional monkeypatches applied to ``ui.main_window`` in tests."""
    module = sys.modules.get("dendro.ui.main_window")
    return getattr(module, name, default) if module is not None else default


class MainWindowTransactionsMixin:
    """DNF5 transactions, repository management, history, advisories, and local RPMs."""

    def _open_transaction_drawer(self):
        """Prepares and opens the execution drawer with consistent geometry."""
        self.transaction_drawer.start_execution_mode()
        self.transaction_drawer.show()
        self.workspace_splitter.setSizes([450, 320])

    def _ask_about_repository_key(self, prompt: RepositoryKeyPrompt) -> bool:
        """Ask whether this repository's signing key may be trusted.

        Runs on the GUI thread from the D-Bus signal handler while the resolve
        waits on a worker. Declining is the safe answer, so anything unexpected
        declines instead of trusting a key the user never saw.
        """
        try:
            key_dialog_cls = _mw_hook(
                "RepositoryKeyImportDialog", RepositoryKeyImportDialog
            )
            dialog = key_dialog_cls(prompt, self)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            if accepted:
                dialog.apply_remembered_choice()
            return accepted
        except Exception:
            LOGGER.exception("Could not ask about a repository signing key.")
            return False

    def open_local_rpm(self, file_path: str):
        """Inspect a standalone RPM in a worker before opening its install prompt."""
        flatpak_fn = _mw_hook("is_running_in_flatpak", is_running_in_flatpak)
        if flatpak_fn():
            QMessageBox.information(
                self,
                tr("Local RPM Installation Is Unavailable"),
                tr(
                    "Flatpak Dendro does not expose the host filesystem or run host commands. "
                    "Open this RPM with an RPM-host Dendro installation instead."
                ),
            )
            return

        self._local_rpm_inspection_request_serial += 1
        request_serial = self._local_rpm_inspection_request_serial
        if self.current_local_rpm_inspection_worker is not None:
            self.current_local_rpm_inspection_worker.cancel()
            self.current_local_rpm_inspection_worker = None

        worker_cls = _mw_hook("LocalRpmInspectionWorker", LocalRpmInspectionWorker)
        worker = worker_cls(file_path)
        self.current_local_rpm_inspection_worker = worker
        worker.signals.inspected.connect(
            lambda inspected_path, info, source=worker, serial=request_serial: (
                self._on_local_rpm_inspected(source, serial, inspected_path, info)
            )
        )
        worker.signals.failed.connect(
            lambda inspected_path, detail, source=worker, serial=request_serial: (
                self._on_local_rpm_inspection_failed(
                    source, serial, inspected_path, detail
                )
            )
        )
        self.status_bar.showMessage(
            tr("Inspecting local package: {name}...").format(
                name=os.path.basename(file_path)
            )
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if worker is self.current_local_rpm_inspection_worker:
                self.current_local_rpm_inspection_worker = None
                QMessageBox.warning(
                    self,
                    tr("Could Not Inspect RPM File"),
                    tr("Dendro could not start inspection of:")
                    + f"\n{file_path}\n\n{type(error).__name__}: {error}",
                )

    def _on_local_rpm_inspected(
        self,
        worker: LocalRpmInspectionWorker,
        request_serial: int,
        file_path: str,
        info: LocalRpmInspectionResult | None,
    ) -> None:
        if (
            request_serial != self._local_rpm_inspection_request_serial
            or worker is not self.current_local_rpm_inspection_worker
        ):
            return
        self.current_local_rpm_inspection_worker = None
        if info is None:
            self.status_bar.showMessage(
                tr("Could not parse the RPM package header for '{path}'.").format(
                    path=file_path
                ),
                8000,
            )
            QMessageBox.warning(
                self,
                tr("Invalid RPM File"),
                tr("Could not parse RPM package header:") + f"\n{file_path}",
            )
            return

        self.status_bar.showMessage(
            tr("Inspected local RPM file '{path}'.").format(path=file_path), 3000
        )
        dlg_cls = _mw_hook("LocalRpmInstallDialog", LocalRpmInstallDialog)
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        dlg = dlg_cls(info, self, host_environment=host_env_fn(self))
        dlg.install_requested.connect(self._on_install_local_rpm_requested)
        dlg.exec()

    def _on_local_rpm_inspection_failed(
        self,
        worker: LocalRpmInspectionWorker,
        request_serial: int,
        file_path: str,
        detail: str,
    ) -> None:
        if (
            request_serial != self._local_rpm_inspection_request_serial
            or worker is not self.current_local_rpm_inspection_worker
        ):
            return
        self.current_local_rpm_inspection_worker = None
        self.status_bar.showMessage(
            tr("Could not inspect local RPM file '{path}': {detail}").format(
                path=file_path, detail=detail
            ),
            8000,
        )
        QMessageBox.warning(
            self,
            tr("Could Not Inspect RPM File"),
            tr("Dendro could not inspect:") + f"\n{file_path}\n\n{detail}",
        )

    def _on_install_local_rpm_requested(self, file_path: str):
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        environment = host_env_fn(self)
        if not environment.supports_dnf5_daemon_mutations:
            title, detail = environment.dnf5_daemon_unavailable(
                tr("install a local RPM file")
            )
            self._show_transaction_error(title, detail)
            return
        try:
            request = TransactionRequest.from_values(installs=[file_path])
        except Dnf5DaemonError as error:
            self._show_transaction_error(error.title, error.detail)
            return
        self._begin_transaction_resolution(request, "local_rpm")

    def _on_system_upgrade_requested(self):
        """Resolve a full system upgrade through the daemon before any authentication."""
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        environment = host_env_fn(self)
        if not environment.supports_dnf5_daemon_mutations:
            title, detail = environment.dnf5_daemon_unavailable(
                tr("perform a DNF5 system upgrade")
            )
            QMessageBox.information(self, title, detail)
            return
        if not self._pending_updates_map:
            QMessageBox.information(
                self, tr("System Up to Date"), tr("Your system is already up to date.")
            )
            return

        up_count = len(self._pending_updates_map)
        reply = QMessageBox.question(
            self,
            tr("Review System Upgrade"),
            tr_plural(
                "There is {count} package update available for your system.",
                "There are {count} package updates available for your system.",
                up_count,
            ).format(count=format_number(up_count))
            + "\n\n"
            + tr(
                "Dendro will ask the DNF5 daemon to resolve the full upgrade first. "
                "You can review the exact package plan before authenticating."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._begin_transaction_resolution(
                TransactionRequest.from_values(upgrade_all=True),
                "system_upgrade",
            )

    def _on_clean_all_orphans_clicked(self):
        """Resolve explicit currently-unneeded packages; never invoke a command runner."""
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        environment = host_env_fn(self)
        if not environment.supports_dnf5_daemon_mutations:
            title, detail = environment.dnf5_daemon_unavailable(
                tr("remove unneeded packages")
            )
            QMessageBox.information(self, title, detail)
            return
        if not self._orphan_cache:
            QMessageBox.information(
                self, tr("No Leaf Orphans"), tr("No unneeded leaf packages found.")
            )
            return

        count = len(self._orphan_cache)
        reply = QMessageBox.question(
            self,
            tr("Review Unneeded Package Removal"),
            tr_plural(
                "Dendro found {count} currently unneeded package.",
                "Dendro found {count} currently unneeded packages.",
                count,
            ).format(count=format_number(count))
            + "\n\n"
            + tr(
                "The DNF5 daemon will resolve the explicit removal set and show all dependent changes before authentication."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._begin_transaction_resolution(
                TransactionRequest.from_values(removals=sorted(self._orphan_cache)),
                "orphan_cleanup",
            )

    def _on_transaction_log_received(self, text: str) -> None:
        self.transaction_drawer.append_log(text)
        dialog = getattr(self, "_active_repo_dialog", None)
        if dialog is not None and getattr(self, "_repo_dialog_op_kind", "") in {
            "clean_cache",
            "toggle",
        }:
            dialog.append_operation_log(text)

    def _on_repo_runner_log_received(self, text: str) -> None:
        self.transaction_drawer.append_log(text)
        dialog = getattr(self, "_active_repo_dialog", None)
        if dialog is not None and getattr(self, "_repo_dialog_op_kind", "") in {
            "add",
            "remove",
        }:
            dialog.append_operation_log(text)

    def _open_repo_dialog(self):
        repo_dialog_cls = _mw_hook("RepoManagerDialog", RepoManagerDialog)
        dialog = repo_dialog_cls(self)
        self._active_repo_dialog = dialog
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        environment = host_env_fn(self)
        if not environment.supports_dnf5_daemon_mutations:
            title, detail = environment.dnf5_daemon_unavailable(
                tr("manage DNF5 repositories")
            )
            dialog.set_read_only_mode(True, note=detail)
            dialog.set_load_error(title, detail)
            dialog.finished.connect(
                lambda _result: self._clear_active_repo_dialog(dialog)
            )
            dialog.exec()
            return
        if self.repository_addition_runner.is_running:
            self._repo_dialog_op_kind = "add"
            dialog.start_operation(
                tr("Adding repository configuration…"), cancellable=True
            )
        elif self.repository_deletion_runner.is_running:
            self._repo_dialog_op_kind = "remove"
            dialog.start_operation(
                tr("Removing repository configuration…"), cancellable=True
            )
        elif self.transaction_controller.is_busy and getattr(
            self, "_repo_dialog_op_kind", ""
        ) in {"clean_cache", "toggle"}:
            dialog.start_operation(
                tr("Running repository maintenance through DNF5…"),
                cancellable=False,
                cancel_tooltip=tr(
                    "DNF5 daemon maintenance calls cannot be cancelled mid-flight."
                ),
            )
        dialog.repo_toggle_requested.connect(self._on_repo_toggle_requested)
        dialog.repo_add_requested.connect(self._on_repo_add_requested)
        dialog.repo_remove_requested.connect(self._on_repo_remove_requested)
        dialog.clean_cache_requested.connect(self._on_clean_cache_requested)
        dialog.refresh_requested.connect(self._load_repositories_from_daemon)
        dialog.cancel_operation_requested.connect(self._on_repo_dialog_cancel_requested)
        dialog.finished.connect(lambda _result: self._clear_active_repo_dialog(dialog))
        self._load_repositories_from_daemon()
        dialog.exec()

    def _clear_active_repo_dialog(self, dialog: RepoManagerDialog):
        if self._active_repo_dialog is not dialog:
            return
        self._active_repo_dialog = None
        op_kind = getattr(self, "_repo_dialog_op_kind", "")
        still_running = (
            self.repository_addition_runner.is_running
            or self.repository_deletion_runner.is_running
            or (
                self.transaction_controller.is_busy
                and op_kind in {"clean_cache", "toggle"}
            )
        )
        if still_running and self.transaction_drawer.isHidden():
            self.transaction_drawer.show()
            self.workspace_splitter.setSizes([450, 320])

    def _on_repo_dialog_cancel_requested(self) -> None:
        if self.repository_addition_runner.is_running:
            self.repository_addition_runner.cancel()
        elif self.repository_deletion_runner.is_running:
            self.repository_deletion_runner.cancel()

    def _load_repositories_from_daemon(self):
        dialog = self._active_repo_dialog
        if dialog is None:
            return
        if self.current_repository_worker is not None:
            self._repository_refresh_after_current_worker = True
            return
        try:
            self.dnf_client.ensure_available()
        except Dnf5DaemonError as error:
            dialog.set_load_error(error.title, error.detail)
            return
        dialog.set_loading(True)
        worker_cls = _mw_hook("RepositoryListWorker", RepositoryListWorker)
        worker = worker_cls(self.dnf_client)
        self.current_repository_worker = worker
        worker.signals.loaded.connect(
            lambda repositories, source=worker, target=dialog: (
                self._on_daemon_repositories_loaded(source, target, repositories)
            )
        )
        worker.signals.failed.connect(
            lambda title, detail, source=worker, target=dialog: (
                self._on_daemon_repositories_failed(source, target, title, detail)
            )
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if worker is self.current_repository_worker:
                self.current_repository_worker = None
                self._repository_refresh_after_current_worker = False
                if self._active_repo_dialog is dialog:
                    dialog.set_load_error(
                        tr("Could Not Load Repositories"),
                        tr(
                            "Could not start the repository query: {error_type}: {error}"
                        ).format(error_type=type(error).__name__, error=error),
                    )

    def _on_daemon_repositories_loaded(
        self,
        worker: RepositoryListWorker,
        dialog: RepoManagerDialog,
        repositories: object,
    ) -> None:
        if worker is not self.current_repository_worker:
            return
        self.current_repository_worker = None
        refresh_after_current_worker = self._repository_refresh_after_current_worker
        self._repository_refresh_after_current_worker = False
        if self._active_repo_dialog is dialog:
            dialog.set_repositories(repositories)
        if refresh_after_current_worker:
            self._load_repositories_from_daemon()

    def _on_daemon_repositories_failed(
        self,
        worker: RepositoryListWorker,
        dialog: RepoManagerDialog,
        title: str,
        detail: str,
    ) -> None:
        if worker is not self.current_repository_worker:
            return
        self.current_repository_worker = None
        refresh_after_current_worker = self._repository_refresh_after_current_worker
        self._repository_refresh_after_current_worker = False
        if self._active_repo_dialog is dialog:
            dialog.set_load_error(title, detail)
        if refresh_after_current_worker:
            self._load_repositories_from_daemon()

    def _on_repo_add_requested(self, repository_url: str):
        dialog = self._active_repo_dialog
        self._repo_dialog_op_kind = "add"
        self.transaction_drawer.start_execution_mode()
        if dialog is not None:
            dialog.start_operation(
                tr("Downloading and validating repository configuration…"),
                cancellable=True,
            )
        else:
            self._open_transaction_drawer()
        try:
            self.repository_addition_runner.add_from_url(repository_url)
        except Dnf5DaemonError as error:
            self._repo_dialog_op_kind = ""
            self.transaction_drawer.finish_execution_mode(False)
            if dialog is not None:
                dialog.finish_operation(False, error.title, detail=error.detail)
            else:
                self._show_transaction_error(error.title, error.detail)

    def _on_repo_remove_requested(self, repo_id: str, repo_file: str):
        dialog = self._active_repo_dialog
        self._repo_dialog_op_kind = "remove"
        self.transaction_drawer.start_execution_mode()
        if dialog is not None:
            dialog.start_operation(
                tr("Removing repository '{repo_id}'…").format(repo_id=repo_id),
                cancellable=True,
            )
        else:
            self._open_transaction_drawer()
        try:
            self.repository_deletion_runner.delete(repo_file)
        except Dnf5DaemonError as error:
            self._repo_dialog_op_kind = ""
            self.transaction_drawer.finish_execution_mode(False)
            if dialog is not None:
                dialog.finish_operation(False, error.title, detail=error.detail)
            else:
                self._show_transaction_error(error.title, error.detail)

    def _on_clean_cache_requested(self):
        dialog = self._active_repo_dialog
        self._repo_dialog_op_kind = "clean_cache"
        self.transaction_drawer.start_execution_mode()
        if dialog is not None:
            dialog.start_operation(
                tr("Cleaning DNF package cache through DNF5…"),
                cancellable=False,
                cancel_tooltip=tr(
                    "DNF5 daemon maintenance calls cannot be cancelled mid-flight."
                ),
            )
        else:
            self._open_transaction_drawer()
        try:
            self.transaction_controller.clean_cache()
        except Dnf5DaemonError as error:
            self._repo_dialog_op_kind = ""
            self.transaction_drawer.finish_execution_mode(False)
            if dialog is not None:
                dialog.finish_operation(False, error.title, detail=error.detail)
            else:
                self._show_transaction_error(error.title, error.detail)

    def _on_repo_toggle_requested(self, repo_id: str, enable: bool):
        dialog = self._active_repo_dialog
        self._repo_dialog_op_kind = "toggle"
        self.transaction_drawer.start_execution_mode()
        description = (
            tr("Enabling repository '{repo_id}'…").format(repo_id=repo_id)
            if enable
            else tr("Disabling repository '{repo_id}'…").format(repo_id=repo_id)
        )
        if dialog is not None:
            dialog.start_operation(
                description,
                cancellable=False,
                cancel_tooltip=tr(
                    "DNF5 daemon maintenance calls cannot be cancelled mid-flight."
                ),
            )
        else:
            self._open_transaction_drawer()
        try:
            self.transaction_controller.set_repository_enabled(repo_id, enable)
        except Dnf5DaemonError as error:
            self._repo_dialog_op_kind = ""
            self.transaction_drawer.finish_execution_mode(False)
            if dialog is not None:
                dialog.finish_operation(False, error.title, detail=error.detail)
                self._load_repositories_from_daemon()
            else:
                self._show_transaction_error(error.title, error.detail)

    def _open_history_dialog(self):
        history_dialog_cls = _mw_hook("DnfHistoryDialog", DnfHistoryDialog)
        dialog = history_dialog_cls(self)
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        environment = host_env_fn(self)
        if not environment.supports_dnf5_daemon_mutations:
            _title, reason = environment.dnf5_daemon_unavailable(
                tr("copy terminal undo commands")
            )
            dialog.set_undo_supported(False, tooltip=reason)
        dialog.refresh_requested.connect(lambda: self._load_dnf_history(dialog))
        self._load_dnf_history(dialog)
        dialog.exec()

    def _open_advisories_dialog(self):
        """Fetch pending security advisories, then show them."""
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        environment = host_env_fn(self)
        if not environment.supports_dnf5_daemon_queries:
            _title, detail = environment.dnf5_daemon_unavailable(
                tr("read security advisories")
            )
            self._show_transaction_error(tr("Security advisories unavailable"), detail)
            return
        self.status_bar.showMessage(tr("Reading security advisories..."))
        worker_cls = _mw_hook("SecurityAdvisoryWorker", SecurityAdvisoryWorker)
        worker = worker_cls()
        worker.signals.security_advisories_loaded.connect(self._on_advisories_loaded)
        worker.signals.error_occurred.connect(self._on_advisories_failed)
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            self._on_advisories_failed(
                tr("Could not start the advisory query"),
                f"{type(error).__name__}: {error}",
            )

    def _on_advisories_failed(self, title: str, detail: str) -> None:
        """Report a failed advisory query where the user asked for it."""
        self.status_bar.showMessage(tr("Error: {detail}").format(detail=detail), 8000)
        self._show_transaction_error(
            title or tr("Could not read security advisories"), detail
        )

    def _on_advisories_loaded(self, advisories: object) -> None:
        entries = tuple(
            item for item in advisories or () if isinstance(item, SecurityAdvisory)
        )
        if not entries:
            self.status_bar.showMessage(
                tr("No security advisory affects an installed package."), 5000
            )
        else:
            total = len(entries)
            self.status_bar.showMessage(
                tr_plural(
                    "{count} security advisory affects installed packages.",
                    "{count} security advisories affect installed packages.",
                    total,
                ).format(count=format_number(total)),
                5000,
            )
        dialog_cls = _mw_hook("SecurityAdvisoryDialog", SecurityAdvisoryDialog)
        dialog = dialog_cls(entries, self)
        copied_signal = getattr(dialog, "advisory_copied", None)
        if copied_signal is not None:
            copied_signal.connect(self._show_clipboard_status)
        dialog.exec()
        dialog.deleteLater()

    def _open_flatpak_dialog(self):
        dialog_cls = _mw_hook("FlatpakInventoryDialog", FlatpakInventoryDialog)
        refresh_fn = _mw_hook("start_flatpak_refresh", start_flatpak_refresh)
        dialog = dialog_cls(self)
        try:
            refresh_fn(dialog, self.thread_pool)
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _load_dnf_history(self, dialog: DnfHistoryDialog):
        self.status_bar.showMessage(tr("Loading DNF transaction history..."))
        request_serial = dialog.set_history_loading()
        worker_cls = _mw_hook("DnfHistoryWorker", DnfHistoryWorker)
        worker = worker_cls()
        worker.signals.history_loaded.connect(
            lambda entries, serial=request_serial: self._on_dnf_history_loaded(
                dialog, serial, entries
            )
        )
        worker.signals.error_occurred.connect(
            lambda package, detail, serial=request_serial: self._on_dnf_history_failed(
                dialog, serial, package, detail
            )
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            self._on_dnf_history_failed(
                dialog,
                request_serial,
                "",
                tr("Could not start DNF history loading: {error_type}: {error}").format(
                    error_type=type(error).__name__, error=error
                ),
            )

    def _on_dnf_history_loaded(
        self, dialog: DnfHistoryDialog, request_serial: int, entries: list[HistoryEntry]
    ) -> None:
        if dialog.set_history_entries_if_current(request_serial, entries):
            self.status_bar.showMessage(tr("DNF transaction history loaded."), 3000)

    def _on_dnf_history_failed(
        self,
        dialog: DnfHistoryDialog,
        request_serial: int,
        package_name: str,
        detail: str,
    ) -> None:
        if dialog.set_history_error_if_current(request_serial, package_name, detail):
            self._on_query_error(package_name, detail)

    def _on_header_apply_clicked(self):
        specs = self.tree_model.get_queued_transaction_specs()
        if specs.total_count == 0:
            if self._pending_updates_map:
                self._on_system_upgrade_requested()
            return
        try:
            request = TransactionRequest.from_values(
                installs=specs.installs,
                removals=specs.removals,
                upgrades=specs.upgrades,
                reinstalls=specs.reinstalls,
                downgrades=specs.downgrades,
                distro_syncs=specs.distro_syncs,
            )
        except Dnf5DaemonError as error:
            self._show_transaction_error(error.title, error.detail)
            return
        self._begin_transaction_resolution(request, "staged")

    def _begin_transaction_resolution(
        self,
        request: TransactionRequest,
        transaction_kind: str,
        *,
        allow_erasing: bool = False,
    ) -> None:
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        environment = host_env_fn(self)
        if not environment.supports_dnf5_daemon_mutations:
            title, detail = environment.dnf5_daemon_unavailable(
                tr("resolve package transactions")
            )
            self._show_transaction_error(title, detail)
            return
        try:
            self._pending_transaction_kind = transaction_kind
            self._last_transaction_upgrades = [
                *request.upgrades,
                *request.distro_syncs,
            ]
            self._last_transaction_removals = self.tree_model.queued_removal_packages()
            self._open_transaction_drawer()
            self.status_bar.showMessage(
                tr("Resolving transaction through Fedora's DNF5 daemon…")
            )
            if allow_erasing:
                self.transaction_controller.resolve(request, allow_erasing=True)
            else:
                self.transaction_controller.resolve(request)
        except Dnf5DaemonError as error:
            self._pending_transaction_kind = ""
            self._last_transaction_upgrades = []
            self._last_transaction_removals = []
            self.transaction_drawer.finish_execution_mode(False)
            self._show_transaction_error(error.title, error.detail)

    def _on_dnf_plan_ready(self, plan: object) -> None:
        if not isinstance(plan, TransactionPlan):
            self._show_transaction_error(
                tr("Invalid transaction plan"),
                tr("The DNF5 daemon controller returned an invalid plan object."),
            )
            return
        if not plan.has_changes:
            QMessageBox.information(
                self,
                tr("No Package Changes"),
                tr(
                    "DNF resolved the request successfully, but no package changes are required."
                ),
            )
            self.transaction_controller.discard_ready_plan()
            return

        plan_dialog_cls = _mw_hook(
            "TransactionPlanReviewDialog", TransactionPlanReviewDialog
        )
        dialog = plan_dialog_cls(plan=plan, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.transaction_controller.discard_ready_plan()
            return

        installs, removals, upgrades, reinstalls, downgrades = plan.grouped_names()
        if reinstalls or downgrades:
            self.transaction_drawer.set_transaction_preview(
                installs, removals, upgrades, reinstalls, downgrades
            )
        else:
            self.transaction_drawer.set_transaction_preview(
                installs, removals, upgrades
            )
        self.transaction_drawer.start_execution_mode()
        if dialog.stage_for_next_boot():
            download_only_fn = getattr(dialog, "stage_download_only", None)
            finish_action_fn = getattr(dialog, "stage_finish_action", None)
            download_only = (
                bool(download_only_fn()) if callable(download_only_fn) else False
            )
            finish_action = (
                str(finish_action_fn()) if callable(finish_action_fn) else "reboot"
            )
            self._pending_transaction_kind = (
                "stage_download_only" if download_only else "stage_offline"
            )
            try:
                self.status_bar.showMessage(
                    tr("Downloading packages for offline pre-staging…")
                    if download_only
                    else tr("Staging this transaction for the next boot…")
                )
                if not download_only and finish_action == "reboot":
                    self.transaction_controller.stage_ready_plan()
                else:
                    self.transaction_controller.stage_ready_plan(
                        download_only=download_only,
                        finish_action=finish_action,
                    )
            except Dnf5DaemonError as error:
                self._pending_transaction_kind = ""
                self.transaction_drawer.finish_execution_mode(False)
                self._show_transaction_error(error.title, error.detail)
            return
        try:
            self.status_bar.showMessage(tr("Awaiting DNF5 daemon authorization…"))
            self.transaction_controller.execute_ready_plan()
        except Dnf5DaemonError as error:
            self.transaction_drawer.finish_execution_mode(False)
            self._show_transaction_error(error.title, error.detail)

    def _on_dnf_plan_discarded(self) -> None:
        if not self.transaction_controller.is_busy:
            self.transaction_drawer.finish_execution_mode(False)
            self.status_bar.showMessage(tr("Transaction plan discarded."))
        self._continue_close_if_requested()

    def _close_transaction_drawer(self):
        if (
            self.transaction_controller.is_busy
            or self.transaction_controller.has_ready_plan
        ):
            self.status_bar.showMessage(
                tr(
                    "A DNF5 operation is still active. Use Cancel to request a safe daemon cancellation."
                )
            )
            return
        if self.repository_addition_runner.is_running:
            self.status_bar.showMessage(
                tr(
                    "Repository-file addition is still active. Use Cancel to request cancellation."
                )
            )
            return
        if self.repository_deletion_runner.is_running:
            self.status_bar.showMessage(
                tr(
                    "Repository-file deletion is still active. Use Cancel to request termination."
                )
            )
            return
        self.transaction_drawer.hide()
        self.workspace_splitter.setSizes([750, 0])

    def _on_drawer_cancel(self):
        if self.repository_addition_runner.is_running:
            self.repository_addition_runner.cancel()
            return
        if self.repository_deletion_runner.is_running:
            self.repository_deletion_runner.cancel()
            return
        if (
            self.transaction_controller.is_busy
            or self.transaction_controller.has_ready_plan
        ):
            self.transaction_controller.request_cancellation()
            return
        self._close_transaction_drawer()

    def _on_drawer_commit(self):
        if not self.transaction_controller.has_ready_plan:
            return
        self.transaction_drawer.start_execution_mode()
        try:
            self.transaction_controller.execute_ready_plan()
        except Dnf5DaemonError as error:
            self.transaction_drawer.finish_execution_mode(False)
            self._show_transaction_error(error.title, error.detail)

    def _reset_metadata_catalogs(self, *, include_desktop: bool = False) -> None:
        """Drop stale metadata singletons so reloads read fresh host state."""
        _mw_hook("AppStreamCatalog", AppStreamCatalog).reset_instance()
        _mw_hook("FedoraCompsCatalog", FedoraCompsCatalog).reset_instance()
        if include_desktop:
            _mw_hook("SystemDesktopIndex", SystemDesktopIndex).reset_instance()

    def _on_transaction_finished(self, success: bool, detail: str):
        transaction_kind = self._pending_transaction_kind
        self.transaction_drawer.finish_execution_mode(success)
        refresh_offline = getattr(self, "_start_offline_status_check", None)
        if success:
            if transaction_kind in {"stage_offline", "stage_download_only"}:
                self.tree_model.clear_all_queued()
                self._sync_queue_states()
                if detail:
                    self.status_bar.showMessage(
                        tr(
                            "Transaction staged, but DNF session cleanup reported an error. See the transaction log."
                        )
                    )
                elif transaction_kind == "stage_download_only":
                    self.status_bar.showMessage(
                        tr(
                            "Packages downloaded and verified for offline pre-staging (not yet scheduled for next boot)."
                        )
                    )
                else:
                    self.status_bar.showMessage(
                        tr("Transaction staged for application during the next boot.")
                    )
                if callable(refresh_offline):
                    refresh_offline(
                        prompt_reboot_if_ready=(transaction_kind == "stage_offline")
                    )
            else:
                self.status_bar.showMessage(tr("Transaction completed successfully."))
                if transaction_kind == "system_upgrade":
                    self._pending_updates_map.clear()
                else:
                    for package in self._last_transaction_upgrades:
                        self._pending_updates_map.pop(package, None)
                    for package in self._last_transaction_removals:
                        self._pending_updates_map.pop(package.name_arch, None)
                        self._orphan_cache.discard(package.full_nevra)
                        self._user_installed_cache.discard(package.full_nevra)

                remaining_updates = len(self._pending_updates_map)
                self.header.update_available_updates_badge(remaining_updates)
                self.sidebar.update_category_counts(
                    {
                        "updates_available": remaining_updates,
                        "orphans": len(self._orphan_cache),
                    }
                )
                cache_cls = _mw_hook("SQLiteCapabilityCache", SQLiteCapabilityCache)
                cache_cls.get_instance().invalidate()
                self._reset_metadata_catalogs(include_desktop=True)
                self.tree_model.clear_all_queued()
                self._sync_queue_states()
                self._load_packages()
                self.proxy_model.refresh_filter()
                if detail:
                    self.status_bar.showMessage(
                        tr(
                            "Transaction completed, but DNF session cleanup reported an error. See the transaction log."
                        )
                    )
                if callable(refresh_offline):
                    refresh_offline(prompt_reboot_if_ready=False)
        else:
            self.status_bar.showMessage(
                tr("Transaction failed or was cancelled: {detail}").format(
                    detail=detail
                )
            )
            if transaction_kind in {
                "stage_offline",
                "stage_download_only",
            } and callable(refresh_offline):
                refresh_offline(prompt_reboot_if_ready=False)
        self._pending_transaction_kind = ""
        self._continue_close_if_requested()

    def _on_maintenance_finished(self, action: str, success: bool, detail: str):
        self._repo_dialog_op_kind = ""
        self.transaction_drawer.finish_execution_mode(success)
        offline_banner = getattr(self, "offline_banner", None)
        if action in _OFFLINE_MAINTENANCE_ACTIONS and offline_banner is not None:
            offline_banner.set_busy(False)
        if success and action not in _OFFLINE_MAINTENANCE_ACTIONS:
            self._reset_metadata_catalogs()
        dialog = self._active_repo_dialog
        if dialog is not None and action not in _OFFLINE_MAINTENANCE_ACTIONS:
            if success:
                dialog.finish_operation(
                    True, tr("Completed: {action}.").format(action=action)
                )
            else:
                self._repo_inline_error_handled = True
                dialog.finish_operation(
                    False,
                    tr("Failed to {action}").format(action=action),
                    detail=detail,
                )
            self._load_repositories_from_daemon()
        if action in _OFFLINE_MAINTENANCE_ACTIONS:
            if success:
                offline_messages = {
                    "schedule offline update": tr(
                        "Pre-staged offline update scheduled for the next boot."
                    ),
                    "cancel pending boot update": tr(
                        "Pending next-boot update cancelled; downloaded packages remain cached."
                    ),
                    "clean staged update cache": tr(
                        "Staged offline update cache cleaned."
                    ),
                    "set offline finish action": tr(
                        "Offline update finish action updated."
                    ),
                }
                self.status_bar.showMessage(offline_messages[action])
            else:
                self.status_bar.showMessage(
                    tr("Offline update action failed: {detail}").format(detail=detail)
                )
            refresh_offline = getattr(self, "_start_offline_status_check", None)
            if callable(refresh_offline):
                refresh_offline(prompt_reboot_if_ready=False)
        else:
            self.status_bar.showMessage(
                tr("DNF5 daemon completed: {action}.").format(action=action)
                if success
                else tr("DNF5 daemon failed to {action}: {detail}").format(
                    action=action, detail=detail
                )
            )
        self._continue_close_if_requested()

    def _run_offline_maintenance_action(self, action_fn, *, status_text: str) -> None:
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        environment = host_env_fn(self)
        if not environment.supports_dnf5_daemon_mutations:
            title, detail = environment.dnf5_daemon_unavailable(
                tr("manage staged offline updates")
            )
            self._show_transaction_error(title, detail)
            return

        offline_banner = getattr(self, "offline_banner", None)
        if offline_banner is not None:
            offline_banner.set_busy(True)
        self.status_bar.showMessage(status_text)
        try:
            action_fn()
        except Dnf5DaemonError as error:
            if offline_banner is not None:
                offline_banner.set_busy(False)
            refresh_offline = getattr(self, "_start_offline_status_check", None)
            if callable(refresh_offline):
                refresh_offline(prompt_reboot_if_ready=False)
            self._show_transaction_error(error.title, error.detail)

    def _on_offline_schedule_requested(self) -> None:
        self._run_offline_maintenance_action(
            self.transaction_controller.schedule_offline_transaction,
            status_text=tr(
                "Requesting authorization to schedule the pre-staged update for the next boot…"
            ),
        )

    def _on_offline_cancel_requested(self) -> None:
        self._run_offline_maintenance_action(
            self.transaction_controller.cancel_offline_transaction,
            status_text=tr(
                "Requesting authorization to cancel the pending next-boot update…"
            ),
        )

    def _on_offline_clean_requested(self) -> None:
        reply = QMessageBox.question(
            self,
            tr("Clean Staged Offline Cache?"),
            tr(
                "This cancels any scheduled next-boot update and deletes downloaded "
                "offline packages and transaction metadata from DNF's cache. Continue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._run_offline_maintenance_action(
            self.transaction_controller.clean_offline_transaction,
            status_text=tr(
                "Requesting authorization to clean the staged offline update cache…"
            ),
        )

    def _on_offline_finish_action_requested(self, action: str) -> None:
        self._run_offline_maintenance_action(
            lambda: self.transaction_controller.set_offline_finish_action(action),
            status_text=tr(
                "Requesting authorization to update the offline finish action…"
            ),
        )

    def _on_offline_reboot_requested(self) -> None:
        status = getattr(self, "_offline_status", None)
        if not isinstance(status, OfflineTransactionStatus):
            return
        self._prompt_reboot_to_apply_offline_update(status)

    def _prompt_reboot_to_apply_offline_update(
        self, status: OfflineTransactionStatus
    ) -> None:
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        environment = host_env_fn(self)
        if not environment.supports_host_reboot_control:
            return
        if status.poweroff_after:
            message = tr(
                "Your system is scheduled to install the staged DNF5 update during the "
                "next restart and power off automatically once complete.\n\n"
                "Save all open work before restarting now."
            )
        else:
            message = tr(
                "Your system is scheduled to install the staged DNF5 update during the "
                "next restart and boot back into Fedora once complete.\n\n"
                "Save all open work before restarting now."
            )
        reply = QMessageBox.question(
            self,
            tr("Restart System to Apply Staged Update?"),
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._start_system_reboot()

    def _start_system_reboot(self) -> None:
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        environment = host_env_fn(self)
        if not environment.supports_host_reboot_control:
            if environment.is_flatpak:
                self._show_transaction_error(
                    tr("Restart from Flatpak is unavailable"),
                    tr(
                        "Dendro's Flatpak sandbox does not access systemd-logind. "
                        "Restart your computer from your desktop menu to apply the staged update."
                    ),
                )
            else:
                self._show_transaction_error(
                    tr("System restart control is unavailable"),
                    tr(
                        "Dendro only triggers system restarts on traditional mutable Fedora hosts."
                    ),
                )
            return

        if getattr(self, "current_reboot_worker", None) is not None:
            return

        offline_banner = getattr(self, "offline_banner", None)
        if offline_banner is not None:
            offline_banner.set_busy(True)
        self.status_bar.showMessage(tr("Requesting system restart via systemd-logind…"))

        worker_cls = _mw_hook("SystemRebootWorker", SystemRebootWorker)
        worker = worker_cls()
        self.current_reboot_worker = worker
        worker.signals.succeeded.connect(
            lambda source=worker: self._on_system_reboot_succeeded(source)
        )
        worker.signals.failed.connect(
            lambda title, detail, source=worker: self._on_system_reboot_failed(
                source, title, detail
            )
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            if worker is getattr(self, "current_reboot_worker", None):
                self.current_reboot_worker = None
            if offline_banner is not None:
                offline_banner.set_busy(False)
            self._show_transaction_error(
                tr("System restart failed"),
                f"{type(error).__name__}: {error}",
            )

    def _on_system_reboot_succeeded(self, worker: SystemRebootWorker) -> None:
        if worker is not getattr(self, "current_reboot_worker", None):
            return
        self.current_reboot_worker = None
        self.status_bar.showMessage(
            tr("System restart requested; waiting for session shutdown…")
        )

    def _on_system_reboot_failed(
        self, worker: SystemRebootWorker, title: str, detail: str
    ) -> None:
        if worker is not getattr(self, "current_reboot_worker", None):
            return
        self.current_reboot_worker = None
        offline_banner = getattr(self, "offline_banner", None)
        if offline_banner is not None:
            offline_banner.set_busy(False)
        refresh_offline = getattr(self, "_start_offline_status_check", None)
        if callable(refresh_offline):
            refresh_offline(prompt_reboot_if_ready=False)
        self.status_bar.showMessage(f"{title}: {detail}")
        self._show_transaction_error(title, detail)

    def _on_repository_addition_finished(self, success: bool, detail: str):
        self._repo_dialog_op_kind = ""
        self.transaction_drawer.finish_execution_mode(success)
        dialog = self._active_repo_dialog
        if success:
            self._reset_metadata_catalogs()
            self.status_bar.showMessage(tr("Repository configuration file added."))
            if dialog is not None:
                dialog.clear_repository_address()
                dialog.finish_operation(
                    True, tr("Repository configuration file added.")
                )
        else:
            self.status_bar.showMessage(detail)
            if dialog is not None:
                dialog.finish_operation(
                    False, tr("Repository addition failed"), detail=detail
                )
            else:
                self._show_transaction_error(tr("Repository addition failed"), detail)
        if dialog is not None:
            self._load_repositories_from_daemon()
        self._continue_close_if_requested()

    def _on_repository_deletion_finished(self, success: bool, detail: str):
        self._repo_dialog_op_kind = ""
        self.transaction_drawer.finish_execution_mode(success)
        dialog = self._active_repo_dialog
        if success:
            self._reset_metadata_catalogs()
            self.status_bar.showMessage(tr("Repository configuration file deleted."))
            if dialog is not None:
                dialog.finish_operation(
                    True, tr("Repository configuration file deleted.")
                )
        else:
            self.status_bar.showMessage(detail)
            if dialog is not None:
                dialog.finish_operation(
                    False, tr("Repository deletion failed"), detail=detail
                )
            else:
                self._show_transaction_error(tr("Repository deletion failed"), detail)
        if dialog is not None:
            self._load_repositories_from_daemon()
        self._continue_close_if_requested()

    def _on_dnf_resolution_failed(self, request: object, error: object) -> None:
        if not isinstance(error, Dnf5DaemonResolutionError):
            self._on_dnf_operation_failed(
                getattr(error, "title", tr("Unable to resolve transaction")),
                getattr(error, "detail", str(error)),
            )
            return
        if (
            error.allow_erasing_attempted
            or not isinstance(request, TransactionRequest)
            or getattr(self, "_close_after_transaction", False)
        ):
            self._on_dnf_operation_failed(error.title, error.detail)
            return

        transaction_kind = getattr(self, "_pending_transaction_kind", "") or "queued"
        self.transaction_drawer.finish_execution_mode(False)
        self.status_bar.showMessage(f"{error.title}: {error.detail}")
        prompt_body = f"{error.detail}\n\n" + tr(
            "Allow DNF5 to erase conflicting packages and re-resolve the transaction plan? "
            "You will still review every removal before applying any changes."
        )
        reply = QMessageBox.question(
            self,
            tr("Dependency Conflict Requires Package Removal"),
            prompt_body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._begin_transaction_resolution(
                request, transaction_kind, allow_erasing=True
            )
            return

        self._pending_transaction_kind = ""
        self._last_transaction_upgrades = []
        self._last_transaction_removals = []
        self._continue_close_if_requested()

    def _on_dnf_operation_failed(self, title: str, detail: str):
        if getattr(self, "_repo_inline_error_handled", False):
            self._repo_inline_error_handled = False
            self.status_bar.showMessage(f"{title}: {detail}")
            self._continue_close_if_requested()
            return
        self.transaction_drawer.finish_execution_mode(False)
        if (
            not self.transaction_controller.is_busy
            and not self.transaction_controller.has_ready_plan
        ):
            self._pending_transaction_kind = ""
            self._last_transaction_upgrades = []
            self._last_transaction_removals = []
        self.status_bar.showMessage(f"{title}: {detail}")
        self._show_transaction_error(title, detail)
        self._continue_close_if_requested()

    def _show_transaction_error(self, title: str, detail: str):
        QMessageBox.critical(self, title, detail)


__all__ = ("MainWindowTransactionsMixin",)
