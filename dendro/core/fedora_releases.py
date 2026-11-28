# dendro/core/fedora_releases.py
"""Detect the running Fedora release and discover upgrade targets safely.

Current release metadata comes strictly from local read-only ``os-release``
files (or informational daemon offline status when running inside Flatpak).
Available target releases are fetched over HTTPS from Fedora's public Bodhi and
``releases.json`` feeds in a background worker, falling back deterministically to
local ``N+1`` / ``N+2`` / ``rawhide`` suggestions whenever offline or blocked.
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from dendro.core.dnf5daemon_types import OfflineTransactionStatus, _normalise_releasever
from dendro.core.host_environment import (
    OS_RELEASE_PATHS,
    HostEnvironmentProbe,
    SystemHostEnvironmentProbe,
    parse_os_release,
    sanitize_display_text,
)
from dendro.core.i18n import tr
from dendro.core.process_utils import is_running_in_flatpak
from dendro.core.worker_lifecycle import ShutdownSafeRunnable

FLATPAK_HOST_OS_RELEASE_PATH: Final[str] = "/run/host/os-release"
BODHI_RELEASES_URL: Final[str] = (
    "https://bodhi.fedoraproject.org/releases/?rows_per_page=100"
)
FEDORA_RELEASES_JSON_URL: Final[str] = "https://fedoraproject.org/releases.json"

MAX_RELEASE_METADATA_BYTES: Final[int] = 2 * 1024 * 1024
MAX_REDIRECT_BODY_BYTES: Final[int] = 64 * 1024
MAX_REDIRECTS: Final[int] = 3
NETWORK_TIMEOUT_SECONDS: Final[float] = 6.0
NETWORK_TOTAL_TIMEOUT_SECONDS: Final[float] = 10.0

_SAFE_RELEASEVER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$"
)
_NUMERIC_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)(?:\b.*)?$")
_monotonic = time.monotonic


@dataclass(frozen=True, slots=True)
class CurrentFedoraRelease:
    """Informational snapshot of the running host release."""

    distro_id: str = ""
    version_id: str = ""
    pretty_name: str = ""
    variant_id: str = ""

    @property
    def is_fedora(self) -> bool:
        return self.distro_id.casefold() == "fedora"

    @property
    def is_rawhide(self) -> bool:
        return self.version_id.casefold() == "rawhide"

    @property
    def numeric_version(self) -> int | None:
        return int(self.version_id) if self.version_id.isdigit() else None

    @property
    def display_label(self) -> str:
        if self.pretty_name:
            return self.pretty_name
        if self.version_id:
            return tr("Fedora Linux {version}").format(version=self.version_id)
        return tr("Fedora Linux (release unknown)")


@dataclass(frozen=True, slots=True)
class FedoraTargetRelease:
    """One candidate Fedora target ``$releasever`` for ``Rpm.system_upgrade``."""

    releasever: str
    label: str
    status: str
    status_badge: str
    detail: str
    eol: str = ""
    is_prerelease: bool = False


@dataclass(frozen=True, slots=True)
class FedoraReleaseDiscoveryResult:
    """Outcome of online + local Fedora target release discovery."""

    targets: tuple[FedoraTargetRelease, ...]
    source: str  # "online" or "local"
    notice: str = ""


def detect_current_fedora_release(
    probe: HostEnvironmentProbe | None = None,
    *,
    in_flatpak: bool | None = None,
    offline_status: OfflineTransactionStatus | None = None,
) -> CurrentFedoraRelease:
    """Read host ``os-release`` metadata safely without touching capability gates."""
    active_probe = probe if probe is not None else SystemHostEnvironmentProbe()
    sandboxed = is_running_in_flatpak() if in_flatpak is None else bool(in_flatpak)
    candidate_paths = (FLATPAK_HOST_OS_RELEASE_PATH,) if sandboxed else OS_RELEASE_PATHS

    fields: dict[str, str] = {}
    for path in candidate_paths:
        content = active_probe.read_bounded_text(path, 64 * 1024)
        if content:
            fields = parse_os_release(content)
            break

    distro_id = sanitize_display_text(fields.get("ID", ""), max_length=32).lower()
    version_id = sanitize_display_text(fields.get("VERSION_ID", ""), max_length=32)
    pretty_name = sanitize_display_text(fields.get("PRETTY_NAME", ""), max_length=120)
    variant_id = sanitize_display_text(fields.get("VARIANT_ID", ""), max_length=48)

    if (
        not version_id
        and offline_status is not None
        and offline_status.system_releasever
    ):
        version_id = sanitize_display_text(
            offline_status.system_releasever, max_length=32
        )
        if not distro_id:
            distro_id = "fedora"

    return CurrentFedoraRelease(
        distro_id=distro_id,
        version_id=version_id,
        pretty_name=pretty_name,
        variant_id=variant_id,
    )


def validate_target_releasever(
    target_releasever: str, current: CurrentFedoraRelease
) -> tuple[str, str, str]:
    """Validate a target ``$releasever`` and return ``(normalized, error, warning)``."""
    stripped = (target_releasever or "").strip()
    if not stripped:
        return "", tr("Select or enter a target Fedora release version."), ""
    try:
        normalized = _normalise_releasever(stripped)
    except Exception as exc:
        detail = getattr(exc, "detail", str(exc))
        return "", detail, ""

    if current.version_id and normalized.casefold() == current.version_id.casefold():
        return (
            normalized,
            tr(
                "Target release {target} matches the currently running system version. "
                "Choose a newer Fedora release."
            ).format(target=normalized),
            "",
        )

    if (
        current.numeric_version is not None
        and normalized.isdigit()
        and int(normalized) < current.numeric_version
    ):
        return (
            normalized,
            tr(
                "Target release {target} is older than the running Fedora {current}. "
                "DNF5 system release upgrades only move forward."
            ).format(target=normalized, current=current.version_id),
            "",
        )

    warning = ""
    if normalized.casefold() == "rawhide":
        warning = tr(
            "Fedora Rawhide is the rolling development branch and can experience "
            "frequent breakage or dependency churn."
        )
    elif (
        current.numeric_version is not None
        and normalized.isdigit()
        and int(normalized) - current.numeric_version > 2
    ):
        warning = tr(
            "Jumping more than two Fedora releases in a single step is untested upstream; "
            "consider upgrading sequentially (N+1 or N+2)."
        )
    return normalized, "", warning


def build_local_fallback_targets(
    current: CurrentFedoraRelease,
) -> tuple[FedoraTargetRelease, ...]:
    """Build deterministic N+1, N+2, and Rawhide candidates from local OS info."""
    if current.is_rawhide:
        return ()
    if current.numeric_version is None:
        return ()
    n = current.numeric_version
    return (
        FedoraTargetRelease(
            releasever=str(n + 1),
            label=tr("Fedora Linux {version}").format(version=n + 1),
            status="suggested",
            status_badge=tr("Next Release"),
            detail=tr("Standard next major Fedora release upgrade target (N+1)."),
            is_prerelease=False,
        ),
        FedoraTargetRelease(
            releasever=str(n + 2),
            label=tr("Fedora Linux {version}").format(version=n + 2),
            status="branched",
            status_badge=tr("N+2 / Branched"),
            detail=tr(
                "Two-release jump or upcoming branched Fedora release (if available on mirrors)."
            ),
            is_prerelease=True,
        ),
        FedoraTargetRelease(
            releasever="rawhide",
            label=tr("Fedora Rawhide"),
            status="rawhide",
            status_badge=tr("Development"),
            detail=tr(
                "Rolling development branch; intended for testing and early adopters."
            ),
            is_prerelease=True,
        ),
    )


def _target_sort_key(target: FedoraTargetRelease) -> tuple[int, int, str]:
    tier_order = {
        "stable": 0,
        "suggested": 1,
        "beta": 2,
        "branched": 3,
        "rawhide": 4,
    }.get(target.status, 5)
    numeric = int(target.releasever) if target.releasever.isdigit() else 999_999
    return (tier_order, numeric, target.releasever.casefold())


def _is_valid_candidate_version(version: str, current: CurrentFedoraRelease) -> bool:
    if not version or not _SAFE_RELEASEVER_RE.fullmatch(version):
        return False
    if ".." in version:
        return False
    if current.version_id and version.casefold() == current.version_id.casefold():
        return False
    if (
        current.numeric_version is not None
        and version.isdigit()
        and int(version) <= current.numeric_version
    ):
        return False
    return not (current.is_rawhide and version.isdigit())


def parse_bodhi_releases(
    payload: Any, current: CurrentFedoraRelease
) -> tuple[FedoraTargetRelease, ...]:
    """Parse Fedora releases from Bodhi's ``/releases/`` JSON payload."""
    if not isinstance(payload, Mapping):
        return ()
    raw_releases = payload.get("releases")
    if isinstance(raw_releases, (str, bytes)) or not isinstance(raw_releases, Sequence):
        return ()

    candidates: dict[str, FedoraTargetRelease] = {}
    for entry in raw_releases:
        if not isinstance(entry, Mapping):
            continue
        id_prefix = str(entry.get("id_prefix") or "").strip().upper()
        if id_prefix != "FEDORA":
            continue
        raw_version = sanitize_display_text(
            str(entry.get("version") or ""), max_length=32
        ).strip()
        version = (
            raw_version.lower() if raw_version.casefold() == "rawhide" else raw_version
        )
        if not _is_valid_candidate_version(version, current):
            continue
        state = str(entry.get("state") or "").strip().casefold()
        if state not in {"current", "frozen", "pending"}:
            continue

        eol = sanitize_display_text(str(entry.get("eol") or ""), max_length=32).strip()
        long_name = sanitize_display_text(
            str(entry.get("long_name") or ""), max_length=80
        ).strip()
        if version == "rawhide":
            label = long_name or tr("Fedora Rawhide")
            status = "rawhide"
            status_badge = tr("Development")
            detail = tr(
                "Rolling development branch reported by Fedora Bodhi (unstable)."
            )
            is_prerelease = True
        elif state == "current":
            label = long_name or tr("Fedora Linux {version}").format(version=version)
            status = "stable"
            status_badge = tr("Stable")
            detail = (
                tr("Supported stable release (EOL {eol}).").format(eol=eol)
                if eol
                else tr("Supported stable Fedora release.")
            )
            is_prerelease = False
        elif state == "frozen":
            label = long_name or tr("Fedora Linux {version}").format(version=version)
            status = "beta"
            status_badge = tr("Beta / Freeze")
            detail = tr("Pre-release milestone currently in Fedora release freeze.")
            is_prerelease = True
        else:
            label = long_name or tr("Fedora Linux {version}").format(version=version)
            status = "branched"
            status_badge = tr("Branched")
            detail = tr("Upcoming branched Fedora release prior to final release.")
            is_prerelease = True

        candidates[version] = FedoraTargetRelease(
            releasever=version,
            label=label,
            status=status,
            status_badge=status_badge,
            detail=detail,
            eol=eol,
            is_prerelease=is_prerelease,
        )

    return tuple(sorted(candidates.values(), key=_target_sort_key))


