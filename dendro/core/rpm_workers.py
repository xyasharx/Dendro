# dendro/core/rpm_workers.py
"""Qt background workers for read-only native librpm and ``rpm`` CLI queries."""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from dendro.core.backend_parsers import (
    CVE_REGEX_PATTERN,
    RPM_DATABASE_PSEUDO_PACKAGE_NAMES,
    RPM_PACKAGE_NAME_PATTERN,
    classification_package_fields,
    parse_rpm_verify_line,
)
from dendro.core.capability_cache import SQLiteCapabilityCache
from dendro.core.host_environment import HostEnvironment, detect_host_environment
from dendro.core.i18n import format_number, tr, tr_plural
from dendro.core.package_classifier import (
    FEDORA_SYSTEM_ROOT_PILLARS,
    ProductionTaxonomyEngine,
)
from dendro.core.package_metadata import (
    AppStreamCatalog,
    FedoraCompsCatalog,
    PackagePhysicalAnatomy,
)
from dendro.core.package_types import (
    DependencyNode,
    FileVerificationResult,
    PackageChangelogEntry,
    PackageFileInfo,
    PackageInfo,
    PackageScriptlets,
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
from dendro.core.rpm_inspection import (
    HAS_NATIVE_RPM,
    RPM_GLOBAL_LOCK,
    _create_read_only_rpm_transaction_set_unlocked,
    _header_matches_package,
    _rpm_query_target,
    create_rpm_transaction_set,
    get_system_protected_packages,
    read_rpm_size_bytes,
    rpm,
)
from dendro.core.worker_lifecycle import ShutdownSafeRunnable

LOGGER = logging.getLogger(__name__)


def _backend_hook(name: str, default: Any) -> Any:
    """Resolve optional monkeypatches applied to ``core.backend`` in tests."""
    facade = sys.modules.get("dendro.core.backend")
    return getattr(facade, name, default) if facade is not None else default


def _get_host_environment() -> HostEnvironment:
    in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
    if in_flatpak():
        return HostEnvironment.flatpak()
    environment = detect_host_environment()
    if environment.is_flatpak:
        return HostEnvironment.traditional()
    return environment


class BackendSignals(QObject):
    packages_loaded = pyqtSignal(list)
    orphans_loaded = pyqtSignal(set)
    userinstalled_loaded = pyqtSignal(set)
    dependencies_resolved = pyqtSignal(str, list, object)
    reverse_dependencies_resolved = pyqtSignal(str, list)
    package_files_loaded = pyqtSignal(str, list)
    package_details_loaded = pyqtSignal(object)
    history_loaded = pyqtSignal(list)
    dry_run_finished = pyqtSignal(object)
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)
    package_changelog_loaded = pyqtSignal(str, list)
    package_verification_finished = pyqtSignal(str, list)
    package_scriptlets_loaded = pyqtSignal(str, object)
    remote_search_finished = pyqtSignal(str, list)
    system_updates_loaded = pyqtSignal(dict)
    repo_list_loaded = pyqtSignal(list)
    security_advisories_loaded = pyqtSignal(list)


