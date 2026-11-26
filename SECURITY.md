# Security Policy

Security is a core design constraint for **Dendro** (`io.github.xyasharx.Dendro`), because package management and repository configuration sit directly adjacent to root-level host state on Fedora Linux.

---

## Supported Versions

Security fixes are backported only to the active release line and supported Fedora platforms:

| Version / Target | Support Status | Notes |
| --- | --- | --- |
| Latest `2.x` release (`2.21.1`, `main`) | Supported | Receives security fixes and GitHub Security Advisories |
| Earlier `2.x` / `1.x` releases | Unsupported | Upgrade to the latest release; no backports |
| Fedora 43+ (`dnf5daemon-server >= 5.2.7`) | Supported | Requires supported `dnf5daemon-server` and `polkit` |
| End-of-life Fedora releases | Unsupported | Upstream platform packages no longer receive fixes |

**Distribution scope:**
- **Native RPM (COPR `xyasharx/dendro`):** Full feature set on traditional mutable Fedora RPM hosts, including the constrained Polkit repository-file helper (`tools/dendro-repo-helper.py` + `data/org.dendro.policy`).
- **AppImage / Source checkout:** Uses the host's DNF5 system daemon (`org.rpm.dnf.v0`) on mutable Fedora hosts; the `/etc/yum.repos.d/` helper is intentionally absent and unsupported outside RPM installations. On Fedora Atomic / OSTree, native composefs `bootc`, and read-only `/usr` hosts, operates strictly in read-only RPM inspection + Flatpak management mode.
- **Flatpak:** Targets traditional mutable Fedora DNF5 hosts through `--system-talk-name=org.rpm.dnf.v0` only; host RPM database reads, Flatpak management, and the repository helper are unavailable inside the sandbox.
- **Unbooted containers (`toolbx`, `distrobox`):** Read-only container RPM and DNF5 inspection only; `org.rpm.dnf.v0` and Polkit helpers are refused when the container does not run its own booted `systemd` PID 1.

---

## Reporting a Vulnerability

**Please do not open a public GitHub issue, pull request, or forum thread for an unpatched security vulnerability.**

### Preferred Channel: GitHub Private Vulnerability Reporting

Submit reports privately through GitHub Security Advisories:

- **Report URL:** `https://github.com/xyasharx/Dendro/security/advisories/new`

*(Maintainer note: If an encrypted email or PGP contact is later added, document it here; otherwise, GitHub Private Vulnerability Reporting keeps the report, discussion, and CVE/advisory coordination private between reporter and maintainer.)*

### What to Include

To speed up triage, please include as much of the following as possible:

1. **Installation form & version:** RPM (COPR), Flatpak, AppImage, or source checkout, plus full output of `dendro --version` (includes release version, Git commit, and Git tree ID).
2. **Environment:** Fedora version, desktop session (`Wayland` / `X11`), and `dnf5daemon-server` / `polkit` / `flatpak` versions (if relevant).
3. **Affected boundary & impact:** Which component or trust boundary is affected (e.g., Polkit repository helper, DNF5 transaction plan integrity, the Flatpak privilege path, signing-key trust prompts, Flatpak sandbox permissions, metadata XML parsing, URL/origin verification, or build/packaging).
4. **Reproduction:** Minimal step-by-step reproduction instructions, sample `.repo`/XML/package metadata input, or proof-of-concept.

### Disclosure & Response Process

- **Initial acknowledgement:** Within **3 business days**.
- **Triage & severity assessment:** Within **7 days**, including confirmation of whether the issue falls within Dendro's trust boundary or upstream Fedora components.
- **Coordinated disclosure:** Fixes are prepared privately in a GitHub Security Advisory temporary fork, tested across affected packaging targets (`dendro.spec`, `io.github.xyasharx.Dendro.yml`, `build_appimage.sh`), and published alongside a release and advisory (with CVE assignment when appropriate).
- **Credit:** Reporters may choose to be credited in the published advisory and release notes or remain anonymous.

---

## Trust Model & Scope

Dendro is explicitly structured around least privilege. Security reports affecting the following boundaries are **in scope**:

### In-Scope Security Boundaries