def parse_fedora_releases_json(
    payload: Any, current: CurrentFedoraRelease
) -> tuple[FedoraTargetRelease, ...]:
    """Parse Fedora targets from ``https://fedoraproject.org/releases.json``."""
    if isinstance(payload, (str, bytes)) or not isinstance(payload, Sequence):
        return ()

    candidates: dict[str, FedoraTargetRelease] = {}
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        raw_version = sanitize_display_text(
            str(entry.get("version") or ""), max_length=48
        ).strip()
        if not raw_version:
            continue
        lowered = raw_version.casefold()
        if lowered == "rawhide":
            version = "rawhide"
            is_prerelease = True
        else:
            match = _NUMERIC_PREFIX_RE.match(raw_version)
            if match is None:
                continue
            version = match.group(1)
            is_prerelease = any(
                tag in lowered for tag in ("beta", "alpha", "rc", "test", "branched")
            )
        if not _is_valid_candidate_version(version, current):
            continue

        if version == "rawhide":
            candidate = FedoraTargetRelease(
                releasever="rawhide",
                label=tr("Fedora Rawhide"),
                status="rawhide",
                status_badge=tr("Development"),
                detail=tr("Rolling development branch reported by Fedora metadata."),
                is_prerelease=True,
            )
        elif is_prerelease:
            candidate = FedoraTargetRelease(
                releasever=version,
                label=tr("Fedora Linux {version}").format(version=version),
                status="beta",
                status_badge=tr("Beta / Pre-release"),
                detail=tr(
                    "Pre-release build ({raw}) listed in Fedora release metadata."
                ).format(raw=raw_version),
                is_prerelease=True,
            )
        else:
            candidate = FedoraTargetRelease(
                releasever=version,
                label=tr("Fedora Linux {version}").format(version=version),
                status="stable",
                status_badge=tr("Stable"),
                detail=tr("Official Fedora release listed in Fedora release metadata."),
                is_prerelease=False,
            )

        existing = candidates.get(version)
        if existing is None or (existing.is_prerelease and not candidate.is_prerelease):
            candidates[version] = candidate

    return tuple(sorted(candidates.values(), key=_target_sort_key))