class PackageQueryWorker(ShutdownSafeRunnable):
    def __init__(self, category: str = "all", search_query: str = ""):
        super().__init__()
        self.signals = BackendSignals()
        self.category = category.lower()
        self.search_query = search_query.strip().lower()
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
        if in_flatpak():
            self.signals.packages_loaded.emit([])
            self.signals.status_update.emit(
                flatpak_read_only_unavailable("Installed-package browsing")
            )
            return

        t_start = time.time()
        try:
            self.signals.status_update.emit(
                tr("Scanning AppStream catalog, comps groups & RPM database...")
            )

            appstream_cls = _backend_hook("AppStreamCatalog", AppStreamCatalog)
            comps_cls = _backend_hook("FedoraCompsCatalog", FedoraCompsCatalog)

            t0 = time.time()
            appstream = appstream_cls.get_instance()
            t_appstream = time.time() - t0

            t0 = time.time()
            comps = comps_cls.get_instance()
            t_comps = time.time() - t0

            t0 = time.time()
            has_native = _backend_hook("HAS_NATIVE_RPM", HAS_NATIVE_RPM)
            if has_native and not in_flatpak():
                packages = self._query_native_librpm(appstream, comps)
            else:
                packages = self._query_cli_subprocess(appstream, comps)
            t_rpm = time.time() - t0

            if self._is_cancelled.is_set():
                return

            packages.sort(key=lambda p: p.name.lower())

            if os.environ.get("DENDRO_DEBUG"):
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                print(
                    f"[{ts}] [Dendro:debug] Startup timings -> AppStream: {t_appstream:.2f}s | Comps: {t_comps:.2f}s | RPM DB: {t_rpm:.2f}s | Total: {time.time() - t_start:.2f}s ({len(packages)} pkgs)",
                    file=sys.stderr,
                )

            self.signals.packages_loaded.emit(packages)
            self.signals.status_update.emit(
                tr_plural(
                    "Loaded {count} package successfully.",
                    "Loaded {count} packages successfully.",
                    len(packages),
                ).format(count=format_number(len(packages)))
            )

        except Exception as ex:
            self.signals.error_occurred.emit(
                "",
                tr("Could not read the installed package database: {detail}").format(
                    detail=describe_subprocess_failure(ex, "rpm")
                ),
            )

    def _query_native_librpm(
        self, appstream: AppStreamCatalog, comps: FedoraCompsCatalog
    ) -> list[PackageInfo]:
        packages: list[PackageInfo] = []
        in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
        has_native = _backend_hook("HAS_NATIVE_RPM", HAS_NATIVE_RPM)
        if not has_native or in_flatpak():
            return self._query_cli_subprocess(appstream, comps)

        rpm_mod = _backend_hook("rpm", rpm)
        protected_fn = _backend_hook(
            "get_system_protected_packages", get_system_protected_packages
        )
        engine_cls = _backend_hook("ProductionTaxonomyEngine", ProductionTaxonomyEngine)
        fields_fn = _backend_hook(
            "classification_package_fields", classification_package_fields
        )
        protected_pkgs = protected_fn()
        native_query_failed = False
        with RPM_GLOBAL_LOCK:
            ts = None
            match_iterator = None
            try:
                ts = _create_read_only_rpm_transaction_set_unlocked()
                match_iterator = ts.dbMatch()
                for header in match_iterator:
                    if self._is_cancelled.is_set():
                        break

                    name = _decode_rpm_str(header[rpm_mod.RPMTAG_NAME])
                    if not name:
                        raise RuntimeError(
                            "Native RPM scan returned a package record with no name."
                        )
                    if name in RPM_DATABASE_PSEUDO_PACKAGE_NAMES:
                        continue
                    ver = _decode_rpm_str(header[rpm_mod.RPMTAG_VERSION])
                    rel = _decode_rpm_str(header[rpm_mod.RPMTAG_RELEASE])
                    arch = _decode_rpm_str(header[rpm_mod.RPMTAG_ARCH])
                    missing_identity_fields = [
                        field_name
                        for field_name, value in (
                            ("version", ver),
                            ("release", rel),
                            ("architecture", arch),
                        )
                        if not value
                    ]
                    if missing_identity_fields:
                        raise RuntimeError(
                            "Native RPM scan returned an incomplete package identity for "
                            f"{name!r}: missing {', '.join(missing_identity_fields)}."
                        )
                    raw_epoch = header[rpm_mod.RPMTAG_EPOCH]
                    epoch_str = _normalise_rpm_epoch(raw_epoch)
                    group = _decode_rpm_str(header[rpm_mod.RPMTAG_GROUP]) or "General"
                    summary = _decode_rpm_str(header[rpm_mod.RPMTAG_SUMMARY])
                    description = _decode_rpm_str(header[rpm_mod.RPMTAG_DESCRIPTION])
                    license_str = _decode_rpm_str(header[rpm_mod.RPMTAG_LICENSE])
                    url = _decode_rpm_str(header[rpm_mod.RPMTAG_URL])
                    packager = _decode_rpm_str(header[rpm_mod.RPMTAG_PACKAGER])
                    vendor = _decode_rpm_str(header[rpm_mod.RPMTAG_VENDOR])

                    b_time_raw = header[rpm_mod.RPMTAG_BUILDTIME]
                    build_time = ""
                    if b_time_raw and b_time_raw > 0:
                        try:
                            build_time = datetime.fromtimestamp(b_time_raw).strftime(
                                "%Y-%m-%d %H:%M"
                            )
                        except (OSError, ValueError, OverflowError):
                            build_time = ""

                    i_time_raw = header[rpm_mod.RPMTAG_INSTALLTIME]
                    install_time = ""
                    if i_time_raw and i_time_raw > 0:
                        try:
                            install_time = datetime.fromtimestamp(i_time_raw).strftime(
                                "%Y-%m-%d %H:%M"
                            )
                        except (OSError, ValueError, OverflowError):
                            install_time = ""

                    size_bytes = read_rpm_size_bytes(header, rpm_mod)
                    if size_bytes < 0:
                        raise RuntimeError(
                            "Native RPM scan returned a package record with a negative installed size."
                        )
                    raw_dirs = [
                        _decode_rpm_str(d)
                        for d in (header[rpm_mod.RPMTAG_DIRNAMES] or [])
                    ]
                    raw_provs = [
                        _decode_rpm_str(p)
                        for p in (header[rpm_mod.RPMTAG_PROVIDENAME] or [])
                    ]
                    raw_basenames = [
                        _decode_rpm_str(b)
                        for b in (header[rpm_mod.RPMTAG_BASENAMES] or [])
                    ]

                    anatomy = PackagePhysicalAnatomy.from_manifest_data(
                        raw_dirs, raw_provs, raw_basenames
                    )

                    owned_desktop_files = [
                        b for b in raw_basenames if b.endswith(".desktop")
                    ]
                    for did in anatomy.provided_desktop_ids:
                        if did not in owned_desktop_files:
                            owned_desktop_files.append(did)

                    raw_reqs = [
                        _decode_rpm_str(r)
                        for r in (header[rpm_mod.RPMTAG_REQUIRENAME] or [])
                    ]
                    has_real_deps = any(
                        not r.startswith(("rpmlib(", "config(", "/", "rtld(", "("))
                        for r in raw_reqs
                    )

                    repo = "Fedora Project"
                    packager_lower = packager.lower()
                    vendor_lower = vendor.lower()
                    if "copr" in packager_lower or "copr" in vendor_lower:
                        repo = "COPR Repository"
                    elif (
                        "rpmfusion" in packager_lower
                        or "rpmfusion" in vendor_lower
                        or "rpm fusion" in packager_lower
                        or "rpm fusion" in vendor_lower
                    ):
                        repo = "RPM Fusion"
                    elif vendor:
                        repo = vendor

                    decision = engine_cls.classify(
                        name=name,
                        summary=summary,
                        description=description,
                        anatomy=anatomy,
                        desktop_entry_files=owned_desktop_files,
                        provides=raw_provs,
                        appstream=appstream,
                        comps=comps,
                        vendor=vendor,
                        packager=packager,
                    )

                    packages.append(
                        PackageInfo(
                            name=name,
                            version=ver,
                            release=rel,
                            arch=arch,
                            epoch=epoch_str,
                            summary=summary,
                            description=description,
                            license=license_str,
                            url=url,
                            packager=packager,
                            vendor=vendor,
                            build_time=build_time,
                            install_time=install_time,
                            group=group,
                            size_bytes=size_bytes,
                            state=PackageState.INSTALLED,
                            repository=repo,
                            is_orphan=False,
                            is_user_installed=False,
                            is_protected=(
                                name.lower() in protected_pkgs
                                or name in FEDORA_SYSTEM_ROOT_PILLARS
                            ),
                            has_dependencies=has_real_deps,
                            dependencies_loaded=(not has_real_deps),
                            **fields_fn(decision),
                        )
                    )
            except Exception:
                native_query_failed = True
                LOGGER.warning(
                    "Native RPM database scan failed; discarding its partial result and trying `rpm -qa`.",
                    exc_info=True,
                )
            finally:
                if match_iterator is not None:
                    del match_iterator
                if ts is not None:
                    del ts

        if self._is_cancelled.is_set():
            return []
        if native_query_failed:
            return self._query_cli_subprocess(appstream, comps)
        return packages

    def _query_cli_subprocess(
        self, appstream: AppStreamCatalog, comps: FedoraCompsCatalog
    ) -> list[PackageInfo]:
        in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
        if in_flatpak():
            raise RuntimeError(
                flatpak_read_only_unavailable("Installed-package browsing")
            )
        protected_fn = _backend_hook(
            "get_system_protected_packages", get_system_protected_packages
        )
        engine_cls = _backend_hook("ProductionTaxonomyEngine", ProductionTaxonomyEngine)
        fields_fn = _backend_hook(
            "classification_package_fields", classification_package_fields
        )
        protected_pkgs = protected_fn()
        field_separator = "\x1f"
        query_format = (
            f"%{{NAME}}{field_separator}%{{EPOCH}}{field_separator}%{{VERSION}}{field_separator}"
            f"%{{RELEASE}}{field_separator}%{{ARCH}}{field_separator}%{{GROUP}}{field_separator}"
            f"%{{SIZE}}{field_separator}%{{LICENSE}}{field_separator}%{{URL}}{field_separator}"
            f"%{{PACKAGER}}{field_separator}%{{VENDOR}}{field_separator}%{{INSTALLTIME:date}}{field_separator}"
            f"%{{SUMMARY}}{field_separator}[%{{DIRNAMES}};]{field_separator}"
            f"[%{{PROVIDENAME}};]{field_separator}[%{{BASENAMES}};]\n"
        )
        cmd = ["rpm", "-qa", "--queryformat", query_format]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            env=get_clean_env(),
            check=False,
            timeout=30,
        )

        if proc.returncode != 0:
            raise RuntimeError(f"RPM query failed: {proc.stderr}")

        packages: list[PackageInfo] = []
        for line in proc.stdout.splitlines():
            if self._is_cancelled.is_set():
                return []
            if not line.strip():
                continue

            parts = line.split(field_separator)
            if len(parts) != 16:
                raise RuntimeError(
                    "RPM query returned a malformed package record; refusing to display a partial inventory."
                )

            (
                name,
                raw_epoch,
                ver,
                rel,
                arch,
                group,
                size_str,
                license_str,
                url,
                packager,
                vendor,
                inst_time,
                summary,
                raw_dirs_field,
                raw_provs_field,
                raw_base_field,
            ) = parts
            if name in RPM_DATABASE_PSEUDO_PACKAGE_NAMES:
                continue
            if not all((name, ver, rel, arch)):
                raise RuntimeError(
                    "RPM query returned a package record with missing identity fields."
                )
            epoch = raw_epoch.strip()
            if epoch in {"", "(none)"}:
                epoch = "0"
            try:
                size_bytes = int(size_str)
            except ValueError as exc:
                raise RuntimeError(
                    "RPM query returned a package record with an invalid installed size."
                ) from exc
            if size_bytes < 0:
                raise RuntimeError(
                    "RPM query returned a package record with a negative installed size."
                )

            repo = "Fedora Project"
            packager_lower = packager.lower()
            vendor_lower = vendor.lower()
            if "copr" in packager_lower or "copr" in vendor_lower:
                repo = "COPR Repository"
            elif (
                "rpmfusion" in packager_lower
                or "rpmfusion" in vendor_lower
                or "rpm fusion" in packager_lower
                or "rpm fusion" in vendor_lower
            ):
                repo = "RPM Fusion"
            elif vendor:
                repo = vendor

            raw_dirs = raw_dirs_field.split(";")
            raw_provs = raw_provs_field.split(";")
            raw_base = raw_base_field.split(";")

            anatomy = PackagePhysicalAnatomy.from_manifest_data(
                raw_dirs, raw_provs, raw_base
            )

            owned_desktop_files = [b for b in raw_base if b.endswith(".desktop")]
            for did in anatomy.provided_desktop_ids:
                if did not in owned_desktop_files:
                    owned_desktop_files.append(did)

            if not owned_desktop_files and not anatomy.provided_desktop_ids:
                anatomy.has_desktop_file = False

            decision = engine_cls.classify(
                name=name,
                summary=summary,
                description="",
                anatomy=anatomy,
                desktop_entry_files=owned_desktop_files,
                provides=raw_provs,
                appstream=appstream,
                comps=comps,
                vendor=vendor,
                packager=packager,
            )

            packages.append(
                PackageInfo(
                    name=name,
                    version=ver,
                    release=rel,
                    arch=arch,
                    epoch=epoch,
                    summary=summary,
                    license=license_str,
                    url=url,
                    packager=packager,
                    vendor=vendor,
                    install_time=inst_time,
                    group=group or "General",
                    size_bytes=size_bytes,
                    state=PackageState.INSTALLED,
                    repository=repo,
                    is_orphan=False,
                    is_user_installed=False,
                    is_protected=(
                        name.lower() in protected_pkgs
                        or name in FEDORA_SYSTEM_ROOT_PILLARS
                    ),
                    **fields_fn(decision),
                )
            )

        return packages


