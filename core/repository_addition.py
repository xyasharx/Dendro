"""Validated, narrowly privileged installation of third-party repository files.

The DNF5 daemon API can list repositories and change the enabled state of an
existing one, but it does not create persistent repository configuration files.
This module deliberately does not invoke ``dnf``, a shell, or an arbitrary
host command. It downloads a direct HTTPS ``.repo`` file as the unprivileged
user, then sends its bounded UTF-8 payload to Dendro's fixed Polkit helper.
The helper independently validates and atomically creates one new file below
``/etc/yum.repos.d``.
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import ssl
import threading
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QRunnable, QThreadPool, pyqtSignal, pyqtSlot

from core.dnf5daemon import Dnf5DaemonValidationError
from core.operation_gate import HostOperationGate
from core.repository_deletion import DEFAULT_HELPER_PATH, DEFAULT_PKEXEC_PATH


MAX_REPOSITORY_URL_LENGTH = 4096
MAX_REPOSITORY_CONFIGURATION_BYTES = 256 * 1024
MAX_REDIRECTS = 3
# A transport deadline bounds an untrusted remote read; lifecycle completion is
# still signal-driven and never coordinated through a timer or polling loop.
NETWORK_TIMEOUT_SECONDS = 30
_FILENAME_SAFE = re.compile(r"[^a-z0-9._-]+")


class RepositoryConfigDownloadError(Dnf5DaemonValidationError):
    """A URL or downloaded repository configuration failed validation."""


@dataclass(frozen=True, slots=True)
class DownloadedRepositoryConfiguration:
    """Unprivileged, validated repository configuration awaiting authorization."""

    source_url: str
    destination_filename: str
    payload: bytes


def validate_repository_configuration_url(value: str) -> str:
    """Accept one direct HTTPS repository-configuration URL without credentials.

    The URL is downloaded before privilege escalation. Restricting it to HTTPS
    avoids local-file and cleartext schemes, while rejecting embedded
    credentials prevents accidental password disclosure through process logs or
    configuration files.
    """
    if not isinstance(value, str) or not value:
        raise RepositoryConfigDownloadError(
            "Invalid repository address",
            "Enter a non-empty direct HTTPS URL for a .repo configuration file.",
        )
    if value != value.strip():
        raise RepositoryConfigDownloadError(
            "Invalid repository address",
            "Repository addresses cannot have leading or trailing whitespace.",
        )
    if len(value) > MAX_REPOSITORY_URL_LENGTH:
        raise RepositoryConfigDownloadError(
            "Invalid repository address",
            f"Repository addresses cannot exceed {MAX_REPOSITORY_URL_LENGTH} characters.",
        )
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise RepositoryConfigDownloadError(
            "Invalid repository address",
            "Repository addresses cannot contain whitespace or control characters.",
        )
    if any(character in value for character in ('\\', '<', '>', '"', '`')):
        raise RepositoryConfigDownloadError(
            "Invalid repository address",
            "Repository addresses contain unsupported URL characters.",
        )

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RepositoryConfigDownloadError("Invalid repository address", f"The URL is malformed: {exc}") from exc

    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise RepositoryConfigDownloadError(
            "Invalid repository address",
            "Only direct HTTPS repository-configuration URLs are accepted.",
        )
    if parsed.username is not None or parsed.password is not None:
        raise RepositoryConfigDownloadError(
            "Invalid repository address",
            "Repository addresses must not contain embedded credentials.",
        )
    if parsed.fragment:
        raise RepositoryConfigDownloadError(
            "Invalid repository address",
            "Repository addresses must not contain a fragment.",
        )
    if port is not None and not 1 <= port <= 65535:
        raise RepositoryConfigDownloadError("Invalid repository address", "The URL port is outside the valid range.")
    return value


def repository_filename_for_url(url: str) -> str:
    """Derive a stable, helper-validated filename without trusting URL paths."""
    normalized_url = validate_repository_configuration_url(url)
    hostname = urlsplit(normalized_url).hostname or "repository"
    try:
        host_label = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise RepositoryConfigDownloadError(
            "Invalid repository address",
            "The URL hostname cannot be normalized safely.",
        ) from exc
    host_label = _FILENAME_SAFE.sub("-", host_label).strip(".-") or "repository"
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:16]
    return f"dendro-{host_label[:48]}-{digest}.repo"


def validate_downloaded_repository_configuration(payload: bytes) -> bytes:
    """Validate transport-level invariants before any Polkit request.

    The privileged helper repeats stricter semantic validation. This early
    check prevents non-text, oversized, or malformed data from needlessly
    reaching the authorization boundary.
    """
    if not isinstance(payload, bytes) or not payload:
        raise RepositoryConfigDownloadError(
            "Repository download failed",
            "The server returned an empty repository configuration file.",
        )
    if len(payload) > MAX_REPOSITORY_CONFIGURATION_BYTES:
        raise RepositoryConfigDownloadError(
            "Repository download failed",
            f"Repository configuration files cannot exceed {MAX_REPOSITORY_CONFIGURATION_BYTES // 1024} KiB.",
        )
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RepositoryConfigDownloadError(
            "Repository download failed",
            "The repository configuration is not valid UTF-8 text.",
        ) from exc
    if not text.strip():
        raise RepositoryConfigDownloadError(
            "Repository download failed",
            "The server returned an empty repository configuration file.",
        )
    if "\x00" in text or any(ord(character) < 32 and character not in "\t\r\n" for character in text):
        raise RepositoryConfigDownloadError(
            "Repository download failed",
            "The repository configuration contains unsupported control characters.",
        )
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


class _HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    """Bound redirects to validated HTTPS targets without copying credentials."""

    def __init__(self, cancellation: threading.Event) -> None:
        super().__init__()
        self._cancellation = cancellation
        self._redirect_count = 0

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # type: ignore[no-untyped-def]
        if self._cancellation.is_set():
            raise RepositoryConfigDownloadError(
                "Repository download cancelled",
                "The repository download was cancelled.",
            )
        self._redirect_count += 1
        if self._redirect_count > MAX_REDIRECTS:
            raise RepositoryConfigDownloadError(
                "Repository download failed",
                f"The repository URL redirected more than {MAX_REDIRECTS} times.",
            )
        validated_url = validate_repository_configuration_url(str(new_url))
        return super().redirect_request(request, file_pointer, code, message, headers, validated_url)


def _download_repository_configuration(url: str, cancellation: threading.Event) -> DownloadedRepositoryConfiguration:
    """Download one bounded configuration on a worker thread as the user."""
    source_url = validate_repository_configuration_url(url)
    if cancellation.is_set():
        raise RepositoryConfigDownloadError("Repository download cancelled", "The repository download was cancelled.")

    request = Request(
        source_url,
        headers={
            "Accept": "text/plain, text/*, application/octet-stream;q=0.5",
            "User-Agent": "Dendro repository configuration downloader",
        },
        method="GET",
    )
    opener = build_opener(_HTTPSOnlyRedirectHandler(cancellation))
    try:
        with opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise RepositoryConfigDownloadError(
                    "Repository download failed",
                    f"The repository server returned HTTP status {status}.",
                )
            final_url = validate_repository_configuration_url(str(response.geturl()))
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    advertised_length = int(content_length)
                except ValueError as exc:
                    raise RepositoryConfigDownloadError(
                        "Repository download failed",
                        "The repository server returned an invalid Content-Length header.",
                    ) from exc
                if advertised_length < 0 or advertised_length > MAX_REPOSITORY_CONFIGURATION_BYTES:
                    raise RepositoryConfigDownloadError(
                        "Repository download failed",
                        f"The repository configuration exceeds {MAX_REPOSITORY_CONFIGURATION_BYTES // 1024} KiB.",
                    )

            chunks: list[bytes] = []
            total = 0
            while True:
                if cancellation.is_set():
                    raise RepositoryConfigDownloadError(
                        "Repository download cancelled",
                        "The repository download was cancelled.",
                    )
                chunk = response.read(min(64 * 1024, MAX_REPOSITORY_CONFIGURATION_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_REPOSITORY_CONFIGURATION_BYTES:
                    raise RepositoryConfigDownloadError(
                        "Repository download failed",
                        f"The repository configuration exceeds {MAX_REPOSITORY_CONFIGURATION_BYTES // 1024} KiB.",
                    )
                chunks.append(chunk)
    except RepositoryConfigDownloadError:
        raise
    except HTTPError as exc:
        raise RepositoryConfigDownloadError(
            "Repository download failed",
            f"The repository server returned HTTP status {exc.code}.",
        ) from exc
    except (URLError, ssl.SSLError, socket.timeout, TimeoutError, OSError, ValueError) as exc:
        if cancellation.is_set():
            raise RepositoryConfigDownloadError(
                "Repository download cancelled",
                "The repository download was cancelled.",
            ) from exc
        raise RepositoryConfigDownloadError(
            "Repository download failed",
            f"Could not download the repository configuration over HTTPS: {exc}",
        ) from exc

    payload = validate_downloaded_repository_configuration(b"".join(chunks))
    return DownloadedRepositoryConfiguration(
        source_url=final_url,
        destination_filename=repository_filename_for_url(final_url),
        payload=payload,
    )


class RepositoryConfigFetchSignals(QObject):
    """One terminal result for an unprivileged repository-config download."""

    downloaded = pyqtSignal(object)
    failed = pyqtSignal(str, str)


class RepositoryConfigFetchWorker(QRunnable):
    """Fetch untrusted repository bytes without blocking the GUI event loop."""

    def __init__(self, url: str, cancellation: threading.Event) -> None:
        super().__init__()
        self._url = url
        self._cancellation = cancellation
        self.signals = RepositoryConfigFetchSignals()
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self) -> None:
        try:
            configuration = _download_repository_configuration(self._url, self._cancellation)
        except RepositoryConfigDownloadError as exc:
            self.signals.failed.emit(exc.title, exc.detail)
        except Exception as exc:  # Every QRunnable path must report one terminal result.
            self.signals.failed.emit("Repository download failed", f"{type(exc).__name__}: {exc}")
        else:
            self.signals.downloaded.emit(configuration)


class RepositoryAdditionRunner(QObject):
    """Own the download-to-helper lifecycle for one new repository file.

    The runner holds the shared host-operation gate only after the unprivileged
    download has completed. A slow remote server must not unnecessarily block a
    pending DNF transaction, while the privileged file mutation itself remains
    serialized with package and repository-delete operations.
    """

    log_received = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(
        self,
        *,
        helper_path: str = DEFAULT_HELPER_PATH,
        flatpak_detector: Callable[[], bool] | None = None,
        host_operation_gate: HostOperationGate | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._helper_path = helper_path
        self._flatpak_detector = flatpak_detector or (lambda: os.path.exists("/.flatpak-info"))
        self._host_operation_gate = host_operation_gate
        self._thread_pool = thread_pool or QThreadPool.globalInstance()
        self._host_operation_owner = "repository-file-addition"
        self._process: QProcess | None = None
        self._configuration: DownloadedRepositoryConfiguration | None = None
        self._payload_offset = 0
        self._cancellation: threading.Event | None = None
        self._process_failure_detail: str | None = None
        self._cancel_requested = False
        self._phase = "idle"
        self._finished = True
        self._gate_claimed = False

    @property
    def is_running(self) -> bool:
        return not self._finished

    def add_from_url(self, url: str) -> None:
        source_url = validate_repository_configuration_url(url)
        if self.is_running:
            raise Dnf5DaemonValidationError(
                "Repository addition already active",
                "Wait for the active repository addition to finish before starting another one.",
            )
        if self._flatpak_detector():
            raise Dnf5DaemonValidationError(
                "Repository addition is unavailable in Flatpak",
                "Flatpak Dendro can use the narrowly permitted DNF5 system service, but it cannot "
                "launch a host privileged helper.",
            )
        if not os.path.isfile(self._helper_path) or not os.access(self._helper_path, os.X_OK):
            raise Dnf5DaemonValidationError(
                "Repository addition helper is unavailable",
                f"The RPM-host helper is not installed or executable: {self._helper_path}",
            )

        self._finished = False
        self._phase = "downloading"
        self._configuration = None
        self._payload_offset = 0
        self._process_failure_detail = None
        self._cancel_requested = False
        self._cancellation = threading.Event()
        self.log_received.emit("Downloading repository configuration as the current user…\n")
        try:
            worker = RepositoryConfigFetchWorker(source_url, self._cancellation)
            worker.signals.downloaded.connect(self._on_configuration_downloaded)
            worker.signals.failed.connect(self._on_configuration_download_failed)
            self._thread_pool.start(worker)
        except Exception as exc:
            self._finish(False, f"Could not start repository download worker: {type(exc).__name__}: {exc}")

    def cancel(self) -> None:
        """Cancel downloading or request graceful termination of the fixed helper."""
        if not self.is_running:
            return
        if self._phase == "downloading":
            if self._cancellation is not None and not self._cancellation.is_set():
                self.log_received.emit("Requesting cancellation of repository configuration download…\n")
                self._cancellation.set()
            # The worker owns the response and reports the terminal result
            # after closing it. Do not release the runner lifecycle early.
            return
        if self._process is not None:
            self._cancel_requested = True
            self.log_received.emit("Requesting cancellation of repository configuration installation…\n")
            self._process.terminate()

    def _on_configuration_downloaded(self, configuration: object) -> None:
        if not self.is_running or self._phase != "downloading":
            return
        if self._cancellation is not None and self._cancellation.is_set():
            self._finish(False, "Repository configuration download cancelled.")
            return
        if not isinstance(configuration, DownloadedRepositoryConfiguration):
            self._finish(False, "Repository downloader returned an invalid configuration result.")
            return
        try:
            self._claim_host_operation()
            self._configuration = configuration
            self._phase = "authorizing"
            self._start_helper()
        except Dnf5DaemonValidationError as exc:
            self._finish(False, exc.detail)
        except Exception as exc:
            self._finish(False, f"Could not start repository helper: {type(exc).__name__}: {exc}")

    def _on_configuration_download_failed(self, title: str, detail: str) -> None:
        if not self.is_running or self._phase != "downloading":
            return
        if self._cancellation is not None and self._cancellation.is_set():
            self._finish(False, "Repository configuration download cancelled.")
            return
        self._finish(False, f"{title}: {detail}")

    def _start_helper(self) -> None:
        configuration = self._configuration
        if configuration is None:
            raise Dnf5DaemonValidationError(
                "Repository addition failed",
                "No validated repository configuration is available.",
            )

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        environment = QProcessEnvironment.systemEnvironment()
        for variable in (
            "LD_LIBRARY_PATH",
            "PYTHONPATH",
            "PYTHONHOME",
            "QT_PLUGIN_PATH",
            "QT_QPA_PLATFORM_PLUGIN_PATH",
        ):
            environment.remove(variable)
        process.setProcessEnvironment(environment)
        process.started.connect(self._write_configuration)
        process.bytesWritten.connect(self._write_configuration)
        process.readyReadStandardOutput.connect(self._read_output)
        process.errorOccurred.connect(self._on_process_error)
        process.finished.connect(self._on_process_finished)
        self._process = process

        if os.geteuid() == 0:
            self.log_received.emit("Installing validated repository configuration with the fixed Dendro helper…\n")
            process.start(self._helper_path, ["add", configuration.destination_filename])
        else:
            self.log_received.emit("Requesting authorization for the fixed Dendro repository helper…\n")
            process.start(DEFAULT_PKEXEC_PATH, [self._helper_path, "add", configuration.destination_filename])

    def _write_configuration(self, _written: int = 0) -> None:
        process = self._process
        configuration = self._configuration
        if process is not None and self._cancel_requested:
            process.terminate()
            return
        if (
            process is None
            or configuration is None
            or self._finished
            or self._process_failure_detail is not None
        ):
            return
        if self._payload_offset >= len(configuration.payload):
            process.closeWriteChannel()
            return

        accepted = process.write(configuration.payload[self._payload_offset:])
        if accepted <= 0:
            # Do not release the host-operation gate until QProcess reports a
            # terminal child state. Closing stdin lets the fixed helper reject
            # a truncated payload and exit without force-killing it.
            self._process_failure_detail = "Could not send the repository configuration to the privileged helper."
            process.closeWriteChannel()
            return
        self._payload_offset += accepted
        if self._payload_offset >= len(configuration.payload):
            process.closeWriteChannel()

    def _claim_host_operation(self) -> None:
        if self._host_operation_gate is None:
            return
        conflict = self._host_operation_gate.claim(
            self._host_operation_owner,
            "repository-file addition",
        )
        if conflict is not None:
            raise Dnf5DaemonValidationError(
                "Another host package operation is active",
                f"Cannot add a repository file while {conflict.label} is active.",
            )
        self._gate_claimed = True

    def _release_host_operation(self) -> None:
        if self._host_operation_gate is None or not self._gate_claimed:
            return
        self._host_operation_gate.release(self._host_operation_owner)
        self._gate_claimed = False

    def _read_output(self) -> None:
        process = self._process
        if process is None:
            return
        data = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if data:
            self.log_received.emit(data)

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        process = self._process
        detail = process.errorString() if process is not None else str(error)
        message = f"Repository helper process error: {detail}"
        if error == QProcess.ProcessError.FailedToStart:
            # Qt does not guarantee a finished signal when exec never starts.
            self._finish(False, message)
            return
        # Crashes and stream errors can be delivered before finished. Preserve
        # the failure but retain the shared gate until QProcess confirms that
        # the privileged child is terminal.
        self._process_failure_detail = message

    def _on_process_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        if self._process is not None:
            self._read_output()
        if self._process_failure_detail:
            self._finish(False, self._process_failure_detail)
            return
        if exit_status != QProcess.ExitStatus.NormalExit:
            self._finish(False, "Repository helper terminated unexpectedly.")
            return
        if exit_code != 0:
            self._finish(False, f"Repository helper exited with status {exit_code}.")
            return
        self._finish(True, "")

    def _finish(self, success: bool, detail: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._phase = "idle"
        self._cancellation = None
        self._configuration = None
        self._payload_offset = 0
        self._process_failure_detail = None
        self._cancel_requested = False
        process = self._process
        self._process = None
        if process is not None:
            process.deleteLater()
        self._release_host_operation()
        self.finished.emit(success, detail)