class _BoundedRedirectBody:
    """Bound HTTP redirect bodies so oversized error pages cannot stall discovery."""

    def __init__(
        self, stream: Any, cancellation: threading.Event, deadline: float
    ) -> None:
        self._stream = stream
        self._cancellation = cancellation
        self._deadline = deadline

    def read(self, amount: int | None = None) -> bytes:
        if amount is not None:
            return self._stream.read(amount)
        remaining = MAX_REDIRECT_BODY_BYTES + 1
        while remaining > 0:
            if self._cancellation.is_set() or _monotonic() > self._deadline:
                self.close()
                return b""
            chunk = self._stream.read(min(16 * 1024, remaining))
            if not chunk:
                return b""
            remaining -= len(chunk)
        self.close()
        return b""

    def close(self) -> None:
        self._stream.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    """Allow at most ``MAX_REDIRECTS`` HTTPS redirects without credential leakage."""

    def __init__(self, cancellation: threading.Event, deadline: float) -> None:
        super().__init__()
        self._cancellation = cancellation
        self._deadline = deadline
        self._redirect_count = 0

    def http_error_302(self, request, file_pointer, code, message, headers):  # type: ignore[no-untyped-def]
        bounded = (
            _BoundedRedirectBody(file_pointer, self._cancellation, self._deadline)
            if file_pointer is not None
            else None
        )
        return super().http_error_302(request, bounded, code, message, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # type: ignore[no-untyped-def]
        if self._cancellation.is_set() or _monotonic() > self._deadline:
            raise URLError("Release metadata request cancelled or timed out")
        self._redirect_count += 1
        if self._redirect_count > MAX_REDIRECTS:
            raise URLError("Too many HTTP redirects")
        parsed = urlsplit(str(new_url or ""))
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            raise URLError("Refusing non-HTTPS redirect")
        if parsed.username is not None or parsed.password is not None:
            raise URLError("Refusing redirect containing embedded credentials")
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )
        if redirected is not None:
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