class DependencyTreeWorker(ShutdownSafeRunnable):
    def __init__(
        self,
        root_package: str,
        max_depth: int = 1,
        target_index: Any | None = None,
        package_info: PackageInfo | None = None,
    ):
        super().__init__()
        self.signals = BackendSignals()
        self.root_package = root_package
        self.max_depth = max_depth
        self.target_index = target_index
        self.package_info = package_info
        self._is_cancelled = threading.Event()
        cache_cls = _backend_hook("SQLiteCapabilityCache", SQLiteCapabilityCache)
        self.cache = cache_cls.get_instance()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
        if in_flatpak():
            self.signals.error_occurred.emit(
                self.root_package,
                flatpak_read_only_unavailable("Dependency inspection"),
            )
            return

        create_ts = _backend_hook(
            "create_rpm_transaction_set", create_rpm_transaction_set
        )
        rpm_mod = _backend_hook("rpm", rpm)
        ts = create_ts()
        try:
            _raw_reqs, parsed_reqs = self._fetch_package_requires(
                self.root_package, ts, self.package_info
            )
            if not parsed_reqs or self._is_cancelled.is_set():
                self.signals.dependencies_resolved.emit(
                    self.root_package, [], self.target_index
                )
                return

            caps_to_query: list[str] = []
            for _, cap_name, _ in parsed_reqs:
                cached = self.cache.get(cap_name)
                if cached is None:
                    caps_to_query.append(cap_name)

            if caps_to_query and not self._is_cancelled.is_set():
                self._resolve_capabilities_batch(caps_to_query, ts)

            resolved_nodes: list[DependencyNode] = []
            seen_clean_names: set[str] = {self.root_package}

            provider_versions: dict[str, str] = {}
            if ts is not None:
                with RPM_GLOBAL_LOCK:
                    for _, cap_name, _ in parsed_reqs:
                        cached = self.cache.get(cap_name)
                        p_name = cached[1] if cached else cap_name
                        if p_name and p_name not in provider_versions:
                            match = None
                            try:
                                match = ts.dbMatch("name", p_name)
                                for hdr in match:
                                    v = _decode_rpm_str(hdr[rpm_mod.RPMTAG_VERSION])
                                    r = _decode_rpm_str(hdr[rpm_mod.RPMTAG_RELEASE])
                                    provider_versions[p_name] = f"{v}-{r}" if r else v
                                    break
                            finally:
                                del match

            for raw_req, cap_name, constraint in parsed_reqs:
                if self._is_cancelled.is_set():
                    return

                cached = self.cache.get(cap_name)
                is_sat, provider = cached or (False, cap_name)

                if provider in seen_clean_names:
                    continue
                seen_clean_names.add(provider)

                inst_ver = provider_versions.get(provider, "")

                resolved_nodes.append(
                    DependencyNode(
                        raw_requirement=raw_req,
                        resolved_package_name=provider,
                        version_constraint=constraint,
                        is_satisfied=is_sat,
                        is_cycle=False,
                        installed_version=inst_ver,
                    )
                )

            if not self._is_cancelled.is_set():
                self.signals.dependencies_resolved.emit(
                    self.root_package, resolved_nodes, self.target_index
                )

        except Exception as ex:
            self.signals.error_occurred.emit(
                self.root_package,
                tr("Dependency error: {detail}").format(detail=str(ex)),
            )
        finally:
            if ts is not None:
                del ts

    def _fetch_package_requires(
        self,
        pkg_name: str,
        ts: object | None,
        package_info: PackageInfo | None = None,
    ) -> tuple[list[str], list[tuple[str, str, str]]]:
        raw_reqs: list[str] = []
        parsed_reqs: list[tuple[str, str, str]] = []
        rpm_mod = _backend_hook("rpm", rpm)

        if ts is not None:
            package_found = False
            with RPM_GLOBAL_LOCK:
                match = None
                try:
                    match = ts.dbMatch("name", pkg_name)
                    for hdr in match:
                        if not _header_matches_package(hdr, package_info):
                            continue
                        package_found = True
                        requires = hdr[rpm_mod.RPMTAG_REQUIRENAME] or []
                        flags = hdr[rpm_mod.RPMTAG_REQUIREFLAGS] or []
                        versions = hdr[rpm_mod.RPMTAG_REQUIREVERSION] or []

                        for i, req in enumerate(requires):
                            req_str = _decode_rpm_str(req)
                            raw_reqs.append(req_str)

                            if req_str.startswith(
                                ("rpmlib(", "config(", "/", "rtld(", "(")
                            ):
                                continue

                            constraint = ""
                            if i < len(flags) and i < len(versions) and versions[i]:
                                flag = flags[i]
                                op = ""
                                if (flag & rpm_mod.RPMSENSE_LESS) and (
                                    flag & rpm_mod.RPMSENSE_EQUAL
                                ):
                                    op = "<="
                                elif (flag & rpm_mod.RPMSENSE_GREATER) and (
                                    flag & rpm_mod.RPMSENSE_EQUAL
                                ):
                                    op = ">="
                                elif flag & rpm_mod.RPMSENSE_LESS:
                                    op = "<"
                                elif flag & rpm_mod.RPMSENSE_GREATER:
                                    op = ">"
                                elif flag & rpm_mod.RPMSENSE_EQUAL:
                                    op = "="

                                ver_str = _decode_rpm_str(versions[i])
                                if op and ver_str:
                                    constraint = f"{op} {ver_str}"

                            parsed_reqs.append((req_str, req_str, constraint))
                        break
                finally:
                    del match
            if not package_found:
                raise LookupError(f"Package '{pkg_name}' is not installed.")
        else:
            cmd = ["rpm", "-qR", "--", _rpm_query_target(pkg_name, package_info)]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=get_clean_env(),
                check=False,
                timeout=5,
            )
            if proc.returncode != 0:
                detail = (
                    proc.stderr.strip()
                    or f"rpm requirement query exited with status {proc.returncode}."
                )
                raise RuntimeError(
                    f"Could not read requirements for '{pkg_name}': {detail}"
                )
            raw_reqs = proc.stdout.splitlines()

            for req in raw_reqs:
                req = req.strip()
                if not req or req.startswith(("rpmlib(", "config(", "/", "rtld(", "(")):
                    continue

                tokens = re.split(r"([<>=]+)", req, maxsplit=1)
                cap_name = tokens[0].strip()
                constraint = (tokens[1] + tokens[2]) if len(tokens) == 3 else ""
                parsed_reqs.append((req, cap_name, constraint))

        return raw_reqs, parsed_reqs

    def _resolve_capabilities_batch(self, capabilities: list[str], ts: object | None):
        batch_results: list[tuple[str, bool, str]] = []
        rpm_mod = _backend_hook("rpm", rpm)

        if ts is not None:
            with RPM_GLOBAL_LOCK:
                for cap in capabilities:
                    match_name = None
                    try:
                        match_name = ts.dbMatch("name", cap)
                        if match_name.count() > 0:
                            batch_results.append((cap, True, cap))
                            continue
                    finally:
                        del match_name

                    matches = None
                    provider = None
                    try:
                        matches = ts.dbMatch("providename", cap)
                        for hdr in matches:
                            provider = _decode_rpm_str(hdr[rpm_mod.RPMTAG_NAME])
                            break
                    finally:
                        del matches

                    if provider:
                        batch_results.append((cap, True, provider))
                    else:
                        clean_name = re.sub(r"\.so(\.[0-9]+)*(\([^\)]*\))?$", "", cap)
                        batch_results.append((cap, False, clean_name))
        else:
            clean_env = get_clean_env()
            for cap in capabilities:
                if self._is_cancelled.is_set():
                    return
                cmd = [
                    "rpm",
                    "-q",
                    "--whatprovides",
                    "--queryformat",
                    "%{NAME}\n",
                    "--",
                    cap,
                ]
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=clean_env,
                    check=False,
                    timeout=4,
                )
                first_line = (
                    proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
                )
                if proc.returncode == 0:
                    if not first_line:
                        raise RuntimeError(
                            f"rpm provider query returned no provider for '{cap}'."
                        )
                    batch_results.append((cap, True, first_line.strip()))
                elif proc.returncode == 1:
                    clean_name = re.sub(r"\.so(\.[0-9]+)*(\([^\)]*\))?$", "", cap)
                    batch_results.append((cap, False, clean_name))
                else:
                    detail = (
                        proc.stderr.strip()
                        or f"rpm provider query exited with status {proc.returncode}."
                    )
                    raise RuntimeError(
                        f"Could not resolve provider for '{cap}': {detail}"
                    )

        self.cache.set_batch(batch_results)


