# dendro/core/dnf_workers.py
"""Qt background workers for DNF5 daemon D-Bus and read-only DNF CLI queries."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any, Final

from PyQt6.QtCore import pyqtSlot

from dendro.core.backend_parsers import (
    _dnf5_json_option_is_unsupported,
    _history_entry_from_daemon_transaction,
    _is_dnf5_binary,
    _parse_dnf5_history_json,
    _parse_dnf_package_identity_output,
    _parse_remote_fd_records,
    _parse_remote_package_records,
    _parse_userinstalled_fd_records,
    classification_package_fields,
)
from dendro.core.dnf5daemon import (
    Dnf5DaemonClient,
    Dnf5DaemonError,
    Dnf5DaemonUnavailable,
    Dnf5DaemonUnsupportedError,
)
from dendro.core.host_environment import HostEnvironment, detect_host_environment
from dendro.core.package_classifier import ProductionTaxonomyEngine
from dendro.core.package_metadata import (
    AppStreamCatalog,
    FedoraCompsCatalog,
    PackagePhysicalAnatomy,
)
from dendro.core.package_types import (
    AvailableUpdateInfo,
    HistoryEntry,
    PackageInfo,
    PackageState,
)
from dendro.core.process_utils import (
    describe_subprocess_failure,
    flatpak_read_only_unavailable,
    get_clean_env,
    is_running_in_flatpak,
)
from dendro.core.rpm_workers import BackendSignals
from dendro.core.taxonomy import TaxonomyInputScope
from dendro.core.worker_lifecycle import ShutdownSafeRunnable

_DEFAULT_DNF5_DAEMON_CLIENT: Final = Dnf5DaemonClient


def _backend_hook(name: str, default: Any) -> Any:
    """Resolve optional monkeypatches applied to ``core.backend`` in tests."""
    facade = sys.modules.get("dendro.core.backend")
    return getattr(facade, name, default) if facade is not None else default


def get_host_environment() -> HostEnvironment:
    """Return the classified runtime environment, honoring sandbox overrides."""
    in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
    if in_flatpak():
        return HostEnvironment.flatpak()
    environment = detect_host_environment()
    if environment.is_flatpak:
        return HostEnvironment.traditional()
    return environment


def get_dnf_binary_path() -> str:
    """Return a local DNF binary only when read-only DNF CLI queries apply."""
    in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
    if in_flatpak():
        return ""
    host_env_fn = _backend_hook("get_host_environment", get_host_environment)
    if not host_env_fn().supports_dnf_cli_queries:
        return ""
    for candidate in ("/usr/bin/dnf5", "/usr/bin/dnf"):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("dnf5") or shutil.which("dnf")
    return os.path.realpath(found) if found and os.path.exists(found) else ""


_DEFAULT_GET_DNF_BINARY_PATH: Final = get_dnf_binary_path


def _effective_runtime(
    explicit_client_factory: Callable[[], Dnf5DaemonClient],
) -> tuple[
    bool, HostEnvironment, Callable[[], str], bool, Callable[[], Dnf5DaemonClient]
]:
    in_flatpak = bool(_backend_hook("is_running_in_flatpak", is_running_in_flatpak)())
    host_env_fn = _backend_hook("get_host_environment", get_host_environment)
    raw_env = host_env_fn()
    effective_env = HostEnvironment.flatpak() if in_flatpak else raw_env
    dnf_bin_fn = _backend_hook("get_dnf_binary_path", get_dnf_binary_path)
    default_dnf_fn = _backend_hook(
        "_DEFAULT_GET_DNF_BINARY_PATH", _DEFAULT_GET_DNF_BINARY_PATH
    )
    cli_hooked = dnf_bin_fn is not default_dnf_fn
    if explicit_client_factory is not _DEFAULT_DNF5_DAEMON_CLIENT:
        factory = explicit_client_factory
    else:
        hooked_cls = _backend_hook("Dnf5DaemonClient", Dnf5DaemonClient)
        if hooked_cls is _DEFAULT_DNF5_DAEMON_CLIENT:
            factory = lambda: hooked_cls(  # noqa: E731
                flatpak_detector=lambda: in_flatpak,
                environment_detector=lambda: effective_env,
            )
        else:
            factory = hooked_cls
    return in_flatpak, effective_env, dnf_bin_fn, cli_hooked, factory


def _close_client_quietly(client: Any) -> None:
    """Close worker-created daemon clients if they expose ``close()``."""
    close_fn = getattr(client, "close", None)
    if callable(close_fn):
        with contextlib.suppress(Exception):
            close_fn()


class SecurityAdvisoryWorker(ShutdownSafeRunnable):
    """Read pending security advisories through the DNF5 daemon."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Dnf5DaemonClient] = Dnf5DaemonClient,
    ) -> None:
        super().__init__()
        self.signals = BackendSignals()
        self._client_factory = client_factory

    @pyqtSlot()
    def run(self) -> None:
        client: Any = None
        try:
            client = self._client_factory()
            advisories = client.list_security_advisories()
        except Dnf5DaemonError as error:
            self.signals.error_occurred.emit(error.title, error.detail)
            return
        except Exception as error:
            self.signals.error_occurred.emit(
                "Could not read security advisories",
                f"{type(error).__name__}: {error}",
            )
            return
        finally:
            if client is not None:
                _close_client_quietly(client)
        self.signals.security_advisories_loaded.emit(list(advisories))


