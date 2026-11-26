"""Deterministic host-environment classification and capability policy.

Dendro runs across traditional mutable Fedora RPM installations, Flatpak
sandboxes, AppImages, Fedora Atomic / OSTree / composefs image-based hosts,
systems with read-only ``/usr``, and desktop containers such as Toolbx and
Distrobox.

Security invariant: capability decisions are derived strictly from kernel and
filesystem facts (``/.flatpak-info``, container marker files, ``/proc/1/comm``,
``/run/ostree-booted``, ``/ostree`` + ``/sysroot`` mount state, and
``statvfs('/usr')``). Caller-controlled environment variables (such as
``$container`` or ``$FLATPAK_ID``) and informational ``os-release`` fields
never enable or disable privileged capabilities.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

from dendro.core.i18n import tr

FLATPAK_INFO_PATH: Final[str] = "/.flatpak-info"
CONTAINERENV_PATH: Final[str] = "/run/.containerenv"
DOCKERENV_PATH: Final[str] = "/.dockerenv"
SYSTEMD_RUNTIME_PATH: Final[str] = "/run/systemd/system"
PID1_COMM_PATH: Final[str] = "/proc/1/comm"
OSTREE_BOOTED_PATH: Final[str] = "/run/ostree-booted"
OSTREE_DIR_PATH: Final[str] = "/ostree"
SYSROOT_PATH: Final[str] = "/sysroot"
SYSROOT_COMPOSEFS_PATH: Final[str] = "/sysroot/composefs"
PROC_MOUNTINFO_PATH: Final[str] = "/proc/self/mountinfo"
USR_PATH: Final[str] = "/usr"
OS_RELEASE_PATHS: Final[tuple[str, ...]] = (
    "/etc/os-release",
    "/usr/lib/os-release",
)

_MAX_OS_RELEASE_BYTES: Final[int] = 64 * 1024
_MAX_PID1_COMM_BYTES: Final[int] = 64
_MAX_MOUNTINFO_BYTES: Final[int] = 128 * 1024
_MAX_LABEL_LENGTH: Final[int] = 120


class HostEnvironmentKind(Enum):
    """Canonical runtime deployment environments recognized by Dendro."""

    TRADITIONAL_FEDORA = "traditional_fedora"
    ATOMIC_OSTREE = "atomic_ostree"
    READ_ONLY_USR = "read_only_usr"
    UNBOOTED_CONTAINER = "unbooted_container"
    FLATPAK_SANDBOX = "flatpak_sandbox"


class HostEnvironmentProbe(Protocol):
    """Injectable read-only kernel/filesystem probe boundary for testing."""

    def path_exists(self, path: str) -> bool:
        """Return whether ``path`` exists in the current mount namespace."""

    def is_dir(self, path: str) -> bool:
        """Return whether ``path`` is an existing directory."""

    def is_mountpoint(self, path: str) -> bool:
        """Return whether ``path`` is a distinct mount point."""

    def is_path_read_only(self, path: str) -> bool:
        """Return whether the filesystem backing ``path`` has ``ST_RDONLY`` set."""

    def read_bounded_text(self, path: str, max_bytes: int) -> str:
        """Read up to ``max_bytes`` of UTF-8 text from ``path``, or ``''``."""


class SystemHostEnvironmentProbe:
    """Production filesystem and VFS probe backed by the local kernel."""

    def path_exists(self, path: str) -> bool:
        try:
            return os.path.exists(path)
        except OSError:
            return False

    def is_dir(self, path: str) -> bool:
        try:
            return os.path.isdir(path)
        except OSError:
            return False

    def is_mountpoint(self, path: str) -> bool:
        try:
            return os.path.ismount(path)
        except OSError:
            return False

    def is_path_read_only(self, path: str) -> bool:
        try:
            vfs_status = os.statvfs(path)
        except OSError:
            return False
        return bool(vfs_status.f_flag & os.ST_RDONLY)

    def read_bounded_text(self, path: str, max_bytes: int) -> str:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return ""
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) and not path.startswith("/proc/"):
                return ""
            chunks: list[bytes] = []
            remaining = max_bytes
            while remaining > 0:
                chunk = os.read(descriptor, min(8192, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")
        except OSError:
            return ""
        finally:
            os.close(descriptor)


def sanitize_display_text(value: str, *, max_length: int = _MAX_LABEL_LENGTH) -> str:
    """Strip ASCII/C1 control characters and bound untrusted display labels."""
    cleaned_chars: list[str] = []
    for character in value:
        codepoint = ord(character)
        if codepoint < 32 or 0x7F <= codepoint <= 0x9F:
            cleaned_chars.append(" ")
        else:
            cleaned_chars.append(character)
    collapsed = " ".join("".join(cleaned_chars).split())
    if len(collapsed) <= max_length:
        return collapsed
    return f"{collapsed[: max_length - 1].rstrip()}…"


def parse_os_release(content: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` assignments from ``os-release(5)`` safely.

    Fields returned here are strictly informational display metadata and must
    never gate privileged operations.
    """
    entries: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not all(ch.isupper() or ch.isdigit() or ch == "_" for ch in key):
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            quote = value[0]
            inner = value[1:-1]
            if quote == '"':
                unescaped: list[str] = []
                index = 0
                while index < len(inner):
                    char = inner[index]
                    if char == "\\" and index + 1 < len(inner):
                        unescaped.append(inner[index + 1])
                        index += 2
                    else:
                        unescaped.append(char)
                        index += 1
                value = "".join(unescaped)
            else:
                value = inner
        sanitized = sanitize_display_text(value)
        if sanitized:
            entries[key] = sanitized
    return entries