class ReverseDependencyWorker(ShutdownSafeRunnable):
    def __init__(self, target_package: str, package_info: PackageInfo | None = None):
        super().__init__()
        self.signals = BackendSignals()
        self.target_package = target_package
        self.package_info = package_info
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
        if in_flatpak():
            self.signals.error_occurred.emit(
                self.target_package,
                flatpak_read_only_unavailable("Reverse-dependency inspection"),
            )
            return

        create_ts = _backend_hook(
            "create_rpm_transaction_set", create_rpm_transaction_set
        )
        rpm_mod = _backend_hook("rpm", rpm)
        try:
            self.signals.status_update.emit(
                tr("Finding packages that depend on '{package}'...").format(
                    package=self.target_package
                )
            )

            ts = create_ts()
            reverse_nodes: list[DependencyNode] = []
            seen: set[str] = set()

            if ts is not None:
                with RPM_GLOBAL_LOCK:
                    capabilities: list[str] = [self.target_package]
                    target_found = False
                    target_match = None
                    try:
                        target_match = ts.dbMatch("name", self.target_package)
                        for hdr in target_match:
                            if not _header_matches_package(hdr, self.package_info):
                                continue
                            target_found = True
                            raw_provs = hdr[rpm_mod.RPMTAG_PROVIDENAME] or []
                            for p in raw_provs:
                                p_str = _decode_rpm_str(p)
                                if p_str and not p_str.startswith(
                                    ("rpmlib(", "config(")
                                ):
                                    capabilities.append(p_str)
                            break
                    finally:
                        del target_match
                    if not target_found:
                        raise LookupError(
                            f"Package '{self.target_package}' is not installed."
                        )

                    for cap in set(capabilities):
                        if self._is_cancelled.is_set():
                            del ts
                            return

                        matches = None
                        try:
                            matches = ts.dbMatch("requirename", cap)
                            for hdr in matches:
                                if self._is_cancelled.is_set():
                                    break

                                pkg_name = _decode_rpm_str(hdr[rpm_mod.RPMTAG_NAME])
                                if (
                                    pkg_name
                                    and pkg_name != self.target_package
                                    and pkg_name not in seen
                                ):
                                    seen.add(pkg_name)
                                    reverse_nodes.append(
                                        DependencyNode(
                                            raw_requirement=cap,
                                            resolved_package_name=pkg_name,
                                            is_satisfied=True,
                                            is_reverse=True,
                                        )
                                    )
                        finally:
                            del matches

                        if self._is_cancelled.is_set():
                            del ts
                            return
                    del ts
            else:
                caps_cmd = [
                    "rpm",
                    "-q",
                    "--provides",
                    "--",
                    _rpm_query_target(self.target_package, self.package_info),
                ]
                caps_proc = subprocess.run(
                    caps_cmd,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=get_clean_env(),
                    check=False,
                    timeout=8,
                )
                if caps_proc.returncode != 0:
                    detail = (
                        caps_proc.stderr.strip()
                        or f"rpm capability query exited with status {caps_proc.returncode}."
                    )
                    raise RuntimeError(
                        f"Could not read provided capabilities for '{self.target_package}': {detail}"
                    )

                caps_to_check = [self.target_package]
                for line in caps_proc.stdout.splitlines():
                    c_clean = line.split("=")[0].strip()
                    if c_clean and not c_clean.startswith(("rpmlib(", "config(")):
                        caps_to_check.append(c_clean)

                for cap in set(caps_to_check):
                    if self._is_cancelled.is_set():
                        return
                    cmd = [
                        "rpm",
                        "-q",
                        "--whatrequires",
                        "--queryformat",
                        "%{NAME}\\n",
                        "--",
                        cap,
                    ]
                    res = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        errors="replace",
                        env=get_clean_env(),
                        check=False,
                        timeout=6,
                    )
                    if res.returncode not in {0, 1}:
                        detail = (
                            res.stderr.strip()
                            or f"rpm reverse-dependency query exited with status {res.returncode}."
                        )
                        raise RuntimeError(
                            f"Could not find dependents for '{cap}': {detail}"
                        )
                    if res.returncode == 0 and not self._is_cancelled.is_set():
                        for line in res.stdout.splitlines():
                            package_name = line.strip()
                            if not package_name:
                                continue
                            if RPM_PACKAGE_NAME_PATTERN.fullmatch(package_name) is None:
                                raise RuntimeError(
                                    "rpm reverse-dependency query returned an invalid package-name record: "
                                    + line
                                )
                            if (
                                package_name not in seen
                                and package_name != self.target_package
                            ):
                                seen.add(package_name)
                                reverse_nodes.append(
                                    DependencyNode(
                                        raw_requirement=cap,
                                        resolved_package_name=package_name,
                                        is_satisfied=True,
                                        is_reverse=True,
                                    )
                                )

            if not self._is_cancelled.is_set():
                self.signals.reverse_dependencies_resolved.emit(
                    self.target_package, reverse_nodes
                )
                self.signals.status_update.emit(
                    tr_plural(
                        "Found {count} dependent for '{package}'.",
                        "Found {count} dependents for '{package}'.",
                        len(reverse_nodes),
                    ).format(
                        count=format_number(len(reverse_nodes)),
                        package=self.target_package,
                    )
                )

        except Exception as ex:
            self.signals.error_occurred.emit(
                self.target_package,
                tr("Reverse dependency error: {detail}").format(detail=str(ex)),
            )


