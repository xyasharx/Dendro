# dendro/core/rpm_inspection.py
"""Native librpm & ``rpm`` CLI helpers for local RPMs, file owners, and protected sets."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Any, Final

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from dendro.core.backend_parsers import (
    RPM_PACKAGE_NAME_PATTERN,
    _classify_local_rpm_manifest,
)
from dendro.core.package_classifier import DEFAULT_SYSTEM_ROOT_PILLARS
from dendro.core.package_metadata import PackagePhysicalAnatomy
from dendro.core.package_types import (
    LocalRpmInspectionResult,
    PackageInfo,
    PackageState,
    _decode_rpm_str,
    _normalise_rpm_epoch,
)
from dendro.core.process_utils import (
    describe_subprocess_failure,
    flatpak_read_only_unavailable,
    get_clean_env,
    is_running_in_flatpak,
)
from dendro.core.worker_lifecycle import ShutdownSafeRunnable

RPM_GLOBAL_LOCK: Final[threading.RLock] = threading.RLock()
LOGGER = logging.getLogger(__name__)

try:
    import rpm  # type: ignore[import-untyped]

    HAS_NATIVE_RPM: Final[bool] = True
except ImportError:
    rpm = None  # type: ignore[assignment]
    HAS_NATIVE_RPM: Final[bool] = False


def _backend_hook(name: str, default: Any) -> Any:
    """Resolve optional monkeypatches applied to ``core.backend`` in tests."""
    facade = sys.modules.get("dendro.core.backend")
    return getattr(facade, name, default) if facade is not None else default


def _create_read_only_rpm_transaction_set_unlocked() -> object:
    """Create a read-only RPM transaction set without signature-verification flags."""
    rpm_mod = _backend_hook("rpm", rpm)
    transaction_set = rpm_mod.TransactionSet()
    vsflags = getattr(
        rpm_mod,
        "RPMVSF_MASK_NOSIGNATURES",
        getattr(rpm_mod, "_RPMVSF_NOSIGNATURES", 0),
    )
    if vsflags:
        transaction_set.setVSFlags(vsflags)
    return transaction_set


def read_rpm_size_bytes(header: Any, rpm_mod: Any) -> int:
    """Return installed payload size preferring 64-bit ``RPMTAG_LONGSIZE``."""
    long_tag = getattr(rpm_mod, "RPMTAG_LONGSIZE", None)
    if long_tag is not None:
        try:
            long_value = header[long_tag]
        except (KeyError, TypeError):
            long_value = None
        if long_value is not None:
            return int(long_value)
    return int(header[rpm_mod.RPMTAG_SIZE] or 0)


def create_rpm_transaction_set() -> object | None:
    """Create a read-only RPM transaction set when native bindings are available."""
    in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
    has_native = _backend_hook("HAS_NATIVE_RPM", HAS_NATIVE_RPM)
    if not has_native or in_flatpak():
        return None
    with RPM_GLOBAL_LOCK:
        try:
            return _create_read_only_rpm_transaction_set_unlocked()
        except Exception:
            LOGGER.warning(
                "Could not create an RPM transaction set; native RPM queries are unavailable.",
                exc_info=True,
            )
            return None


def get_system_protected_packages() -> set[str]:
    """Read DNF protected packages and retain a conservative static fallback."""
    protected: set[str] = set()
    in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
    protected_dirs = (
        [
            "/usr/share/dnf5/protected.d",
            "/etc/dnf/protected.d",
            "/etc/yum/protected.d",
        ]
        if not in_flatpak()
        else []
    )
    for protected_dir in protected_dirs:
        if not os.path.isdir(protected_dir):
            continue
        try:
            for filename in os.listdir(protected_dir):
                if not filename.endswith(".conf"):
                    continue
                with open(
                    os.path.join(protected_dir, filename),
                    encoding="utf-8",
                    errors="replace",
                ) as stream:
                    for line in stream:
                        package_name = line.split("#", 1)[0].strip()
                        if package_name:
                            protected.add(package_name.lower())
        except OSError:
            LOGGER.warning(
                "Could not read protected-package configuration from %s.",
                protected_dir,
                exc_info=True,
            )
    try:
        uname_release = os.uname().release.lower()
        protected.update(
            {
                f"kernel-{uname_release}",
                f"kernel-core-{uname_release}",
                f"kernel-modules-{uname_release}",
                f"kernel-modules-core-{uname_release}",
                "kernel",
                "kernel-core",
                "kernel-modules",
            }
        )
    except OSError:
        LOGGER.warning(
            "Could not determine the running kernel release for protected-package checks.",
            exc_info=True,
        )
    if not protected:
        protected.update(DEFAULT_SYSTEM_ROOT_PILLARS)
    else:
        protected.update(
            {"systemd", "glibc", "dnf", "dnf5", "rpm", "shared-mime-info", "glib2"}
        )
    return protected


def _rpm_query_target(package_name: str, package: PackageInfo | None) -> str:
    """Return the exact RPM query target when a selected record is available."""
    return package.full_nevra if package is not None else package_name


def _header_matches_package(header: Any, package: PackageInfo | None) -> bool:
    """Match a librpm header to one concrete selected package, if supplied."""
    if package is None:
        return True
    rpm_mod = _backend_hook("rpm", rpm)
    if not all(
        (
            package.name.strip(),
            package.version.strip(),
            package.release.strip(),
            package.arch.strip(),
        )
    ):
        return (
            _decode_rpm_str(header[rpm_mod.RPMTAG_NAME]).strip() == package.name.strip()
        )
    return (
        _decode_rpm_str(header[rpm_mod.RPMTAG_NAME]).strip() == package.name.strip()
        and _decode_rpm_str(header[rpm_mod.RPMTAG_VERSION]).strip()
        == package.version.strip()
        and _decode_rpm_str(header[rpm_mod.RPMTAG_RELEASE]).strip()
        == package.release.strip()
        and _decode_rpm_str(header[rpm_mod.RPMTAG_ARCH]).strip() == package.arch.strip()
        and _normalise_rpm_epoch(header[rpm_mod.RPMTAG_EPOCH])
        == _normalise_rpm_epoch(package.epoch)
    )


class LocalRpmInspectionError(RuntimeError):
    """A local RPM could not be inspected, rather than merely being absent."""


def inspect_local_rpm_file(
    file_path: str, *, raise_on_failure: bool = False
) -> LocalRpmInspectionResult | None:
    """Read one local RPM header without confusing a failed read with bad data."""

    def fail(detail: str) -> LocalRpmInspectionResult | None:
        LOGGER.warning("Local RPM inspection failed for %r: %s", file_path, detail)
        if raise_on_failure:
            raise LocalRpmInspectionError(detail)
        return None

    in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
    if in_flatpak():
        return fail(flatpak_read_only_unavailable("Local RPM inspection"))
    if not os.path.isfile(file_path):
        return fail("The selected RPM file does not exist or is not a regular file.")

    has_native = _backend_hook("HAS_NATIVE_RPM", HAS_NATIVE_RPM)
    rpm_mod = _backend_hook("rpm", rpm)
    classify_fn = _backend_hook(
        "_classify_local_rpm_manifest", _classify_local_rpm_manifest
    )

    if has_native:
        with RPM_GLOBAL_LOCK:
            try:
                ts = _create_read_only_rpm_transaction_set_unlocked()

                with open(file_path, "rb") as fd:
                    hdr = ts.hdrFromFdno(fd.fileno())
                    if hdr is None:
                        raise RuntimeError("librpm returned no package header")

                    name = _decode_rpm_str(hdr[rpm_mod.RPMTAG_NAME]).strip()
                    ver = _decode_rpm_str(hdr[rpm_mod.RPMTAG_VERSION]).strip()
                    rel = _decode_rpm_str(hdr[rpm_mod.RPMTAG_RELEASE]).strip()
                    arch = _decode_rpm_str(hdr[rpm_mod.RPMTAG_ARCH]).strip()
                    if not all((name, ver, rel, arch)):
                        raise RuntimeError(
                            "librpm returned a package header with missing identity fields"
                        )
                    summary = _decode_rpm_str(hdr[rpm_mod.RPMTAG_SUMMARY])
                    desc = _decode_rpm_str(hdr[rpm_mod.RPMTAG_DESCRIPTION])
                    lic = _decode_rpm_str(hdr[rpm_mod.RPMTAG_LICENSE])
                    url = _decode_rpm_str(hdr[rpm_mod.RPMTAG_URL])
                    size = read_rpm_size_bytes(hdr, rpm_mod)
                    if size < 0:
                        raise RuntimeError("librpm returned a negative package size")

                    raw_reqs = [
                        _decode_rpm_str(r)
                        for r in (hdr[rpm_mod.RPMTAG_REQUIRENAME] or [])
                    ]
                    raw_provs = [
                        _decode_rpm_str(p)
                        for p in (hdr[rpm_mod.RPMTAG_PROVIDENAME] or [])
                    ]
                    raw_dirs = (
                        [
                            _decode_rpm_str(directory)
                            for directory in (hdr[rpm_mod.RPMTAG_DIRNAMES] or [])
                        ]
                        if hasattr(rpm_mod, "RPMTAG_DIRNAMES")
                        else []
                    )
                    raw_basenames = (
                        [
                            _decode_rpm_str(basename)
                            for basename in (hdr[rpm_mod.RPMTAG_BASENAMES] or [])
                        ]
                        if hasattr(rpm_mod, "RPMTAG_BASENAMES")
                        else []
                    )
                    raw_dirindexes = (
                        [int(idx) for idx in (hdr[rpm_mod.RPMTAG_DIRINDEXES] or [])]
                        if hasattr(rpm_mod, "RPMTAG_DIRINDEXES")
                        else []
                    )
                    mapped_file_paths: list[str] = []
                    if (
                        raw_dirs
                        and raw_basenames
                        and len(raw_dirindexes) == len(raw_basenames)
                    ):
                        for basename, dir_idx in zip(
                            raw_basenames, raw_dirindexes, strict=True
                        ):
                            if not basename or not (0 <= dir_idx < len(raw_dirs)):
                                continue
                            directory = raw_dirs[dir_idx].rstrip("/")
                            mapped_file_paths.append(
                                f"/{basename}"
                                if not directory
                                else f"{directory}/{basename}"
                            )
                    elif hasattr(rpm_mod, "RPMTAG_OLDFILENAMES"):
                        try:
                            mapped_file_paths = [
                                _decode_rpm_str(path)
                                for path in (hdr[rpm_mod.RPMTAG_OLDFILENAMES] or [])
                                if _decode_rpm_str(path)
                            ]
                        except (KeyError, IndexError, TypeError):
                            mapped_file_paths = []
                    src_tag = getattr(rpm_mod, "RPMTAG_SOURCERPM", None)
                    try:
                        source_rpm = (
                            _decode_rpm_str(hdr[src_tag]) if src_tag is not None else ""
                        )
                    except (KeyError, IndexError, TypeError):
                        source_rpm = ""
                    anatomy = PackagePhysicalAnatomy.from_rpm_header(
                        hdr,
                        rpm_module=rpm_mod,
                        raw_provides=raw_provs,
                    )
                    if classify_fn is _classify_local_rpm_manifest:
                        classification = classify_fn(
                            name,
                            summary,
                            desc,
                            raw_dirs,
                            raw_provs,
                            raw_basenames,
                            file_paths=mapped_file_paths or None,
                            anatomy=anatomy,
                            source_rpm=source_rpm,
                            requires=raw_reqs,
                        )
                    else:
                        classification = classify_fn(
                            name,
                            summary,
                            desc,
                            raw_dirs,
                            raw_provs,
                            raw_basenames,
                        )

                    is_installed = False
                    installed_ver = ""
                    matches = None
                    try:
                        matches = ts.dbMatch("name", name)
                        for inst_hdr in matches:
                            inst_arch = _decode_rpm_str(
                                inst_hdr[rpm_mod.RPMTAG_ARCH]
                            ).strip()
                            if inst_arch != arch:
                                continue
                            is_installed = True
                            inst_v = _decode_rpm_str(inst_hdr[rpm_mod.RPMTAG_VERSION])
                            inst_r = _decode_rpm_str(inst_hdr[rpm_mod.RPMTAG_RELEASE])
                            installed_ver = f"{inst_v}-{inst_r}" if inst_r else inst_v
                            break
                    finally:
                        if matches is not None:
                            del matches

                    return LocalRpmInspectionResult(
                        file_path=os.path.abspath(file_path),
                        name=name,
                        version=ver,
                        release=rel,
                        arch=arch,
                        summary=summary,
                        description=desc,
                        license=lic,
                        url=url,
                        size_bytes=size,
                        requires=raw_reqs,
                        provides=raw_provs,
                        classification=classification,
                        is_already_installed=is_installed,
                        installed_version=installed_ver,
                    )
            except Exception:
                LOGGER.warning(
                    "Native RPM inspection failed for %r; trying `rpm -qp` as a fallback.",
                    file_path,
                    exc_info=True,
                )

    record_separator = "\x1e"
    field_separator = "\x1f"
    list_separator = "\x07"
    query_format = (
        f"%{{NAME}}{field_separator}%{{VERSION}}{field_separator}"
        f"%{{RELEASE}}{field_separator}%{{ARCH}}{field_separator}"
        f"%{{SUMMARY}}{field_separator}%{{DESCRIPTION}}{field_separator}"
        f"%{{LICENSE}}{field_separator}%{{URL}}{field_separator}"
        f"%{{LONGSIZE}}{field_separator}%{{SOURCERPM}}{field_separator}"
        f"[%{{DIRNAMES}}{list_separator}]{field_separator}"
        f"[%{{BASENAMES}}{list_separator}]{field_separator}"
        f"[%{{REQUIRENAME}}{list_separator}]{field_separator}"
        f"%|FILENAMES?{{[%{{FILENAMES}}{list_separator}]}}:{{[%{{OLDFILENAMES}}{list_separator}]}}|{record_separator}"
    )
    try:
        proc = subprocess.run(
            [
                "rpm",
                "-qp",
                "--nosignature",
                "--queryformat",
                query_format,
                "--",
                file_path,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            env=get_clean_env(),
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return fail(
            f"Could not read the RPM package header: {describe_subprocess_failure(exc)}"
        )

    if proc.returncode != 0:
        detail = (
            proc.stderr.strip()
            or f"rpm package-header query exited with status {proc.returncode}."
        )
        return fail(f"Could not read the RPM package header: {detail}")

    if record_separator in proc.stdout:
        records = [
            record for record in proc.stdout.split(record_separator) if record.strip()
        ]
        if len(records) != 1:
            return fail(
                "rpm package-header query returned an unexpected number of records."
            )
        raw_header = records[0]
    else:
        raw_header = proc.stdout.rstrip("\r\n")
        if not raw_header.strip():
            return fail(
                "rpm package-header query returned an unexpected number of records."
            )
    parts = raw_header.split(field_separator)
    if len(parts) != 14:
        records = [line for line in proc.stdout.splitlines() if line.strip()]
        if len(records) > 1 and record_separator not in proc.stdout:
            return fail(
                "rpm package-header query returned an unexpected number of records."
            )
        return fail("rpm package-header query returned a malformed package record.")
    (
        name,
        ver,
        rel,
        arch,
        summary,
        desc,
        lic,
        url,
        size_str,
        raw_sourcerpm,
        raw_dirs_field,
        raw_basenames_field,
        raw_reqs_field,
        raw_files_field,
    ) = parts
    summary = " ".join(summary.split())
    desc = desc.strip()
    lic = " ".join(lic.split())
    url = url.strip()
    source_rpm = raw_sourcerpm.strip()
    if source_rpm == "(none)":
        source_rpm = ""
    if any(
        "\n" in field or "\r" in field for field in (name, ver, rel, arch, size_str)
    ):
        return fail(
            "rpm package-header query returned an unexpected number of records."
        )
    name, ver, rel, arch = (value.strip() for value in (name, ver, rel, arch))
    if not all((name, ver, rel, arch)):
        return fail("rpm package-header query returned missing identity fields.")
    try:
        size = int(size_str.strip())
    except ValueError:
        return fail("rpm package-header query returned an invalid package size.")
    if size < 0:
        return fail("rpm package-header query returned a negative package size.")

    raw_dirs = [item for item in raw_dirs_field.split(list_separator) if item]
    raw_basenames = [item for item in raw_basenames_field.split(list_separator) if item]
    raw_reqs = [item for item in raw_reqs_field.split(list_separator) if item]
    raw_file_paths = [item for item in raw_files_field.split(list_separator) if item]

    try:
        provides_proc = subprocess.run(
            ["rpm", "-qp", "--nosignature", "--provides", "--", file_path],
            capture_output=True,
            text=True,
            errors="replace",
            env=get_clean_env(),
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return fail(
            f"Could not read local RPM capabilities: {describe_subprocess_failure(exc)}"
        )
    if provides_proc.returncode != 0:
        detail = (
            provides_proc.stderr.strip()
            or f"rpm capability query exited with status {provides_proc.returncode}."
        )
        return fail("Could not read local RPM capabilities: " + detail)
    raw_provs = [
        line.strip() for line in provides_proc.stdout.splitlines() if line.strip()
    ]
    if classify_fn is _classify_local_rpm_manifest:
        classification = classify_fn(
            name,
            summary,
            desc,
            raw_dirs,
            raw_provs,
            raw_basenames,
            file_paths=raw_file_paths or None,
            source_rpm=source_rpm,
            requires=raw_reqs,
        )
    else:
        classification = classify_fn(
            name,
            summary,
            desc,
            raw_dirs,
            raw_provs,
            raw_basenames,
        )

    try:
        chk_proc = subprocess.run(
            [
                "rpm",
                "-q",
                "--queryformat",
                "%{VERSION}-%{RELEASE}\n",
                "--",
                f"{name}.{arch}",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            env=get_clean_env(),
            check=False,
            timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return fail(
            "Could not check whether the local RPM is already installed: "
            f"{describe_subprocess_failure(exc)}"
        )

    if chk_proc.returncode == 0:
        inst_versions = [
            line.strip() for line in chk_proc.stdout.splitlines() if line.strip()
        ]
        inst_ver = ", ".join(dict.fromkeys(inst_versions))
        if not inst_ver:
            return fail(
                "rpm reported that the package is installed but returned no installed version."
            )
        is_installed = True
    elif chk_proc.returncode == 1:
        inst_ver = ""
        is_installed = False
    else:
        detail = (
            chk_proc.stderr.strip()
            or f"rpm installed-version query exited with status {chk_proc.returncode}."
        )
        return fail(
            "Could not check whether the local RPM is already installed: " + detail
        )

    return LocalRpmInspectionResult(
        file_path=os.path.abspath(file_path),
        name=name,
        version=ver,
        release=rel,
        arch=arch,
        summary=summary,
        description=desc,
        license=lic,
        url=url,
        size_bytes=size,
        requires=raw_reqs,
        provides=raw_provs,
        classification=classification,
        is_already_installed=is_installed,
        installed_version=inst_ver,
    )


class LocalRpmInspectionSignals(QObject):
    """Terminal outcomes for one local RPM header inspection."""

    inspected = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)


class LocalRpmInspectionWorker(ShutdownSafeRunnable):
    """Inspect a local RPM without pausing the application event loop."""

    def __init__(self, file_path: str) -> None:
        super().__init__()
        self.file_path = file_path
        self.signals = LocalRpmInspectionSignals()
        self._is_cancelled = threading.Event()
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self) -> None:
        if self._is_cancelled.is_set():
            return
        try:
            inspect_fn = _backend_hook("inspect_local_rpm_file", inspect_local_rpm_file)
            result = inspect_fn(self.file_path, raise_on_failure=True)
        except LocalRpmInspectionError as error:
            if not self._is_cancelled.is_set():
                self.signals.failed.emit(self.file_path, str(error))
        except Exception as error:
            if not self._is_cancelled.is_set():
                self.signals.failed.emit(
                    self.file_path, f"{type(error).__name__}: {error}"
                )
        else:
            if not self._is_cancelled.is_set():
                self.signals.inspected.emit(self.file_path, result)


class FileOwnerLookupError(RuntimeError):
    """RPM ownership inspection could not complete, rather than finding no owner."""


def find_package_owning_file_info(
    file_path: str, *, raise_on_failure: bool = False
) -> PackageInfo | None:
    """Resolve the exact installed RPM record owning ``file_path``."""
    in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
    if in_flatpak():
        if raise_on_failure:
            raise FileOwnerLookupError(
                flatpak_read_only_unavailable("File-owner lookup")
            )
        return None

    def fail(detail: str) -> None:
        LOGGER.warning("RPM file-owner lookup failed for %r: %s", file_path, detail)
        if raise_on_failure:
            raise FileOwnerLookupError(detail)

    clean_path = file_path.strip().removeprefix("file:").strip()
    if not clean_path.startswith("/"):
        return None

    clean_path = os.path.normpath(clean_path)
    real_path = os.path.realpath(clean_path)
    target_paths = {clean_path, real_path}
    candidate_basenames = [os.path.basename(real_path), os.path.basename(clean_path)]
    rpm_mod = _backend_hook("rpm", rpm)

    def package_info_from_header(header: Any) -> PackageInfo:
        name = _decode_rpm_str(header[rpm_mod.RPMTAG_NAME]).strip()
        version = _decode_rpm_str(header[rpm_mod.RPMTAG_VERSION]).strip()
        release = _decode_rpm_str(header[rpm_mod.RPMTAG_RELEASE]).strip()
        arch = _decode_rpm_str(header[rpm_mod.RPMTAG_ARCH]).strip()
        if not all((name, version, release, arch)):
            raise RuntimeError(
                "Native RPM file-owner record has an incomplete package identity."
            )
        return PackageInfo(
            name=name,
            version=version,
            release=release,
            arch=arch,
            epoch=_normalise_rpm_epoch(header[rpm_mod.RPMTAG_EPOCH]),
            state=PackageState.INSTALLED,
        )

    create_ts = _backend_hook("create_rpm_transaction_set", create_rpm_transaction_set)
    ts = create_ts()
    if ts is not None:
        with RPM_GLOBAL_LOCK:
            matches = None
            try:
                for base_name in dict.fromkeys(candidate_basenames):
                    matches = ts.dbMatch("basenames", base_name)
                    for hdr in matches:
                        dirs = [
                            _decode_rpm_str(d)
                            for d in (hdr[rpm_mod.RPMTAG_DIRNAMES] or [])
                        ]
                        bases = [
                            _decode_rpm_str(b)
                            for b in (hdr[rpm_mod.RPMTAG_BASENAMES] or [])
                        ]
                        d_indices = hdr[rpm_mod.RPMTAG_DIRINDEXES] or []

                        if len(d_indices) == len(bases):
                            for b, d_idx in zip(bases, d_indices, strict=True):
                                if b == base_name and 0 <= d_idx < len(dirs):
                                    full_file = dirs[d_idx].rstrip("/") + "/" + b
                                    if full_file in target_paths:
                                        return package_info_from_header(hdr)
                        else:
                            files = [
                                _decode_rpm_str(f)
                                for f in (hdr[rpm_mod.RPMTAG_FILENAMES] or [])
                            ]
                            if any(p in files for p in target_paths):
                                return package_info_from_header(hdr)
                    del matches
                    matches = None
            except Exception:
                LOGGER.warning(
                    "Native RPM ownership lookup failed for %r; trying `rpm -qf` as a fallback.",
                    clean_path,
                    exc_info=True,
                )
            finally:
                if matches is not None:
                    del matches
                del ts

    field_separator = "\x1f"
    query_format = (
        f"%{{NAME}}{field_separator}%{{EPOCH}}{field_separator}%{{VERSION}}"
        f"{field_separator}%{{RELEASE}}{field_separator}%{{ARCH}}\n"
    )
    try:
        for query_target in dict.fromkeys((real_path, clean_path)):
            cmd = ["rpm", "-qf", "--queryformat", query_format, "--", query_target]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=4,
            )
            if res.returncode == 1:
                continue
            if res.returncode != 0:
                fail(
                    res.stderr.strip()
                    or f"rpm file-owner query exited with status {res.returncode}."
                )
                return None
            records = [line for line in res.stdout.splitlines() if line.strip()]
            if len(records) == 1:
                parts = records[0].split(field_separator)
                if len(parts) == 5:
                    name, raw_epoch, version, release, arch = (
                        value.strip() for value in parts
                    )
                    if (
                        RPM_PACKAGE_NAME_PATTERN.fullmatch(name) is not None
                        and version
                        and release
                        and arch
                    ):
                        return PackageInfo(
                            name=name,
                            version=version,
                            release=release,
                            arch=arch,
                            epoch=_normalise_rpm_epoch(raw_epoch),
                            state=PackageState.INSTALLED,
                        )
            fail(
                "rpm file-owner query succeeded without returning one valid package identity."
            )
            return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"rpm file-owner query failed: {describe_subprocess_failure(exc)}")

    return None


class FileOwnerLookupSignals(QObject):
    """Terminal outcomes for one potentially slow RPM file-owner lookup."""

    resolved = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)


class FileOwnerLookupWorker(ShutdownSafeRunnable):
    """Resolve one file owner without blocking interactive search input."""

    def __init__(self, file_path: str) -> None:
        super().__init__()
        self.file_path = file_path
        self.signals = FileOwnerLookupSignals()
        self._is_cancelled = threading.Event()
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self) -> None:
        if self._is_cancelled.is_set():
            return
        try:
            lookup_fn = _backend_hook(
                "find_package_owning_file_info", find_package_owning_file_info
            )
            owner = lookup_fn(self.file_path, raise_on_failure=True)
        except FileOwnerLookupError as error:
            if not self._is_cancelled.is_set():
                self.signals.failed.emit(self.file_path, str(error))
        except Exception as error:
            if not self._is_cancelled.is_set():
                self.signals.failed.emit(
                    self.file_path, f"{type(error).__name__}: {error}"
                )
        else:
            if not self._is_cancelled.is_set():
                self.signals.resolved.emit(self.file_path, owner)


__all__ = (
    "HAS_NATIVE_RPM",
    "RPM_GLOBAL_LOCK",
    "FileOwnerLookupError",
    "FileOwnerLookupSignals",
    "FileOwnerLookupWorker",
    "LocalRpmInspectionError",
    "LocalRpmInspectionSignals",
    "LocalRpmInspectionWorker",
    "_create_read_only_rpm_transaction_set_unlocked",
    "_header_matches_package",
    "_rpm_query_target",
    "create_rpm_transaction_set",
    "find_package_owning_file_info",
    "get_system_protected_packages",
    "inspect_local_rpm_file",
    "read_rpm_size_bytes",
    "rpm",
)