class UserInstalledQueryWorker(ShutdownSafeRunnable):
    """Identify user-installed RPM packages via DNF5 daemon ``Rpm.list_fd`` with CLI fallback."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Dnf5DaemonClient] = Dnf5DaemonClient,
    ) -> None:
        super().__init__()
        self.signals = BackendSignals()
        self._client_factory = client_factory
        self._is_cancelled = threading.Event()

    def cancel(self) -> None:
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self) -> None:
        if self._is_cancelled.is_set():
            return

        in_flatpak, effective_env, dnf_bin_fn, cli_hooked, client_factory = (
            _effective_runtime(self._client_factory)
        )
        custom_client = self._client_factory is not _DEFAULT_DNF5_DAEMON_CLIENT

        if (
            not custom_client
            and not cli_hooked
            and not effective_env.supports_dnf5_daemon_queries
            and not effective_env.supports_dnf_cli_queries
        ):
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    effective_env.feature_unavailable(
                        "User-installed package detection"
                    ),
                )
            return

        if effective_env.supports_dnf5_daemon_queries or custom_client:
            client: Any = None
            try:
                client = client_factory()
                raw_records = client.list_packages_fd(
                    scope="installed",
                    package_attrs=(
                        "name",
                        "epoch",
                        "version",
                        "release",
                        "arch",
                        "reason",
                    ),
                    load_system_repo=True,
                    load_available_repos=False,
                    cancel_event=self._is_cancelled,
                )
                if self._is_cancelled.is_set():
                    return
                user_pkg_nevras = _parse_userinstalled_fd_records(raw_records)
            except (Dnf5DaemonUnavailable, Dnf5DaemonUnsupportedError):
                if in_flatpak or (
                    not cli_hooked and not effective_env.supports_dnf_cli_queries
                ):
                    if not self._is_cancelled.is_set():
                        detail = (
                            flatpak_read_only_unavailable(
                                "User-installed package detection"
                            )
                            if in_flatpak
                            else effective_env.feature_unavailable(
                                "User-installed package detection"
                            )
                        )
                        self.signals.error_occurred.emit("", detail)
                    return
            except Dnf5DaemonError as exc:
                if not self._is_cancelled.is_set():
                    self.signals.error_occurred.emit(
                        "",
                        f"Could not identify user-installed packages: {exc.detail}",
                    )
                return
            except Exception as exc:
                if not self._is_cancelled.is_set():
                    self.signals.error_occurred.emit(
                        "",
                        f"Could not identify user-installed packages: {describe_subprocess_failure(exc)}",
                    )
                return
            else:
                if not self._is_cancelled.is_set():
                    self.signals.userinstalled_loaded.emit(user_pkg_nevras)
                return
            finally:
                if client is not None:
                    _close_client_quietly(client)

        if in_flatpak:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    flatpak_read_only_unavailable("User-installed package detection"),
                )
            return
        if not cli_hooked and not effective_env.supports_dnf_cli_queries:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    effective_env.feature_unavailable(
                        "User-installed package detection"
                    ),
                )
            return

        dnf_bin = dnf_bin_fn()
        if not dnf_bin:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    "User-installed package detection requires an installed 'dnf5' or 'dnf' command on this RPM host.",
                )
            return

        try:
            cmd = [
                dnf_bin,
                "repoquery",
                "--userinstalled",
                "-q",
                "--queryformat",
                "%{name}\x1f%{epoch}\x1f%{version}\x1f%{release}\x1f%{arch}\n",
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=35,
            )
            if self._is_cancelled.is_set():
                return
            if res.returncode != 0:
                detail = (
                    res.stderr.strip()
                    or f"{dnf_bin} repoquery exited with status {res.returncode}."
                )
                self.signals.error_occurred.emit(
                    "", f"Could not identify user-installed packages: {detail}"
                )
                return
            user_pkg_nevras = _parse_dnf_package_identity_output(
                res.stdout, "DNF user-installed package query"
            )
            self.signals.userinstalled_loaded.emit(user_pkg_nevras)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Could not identify user-installed packages: {describe_subprocess_failure(exc)}",
                )
        except Exception as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Could not identify user-installed packages: {describe_subprocess_failure(exc)}",
                )


class OrphanQueryWorker(ShutdownSafeRunnable):
    """Identify unneeded leaf packages via ``dnf5 repoquery --unneeded``."""

    def __init__(self):
        super().__init__()
        self.signals = BackendSignals()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
        if in_flatpak():
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "", flatpak_read_only_unavailable("Unneeded-package detection")
                )
            return
        host_env_fn = _backend_hook("get_host_environment", get_host_environment)
        environment = host_env_fn()
        dnf_bin_fn = _backend_hook("get_dnf_binary_path", get_dnf_binary_path)
        default_dnf_fn = _backend_hook(
            "_DEFAULT_GET_DNF_BINARY_PATH", _DEFAULT_GET_DNF_BINARY_PATH
        )
        if dnf_bin_fn is default_dnf_fn and not environment.supports_orphan_detection:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "", environment.feature_unavailable("Unneeded-package detection")
                )
            return
        dnf_bin = dnf_bin_fn()
        if not dnf_bin:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    "Unneeded-package detection requires an installed 'dnf5' or 'dnf' command on this RPM host.",
                )
            return

        try:
            cmd = [
                dnf_bin,
                "repoquery",
                "--unneeded",
                "--installed",
                "-q",
                "--queryformat",
                "%{name}\x1f%{epoch}\x1f%{version}\x1f%{release}\x1f%{arch}\n",
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=35,
            )
            if self._is_cancelled.is_set():
                return
            if res.returncode != 0:
                detail = (
                    res.stderr.strip()
                    or f"{dnf_bin} repoquery exited with status {res.returncode}."
                )
                self.signals.error_occurred.emit(
                    "", f"Could not identify unneeded packages: {detail}"
                )
                return
            orphan_nevras = _parse_dnf_package_identity_output(
                res.stdout, "DNF unneeded-package query"
            )
            self.signals.orphans_loaded.emit(orphan_nevras)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Could not identify unneeded packages: {describe_subprocess_failure(exc)}",
                )
        except Exception as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Could not identify unneeded packages: {describe_subprocess_failure(exc)}",
                )


class DnfHistoryWorker(ShutdownSafeRunnable):
    """Read DNF history via ``org.rpm.dnf.v0.History.list`` with CLI fallback."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Dnf5DaemonClient] = Dnf5DaemonClient,
    ) -> None:
        super().__init__()
        self.signals = BackendSignals()
        self._client_factory = client_factory
        self._is_cancelled = threading.Event()

    def cancel(self) -> None:
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self) -> None:
        if self._is_cancelled.is_set():
            return

        in_flatpak, effective_env, dnf_bin_fn, cli_hooked, client_factory = (
            _effective_runtime(self._client_factory)
        )
        custom_client = self._client_factory is not _DEFAULT_DNF5_DAEMON_CLIENT

        if (
            not custom_client
            and not cli_hooked
            and not effective_env.supports_dnf5_daemon_queries
            and not effective_env.supports_dnf_cli_queries
        ):
            self.signals.error_occurred.emit(
                "", effective_env.feature_unavailable("DNF history")
            )
            return

        if effective_env.supports_dnf5_daemon_queries or custom_client:
            client: Any = None
            try:
                client = client_factory()
                transactions = client.list_history()
                if self._is_cancelled.is_set():
                    return
                history_list = [
                    _history_entry_from_daemon_transaction(tx) for tx in transactions
                ]
            except Dnf5DaemonUnsupportedError:
                if in_flatpak:
                    self.signals.error_occurred.emit(
                        "",
                        "DNF history requires DNF5 5.4 or newer on the host daemon, "
                        "and host CLI fallback is unavailable in Flatpak.",
                    )
                    return
                if not cli_hooked and not effective_env.supports_dnf_cli_queries:
                    self.signals.error_occurred.emit(
                        "", effective_env.feature_unavailable("DNF history")
                    )
                    return
            except Dnf5DaemonUnavailable:
                if in_flatpak:
                    self.signals.error_occurred.emit(
                        "", flatpak_read_only_unavailable("DNF history")
                    )
                    return
                if not cli_hooked and not effective_env.supports_dnf_cli_queries:
                    self.signals.error_occurred.emit(
                        "", effective_env.feature_unavailable("DNF history")
                    )
                    return
            except Dnf5DaemonError as exc:
                self.signals.error_occurred.emit(
                    "", f"Could not read DNF history: {exc.detail}"
                )
                return
            except Exception as exc:
                self.signals.error_occurred.emit(
                    "", f"Could not read DNF history: {exc}"
                )
                return
            else:
                self.signals.history_loaded.emit(history_list)
                return
            finally:
                if client is not None:
                    _close_client_quietly(client)

        if in_flatpak:
            self.signals.error_occurred.emit(
                "", flatpak_read_only_unavailable("DNF history")
            )
            return
        if not cli_hooked and not effective_env.supports_dnf_cli_queries:
            self.signals.error_occurred.emit(
                "", effective_env.feature_unavailable("DNF history")
            )
            return

        dnf_bin = dnf_bin_fn()
        if not dnf_bin:
            self.signals.error_occurred.emit(
                "",
                "DNF history requires an installed 'dnf5' or 'dnf' command on this RPM host.",
            )
            return

        is_dnf5_fn = _backend_hook("_is_dnf5_binary", _is_dnf5_binary)
        uses_dnf5_json = is_dnf5_fn(dnf_bin)
        history_env = get_clean_env()
        command = [dnf_bin, "history", "list"]
        if uses_dnf5_json:
            command.append("--json")
        else:
            history_env["LC_ALL"] = "C"
            history_env["LANG"] = "C"

        try:
            res = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                env=history_env,
                check=False,
                timeout=15,
            )
            if uses_dnf5_json and _dnf5_json_option_is_unsupported(res):
                uses_dnf5_json = False
                history_env["LC_ALL"] = "C"
                history_env["LANG"] = "C"
                res = subprocess.run(
                    [dnf_bin, "history", "list"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=history_env,
                    check=False,
                    timeout=15,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Could not read DNF history: {describe_subprocess_failure(exc)}",
                )
            return

        if self._is_cancelled.is_set():
            return
        if res.returncode != 0:
            detail = (
                res.stderr.strip()
                or f"{dnf_bin} history list exited with status {res.returncode}."
            )
            self.signals.error_occurred.emit(
                "", f"Could not read DNF history: {detail}"
            )
            return

        if uses_dnf5_json:
            try:
                history_list = _parse_dnf5_history_json(res.stdout)
            except RuntimeError as exc:
                self.signals.error_occurred.emit(
                    "", f"Could not read DNF history: {exc}"
                )
                return
            self.signals.history_loaded.emit(history_list)
            return

        history_list = []
        malformed_line = ""
        for raw_line in res.stdout.splitlines():
            if self._is_cancelled.is_set():
                return
            line = raw_line.strip()
            if not line:
                continue
            normalized = line.casefold()
            if (
                "command line" in normalized
                and "date and time" in normalized
                and "action" in normalized
                and "altered" in normalized
            ):
                continue
            if all(character in "-|+ " for character in line):
                continue
            if normalized.startswith("no transactions"):
                continue

            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 5 or not parts[0].isdigit():
                malformed_line = line
                break
            try:
                altered_count = int(parts[4])
            except ValueError:
                malformed_line = line
                break
            if altered_count < 0:
                malformed_line = line
                break
            history_list.append(
                HistoryEntry(
                    id=int(parts[0]),
                    command_line=parts[1],
                    date_time=parts[2],
                    action=parts[3],
                    altered_count=altered_count,
                    return_code=0,
                )
            )

        if malformed_line:
            self.signals.error_occurred.emit(
                "",
                "Could not read DNF history: dnf history returned an unrecognized "
                f"record: {malformed_line}",
            )
            return
        self.signals.history_loaded.emit(history_list)


class SystemUpdatesCheckWorker(ShutdownSafeRunnable):
    """Fetch upgrade candidates through the typed DNF5 daemon API."""

    def __init__(
        self,
        force_refresh: bool = False,
        *,
        client_factory: Callable[[], Dnf5DaemonClient] = Dnf5DaemonClient,
    ):
        super().__init__()
        self.signals = BackendSignals()
        self.force_refresh = force_refresh
        self._client_factory = client_factory
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        if self._is_cancelled.is_set():
            return

        client: Any = None
        try:
            if self._client_factory is _DEFAULT_DNF5_DAEMON_CLIENT:
                host_env_fn = _backend_hook(
                    "get_host_environment", get_host_environment
                )
                environment = host_env_fn()
                if not environment.supports_dnf5_daemon_queries:
                    if not self._is_cancelled.is_set():
                        self.signals.error_occurred.emit(
                            "", environment.feature_unavailable("System update checks")
                        )
                    return

            if self.force_refresh:
                self.signals.status_update.emit(
                    "Refreshing DNF metadata through the system daemon…"
                )
            else:
                self.signals.status_update.emit(
                    "Checking DNF5 daemon for available updates…"
                )

            factory = (
                _backend_hook("Dnf5DaemonClient", Dnf5DaemonClient)
                if self._client_factory is _DEFAULT_DNF5_DAEMON_CLIENT
                else self._client_factory
            )
            client = factory()
            updates = client.list_available_updates(refresh=self.force_refresh)
            if self._is_cancelled.is_set():
                return

            updates_map = {
                update.name_arch: AvailableUpdateInfo(
                    name=update.name,
                    new_version=update.version,
                    new_release=update.release,
                    arch=update.arch,
                    epoch=update.epoch,
                    repository=update.repository or "Updates",
                )
                for update in updates
            }
        except Dnf5DaemonError as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit("", f"{exc.title}: {exc.detail}")
            return
        except Exception as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "", f"Unexpected update-query error: {type(exc).__name__}: {exc}"
                )
            return
        finally:
            if client is not None:
                _close_client_quietly(client)

        if not self._is_cancelled.is_set():
            self.signals.system_updates_loaded.emit(updates_map)