class PackageFilesWorker(ShutdownSafeRunnable):
    def __init__(self, package_name: str, package_info: PackageInfo | None = None):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name
        self.package_info = package_info
        self._is_cancelled = threading.Event()

    def cancel(self):
        self._is_cancelled.set()

    @pyqtSlot()
    def run(self):
        in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
        if in_flatpak():
            self.signals.error_occurred.emit(
                self.package_name,
                flatpak_read_only_unavailable("Installed-package file inspection"),
            )
            return

        create_ts = _backend_hook(
            "create_rpm_transaction_set", create_rpm_transaction_set
        )
        rpm_mod = _backend_hook("rpm", rpm)
        try:
            files: list[PackageFileInfo] = []
            ts = create_ts()

            if ts is not None:
                package_found = False
                with RPM_GLOBAL_LOCK:
                    matches = None
                    try:
                        matches = ts.dbMatch("name", self.package_name)
                        for hdr in matches:
                            if not _header_matches_package(hdr, self.package_info):
                                continue
                            package_found = True
                            if self._is_cancelled.is_set():
                                return

                            filenames = hdr[rpm_mod.RPMTAG_FILENAMES] or []
                            filesizes = hdr[rpm_mod.RPMTAG_FILESIZES] or []
                            filemodes = hdr[rpm_mod.RPMTAG_FILEMODES] or []
                            if len(filesizes) != len(filenames) or len(
                                filemodes
                            ) != len(filenames):
                                raise RuntimeError(
                                    "Native RPM file manifest has mismatched path, size, or mode fields."
                                )

                            for i, raw_path in enumerate(filenames):
                                path = _decode_rpm_str(raw_path)
                                if not path.startswith("/"):
                                    raise RuntimeError(
                                        "Native RPM file manifest contains an invalid path."
                                    )
                                size = int(filesizes[i])
                                if size < 0:
                                    raise RuntimeError(
                                        "Native RPM file manifest contains a negative file size."
                                    )
                                mode_int = int(filemodes[i])
                                mode_octal = oct(mode_int)

                                is_dir = stat.S_ISDIR(mode_int)
                                is_config = path.startswith("/etc/")
                                is_executable = stat.S_ISREG(mode_int) and bool(
                                    mode_int & 0o111
                                )

                                files.append(
                                    PackageFileInfo(
                                        path=path,
                                        size_bytes=size,
                                        mode=mode_octal,
                                        is_dir=is_dir,
                                        is_config=is_config,
                                        is_executable=is_executable,
                                    )
                                )
                            break
                    finally:
                        del matches
                        del ts
                if not package_found:
                    raise LookupError(
                        f"Package '{self.package_name}' is not installed."
                    )
            else:
                field_separator = "\x1f"
                query_format = (
                    f"[%{{FILENAMES}}{field_separator}%{{FILESIZES}}"
                    f"{field_separator}%{{FILEMODES:octal}}\n]"
                )
                cmd = [
                    "rpm",
                    "-q",
                    "--queryformat",
                    query_format,
                    "--",
                    _rpm_query_target(self.package_name, self.package_info),
                ]
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=get_clean_env(),
                    check=False,
                    timeout=10,
                )

                if res.returncode != 0:
                    detail = (
                        res.stderr.strip()
                        or f"rpm file-list query exited with status {res.returncode}."
                    )
                    raise RuntimeError(
                        f"Could not read files for '{self.package_name}': {detail}"
                    )
                if not self._is_cancelled.is_set():
                    for line in res.stdout.splitlines():
                        if not line:
                            continue
                        parts = line.split(field_separator)
                        if len(parts) != 3:
                            raise RuntimeError(
                                "rpm file-list query returned a malformed file record; "
                                "refusing to display a partial manifest."
                            )
                        path, size_text, mode = parts
                        if not path.startswith("/"):
                            raise RuntimeError(
                                "rpm file-list query returned a file record with an invalid path."
                            )
                        try:
                            size = int(size_text)
                        except ValueError as exc:
                            raise RuntimeError(
                                "rpm file-list query returned a file record with an invalid size."
                            ) from exc
                        if size < 0:
                            raise RuntimeError(
                                "rpm file-list query returned a file record with a negative size."
                            )
                        if not mode:
                            raise RuntimeError(
                                "rpm file-list query returned a file record with an invalid mode."
                            )
                        try:
                            parsed_mode = int(mode, 8)
                        except ValueError:
                            is_dir = False
                            is_executable = "/bin/" in path or "/sbin/" in path
                        else:
                            is_dir = stat.S_ISDIR(parsed_mode)
                            is_executable = stat.S_ISREG(parsed_mode) and bool(
                                parsed_mode & 0o111
                            )
                        is_config = path.startswith("/etc/")

                        files.append(
                            PackageFileInfo(
                                path=path,
                                size_bytes=size,
                                mode=mode,
                                is_dir=is_dir,
                                is_config=is_config,
                                is_executable=is_executable,
                            )
                        )

            if not self._is_cancelled.is_set():
                self.signals.package_files_loaded.emit(self.package_name, files)

        except Exception as ex:
            self.signals.error_occurred.emit(
                self.package_name,
                tr("Could not read the package file list: {detail}").format(
                    detail=describe_subprocess_failure(ex)
                ),
            )