def _fetch_https_json(url: str, *, cancellation: threading.Event | None = None) -> Any:
    """Download and parse bounded UTF-8 JSON over HTTPS with strict verification."""
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ValueError(f"Unsupported URL scheme for release metadata: {url!r}")

    cancel_ev = cancellation if cancellation is not None else threading.Event()
    deadline = _monotonic() + NETWORK_TOTAL_TIMEOUT_SECONDS
    opener = build_opener(
        _HTTPSOnlyRedirectHandler(cancel_ev, deadline),
        HTTPSHandler(context=ssl.create_default_context()),
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Dendro",
        },
        method="GET",
    )
    chunks: list[bytes] = []
    total = 0
    with opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
        final_parsed = urlsplit(str(getattr(response, "geturl", lambda: url)()))
        if final_parsed.scheme.casefold() != "https":
            raise URLError("Redirected outside HTTPS")
        while True:
            if cancel_ev.is_set():
                raise RuntimeError("Cancelled")
            if _monotonic() > deadline:
                raise TimeoutError("Timed out reading Fedora release metadata")
            chunk = response.read(32 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RELEASE_METADATA_BYTES:
                raise ValueError("Fedora release metadata exceeded size limit")
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def discover_fedora_releases(
    current: CurrentFedoraRelease,
    *,
    cancellation: threading.Event | None = None,
    json_fetcher: Callable[[str], Any] | None = None,
) -> FedoraReleaseDiscoveryResult:
    """Fetch upstream Fedora targets with deterministic local fallback."""
    cancel_ev = cancellation if cancellation is not None else threading.Event()
    fetcher = (
        json_fetcher
        if json_fetcher is not None
        else (lambda target_url: _fetch_https_json(target_url, cancellation=cancel_ev))
    )

    if not cancel_ev.is_set():
        try:
            bodhi_payload = fetcher(BODHI_RELEASES_URL)
            targets = parse_bodhi_releases(bodhi_payload, current)
            if targets:
                return FedoraReleaseDiscoveryResult(
                    targets=targets,
                    source="online",
                    notice=tr("Loaded available Fedora releases from Bodhi metadata."),
                )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError):
            pass

    if not cancel_ev.is_set():
        try:
            releases_payload = fetcher(FEDORA_RELEASES_JSON_URL)
            targets = parse_fedora_releases_json(releases_payload, current)
            if targets:
                return FedoraReleaseDiscoveryResult(
                    targets=targets,
                    source="online",
                    notice=tr(
                        "Loaded available Fedora releases from Fedora release metadata."
                    ),
                )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError):
            pass

    fallback = build_local_fallback_targets(current)
    notice = (
        tr(
            "Online Fedora release metadata could not be reached; showing local release suggestions."
        )
        if fallback
        else tr(
            "Online Fedora release metadata could not be reached; enter the target release version manually."
        )
    )
    return FedoraReleaseDiscoveryResult(
        targets=fallback,
        source="local",
        notice=notice,
    )