def _read_os_release(probe: HostEnvironmentProbe) -> tuple[str, str]:
    for candidate in OS_RELEASE_PATHS:
        content = probe.read_bounded_text(candidate, _MAX_OS_RELEASE_BYTES)
        if not content:
            continue
        parsed = parse_os_release(content)
        pretty_name = parsed.get("PRETTY_NAME") or parsed.get("NAME", "")
        variant_id = parsed.get("VARIANT_ID", "").casefold()
        if pretty_name or variant_id:
            return pretty_name, variant_id
    return "", ""


def _is_booted_systemd_init(probe: HostEnvironmentProbe) -> bool:
    """Return True only when this namespace runs its own booted systemd PID 1.

    Checking ``/proc/1/comm`` prevents a desktop container (such as Distrobox)
    that bind-mounts host runtime sockets from being mistaken for a booted
    system owning its own ``org.rpm.dnf.v0`` D-Bus service.
    """
    if not probe.path_exists(SYSTEMD_RUNTIME_PATH):
        return False
    comm = probe.read_bounded_text(PID1_COMM_PATH, _MAX_PID1_COMM_BYTES).strip()
    return comm == "systemd"


def _has_ostree_host_markers(probe: HostEnvironmentProbe) -> bool:
    """Detect both classic ``/run/ostree-booted`` and composefs OSTree layouts.

    Intentionally evaluated before ``statvfs('/usr')`` so an active transient
    ``'rpm-ostree usroverlay'`` or ``'bootc usr-overlay'`` cannot trick Dendro
    into running persistent DNF5 transactions against a volatile ``/usr``.
    """
    if probe.path_exists(OSTREE_BOOTED_PATH):
        return True
    return probe.is_dir(OSTREE_DIR_PATH) and probe.is_mountpoint(SYSROOT_PATH)


def _has_composefs_root_mount(probe: HostEnvironmentProbe) -> bool:
    """Return True when ``/`` is mounted from a native ``composefs`` source."""
    content = probe.read_bounded_text(PROC_MOUNTINFO_PATH, _MAX_MOUNTINFO_BYTES)
    if not content:
        return False
    for raw_line in content.splitlines():
        left, separator, right = raw_line.partition(" - ")
        if not separator:
            continue
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or len(right_fields) < 2:
            continue
        mount_point = left_fields[4]
        fs_type = right_fields[0]
        mount_source = right_fields[1]
        if mount_point != "/" or fs_type != "overlay":
            continue
        if mount_source.startswith(("composefs:", "transient:composefs=")):
            return True
    return False