class PackageChangelogWorker(ShutdownSafeRunnable):
    """Read installed-package changelogs without hiding RPM or I/O failures."""

    def __init__(self, package_name: str, package_info: PackageInfo | None = None):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name
        self.package_info = package_info
        self._cve_pattern = CVE_REGEX_PATTERN

    @pyqtSlot()
    def run(self):
        in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
        if in_flatpak():
            self.signals.error_occurred.emit(
                self.package_name,
                flatpak_read_only_unavailable("Package changelog inspection"),
            )
            return

        create_ts = _backend_hook(
            "create_rpm_transaction_set", create_rpm_transaction_set
        )
        rpm_mod = _backend_hook("rpm", rpm)
        entries: list[PackageChangelogEntry] = []
        failures: list[str] = []
        ts = create_ts()
        if ts is not None:
            matches = None
            try:
                with RPM_GLOBAL_LOCK:
                    matches = ts.dbMatch("name", self.package_name)
                    for hdr in matches:
                        if not _header_matches_package(hdr, self.package_info):
                            continue
                        names = hdr[rpm_mod.RPMTAG_CHANGELOGNAME] or []
                        times = hdr[rpm_mod.RPMTAG_CHANGELOGTIME] or []
                        texts = hdr[rpm_mod.RPMTAG_CHANGELOGTEXT] or []
                        for index, raw_author in enumerate(names):
                            timestamp = int(times[index]) if index < len(times) else 0
                            text = (
                                _decode_rpm_str(texts[index])
                                if index < len(texts)
                                else ""
                            )
                            entries.append(
                                PackageChangelogEntry(
                                    author=_decode_rpm_str(raw_author),
                                    timestamp=timestamp,
                                    date_str=datetime.fromtimestamp(timestamp).strftime(
                                        "%Y-%m-%d"
                                    )
                                    if timestamp
                                    else "",
                                    text=text,
                                    cves=sorted(set(self._cve_pattern.findall(text))),
                                )
                            )
                        break
            except Exception as exc:
                entries.clear()
                failures.append(
                    f"native RPM changelog query failed: {describe_subprocess_failure(exc)}"
                )
            finally:
                if matches is not None:
                    del matches
                del ts

        if not entries:
            try:
                res = subprocess.run(
                    [
                        "rpm",
                        "-q",
                        "--changelog",
                        "--",
                        _rpm_query_target(self.package_name, self.package_info),
                    ],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=get_clean_env(),
                    check=False,
                    timeout=8,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(
                    "rpm is not installed on this system"
                    if isinstance(exc, FileNotFoundError)
                    else f"rpm changelog command failed: {describe_subprocess_failure(exc)}"
                )
            else:
                if res.returncode != 0:
                    failures.append(
                        res.stderr.strip()
                        or f"rpm changelog query exited with status {res.returncode}."
                    )
                else:
                    failures.clear()
                    header = ""
                    body: list[str] = []
                    for line in res.stdout.splitlines():
                        if line.startswith("* "):
                            if header:
                                content = "\n".join(body).strip()
                                entries.append(
                                    PackageChangelogEntry(
                                        author=header,
                                        timestamp=0,
                                        date_str="",
                                        text=content,
                                        cves=sorted(
                                            set(self._cve_pattern.findall(content))
                                        ),
                                    )
                                )
                            header, body = line[2:].strip(), []
                        else:
                            body.append(line)
                    if header:
                        content = "\n".join(body).strip()
                        entries.append(
                            PackageChangelogEntry(
                                author=header,
                                timestamp=0,
                                date_str="",
                                text=content,
                                cves=sorted(set(self._cve_pattern.findall(content))),
                            )
                        )
                    if res.stdout.strip() and not entries:
                        failures.append(
                            "rpm changelog output did not contain recognizable entries."
                        )

        if failures and not entries:
            self.signals.error_occurred.emit(
                self.package_name,
                tr("Could not read package changelog: {detail}").format(
                    detail=" | ".join(failures)
                ),
            )
            return
        self.signals.package_changelog_loaded.emit(self.package_name, entries)


class PackageScriptletsWorker(ShutdownSafeRunnable):
    """Read installed RPM scriptlets without misreporting failed reads as empty data."""

    def __init__(self, package_name: str, package_info: PackageInfo | None = None):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name
        self.package_info = package_info

    @pyqtSlot()
    def run(self):
        in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
        if in_flatpak():
            self.signals.error_occurred.emit(
                self.package_name,
                flatpak_read_only_unavailable("RPM scriptlet inspection"),
            )
            return

        create_ts = _backend_hook(
            "create_rpm_transaction_set", create_rpm_transaction_set
        )
        rpm_mod = _backend_hook("rpm", rpm)
        scripts = PackageScriptlets()
        failures: list[str] = []
        ts = create_ts()
        if ts is not None:
            matches = None
            try:
                with RPM_GLOBAL_LOCK:
                    matches = ts.dbMatch("name", self.package_name)
                    for hdr in matches:
                        if not _header_matches_package(hdr, self.package_info):
                            continue
                        native_tags = (
                            ("pretrans", "RPMTAG_PRETRANS", "RPMTAG_PRETRANSPROG"),
                            ("prein", "RPMTAG_PREIN", "RPMTAG_PREINPROG"),
                            ("postin", "RPMTAG_POSTIN", "RPMTAG_POSTINPROG"),
                            ("preun", "RPMTAG_PREUN", "RPMTAG_PREUNPROG"),
                            ("postun", "RPMTAG_POSTUN", "RPMTAG_POSTUNPROG"),
                            ("posttrans", "RPMTAG_POSTTRANS", "RPMTAG_POSTTRANSPROG"),
                            (
                                "preuntrans",
                                "RPMTAG_PREUNTRANS",
                                "RPMTAG_PREUNTRANSPROG",
                            ),
                            (
                                "postuntrans",
                                "RPMTAG_POSTUNTRANS",
                                "RPMTAG_POSTUNTRANSPROG",
                            ),
                            (
                                "verify",
                                "RPMTAG_VERIFYSCRIPT",
                                "RPMTAG_VERIFYSCRIPTPROG",
                            ),
                        )
                        for attr, body_tag_name, prog_tag_name in native_tags:
                            body_tag = getattr(rpm_mod, body_tag_name, None)
                            if body_tag is None:
                                continue
                            body = _decode_rpm_str(hdr[body_tag] or "").strip()
                            if not body:
                                prog_tag = getattr(rpm_mod, prog_tag_name, None)
                                raw_prog = (
                                    hdr[prog_tag] if prog_tag is not None else None
                                )
                                if isinstance(raw_prog, (list, tuple)):
                                    prog = " ".join(
                                        _decode_rpm_str(p).strip()
                                        for p in raw_prog
                                        if _decode_rpm_str(p).strip()
                                    )
                                else:
                                    prog = _decode_rpm_str(raw_prog or "").strip()
                                if prog:
                                    body = f"# program: {prog}"
                            if body:
                                setattr(scripts, attr, body)
                        break
            except Exception as exc:
                scripts = PackageScriptlets()
                failures.append(
                    f"native RPM scriptlet query failed: {describe_subprocess_failure(exc)}"
                )
            finally:
                if matches is not None:
                    del matches
                del ts

        if not scripts.has_any:
            try:
                script_env = get_clean_env()
                script_env["LC_ALL"] = "C"
                res = subprocess.run(
                    [
                        "rpm",
                        "-q",
                        "--scripts",
                        "--",
                        _rpm_query_target(self.package_name, self.package_info),
                    ],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=script_env,
                    check=False,
                    timeout=6,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(
                    "rpm is not installed on this system"
                    if isinstance(exc, FileNotFoundError)
                    else f"rpm scriptlet command failed: {describe_subprocess_failure(exc)}"
                )
            else:
                if res.returncode != 0:
                    failures.append(
                        res.stderr.strip()
                        or f"rpm scriptlet query exited with status {res.returncode}."
                    )
                else:
                    failures.clear()
                if res.returncode == 0 and res.stdout.strip():
                    current_tag: str | None = None
                    recognized_heading = False
                    buffer: list[str] = []
                    scriptlet_headings = {
                        "pretrans scriptlet": "pretrans",
                        "preinstall scriptlet": "prein",
                        "postinstall scriptlet": "postin",
                        "preuninstall scriptlet": "preun",
                        "postuninstall scriptlet": "postun",
                        "posttrans scriptlet": "posttrans",
                        "preuntrans scriptlet": "preuntrans",
                        "postuntrans scriptlet": "postuntrans",
                        "verify scriptlet": "verify",
                    }
                    program_headings = {
                        "pretrans program:": "pretrans",
                        "preinstall program:": "prein",
                        "postinstall program:": "postin",
                        "preuninstall program:": "preun",
                        "postuninstall program:": "postun",
                        "posttrans program:": "posttrans",
                        "preuntrans program:": "preuntrans",
                        "postuntrans program:": "postuntrans",
                        "verify program:": "verify",
                    }
                    for line in res.stdout.splitlines():
                        stripped_lower = line.strip().lower()
                        next_tag = next(
                            (
                                tag
                                for heading, tag in scriptlet_headings.items()
                                if stripped_lower.startswith(heading)
                                and stripped_lower.endswith(":")
                            ),
                            None,
                        )
                        if next_tag:
                            recognized_heading = True
                            if current_tag:
                                setattr(scripts, current_tag, "\n".join(buffer).strip())
                            current_tag, buffer = next_tag, []
                            continue
                        prog_match = next(
                            (
                                (heading, tag)
                                for heading, tag in program_headings.items()
                                if stripped_lower.startswith(heading)
                            ),
                            None,
                        )
                        if prog_match is not None:
                            recognized_heading = True
                            if current_tag:
                                setattr(scripts, current_tag, "\n".join(buffer).strip())
                                current_tag, buffer = None, []
                            heading, tag = prog_match
                            prog_value = line.strip()[len(heading) :].strip()
                            setattr(
                                scripts,
                                tag,
                                f"# program: {prog_value}" if prog_value else heading,
                            )
                            continue
                        buffer.append(line)
                    if current_tag:
                        setattr(scripts, current_tag, "\n".join(buffer).strip())
                    if not recognized_heading:
                        failures.append(
                            "rpm scriptlet output did not contain recognizable scriptlet headings."
                        )

        if failures and not scripts.has_any:
            self.signals.error_occurred.emit(
                self.package_name,
                tr("Could not read RPM scriptlets: {detail}").format(
                    detail=" | ".join(failures)
                ),
            )
            return
        self.signals.package_scriptlets_loaded.emit(self.package_name, scripts)


class PackageVerifyWorker(ShutdownSafeRunnable):
    def __init__(
        self,
        package_name: str,
        package_info: PackageInfo | None = None,
        *,
        ignore_mtime: bool | None = None,
    ):
        super().__init__()
        self.signals = BackendSignals()
        self.package_name = package_name
        self.package_info = package_info
        self._ignore_mtime_override = ignore_mtime
        self.ignored_mtime = False

    @pyqtSlot()
    def run(self):
        in_flatpak = _backend_hook("is_running_in_flatpak", is_running_in_flatpak)
        if in_flatpak():
            self.signals.error_occurred.emit(
                self.package_name,
                flatpak_read_only_unavailable("RPM file verification"),
            )
            return

        host_env_fn = _backend_hook("get_host_environment", _get_host_environment)
        ignore_mtime = (
            host_env_fn().normalizes_rpm_mtimes
            if self._ignore_mtime_override is None
            else bool(self._ignore_mtime_override)
        )
        self.ignored_mtime = ignore_mtime
        command = ["rpm", "-V"]
        if ignore_mtime:
            command.append("--nomtime")
        command.extend(("--", _rpm_query_target(self.package_name, self.package_info)))

        verify_env = get_clean_env()
        verify_env["LC_ALL"] = "C"

        try:
            res = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                env=verify_env,
                check=False,
                timeout=18,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.signals.error_occurred.emit(
                self.package_name,
                tr("Could not verify package files: {detail}").format(
                    detail=describe_subprocess_failure(exc)
                ),
            )
            return

        if res.returncode not in {0, 1} or (
            res.returncode == 1 and not res.stdout.strip() and res.stderr.strip()
        ):
            detail = (
                res.stderr.strip()
                or f"rpm verification exited with status {res.returncode}."
            )
            self.signals.error_occurred.emit(
                self.package_name,
                tr("Could not verify package files: {detail}").format(detail=detail),
            )
            return

        results: list[FileVerificationResult] = []
        unparsed_lines: list[str] = []
        for line in res.stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue

            parsed = parse_rpm_verify_line(line)
            if parsed is None:
                unparsed_lines.append(line)
                continue
            results.append(parsed)
        if unparsed_lines:
            self.signals.error_occurred.emit(
                self.package_name,
                tr("Could not verify package files: {detail}").format(
                    detail=(
                        "rpm verification output contained an unrecognized line: "
                        + unparsed_lines[0]
                    )
                ),
            )
            return
        self.signals.package_verification_finished.emit(self.package_name, results)


__all__ = (
    "BackendSignals",
    "DependencyTreeWorker",
    "PackageChangelogWorker",
    "PackageFilesWorker",
    "PackageQueryWorker",
    "PackageScriptletsWorker",
    "PackageVerifyWorker",
    "ReverseDependencyWorker",
)