1. **Constrained Repository Helper & Polkit Policy**
   (`tools/dendro-repo-helper.py`, `data/org.dendro.policy`, `dendro/core/repository_addition.py`, `dendro/core/repository_deletion.py`)
   - Any bypass of the helper's option allowlist, HTTPS source requirement, GPG signature enforcement (`gpgcheck`), or duplicate repository ID checks.
   - Path traversal, symlink/TOCTOU races, or non-atomic/overwrite bugs affecting `/etc/yum.repos.d/`.
   - Discrepancies between the unprivileged `.repo` content validated in the UI/fetch path and what the root helper actually writes.
   - Any code path allowing non-RPM distributions (Flatpak, AppImage, source checkout) to spawn or reach a host helper.

2. **DNF5 Daemon Transactions & Operation Serialization**
   (`dendro/core/dnf5daemon.py`, `dendro/core/transaction_controller.py`, `dendro/core/operation_gate.py`, `dendro/ui/transaction_plan_dialog.py`)
   - Any ability to execute a transaction different from the resolved and retained plan displayed to the user.
   - Bypasses of `/etc/dnf/protected.d/` protected-package removal warnings.
   - Any regression introducing privileged `dnf` CLI execution (`pkexec dnf ...` / `sudo dnf ...`) instead of `org.rpm.dnf.v0`.
   - Concurrency/race conditions circumventing `dendro/core/operation_gate.py`.

3. **Flatpak Privilege Path & System-Wide Operations**
   (`dendro/core/flatpak_system_update.py`, `dendro/core/flatpak_system_uninstall.py`, `dendro/core/flatpak_runtime_removal.py`, `dendro/core/flatpak_uninstall.py`, `dendro/core/flatpak_cancel.py`)
   - Any attempt by Dendro to escalate privilege itself — `sudo`, `pkexec`, a setuid path, or `flatpak-spawn --host` — instead of letting **Flatpak's own Polkit helper** authorize the operation.
   - Dendro reading, storing, or echoing a password; or `--noninteractive` appearing on any **system-wide** Flatpak invocation, where it suppresses the Polkit prompt and fails the operation instead of authorizing it. (On `--user` operations the flag only suppresses progress output and is legitimate.)
   - A system-wide uninstall or update that does not route through `dendro/core/operation_gate.py`, and so can race a DNF5 transaction or another Flatpak operation.
   - Deletion of another user's application data, or a `--delete-data` path that is not scoped to the calling user.
   - Argument injection into the `flatpak` invocation, and any code path that invokes `flatpak` from a thread that blocks the Qt event loop while it prompts.

4. **Repository Signing-Key Trust Prompts**
   (`dendro/ui/key_import_dialog.py`, `dendro/core/repository_addition.py`, `dendro/core/repository_deletion.py`)
   - A prompt that can be satisfied by an unintended default: it must decline on Escape *and* on Enter, and a prompt that cannot be displayed must **decline** rather than trust an unseen key.
   - A "remember my choice" path that records trust the user did not grant, or that widens to keys other than the one whose fingerprint was shown.
   - Any discrepancy between the key identity, fingerprint, or download source displayed and the key the daemon actually imports.
   - Letting a key prompt stall the transaction until the daemon times out, instead of answering it.

5. **Flatpak Sandbox & Packaging Integrity**
   (`io.github.xyasharx.Dendro.yml`, `build_appimage.sh`, `dendro.spec`)
   - Unintended Flatpak permission expansion (anything beyond `--system-talk-name=org.rpm.dnf.v0` on the system bus, host filesystem mounts, `flatpak-spawn --host`, or host RPM MIME claims).
   - Unpinned, unverified, or non-reproducible external downloads in `io.github.xyasharx.Dendro.yml` or `build_appimage.sh`.

6. **Untrusted Metadata, XML, Advisories & Repository Origin Parsing**
   (`dendro/core/package_metadata.py`, `dendro/ui/advisory_dialog.py`, repository origin validation)
   - XML entity expansion (XXE / Billion Laughs) or unbounded memory/CPU consumption in Comps or AppStream catalog parsing (must use streaming `defusedxml`).
   - Spoofing Fedora, COPR, or RPM Fusion trust badges/origins via URL path, query, user-info (`@`), or lookalike hostname tricks rather than strict parsed `baseurl` hostname matching.
   - Rendering unescaped repository advisory descriptions as HTML or building clickable CVE/Bugzilla links from unvalidated, unencoded identifiers.