class FedoraReleasesSignals(QObject):
    """Typed signal emitted when target release discovery finishes."""

    loaded = pyqtSignal(object)


class FedoraReleasesQueryWorker(ShutdownSafeRunnable):
    """Background worker discovering available Fedora target releases."""

    def __init__(
        self,
        current_release: CurrentFedoraRelease,
        *,
        json_fetcher: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__()
        self.signals = FedoraReleasesSignals()
        self._current_release = current_release
        self._json_fetcher = json_fetcher
        self._is_cancelled = threading.Event()

    def cancel(self) -> None:
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self) -> None:
        if self._is_cancelled.is_set():
            return
        result = discover_fedora_releases(
            self._current_release,
            cancellation=self._is_cancelled,
            json_fetcher=self._json_fetcher,
        )
        if not self._is_cancelled.is_set():
            self.signals.loaded.emit(result)


__all__ = (
    "BODHI_RELEASES_URL",
    "FEDORA_RELEASES_JSON_URL",
    "CurrentFedoraRelease",
    "FedoraReleaseDiscoveryResult",
    "FedoraReleasesQueryWorker",
    "FedoraReleasesSignals",
    "FedoraTargetRelease",
    "build_local_fallback_targets",
    "detect_current_fedora_release",
    "discover_fedora_releases",
    "parse_bodhi_releases",
    "parse_fedora_releases_json",
    "validate_target_releasever",
)
