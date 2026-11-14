# Security Policy

Security is a core design constraint for **Dendro** (`io.github.xyasharx.Dendro`), because package management and repository configuration sit directly adjacent to root-level host state on Fedora Linux.

---

## Supported Versions

Security fixes are backported only to the active release line and supported Fedora platforms:

| Version / Target | Support Status | Notes |
| --- | --- | --- |
| Latest `2.x` release (`main`) | Supported | Receives security fixes and GitHub Security Advisories |
| Earlier `2.x` / `1.x` releases | Unsupported | Upgrade to the latest release |
| Fedora 41+ (`dnf5daemon-server >= 5.2.7`) | Supported | Requires supported `dnf5daemon-server` and `polkit` |
| End-of-life Fedora releases | Unsupported | Upstream platform packages no longer receive fixes |

**Distribution scope:**
- **Native RPM (COPR `xyasharx/dendro`):** Full feature set, including the constrained Polkit repository-file helper (`tools/dendro-repo-helper.py` + `data/org.dendro.policy`).
- **Flatpak / AppImage / Source checkout:** Uses the host's DNF5 system daemon (`org.rpm.dnf.v0`) where available; the `/etc/yum.repos.d/` helper is intentionally absent and unsupported outside RPM installations.

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
3. **Affected boundary & impact:** Which component or trust boundary is affected (e.g., Polkit helper, DNF5 transaction plan integrity, Flatpak sandbox permissions, metadata XML parsing, URL/origin verification, or build/packaging).
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
   (`tools/dendro-repo-helper.py`, `data/org.dendro.policy`, `core/repository_addition.py`, `core/repository_deletion.py`)
   - Any bypass of the helper's option allowlist, HTTPS source requirement, GPG signature enforcement (`gpgcheck`), or duplicate repository ID checks.
   - Path traversal, symlink/TOCTOU races, or non-atomic/overwrite bugs affecting `/etc/yum.repos.d/`.
   - Discrepancies between the unprivileged `.repo` content validated in the UI/fetch path and what the root helper actually writes.
   - Any code path allowing non-RPM distributions (Flatpak, AppImage, source checkout) to spawn or reach a host helper.

2. **DNF5 Daemon Transactions & Operation Serialization**
   (`core/dnf5daemon.py`, `core/transaction_controller.py`, `core/operation_gate.py`, `ui/transaction_plan_dialog.py`)
   - Any ability to execute a transaction different from the resolved and retained plan displayed to the user.
   - Bypasses of `/etc/dnf/protected.d/` protected-package removal warnings.
   - Any regression introducing privileged `dnf` CLI execution (`pkexec dnf ...` / `sudo dnf ...`) instead of `org.rpm.dnf.v0`.
   - Concurrency/race conditions circumventing `core/operation_gate.py`.

3. **Flatpak Sandbox & Packaging Integrity**
   (`io.github.xyasharx.Dendro.yml`, `build_appimage.sh`, `dendro.spec`)
   - Unintended Flatpak permission expansion (anything beyond `--system-talk-name=org.rpm.dnf.v0` on the system bus, host filesystem mounts, `flatpak-spawn --host`, or host RPM MIME claims).
   - Unpinned, unverified, or non-reproducible external downloads in `io.github.xyasharx.Dendro.yml` or `build_appimage.sh`.

4. **Untrusted Metadata, XML & Repository Origin Parsing**
   (`core/package_metadata.py`, repository origin validation)
   - XML entity expansion (XXE / Billion Laughs) or unbounded memory/CPU consumption in Comps or AppStream catalog parsing (must use streaming `defusedxml`).
   - Spoofing Fedora, COPR, or RPM Fusion trust badges/origins via URL path, query, user-info (`@`), or lookalike hostname tricks rather than strict parsed `baseurl` hostname matching.

5. **Subprocess & External Helper Invocation**
   (`core/backend.py`, external URL/verification/Flatpak helpers)
   - Argument injection, unsafe shell invocation (`shell=True`), or missing `--` option terminators when invoking local inspection or Flatpak commands.

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

- **Static & dependency security:** GitHub CodeQL, Dependabot, GitHub Advanced Security, and `bandit -lll` scanning.
- **Hardened parsing & pinned inputs:** Streaming `defusedxml` for all catalog XML, hash-pinned (`sha256`) offline Flatpak runtime dependencies, and checksum-verified AppImage build inputs.
- **Privilege isolation:** Host mutations go exclusively through `org.rpm.dnf.v0` (for DNF5) or the single-purpose Polkit helper (RPM-only), with mocked D-Bus/Flatpak transports in `tests/` so CI never runs live system mutations.