def _has_composefs_bootc_markers(probe: HostEnvironmentProbe) -> bool:
    """Detect native composefs ``bootc`` deployments without OSTree.

    Pure composefs-native ``bootc`` boots omit ``/run/ostree-booted`` and
    ``/ostree`` while mounting ``/`` from a ``composefs:`` (or volatile
    ``transient:composefs=``) overlay backed by ``/sysroot/composefs``.
    Evaluated before ``statvfs('/usr')`` so an active ``'bootc usr-overlay'``
    still locks Dendro out of persistent RPM/DNF5 mutations.
    """
    if probe.is_dir(SYSROOT_COMPOSEFS_PATH) and probe.is_mountpoint(SYSROOT_PATH):
        return True
    return _has_composefs_root_mount(probe)


@dataclass(frozen=True, slots=True)
class HostEnvironment:
    """Immutable snapshot of the runtime host mode and allowed capabilities."""

    kind: HostEnvironmentKind
    os_pretty_name: str = ""
    os_variant_id: str = ""
    usr_read_only: bool = False
    ostree_booted: bool = False
    bootc_composefs: bool = False
    in_container: bool = False
    container_booted_systemd: bool = False

    @classmethod
    def traditional(cls, *, os_pretty_name: str = "") -> HostEnvironment:
        """Construct a standard mutable Fedora RPM host descriptor."""
        return cls(
            kind=HostEnvironmentKind.TRADITIONAL_FEDORA,
            os_pretty_name=os_pretty_name,
        )

    @classmethod
    def flatpak(cls, *, os_pretty_name: str = "") -> HostEnvironment:
        """Construct a Flatpak sandbox environment descriptor."""
        return cls(
            kind=HostEnvironmentKind.FLATPAK_SANDBOX,
            os_pretty_name=os_pretty_name,
            usr_read_only=True,
            in_container=True,
        )

    @property
    def is_flatpak(self) -> bool:
        return self.kind is HostEnvironmentKind.FLATPAK_SANDBOX

    @property
    def is_atomic_ostree(self) -> bool:
        return self.kind is HostEnvironmentKind.ATOMIC_OSTREE

    @property
    def is_read_only_usr(self) -> bool:
        return self.kind is HostEnvironmentKind.READ_ONLY_USR

    @property
    def is_bootc_composefs(self) -> bool:
        """Whether this host booted a native composefs ``bootc`` image without OSTree."""
        return self.bootc_composefs

    @property
    def is_immutable_host(self) -> bool:
        return self.kind in {
            HostEnvironmentKind.ATOMIC_OSTREE,
            HostEnvironmentKind.READ_ONLY_USR,
        }

    @property
    def is_unbooted_container(self) -> bool:
        return self.kind is HostEnvironmentKind.UNBOOTED_CONTAINER

    @property
    def supports_local_rpm_inspection(self) -> bool:
        """Installed RPM and local ``.rpm`` header inspection via ``librpm``/``rpm``."""
        return not self.is_flatpak

    @property
    def supports_dnf5_daemon_mutations(self) -> bool:
        """Install, remove, upgrade, repository toggle, and cache clean via DNF5."""
        return self.kind in {
            HostEnvironmentKind.TRADITIONAL_FEDORA,
            HostEnvironmentKind.FLATPAK_SANDBOX,
        }

    @property
    def supports_dnf5_daemon_queries(self) -> bool:
        """Update checks and repository listing via ``org.rpm.dnf.v0``."""
        return self.kind in {
            HostEnvironmentKind.TRADITIONAL_FEDORA,
            HostEnvironmentKind.FLATPAK_SANDBOX,
        }

    @property
    def supports_repository_helper(self) -> bool:
        """Privileged ``/etc/yum.repos.d`` add/delete via ``dendro-repo-helper``."""
        return self.kind is HostEnvironmentKind.TRADITIONAL_FEDORA

    @property
    def supports_dnf_cli_queries(self) -> bool:
        """Unprivileged read-only ``'dnf5'`` metadata, history, and search queries."""
        return self.kind in {
            HostEnvironmentKind.TRADITIONAL_FEDORA,
            HostEnvironmentKind.UNBOOTED_CONTAINER,
        }

    @property
    def supports_orphan_detection(self) -> bool:
        """Unneeded leaf-dependency inspection via local DNF system state."""
        return self.kind in {
            HostEnvironmentKind.TRADITIONAL_FEDORA,
            HostEnvironmentKind.UNBOOTED_CONTAINER,
        }

    @property
    def supports_rpmostree_readonly(self) -> bool:
        """Unprivileged read-only deployment queries via ``org.projectatomic.rpmostree1``.

        Fedora Atomic Desktops (43-45) run ``rpm-ostreed``, so unprivileged
        D-Bus ``Deployments`` queries work natively on ``ATOMIC_OSTREE`` hosts.
        Pure image-mode ``bootc`` installations without ``rpm-ostreed`` fall
        back gracefully to unavailable deployment metadata (or composefs
        ``READ_ONLY_USR``) because ``bootc status --json`` requires root
        (``CAP_SYS_ADMIN``) and Dendro never spawns privileged CLI commands for
        read-only inspection.
        """
        return self.kind is HostEnvironmentKind.ATOMIC_OSTREE

    @property
    def supports_flatpak_management(self) -> bool:
        """Direct host ``'/usr/bin/flatpak'`` app/runtime inspection and updates."""
        return not self.is_flatpak

    @property
    def normalizes_rpm_mtimes(self) -> bool:
        """Whether OSTree normalizes ``/usr`` file mtimes to Unix epoch 0."""
        return self.ostree_booted

    def display_label(self) -> str:
        """Short, human-readable runtime mode label for About and diagnostics.

        Only the mode name is translated: the distribution name in the suffix
        and the product names (Fedora, DNF5, Flatpak) read the same in every
        language, and a catalog entry that embedded them would have to be
        updated whenever a test host reported a different OS.
        """
        suffix = f" ({self.os_pretty_name})" if self.os_pretty_name else ""
        if self.kind is HostEnvironmentKind.ATOMIC_OSTREE:
            return f"{tr('Fedora Atomic Read-Only RPM Mode')}{suffix}"
        if self.kind is HostEnvironmentKind.READ_ONLY_USR:
            immutable = tr("Immutable Host — Read-Only '/usr'")
            return f"{immutable}{suffix}"
        if self.kind is HostEnvironmentKind.UNBOOTED_CONTAINER:
            return f"{tr('Container Read-Only Mode')}{suffix}"
        if self.kind is HostEnvironmentKind.FLATPAK_SANDBOX:
            return tr("Flatpak Sandbox (DNF5 System Bus)")
        if self.in_container and self.container_booted_systemd:
            return f"{tr('Booted Fedora Container (DNF5 Daemon)')}{suffix}"
        return f"{tr('Fedora RPM Host (DNF5 Daemon)')}{suffix}"

    def mode_banner(self) -> tuple[str, str] | None:
        """Return ``(title, detail)`` for non-standard host modes, else ``None``."""
        if self.kind is HostEnvironmentKind.ATOMIC_OSTREE:
            title = tr("Fedora Atomic Read-Only RPM Mode")
            if self.os_pretty_name:
                title = f"{title} — {self.os_pretty_name}"
            detail = tr(
                "Installed RPM inspection, dependency trees, layered-package status, "
                "'rpm -V --nomtime' integrity audits, and Flatpak management are active. "
                "Live DNF5 package and repository changes are disabled to protect your "
                "immutable deployment."
            )
            return title, detail
        if self.kind is HostEnvironmentKind.READ_ONLY_USR:
            title = tr("Read-Only '/usr' Mode")
            if self.os_pretty_name:
                title = f"{title} — {self.os_pretty_name}"
            detail = tr(
                "Installed RPM inspection, dependency trees, file verification, and "
                "Flatpak management are active. DNF5 package and repository changes are "
                "disabled because '/usr' is mounted read-only."
            )
            return title, detail
        if self.kind is HostEnvironmentKind.UNBOOTED_CONTAINER:
            title = tr("Container Inspection Mode")
            detail = tr(
                "Inspecting packages inside this container. DNF5 system-daemon mutations "
                "and Polkit repository changes are disabled because this container is not "
                "running its own booted 'systemd' bus, avoiding cross-talk with the host."
            )
            return title, detail
        return None

    def dnf5_daemon_unavailable(self, action: str) -> tuple[str, str]:
        """Return ``(title, detail)`` when ``org.rpm.dnf.v0`` is disallowed."""
        # ``action`` is itself a translated phrase, so it is substituted into an
        # already-translated template rather than being part of the message id:
        # translating a sentence with a hole in it would leave the hole's
        # position fixed to English word order.
        if self.kind is HostEnvironmentKind.ATOMIC_OSTREE:
            return (
                tr("DNF5 operations are disabled on Fedora Atomic"),
                tr(
                    "Dendro cannot {action} through 'org.rpm.dnf.v0' on an image-based "
                    "Fedora Atomic / OSTree system. Running live DNF5 mutations or "
                    "repository overrides would conflict with 'rpm-ostree' and read-only "
                    "'/usr'. Installed RPM inspection and Flatpak management remain "
                    "available."
                ).format(action=action),
            )
        if self.kind is HostEnvironmentKind.READ_ONLY_USR:
            return (
                tr("DNF5 operations are disabled on read-only '/usr'"),
                tr(
                    "Dendro cannot {action} through 'org.rpm.dnf.v0' because '/usr' is "
                    "mounted read-only on this system. Installed RPM inspection and "
                    "Flatpak management remain available."
                ).format(action=action),
            )
        if self.kind is HostEnvironmentKind.UNBOOTED_CONTAINER:
            return (
                tr("DNF5 daemon is unavailable in this container"),
                tr(
                    "Dendro cannot {action} because this container does not run its own "
                    "booted 'systemd' system bus. Dendro refuses to use a forwarded host "
                    "system bus from inside a container. Local container RPM inspection "
                    "remains available."
                ).format(action=action),
            )
        return (
            tr("DNF5 daemon is unavailable"),
            tr("Dendro cannot {action} in the current runtime environment.").format(
                action=action
            ),
        )

    def repository_helper_unavailable(self, operation: str) -> tuple[str, str]:
        """Return ``(title, detail)`` when ``dendro-repo-helper`` is disallowed.

        ``operation`` should be a noun such as ``'addition'`` or ``'deletion'``.
        """
        label = operation.strip().lower() or "modification"
        operation_phrases = {
            "addition": tr("Repository addition"),
            "deletion": tr("Repository deletion"),
            "modification": tr("Repository modification"),
        }
        capitalized = operation_phrases.get(
            label,
            tr("Repository {operation}").format(operation=tr(label)),
        )
        if self.kind is HostEnvironmentKind.FLATPAK_SANDBOX:
            return (
                tr("{operation} is unavailable in Flatpak").format(
                    operation=capitalized
                ),
                tr(
                    "Flatpak Dendro can use the narrowly permitted DNF5 system service, "
                    "but it cannot launch a host privileged helper."
                ),
            )
        if self.kind is HostEnvironmentKind.ATOMIC_OSTREE:
            return (
                tr("{operation} is disabled on Fedora Atomic").format(
                    operation=capitalized
                ),
                tr(
                    "{operation} through Dendro's repository helper is disabled on "
                    "Fedora Atomic and OSTree hosts so '/etc/yum.repos.d' and DNF5 "
                    "overrides cannot drift from your image deployment."
                ).format(operation=capitalized),
            )
        if self.kind is HostEnvironmentKind.READ_ONLY_USR:
            if self.bootc_composefs:
                return (
                    tr("{operation} is disabled on bootc image-mode hosts").format(
                        operation=capitalized
                    ),
                    tr(
                        "{operation} through Dendro's repository helper is disabled on "
                        "native composefs bootc deployments so '/etc/yum.repos.d' cannot "
                        "drift from your OS image, even when 'bootc usr-overlay' is active."
                    ).format(operation=capitalized),
                )
            return (
                tr("{operation} is disabled on read-only '/usr'").format(
                    operation=capitalized
                ),
                tr(
                    "{operation} through Dendro's repository helper is disabled "
                    "because '/usr' is mounted read-only on this immutable host."
                ).format(operation=capitalized),
            )
        if self.kind is HostEnvironmentKind.UNBOOTED_CONTAINER:
            return (
                tr("{operation} is unavailable in this container").format(
                    operation=capitalized
                ),
                tr(
                    "{operation} through Polkit is disabled inside an unbooted "
                    "container without its own 'systemd' session and system bus."
                ).format(operation=capitalized),
            )
        return (
            tr("{operation} helper is unavailable").format(operation=capitalized),
            tr("{operation} is not supported in this runtime environment.").format(
                operation=capitalized
            ),
        )

    def feature_unavailable(self, feature: str) -> str:
        """Return a concise explanation for an unavailable inspection feature."""
        feature_key = feature.casefold()
        feature_label = tr(feature)
        if self.kind is HostEnvironmentKind.FLATPAK_SANDBOX:
            return tr(
                "{feature} is not available in Flatpak yet because the Flatpak "
                "sandbox does not expose the host RPM database or run host commands."
            ).format(feature=feature_label)
        if self.kind is HostEnvironmentKind.ATOMIC_OSTREE:
            if "unneeded" in feature_key or "orphan" in feature_key:
                return tr(
                    "{feature} does not apply on Fedora Atomic: base packages belong "
                    "to the immutable OS image and layered dependencies are recomputed "
                    "automatically across deployments."
                ).format(feature=feature_label)
            if "history" in feature_key:
                return tr(
                    "{feature} is unavailable on Fedora Atomic because system changes "
                    "are tracked as atomic OSTree deployments rather than live DNF5 "
                    "transactions. Use 'rpm-ostree status' (or 'bootc status' on "
                    "bootc hosts) to inspect deployments."
                ).format(feature=feature_label)
            if "remote" in feature_key or "search" in feature_key:
                return tr(
                    "{feature} via DNF5 is disabled on Fedora Atomic; installed RPM "
                    "browsing, dependency analysis, and Flatpak management remain active."
                ).format(feature=feature_label)
            if "update" in feature_key:
                return tr(
                    "{feature} via the DNF5 daemon is disabled on Fedora Atomic because "
                    "OS updates are delivered as atomic deployments rather than live "
                    "per-package DNF5 upgrades."
                ).format(feature=feature_label)
            if "user-installed" in feature_key:
                return tr(
                    "{feature} via DNF5 is disabled on Fedora Atomic; Dendro reads "
                    "layered packages from 'org.projectatomic.rpmostree1' when available."
                ).format(feature=feature_label)
            return tr(
                "{feature} is disabled on Fedora Atomic to avoid conflicting with the "
                "immutable OSTree deployment."
            ).format(feature=feature_label)
        if self.kind is HostEnvironmentKind.READ_ONLY_USR:
            if self.bootc_composefs:
                if "history" in feature_key:
                    return tr(
                        "{feature} is unavailable on bootc image-mode hosts because "
                        "system updates are deployed as immutable composefs container "
                        "images rather than live DNF5 transactions. Use 'bootc status' "
                        "to inspect deployments."
                    ).format(feature=feature_label)
                return tr(
                    "{feature} is disabled on this immutable composefs bootc image. "
                    "Installed RPM inspection and Flatpak management remain available."
                ).format(feature=feature_label)
            return tr(
                "{feature} is disabled because '/usr' is mounted read-only on this "
                "immutable system. Installed RPM inspection and Flatpak management "
                "remain available."
            ).format(feature=feature_label)
        if self.kind is HostEnvironmentKind.UNBOOTED_CONTAINER:
            return tr(
                "{feature} is disabled because this container is not running its own "
                "booted 'systemd' system bus."
            ).format(feature=feature_label)
        return tr(
            "{feature} is unavailable in the current runtime environment."
        ).format(feature=feature_label)