7. **Subprocess, Host-Environment & External Helper Invocation**
   (`dendro/core/backend.py`, `dendro/core/host_environment.py`, `dendro/core/rpmostree_status.py`, `dendro/core/process_utils.py`, `dendro/ui/external_links.py`, Flatpak helpers)
   - Argument injection, unsafe shell invocation (`shell=True`), missing `--` option terminators, or leaking AppImage `LD_LIBRARY_PATH` / `PYTHONHOME` / `QT_PLUGIN_PATH` variables when launching external desktop URL handlers (`DENDRO_ORIG_*` restoration in `data/AppRun` / `dendro/core/process_utils.py`).
   - Any bypass allowing live `org.rpm.dnf.v0` mutations or `tools/dendro-repo-helper.py` execution on Fedora Atomic / OSTree (including unlocked `usroverlay`), native composefs `bootc` deployments (including `bootc usr-overlay`), or read-only `/usr` hosts, or allowing container-to-host system-bus cross-talk from an unbooted container.
   - Any code path in `dendro/core/rpmostree_status.py` performing privileged mutations, invoking root-privileged `bootc status`, connecting to `rpm-ostree` transaction sockets, or trusting caller environment variables for host capability gating.

### Out of Scope / Non-Vulnerabilities

The following are generally **not** considered vulnerabilities in Dendro:

- **Upstream component bugs:** Vulnerabilities inside `dnf5daemon-server`, `libdnf5`, `librpm`, `polkit`, `flatpak`, `PyQt6`, or Qt itself should be reported to Fedora / Red Hat Bugzilla or the respective upstream project (though reports explaining how Dendro triggers or amplifies an upstream bug are welcome).
- **Expected Polkit-authorized actions:** Installing, updating, or removing packages or adding/removing a signed HTTPS `.repo` file after the user explicitly authenticates to Polkit.
- **Malicious third-party repositories or packages:** Risks arising from a user intentionally adding an untrusted repository or installing untrusted packages/scriptlets (Dendro exposes `%pre`/`%post`/`%preun`/`%postun` scriptlets for user review prior to installation).
- **Container / non-systemd limitations:** Unavailability of DNF5/Polkit mutations inside unbooted containers (e.g., `toolbox`, `webtop`) lacking systemd PID 1 and system D-Bus.
- **Build provenance strings:** `dendro --version` reports the source snapshot's Git commit and tree ID for diagnostics; it is not a cryptographic attestation of the build environment.

---

## Defensive Controls & Verification

Dendro relies on layered preventive checks in development and CI (`.github/workflows/ci.yml`):

- **Static & dependency security:** GitHub CodeQL (workflow and Python), Dependabot and Renovate for dependencies and pinned actions, GitHub secret scanning and code scanning, and `bandit -lll` over the security-sensitive modules. Actions are pinned by commit SHA.
- **Hardened parsing & pinned inputs:** Streaming `defusedxml` for all catalog XML, hash-pinned (`sha256`) offline Flatpak runtime dependencies, and checksum-verified AppImage build inputs. Build provenance (`dendro --version`) reports the Git commit and tree ID for diagnostics; it is not a build attestation, and is out of scope as a claim of one.
- **Privilege isolation:** Host mutations go exclusively through `org.rpm.dnf.v0` (for DNF5), Flatpak's own Polkit helper (system-wide Flatpak), or the single-purpose repository helper (RPM-only). Dendro never holds root, never reads a password, and serialises every host-affecting operation through `dendro/core/operation_gate.py`.
- **No live host mutations in CI:** `tests/` uses a typed fake D-Bus transport and mocked subprocess/Flatpak responses, so the suite runs in containers without Polkit, system D-Bus, or a real package database. A test that needs any of those is a bug in the test.
- **The trust model is a test surface, not just prose:** the invariants above — declining key prompts, no `sudo`/`pkexec`/`flatpak-spawn` in the Flatpak path, strict `baseurl` hostname matching, and read-only behaviour on Atomic/OSTree — are asserted by the suite, so a regression fails CI rather than shipping.