class RemotePackageSearchWorker(ShutdownSafeRunnable):
    """Query remote enabled repositories via ``Rpm.list_fd`` with CLI fallback."""

    def __init__(
        self,
        query: str,
        limit: int = 150,
        *,
        client_factory: Callable[[], Dnf5DaemonClient] = Dnf5DaemonClient,
    ):
        super().__init__()
        self.signals = BackendSignals()
        self.query = query.strip()
        self.limit = limit
        self._client_factory = client_factory
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    def _build_remote_packages(
        self, records, *, include_provides: bool
    ) -> list[PackageInfo]:
        appstream_cls = _backend_hook("AppStreamCatalog", AppStreamCatalog)
        comps_cls = _backend_hook("FedoraCompsCatalog", FedoraCompsCatalog)
        engine_cls = _backend_hook("ProductionTaxonomyEngine", ProductionTaxonomyEngine)
        fields_fn = _backend_hook(
            "classification_package_fields", classification_package_fields
        )
        appstream = appstream_cls.get_instance()
        comps = comps_cls.get_instance()

        results: list[PackageInfo] = []
        seen_name_arches: set[str] = set()
        for record in records:
            if len(results) >= self.limit or self._is_cancelled.is_set():
                break
            name_arch = f"{record.name}.{record.arch}"
            if name_arch in seen_name_arches:
                continue
            seen_name_arches.add(name_arch)

            anatomy = (
                PackagePhysicalAnatomy.from_manifest_data([], record.provides)
                if include_provides
                else None
            )
            decision = engine_cls.classify(
                name=record.name,
                summary=record.summary,
                anatomy=anatomy,
                provides=list(record.provides),
                appstream=appstream,
                comps=comps,
                input_scope=TaxonomyInputScope.REMOTE_METADATA,
            )

            results.append(
                PackageInfo(
                    name=record.name,
                    version=record.version,
                    release=record.release,
                    arch=record.arch,
                    summary=record.summary,
                    size_bytes=record.size_bytes,
                    state=PackageState.AVAILABLE,
                    repository=record.repository or "Repositories",
                    provides=list(record.provides),
                    **fields_fn(decision),
                )
            )
        return results

    @pyqtSlot()
    def run(self):
        if self._is_cancelled.is_set():
            return

        in_flatpak, effective_env, dnf_bin_fn, cli_hooked, client_factory = (
            _effective_runtime(self._client_factory)
        )
        custom_client = self._client_factory is not _DEFAULT_DNF5_DAEMON_CLIENT

        if (
            not custom_client
            and not cli_hooked
            and not effective_env.supports_dnf5_daemon_queries
            and not effective_env.supports_dnf_cli_queries
        ):
            self.signals.error_occurred.emit(
                "",
                effective_env.feature_unavailable("Remote repository search"),
            )
            return

        words = self.query.split()
        if len(words) > 1 and not any(c in self.query for c in ("*", "?")):
            spec = f"*{'*'.join(words)}*"
        elif "*" in self.query or "?" in self.query:
            spec = self.query
        else:
            spec = f"*{self.query}*"

        if effective_env.supports_dnf5_daemon_queries or custom_client:
            client: Any = None
            try:
                self.signals.status_update.emit(
                    f"Searching remote repositories for '{self.query}'..."
                )
                client = client_factory()
                raw_records = client.list_packages_fd(
                    scope="available",
                    patterns=(spec,),
                    package_attrs=(
                        "name",
                        "version",
                        "release",
                        "arch",
                        "repo_id",
                        "install_size",
                        "summary",
                        "provides",
                    ),
                    latest_limit=1,
                    with_src=True,
                    with_nevra=True,
                    with_provides=False,
                    with_filenames=False,
                    with_binaries=False,
                    icase=True,
                    load_system_repo=False,
                    load_available_repos=True,
                    cancel_event=self._is_cancelled,
                )
                if self._is_cancelled.is_set():
                    return
                records = _parse_remote_fd_records(raw_records)
                results = self._build_remote_packages(records, include_provides=True)
            except (Dnf5DaemonUnavailable, Dnf5DaemonUnsupportedError):
                if in_flatpak or (
                    not cli_hooked and not effective_env.supports_dnf_cli_queries
                ):
                    if not self._is_cancelled.is_set():
                        detail = (
                            flatpak_read_only_unavailable("Remote repository search")
                            if in_flatpak
                            else effective_env.feature_unavailable(
                                "Remote repository search"
                            )
                        )
                        self.signals.error_occurred.emit("", detail)
                    return
            except Dnf5DaemonError as exc:
                if not self._is_cancelled.is_set():
                    self.signals.error_occurred.emit(
                        "", f"Remote repository search failed: {exc.detail}"
                    )
                return
            except Exception as exc:
                if not self._is_cancelled.is_set():
                    self.signals.error_occurred.emit(
                        "",
                        f"Remote repository search failed: {type(exc).__name__}: {exc}",
                    )
                return
            else:
                if not self._is_cancelled.is_set():
                    self.signals.remote_search_finished.emit(self.query, results)
                return
            finally:
                if client is not None:
                    _close_client_quietly(client)

        if in_flatpak:
            self.signals.error_occurred.emit(
                "",
                flatpak_read_only_unavailable("Remote repository search"),
            )
            return
        if not cli_hooked and not effective_env.supports_dnf_cli_queries:
            self.signals.error_occurred.emit(
                "",
                effective_env.feature_unavailable("Remote repository search"),
            )
            return

        dnf_bin = dnf_bin_fn()
        if not dnf_bin:
            self.signals.error_occurred.emit(
                "",
                "Remote repository search requires an installed 'dnf5' or 'dnf' command on this RPM host.",
            )
            return

        is_dnf5_fn = _backend_hook("_is_dnf5_binary", _is_dnf5_binary)
        use_dnf5_capabilities = is_dnf5_fn(dnf_bin)
        if use_dnf5_capabilities:
            qf = (
                "%{name}\x1f%{version}\x1f%{release}\x1f%{arch}\x1f"
                "%{repoid}\x1f%{installsize}\x1f%{summary}\x1f%{provides}\x1e"
            )
        else:
            qf = (
                "%{name}\x1f%{version}\x1f%{release}\x1f%{arch}\x1f"
                "%{repo}\x1f%{size}\x1f%{summary}\n"
            )
        cmd = [
            dnf_bin,
            "repoquery",
            "--available",
            *(("--latest-limit=1",) if use_dnf5_capabilities else ()),
            "-q",
            f"--queryformat={qf}",
            "--",
            spec,
        ]

        try:
            self.signals.status_update.emit(
                f"Searching remote repositories for '{self.query}'..."
            )
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=20,
            )
            if self._is_cancelled.is_set():
                return

            results: list[PackageInfo] = []
            if res.returncode != 0:
                detail = (
                    res.stderr.strip()
                    or f"{dnf_bin} repoquery exited with status {res.returncode}."
                )
                self.signals.error_occurred.emit(
                    "", f"Remote repository search failed: {detail}"
                )
                return

            if res.stdout.strip():
                records = _parse_remote_package_records(
                    res.stdout,
                    includes_provides=use_dnf5_capabilities,
                )
                results = self._build_remote_packages(
                    records, include_provides=use_dnf5_capabilities
                )

            if not self._is_cancelled.is_set():
                self.signals.remote_search_finished.emit(self.query, results)

        except (OSError, subprocess.TimeoutExpired) as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Remote repository search failed: {type(exc).__name__}: {exc}",
                )
        except Exception as exc:
            if not self._is_cancelled.is_set():
                self.signals.error_occurred.emit(
                    "",
                    f"Remote repository search failed: {type(exc).__name__}: {exc}",
                )


__all__ = (
    "_DEFAULT_GET_DNF_BINARY_PATH",
    "DnfHistoryWorker",
    "OrphanQueryWorker",
    "RemotePackageSearchWorker",
    "SecurityAdvisoryWorker",
    "SystemUpdatesCheckWorker",
    "UserInstalledQueryWorker",
    "get_dnf_binary_path",
    "get_host_environment",
)