def detect_host_environment(
    probe: HostEnvironmentProbe | None = None,
) -> HostEnvironment:
    """Classify the active host environment from read-only filesystem probes.

    Evaluation order is security-critical:
    1. ``/.flatpak-info`` (Flatpak runtimes also have container markers and
       read-only ``/usr``; sandbox rules must take precedence).
    2. Container markers (``/run/.containerenv``, ``/.dockerenv``), separating
       unbooted desktop containers (Toolbx, Distrobox, CI containers) from
       booted ``systemd`` containers that own their own system bus.
    3. OSTree Atomic host markers (``/run/ostree-booted`` or ``/ostree`` with a
       mounted ``/sysroot``), evaluated *before* ``statvfs('/usr')`` so an
       unlocked volatile ``usroverlay`` cannot masquerade as a mutable host.
    4. Native composefs ``bootc`` markers (``/sysroot/composefs`` with a mounted
       ``/sysroot``, or ``/`` mounted from ``composefs:`` / ``transient:composefs=``
       in ``/proc/self/mountinfo``), also evaluated *before* ``statvfs('/usr')``
       so ``'bootc usr-overlay'`` stays locked immutable.
    5. Read-only ``/usr`` (``ST_RDONLY``) for other immutable systems.
    6. Traditional mutable Fedora RPM host.
    """
    active_probe = probe if probe is not None else SystemHostEnvironmentProbe()

    if active_probe.path_exists(FLATPAK_INFO_PATH):
        return HostEnvironment.flatpak()

    pretty_name, variant_id = _read_os_release(active_probe)
    usr_read_only = active_probe.is_path_read_only(USR_PATH)

    in_container = active_probe.path_exists(
        CONTAINERENV_PATH
    ) or active_probe.path_exists(DOCKERENV_PATH)
    if in_container:
        booted_systemd = _is_booted_systemd_init(active_probe)
        if not booted_systemd:
            return HostEnvironment(
                kind=HostEnvironmentKind.UNBOOTED_CONTAINER,
                os_pretty_name=pretty_name,
                os_variant_id=variant_id,
                usr_read_only=usr_read_only,
                ostree_booted=False,
                in_container=True,
                container_booted_systemd=False,
            )
        if usr_read_only:
            return HostEnvironment(
                kind=HostEnvironmentKind.READ_ONLY_USR,
                os_pretty_name=pretty_name,
                os_variant_id=variant_id,
                usr_read_only=True,
                ostree_booted=False,
                in_container=True,
                container_booted_systemd=True,
            )
        return HostEnvironment(
            kind=HostEnvironmentKind.TRADITIONAL_FEDORA,
            os_pretty_name=pretty_name,
            os_variant_id=variant_id,
            usr_read_only=False,
            ostree_booted=False,
            in_container=True,
            container_booted_systemd=True,
        )

    if _has_ostree_host_markers(active_probe):
        return HostEnvironment(
            kind=HostEnvironmentKind.ATOMIC_OSTREE,
            os_pretty_name=pretty_name,
            os_variant_id=variant_id,
            usr_read_only=usr_read_only,
            ostree_booted=True,
            in_container=False,
            container_booted_systemd=False,
        )

    if _has_composefs_bootc_markers(active_probe):
        return HostEnvironment(
            kind=HostEnvironmentKind.READ_ONLY_USR,
            os_pretty_name=pretty_name,
            os_variant_id=variant_id,
            usr_read_only=usr_read_only,
            ostree_booted=False,
            bootc_composefs=True,
            in_container=False,
            container_booted_systemd=False,
        )

    if usr_read_only:
        return HostEnvironment(
            kind=HostEnvironmentKind.READ_ONLY_USR,
            os_pretty_name=pretty_name,
            os_variant_id=variant_id,
            usr_read_only=True,
            ostree_booted=False,
            in_container=False,
            container_booted_systemd=False,
        )

    return HostEnvironment(
        kind=HostEnvironmentKind.TRADITIONAL_FEDORA,
        os_pretty_name=pretty_name,
        os_variant_id=variant_id,
        usr_read_only=False,
        ostree_booted=False,
        in_container=False,
        container_booted_systemd=False,
    )


__all__ = [
    "CONTAINERENV_PATH",
    "DOCKERENV_PATH",
    "FLATPAK_INFO_PATH",
    "OSTREE_BOOTED_PATH",
    "OSTREE_DIR_PATH",
    "OS_RELEASE_PATHS",
    "PID1_COMM_PATH",
    "PROC_MOUNTINFO_PATH",
    "SYSROOT_COMPOSEFS_PATH",
    "SYSROOT_PATH",
    "SYSTEMD_RUNTIME_PATH",
    "USR_PATH",
    "HostEnvironment",
    "HostEnvironmentKind",
    "HostEnvironmentProbe",
    "SystemHostEnvironmentProbe",
    "detect_host_environment",
    "parse_os_release",
    "sanitize_display_text",
]
