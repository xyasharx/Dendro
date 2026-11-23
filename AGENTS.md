# AGENTS.md

Instructions for AI coding agents working in **Dendro** (`io.github.xyasharx.Dendro`), a native PyQt6 graphical package manager and dependency tree explorer for Fedora Linux (Python 3.12+, Fedora 43+, targeting `dnf5daemon-server >= 5.2.7`).

This file describes the project as of **2.20.0**. It is not a changelog: it states the
invariants an agent must not break and the commands that prove they hold. When the code
and this file disagree, the code is right and this file is a bug — fix it in the same
change.

---

## 1. Grounding & Discovery Before Editing

- **Inspect live files before assuming layout:** Verify the current contents of `core/`, `ui/`, `tests/`, and `.github/workflows/ci.yml` at the start of a task. Newer releases may add modules or CI lint targets beyond older README listings.
- **Check both `.github/workflows/ci.yml` and `README.md`:** `.github/workflows/ci.yml` is the source of truth for required CI gates. Always run the full check suite (or every check covering touched files) before finishing.
- **Keep this file true.** Adding or renaming a module means updating the repository map in
  the same change; adding a CI target means updating the verification section, or rather
  checking that the section still tells an agent to read `.github/workflows/ci.yml` instead of a copy of its
  file list. A stale map is worse than no map, because an agent trusts it and then edits a
  module that no longer exists.
- **Keep user state untouched:** Always isolate `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` in temporary directories when running tests or CLI checks so `~/.cache/dendro/capabilities_v6.db` and user config are never mutated.

---

## 2. Repository Map

```text
.
├── main.py                        # Entry point (--version, -d debug, Qt startup)
├── pyproject.toml                 # Canonical version & packaging metadata
├── setup.py                       # Setuptools hooks stamping build provenance
├── requirements.txt               # Python runtime requirements
├── dendro.spec                    # Fedora RPM spec (COPR: xyasharx/dendro)
├── io.github.xyasharx.Dendro.yml  # Flatpak manifest (KDE 6.11 + PyQt BaseApp)
├── build_appimage.sh              # x86_64 AppImage builder (pinned downloads)
├── .editorconfig / .gitattributes # Formatting & LF line-ending enforcement
├── .copr/Makefile                 # COPR SRPM build hook
├── .github/                       # dependabot.yml & workflows (ci, release, update-release-pins)
├── ci/                            # CI helper scripts (AppStream, AppImage smoke test)
├── core/                          # Domain logic, bindings, D-Bus, models, gates
│   ├── backend.py                 # librpm / DNF5 CLI inspection & workers
│   ├── build_provenance.py        # Git commit/tree ID stamping into _build_info.py
│   ├── classification_audit.py    # Corpus coverage & evidence-gap audit engine
│   ├── daemon_locale.py           # Which D-Bus strings are data and which are prose
│   ├── dnf5daemon.py              # Typed org.rpm.dnf.v0 D-Bus client
│   ├── ecosystems.py              # Declarative RPM ecosystem evidence registry
│   ├── flatpak_cancel.py          # Cancellable subprocess runner
│   ├── flatpak_inventory.py       # App/runtime enumeration & typed entries
│   ├── flatpak_progress.py        # Streaming Flatpak progress parsing
│   ├── flatpak_runtime_removal.py # Runtime removal with dependent-app checks
│   ├── flatpak_system_uninstall.py# System-wide uninstall via Flatpak's helper
│   ├── flatpak_system_update.py   # System-wide update via Flatpak's helper
│   ├── flatpak_uninstall.py       # Per-user uninstall (data-deletion option)
│   ├── flatpak_versions.py        # Available-version queries
│   ├── host_environment.py        # Kernel/VFS host mode & capability policy
│   ├── i18n.py                    # Locale resolution & message-catalog lookup
│   ├── models.py                  # DependencyTreeModel, filters, tree nodes
│   ├── operation_gate.py          # Serialization of host-affecting operations
│   ├── package_classifier.py      # Two-phase taxonomy & removal guidance
│   ├── package_metadata.py        # defusedxml Comps/AppStream, RPM anatomy, FHS
│   ├── repository_addition.py     # Unprivileged HTTPS .repo fetch & helper flow
│   ├── repository_deletion.py     # Constrained third-party .repo deletion flow
│   ├── rpmostree_status.py        # Unprivileged read-only rpm-ostreed state reader
│   ├── taxonomy.py                # Shared category/sidebar/filter registry
│   ├── transaction_controller.py  # Daemon plan resolution, review, & execution
│   └── worker_lifecycle.py        # Shutdown-safe QRunnable base (teardown no-op)
├── ui/                            # PyQt6 UI layer (never blocks event loop)
│   ├── advisory_dialog.py         # Security advisory list & detail pane
│   ├── delegates.py               # Tree rendering & status delegates
│   ├── external_links.py          # URL launching with surfaced failures
│   ├── flatpak_dialog.py          # Flatpak apps & runtimes inventory
│   ├── fonts.py                   # Font scaling & labelled-font application
│   ├── header.py                  # Search bar, theme toggle, update/action bar
│   ├── history_dialog.py          # DNF transaction history viewer
│   ├── i18n.py                    # Locale manager & live retranslation
│   ├── icon_data.py / icons.py    # Bundled icon payload & theme icon lookup
│   ├── inspector_panel.py         # Metadata, rpm -V, scriptlets, CVEs, deps
│   ├── key_import_dialog.py       # Repository signing-key trust decision
│   ├── main_window.py             # Main controller, QThreadPool workers, tray
│   ├── repo_dialog.py             # DNF5 repository manager & cache controls
│   ├── settings_dialog.py         # Application preferences
│   ├── sidebar.py                 # Category tree & package counters
│   ├── styles.py                  # Palettes, QSS, portal theme sync
│   ├── table_rows.py              # Whole-row table fitting
│   ├── theme_coordinator.py       # Theme resolution & portal subscription
│   ├── transaction_drawer.py      # Progress & Polkit authorization status
│   ├── transaction_plan_dialog.py # Pre-execution transaction plan review
│   ├── window_placement.py       # Fit the window to the screen's available area
│   └── window_presentation.py     # Title/empty-state presentation decisions
├── data/
│   ├── locales/                   # Shipped JSON message catalogs (8 files)
│   ├── icons/                     # High-DPI raster application icons
│   ├── screenshots/               # AppStream screenshots
│   ├── AppRun / dendro.bash       # AppImage entry point & launcher
│   ├── io.github.xyasharx.Dendro.desktop
│   ├── io.github.xyasharx.Dendro.metainfo.xml
│   └── org.dendro.policy          # Polkit policy for constrained repo helper
├── tools/
│   ├── dendro-i18n.py             # Translation extractor & catalog coverage check
│   ├── dendro-repo-helper.py      # Root helper for /etc/yum.repos.d/ add/delete
│   ├── dendro-taxonomy-audit.py   # Read-only metadata corpus audit CLI
│   ├── update_release_pins.py     # Hash-verified AppImage & release pin updater
│   └── verify_release_tag.py      # Tag-to-pyproject version gate
└── tests/                         # 55 test files; unit & headless Qt (fake D-Bus)
```



---

## 3. Non-Negotiable Security & Privilege Invariants

1. **Never construct a privileged `dnf` CLI command for package transactions.**
   - All RPM package installs, removals, upgrades, update checks, repository enable/disable/list/clean, and cache actions go through Fedora's DNF5 system daemon (`org.rpm.dnf.v0`) via `core/dnf5daemon.py`.
   - The daemon owns RPM transaction locking and Polkit authorization.
2. **Preserve the retain-and-confirm transaction lifecycle.**
   - `core/transaction_controller.py` must resolve the transaction plan via the daemon, present that exact resolved plan in `ui/transaction_plan_dialog.py`, and execute **only that retained plan** upon user confirmation.
   - Check resolved removal items against `/etc/dnf/protected.d/` independently of taxonomy classification and surface explicit warnings.
3. **Serialize all host-affecting operations.**
   - Route daemon-backed mutations and constrained helper runs through `core/operation_gate.py` so concurrent host mutations cannot race.
4. **Keep `tools/dendro-repo-helper.py` strictly constrained and RPM-install-only.**
   - Flatpak, AppImage, and source checkouts do **not** have the installed root helper (`data/org.dendro.policy`) and **must never** attempt to invoke a host helper or spawn a host shell.
   - `.repo` downloads (`core/repository_addition.py`) accept **only direct HTTPS URLs** (never COPR project names, `dnf copr` commands, local file paths, or shell commands), run as the unprivileged desktop user, bound response size, require UTF-8 text, and normalize line endings before Polkit authorization.
   - The helper itself must independently enforce its small option allowlist, require HTTPS repo URLs and package signature verification, reject duplicate repo IDs, and write root-owned files under `/etc/yum.repos.d/` with atomic no-overwrite semantics.
   - After any terminal helper outcome, refresh the daemon-backed repository list rather than assuming local file state matches daemon state.
5. **Validate repository origins from parsed `baseurl` hostnames.**
   - Classify Fedora, COPR, and RPM Fusion origins strictly from parsed `baseurl` hostnames—never by substring matching in paths, queries, userinfo fields, or lookalike hostnames.
6. **Preserve the Flatpak sandbox contract (`io.github.xyasharx.Dendro.yml`).**
   - Only `--system-talk-name=org.rpm.dnf.v0` is permitted on the system bus. Never add `--talk-name=org.freedesktop.Flatpak`, `flatpak-spawn --host`, host filesystem mounts, generic system-bus access, or generic Polkit actions.
   - Never add networked `pip install` or PyPI dependencies inside the Flatpak manifest; dependencies outside `PyQt.BaseApp` (such as `defusedxml`) must be hash-pinned (`sha256`) and installed offline (`--no-index --no-deps`).
   - Keep `MimeType=` and `%[fFuU]` stripped from the Flatpak-installed `.desktop` file.
7. **Parse all external/catalog XML with `defusedxml`.**
   - Stream Comps and AppStream XML via `defusedxml` in `core/package_metadata.py` without retaining full DOM trees; index every `pkgname` in multi-package components.
8. **Never acquire root for Flatpak, and never handle a password.**
   - System-wide Flatpak installs are authorised by **Flatpak's own Polkit helper**, not Dendro's. The `flatpak` command runs as the logged-in user (`core/flatpak_system_update.py`, `core/flatpak_system_uninstall.py`); Dendro never escalates and never reads or stores a password. `--noninteractive` must stay **absent** from every system-wide invocation — it would stop Polkit from prompting and fail the operation instead. It is legitimate only on `--user` operations, where it suppresses progress output rather than authorization (`core/flatpak_uninstall.py`). Know which of the two a call is before adding or removing the flag.
   - Never reach for `sudo`, `pkexec`, or `flatpak-spawn --host` to make a Flatpak operation succeed. If an operation requires privilege Dendro does not have, it is out of scope, not something to escalate around.
   - A system-wide removal affects **every user on the machine**; `--delete-data` applies only to the calling user's data. Any new destructive Flatpak option belongs behind an explicit confirmation that says so.
   - System-wide mutations route through `core/operation_gate.py` exactly like daemon mutations, so they cannot race a DNF5 transaction.
9. **Never trust a repository signing key on the user's behalf.**
   - `ui/key_import_dialog.py` shows the key's fingerprint, identities, and download source, and **declines on Escape and on Enter** — the safe answer is the default, including when the dialog cannot be shown at all. A prompt that cannot be displayed declines; it must never fall back to trusting an unseen key.
   - Never remember a trust decision the user did not make, and never widen "remember" to cover keys the user has not seen.
   - The prompt answers the DNF5 daemon rather than letting the transaction stall until it times out.
10. **Preserve Fedora Atomic, read-only `/usr`, and unbooted-container guardrails.**
   - Classify host mode strictly through kernel/filesystem facts in `core/host_environment.py` (never environment variables or `os-release` strings).
   - Never run `org.rpm.dnf.v0` mutations/overrides, `dnf5 --transient`, `usroverlay`, `pkexec rpm-ostree`, or `tools/dendro-repo-helper.py` on Fedora Atomic / OSTree or read-only `/usr` hosts, and never forward container calls to a host system bus when PID 1 is not booted `systemd`.
   - Keep `core/rpmostree_status.py` strictly unprivileged and read-only (`Properties.Get` on `org.projectatomic.rpmostree1.Sysroot.Deployments` outside Flatpak).

---

## 4. Runtime, Threading & Backend Invariants

- **Never block the Qt main/event loop:** Run RPM inspections, catalog parsing, `rpm -V`, D-Bus daemon calls, and Flatpak CLI/helper actions off the UI thread via `QThreadPool` workers (`core/backend.py`, `ui/main_window.py`).
- **Guard native bindings with `RPM_GLOBAL_LOCK`:** Every native `librpm` call must hold `RPM_GLOBAL_LOCK`.
- **Degrade optional bindings explicitly:** Missing native `librpm` bindings (e.g., in AppImage/Flatpak/non-host environments) must result in explicit reduced capability or subprocess fallback, never silent failure or crashes.
- **Handle RPM pseudo-packages cleanly:** In `_query_native_librpm` (`core/backend.py`), skip `gpg-pubkey` pseudo-package records (which intentionally lack an architecture) rather than treating them as malformed and triggering a full `'rpm -qa'` fallback. Reserve fallback for genuinely incomplete package records.
- **Keep DNF5 vs DNF4 `Provides:` framing honest:** DNF5 remote search safely record-frames newline-valued `Provides:` records before classification; keep legacy DNF4 scalar-only rather than guessing list boundaries.
- **Workers outlive the widgets that own them.** A `QThreadPool` runnable can still be running when the window closes and Qt destroys the window's children; emitting a signal from it then raises `RuntimeError: wrapped C/C++ object ... has been deleted` inside the pool thread. Build every background worker on `core/worker_lifecycle.py::ShutdownSafeRunnable`, which turns that specific teardown error into a silent terminal no-op while letting genuine programming errors propagate. Do not catch `RuntimeError` by hand and do not swallow it broadly — a blanket catch also hides the bugs that are not teardown.
- **Never `setFont` from inside a model notification.** A font change starts its own cascade; if it begins inside `rowsInserted`/`layoutChanged`, the cascade reaches a shared header's `updateGeometries` while the model is still being mutated, which segfaults. Follow the font from the widget's `FontChange` event instead, where a cascade is the event being handled rather than something started inside a notification.
- **Never mutate a sibling scrollbar from inside `rangeChanged`.** Setting a bar's value while it is delivering a range change re-enters the same delivery. Where two views must share a scroll position, give them **one bar object** (`setVerticalScrollBar`) rather than copying values between two.
- **SQLite WAL cache:** Capability lookups cache in `~/.cache/dendro/capabilities_v6.db`. Respect `XDG_CACHE_HOME` and keep schema/versioning consistent if changing cached shapes.

---

## 5. Evidence-Driven Taxonomy & Search Invariants

- **Single source of truth:** `core/taxonomy.py` owns category definitions, sidebar labels, filter predicates (`cat:`, `pillar:`, `sub:`, `type:`, `evidence:`, `scope:`, `tag:`, etc.), badge counters, and removal guidance. `core/ecosystems.py` is the single declarative registry for language/ecosystem evidence. Never duplicate category or search filter rules in UI widgets.
- **Two-phase classification:** Phase 1 determines Physical Delivery Archetype from FHS / RPM header facts (e.g., `DIRINDEXES` verifying `/usr/share/applications/` launchers vs autostart/daemons, and positive exported ELF `.so` capability signals for shared libraries); Phase 2 assigns Functional Domain (XDG / POSIX / Ecosystems).
- **No broad name guessing:** Never classify packages solely by suffix heuristics (`-data`, `-common`, `-filesystem`). Use FHS evidence, `PACKAGE_TAXONOMY_OVERRIDES`, or constrained exact/namespace policies.
- **Keep ambiguity honest:** If affirmative evidence is absent or manifest ecosystems conflict without a resolving package namespace, retain `Needs Classification` (`cat:unclassified`) and expose the scope/evidence in the inspector instead of picking by rule order.
- **Audit before taxonomy changes:** When working on a Fedora RPM host and modifying classifier/ecosystem rules, run a before/after audit:
  ```bash
  python3 tools/dendro-taxonomy-audit.py \
    --installed-rpm-database \
    --with-system-catalogs \
    --output taxonomy-audit.json
  ```

---

## 6. UI, Diagnostics & Build Provenance Conventions

- **Quote commands in CLI/log diagnostics:** Whenever log output or user-facing diagnostic messages reference an external command, explicitly delimit it (e.g., `'rpm -qa'`) so commands are visually distinct from prose.
- **Surface external-launch failures in GUI:** When opening external URLs (homepage, CVE, Bugzilla links) fails—such as missing default `text/html` handlers—display clear, actionable GUI feedback instead of failing silently.
- **Theme integration:** Preserve `Follow System`, `Light`, and `Dark` theme handling, split between `ui/styles.py` (palettes and QSS) and `ui/theme_coordinator.py` (resolution and the XDG Desktop Portal color-scheme subscription). Theme resolution must stay reachable without a live window: `ui/main_window.py` had been the only entry point to it, which is why those decisions sat at 0% coverage.
- **Window title and empty-state copy are decisions, not markup:** `ui/window_presentation.py` owns them so they can be tested without a display server. A title must resolve the **same message id** at construction and after a language switch; resolving two different ids made the window announce one name and come back with another, so the switch read as a replacement rather than a translation.
- **No table may cut its last row.** Rows are a fixed height while the view's height comes from a dialog, splitter, or resize, so the viewport almost never holds a whole number of rows and the next row's sliver is painted at the bottom edge. `ui/table_rows.py::WholeRowFitter` snaps the visible area to a whole multiple of `rowHeight(0)` — note `sizeHintForRow` is the delegate's *preferred* height (27px here) and not the height rows are laid out at (30px), so it is the wrong divisor. It is a no-op for an empty table, where shrinking only opens a gap under the frame, and it must iterate because shrinking can hide a horizontal scrollbar, which changes the chrome mid-calculation.
- **Fit the window to `availableGeometry()`, never to the screen.** A maximized window is not the screen: the compositor reserves the panel strips, so on a 1080-row display with GNOME Shell's top bar a maximized window is 1046 rows. Sizing to the screen and letting the window manager clip put the status bar and the last inspector row past the bottom edge, while full screen looked correct — the difference between the two is exactly that reservation. `restoreGeometry()` does not cover it either: it only rescues a window that is entirely off-screen or has lost its title bar, so a geometry saved on a larger display is restored verbatim and then overhangs. `ui/window_placement.py` clamps both the restored and the initial size, and `MainWindow.showEvent` applies it once before the first paint. Only the size is corrected; moving a window the user placed is a worse bug than the one being fixed.
- **A table cell widget is sized to its own hint, not to its row.** A 22px checkbox in a 30px row got an 18px container and its bottom was clipped in every row. Pin cell widgets to `rowHeight(row)` with zero margins.
- **Rounded tab tops and a straight accent `border-top` cannot coexist.** Qt paints the border as an arc above a separately drawn rounded body, so the selected tab reads as two shapes. Tabs are square with `border-top: 2px solid` (transparent when unselected, accent when selected).
- **Give every styled `QTabWidget` an object name.** An unnamed one matches no `::pane` rule, falls back to Qt's native frame, and leaves a seam between the strip and the content. The pane rule covers `#InspectorTabs` and `#SettingsTabs`.
- **Adjacent controls on one row are one height.** A styled `QPushButton` is `min-height + 10` (10px of padding plus the border); a `QLineEdit`/`QComboBox` is `min-height + padding + border`. A per-widget `min-height` override is how they drift — the repository fields carried 26px against the standard 30px and stood 4px below their button. Change the shared value rather than adding a local one.
- **Do not chase a screenshot: measure.** Every defect above was invisible in the source and obvious in a `grab()` plus a widget lookup. Compare heights, sample pixels, and count straddling rows; "looks right" is not evidence, and a stylesheet rule that no widget matches looks exactly like a rule that works.
- **Build provenance (`core/build_provenance.py`, `setup.py`):**
  - `main.py --version` must work cleanly without heavy runtime dependencies by reading `pyproject.toml` and `core/_build_info.py`.
  - `BuildPyWithProvenance` and `SdistWithProvenance` in `setup.py` stamp `core/_build_info.py` only in `build_lib` / `base_dir`—never dirty the source checkout during packaging.
  - Flatpak/AppImage/RPM builds must never ship `__pycache__`, `*.py[co]`, or `.git` inside installed app directories.
- **Line endings & formatting:** Maintain LF (`\n`) across the repo (`.editorconfig`, `.gitattributes`). Match Ruff formatting (`ruff format`) and style.

---

## 7. Verification & CI Commands

Always run checks with isolated XDG state:

```bash
export XDG_CONFIG_HOME="$(mktemp -d)"
export XDG_CACHE_HOME="$(mktemp -d)"
trap 'rm -rf "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME"' EXIT
```

### Bytecode, Lint, Format & Security

```bash
python3 -m compileall -q core ui tools main.py setup.py

# Broad correctness pass: every module, both directories, main and setup.
ruff check core ui main.py tests tools --select=F,E9

# Strict modern-rules pass, format check, and Bandit.
# Do not copy a file list into this file. The authoritative set lives in
# .github/workflows/ci.yml and grows with every module added; a list duplicated
# here drifts within a release or two and then silently under-checks new code.
# Run the exact block CI runs:
sed -n '/ruff check --select=E,F,I,UP,B,SIM,PERF,RUF/,/test_worker_lifecycle.py/p' \
  .github/workflows/ci.yml
```

Run the gates from that file rather than from memory. All three blocks take a
plain list of paths, so the same extraction drives all of them — it strips the
line continuation and keeps every token that ends in `.py`:

```bash
# The exact file list each CI gate uses (quote the block, not the paths).
ci_files() {
  sed -n "/$1/,/$2/p" .github/workflows/ci.yml \
    | tr -d '\r' | tr ' ' '\n' | grep -E '^[A-Za-z0-9_/]+\.py$'
}

# Strict lint over exactly the modules CI holds to the modern rules.
ruff check --select=E,F,I,UP,B,SIM,PERF,RUF --ignore=E501 \
  $(ci_files 'ruff check --select=E,F,I,UP,B,SIM,PERF,RUF' 'test_worker_lifecycle.py')

# Format check over exactly the files CI formats.
ruff format --check \
  $(ci_files 'ruff format --check' 'test_worker_lifecycle.py')

# Bandit over the security-sensitive set.
.dendro-ci-venv/bin/python -m bandit -q -r -lll \
  $(ci_files 'bandit -q -r' 'lll')
```

Each of those yields a non-empty list (47, 59 and 9 paths at the time of
writing); if one yields nothing, the block has moved in `.github/workflows/ci.yml` and the
extractor needs updating rather than the gate skipping silently.

Bandit is not packaged for Fedora any more, so CI installs it into its own
environment; create that venv with `--system-site-packages` rather than
installing into your project's.

*Scope rule, not a list:* anything that parses untrusted input, renders external
data, runs a subprocess, or touches a privileged path is security-sensitive.
When you change such a module — `tools/dendro-repo-helper.py`,
`core/repository_addition.py`, `core/repository_deletion.py`,
`core/dnf5daemon.py`, `core/transaction_controller.py`,
`core/package_metadata.py`, the `core/flatpak_*.py` family, or anything in
`ui/` that builds subprocess arguments — run `ruff check`, `ruff format --check`,
and `bandit -lll` on those files directly, even if CI's list has not caught up.

### Unit & Headless Qt Tests

Always run with isolated XDG state (above) and never against your real config:

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest -q tests
```

**CI runs one process per test file, and reproduces that in this file too:**

```bash
for test_file in tests/test_*.py; do
  python3 -u -X faulthandler -m pytest -q "$test_file" || echo "FAILED $test_file"
done
```

Use that form, not a single-process run, when a failure looks non-deterministic.
A single Python process shares one Qt state across 52 files, so a fault takes the
whole interpreter with it and reports only the file that died — losing every
result after it. CI's `-u` is not cosmetic either: on a segfault the interpreter
dies before `atexit` flushes buffered pipes, so a crash once arrived with an
empty log. `-X faulthandler` prints the C stack that a bare exit 139 does not.

If a test file exits 139 (SIGSEGV) or 134 (SIGABRT) while its own summary says
every test passed, **the fault is in teardown, not in a test.** That is the exact
signature that sent earlier work chasing the wrong file: pytest printed
`94 passed` and the process died afterwards. Do not "fix" the test named in the
annotation; it is only the last line of a completed log.

### Translation Catalogs

```bash
# Coverage gate: fails on missing, stale, or empty entries in any catalog.
python3 tools/dendro-i18n.py check

# Coverage summary and the authoritative key list.
python3 tools/dendro-i18n.py stats
python3 tools/dendro-i18n.py list
```

`check` proves the catalogs agree with the *extracted* sources, which is not the
same as proving the interface is translated. Screenshot a real window, because a
whole class of strings stayed invisible until someone looked at one:

```python
# QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 this_file.py
from PyQt6.QtWidgets import QApplication

app = QApplication([])
from ui.i18n import install
from ui.main_window import MainWindow

install("de")  # or ru, hi, fr, es, pt_BR, pt_PT
window = MainWindow()
window.resize(1400, 900)
window.grab().save("/tmp/win_de.png")  # then read the image
```

Do the same for `DendroSettingsDialog` and `PackageInspectorPanel`; those hold
the constructor-argument copy that a green `check` used to hide.

Cover all five host topologies, not just the one the current machine reports.
`tests/test_i18n.py` builds each of them through the injectable
`HostEnvironmentProbe` (`FakeHostProbe` in `tests/test_host_environment.py`),
because a normal CI box only ever exercises one branch and the other four -
Atomic, read-only `/usr`, unbooted container, Flatpak sandbox - are exactly the
ones whose users see the banners.

### Desktop, AppStream & Flatpak Manifest Validation

```bash
desktop-file-validate data/io.github.xyasharx.Dendro.desktop
appstreamcli validate --no-net data/io.github.xyasharx.Dendro.metainfo.xml
flatpak-builder --show-manifest io.github.xyasharx.Dendro.yml >/dev/null
```

---

## 8. Testing & Container Boundaries

- **Mocked host boundaries in unit tests:** The test suite uses a fake typed D-Bus transport and mocked subprocess/Flatpak responses. Unit tests must **never** execute real RPM/DNF transactions, require Polkit prompts, or depend on a locally installed `flatpak` binary.
- **No host-escape hacks in containers:** Inside desktop containers without systemd PID 1, system D-Bus, and `org.rpm.dnf.v0` (such as `toolbox` or `webtop`), package transactions are unsupported. **Never** mount the host D-Bus socket, host RPM database, or host filesystem, and never pass `--privileged` to work around this. Test real DNF5/Polkit/Flatpak transactions only on a booted Fedora host or VM.
- **Keep the CI matrix on the supported Fedora window:** `.github/workflows/ci.yml` runs the job in `fedora:<version>` containers for every release the project claims to support. The matrix is not cosmetic: COPR (`xyasharx/dendro`) publishes the RPM to `fedora-43`, `fedora-44`, `fedora-45`, and `fedora-rawhide` chroots, so any release users can install from must be one CI actually exercises. When a release reaches GA, add it here and to the COPR chroots. When a release reaches end-of-life, move the supported floor in `README.md`/`SECURITY.md`, this matrix, and the COPR chroots together in one change; an expired image tag makes the job fail at `dnf install` before any test runs. Never leave a release published but untested, and never quietly drop a release that is still supported.

---

## 9. Localisation Invariants

Dendro ships its own JSON message catalogs instead of Qt Linguist `.qm` files. `.qm` requires `lrelease`, which would add a Qt linguist toolchain dependency to the RPM build and to every contributor's machine; JSON needs nothing beyond the standard library and stays reviewable in a diff.

- **Every user-facing string goes through `tr()`.** Import it with `from core.i18n import tr`. Do not use `self.tr()`: the majority of Dendro's vocabulary lives in module-level tables (category labels, sidebar sections, classification evidence), which a per-object Qt lookup cannot see. One mechanism, not two.
- **Never translate at import time.** `core/taxonomy.py` stores English *message ids* in its frozen registry and exposes `label_text` / `tooltip_text` properties that translate on read. Assigning `tr(...)` into a module-level constant freezes it to whatever locale was active during import, so a language switch silently stops working. Compute translated text inside a method or property.
- **Plurals go through `tr_plural(singular, plural, count)`.** Each language declares its own boundary in `plural_boundary`; English's "one vs many" rule is wrong for several supported languages.
- **Coverage is enforced in CI.** Run `python tools/dendro-i18n.py check` after touching any user-visible string. It fails on missing, stale, and empty entries. `python tools/dendro-i18n.py list` prints the authoritative key list; `stats` shows per-locale coverage. `tests/test_i18n.py` asserts the same invariants, so a drifted catalog fails the test suite before CI does.
- **Adding a language:** add the locale to `SUPPORTED_LOCALES`, `LOCALE_ENDONYMS`, and `LOCALE_NAMES` in `core/i18n.py`; add the catalog to `[tool.setuptools.data-files]` in `pyproject.toml` (setuptools cannot glob there, and `tests/test_i18n.py` fails if the list and `data/locales/` disagree); create `data/locales/<locale>.json` with full coverage; confirm every word in `LOCALE_ENDONYMS` is the genuine native spelling. German must use `ä ö ü ß`, not `ae oe ue ss`; French, Spanish, and Portuguese must be fully accented; Hindi and Russian must ship real script, not `\u` escapes. Three tests assert these.
- **Typography and script are part of accuracy.** Spanish keeps `¿` and `¡` on questions and exclamations, French uses its own spacing before `:` and `;`, and the quote characters follow each locale rather than the English `"`. A catalogue that reads as translated but is punctuated like English is a defect, not a nuance.
- **Do not translate:** taxonomy identifiers, icon names, format tokens, product and project names (DNF5, Flatpak, COPR, RPM Fusion, Fedora), and proper nouns such as `Tcl/Tk`, `R & CRAN`, `TeX & LaTeX`. `tests/test_i18n.py` allow-lists exactly these as legitimately unchanged.
- **Never retranslate inside a signal.** `install(locale)` walks `QApplication.allWidgets()` and calls `retranslate()` on every live widget that defines one, but it does that on the *next event-loop turn*, never inline. A language change normally begins in a widget's own handler (the Preferences combo box emits `currentIndexChanged`), and retranslating the tree from there runs every hook with signal delivery still on the stack — including hooks on the widget that emitted it. That crashed on two Fedora Qt builds, on different tests, and not on a third; deferring costs one turn and removes the class of failure. Any test that switches languages must let the loop turn once before asserting.
- **Live switching:** `ui/i18n.py` owns a `LocaleManager`. There is deliberately **no registration list**: an opt-in registry only covers objects that existed at startup, so a dialog opened after the switch, or a panel whose author forgot to register it, silently keeps the old language. Membership is "is this widget alive", which cannot be forgotten. `MainWindow._apply_language_choice` therefore does not enumerate children; it only refreshes the things with no widget of their own (the window title and the running status message).
- **`retranslate()` must not destroy widgets, and must not re-enter.** Patch text in place. The first implementation tore the tab pages down and re-ran `_init_ui()`; on Fedora 45 that segfaulted, because the combo box delivering the `currentIndexChanged` signal that triggered the rebuild was deleted underneath the handler. It crashed on one Fedora version and not the others, which is the worst kind of bug to leave in. Two further rules follow from the same class of bug, and both are enforced in `ui/i18n.py::LocaleManager.retranslate_all` rather than left to each hook:
  - A widget already being retranslated is skipped, so a hook that ends up calling `install()` again cannot call itself. The Preferences dialog sits on the signal path that starts the sweep, so "retranslate while retranslating" is a reachable state, not a hypothetical; without the guard it recurses until the stack runs out.
  - `sip.isdeleted(widget)` is checked before the hook is looked up, and a `RuntimeError` from a widget torn down mid-sweep ends that widget rather than the sweep. `allWidgets()` is allowed to still list a destroyed C++ object.
- **A `retranslate()` hook must not resize its own window synchronously.** Re-fitting the layout while a combo box is delivering the signal that changed the language is a re-entrant layout change, and the text it measures is a frame behind in some Qt builds. `DendroSettingsDialog` defers the re-fit with `QTimer.singleShot(0, ...)` behind a `sip.isdeleted` check. Translated text is a different length, so the re-fit itself is required, not optional: the dialog widens from 563px in German to 714px in Hindi.
- **`retranslate()` must rewrite labels, not only tooltips.** `HeaderBar.retranslate` refreshed seven tooltips and left all eighteen button labels in English, because they were written once in `_init_ui`. A widget built at startup needs every piece of its displayed text re-applied, and a test that only checks tooltips will pass while the UI is half translated. `tests/test_i18n.py` now asserts both, per locale.
- **A label carrying a count has to keep the count on the widget.** `f"Updates ({count})"` is invisible to the extractor and never translates; a one-off `tr(...)` loses the number when the language changes. Store the count (`self._available_count`) and rebuild the label in `retranslate`, using a `{count}` placeholder in the message id. Both the number and the language then survive either order of change.
- **A `QAction` is not a widget.** `QApplication.allWidgets()` cannot return the search field's trailing action, so the sweep never reaches it: its text and tooltip must be re-applied explicitly inside the owning widget's `retranslate`.
- **Prose in a tooltip belongs in a row table, not a markup blob.** The search-filter reference was one module-level constant of English HTML: outside the catalog (an id containing `<b>` is not translatable) and frozen at import. It is now `SEARCH_FILTER_ROWS` — `(message, shell, has_values)` — rendered by `search_filter_tooltip()` on every read. Literal tokens the user types live in `SEARCH_FILTER_VALUES` and are identical in every locale; the extractor reads column 0 via `_DISPLAY_TABLES`.
- **A translator's placeholder must never hold markup.** `{grammar}` in a catalog is filled from `SEARCH_FILTER_VALUES`, so a translation that writes its own `<code>` inside a placeholder has its braces rejected by the shell. Put the word in the prose and let the shell supply the markup.

- **Counts come from the source, not from memory.** `tools/dendro-i18n.py stats` is the number to quote; docs that repeat a string count go stale the next time a message is added.
- **`core.i18n` owns the active locale.** `LocaleManager.locale` reads it rather than caching a second copy; two stores drift the moment anything calls `set_active_locale()` directly, and then `translate()` and the manager disagree about which language is live.
- **Widget constructors take display text positionally.** `QLabel("…")`, `QCheckBox("…")`, `QGroupBox("…")`, `QPushButton("…")` and `QAction` are the bulk of the dialog copy and `tools/dendro-i18n.py` collects them, but only when they are wrapped: write `QLabel(tr("…"))`. A bare literal is invisible to the extractor and ships in English.
- **Text built from a variable needs a declared id.** When a caption is passed through a helper (`field_name("License:")`), the extractor cannot see the call site. Either wrap at the call (`field_name(tr("License:"))`) or list the ids in a module-level tuple that the tool reads (`COLUMN_HEADERS`, `_STATS_CARD_MESSAGES`).
- **Do not hide translatable words inside markup.** `<b>License:</b>` is rejected as non-prose, so the caption never reaches a catalog. Use an object name (`AboutFieldName`) and style it in the stylesheet instead.
- **`retranslate()` must not re-measure against English.** The inspector's field-name gutter is a fixed pixel width; measure it from the painted (`tr()`-ed) labels, or a wider translation such as Russian "Архитектура" is clipped.
- **Font coverage:** Hindi needs a Devanagari-capable font, so `dendro.spec` recommends `google-noto-sans-devanagari-fonts`. Without it the UI renders placeholder boxes on minimal installations.

---

## 10. Packaging & Change-Sync Checklist

When your changes touch cross-cutting parts of the project, keep these synchronized:

- **Version bumps — four surfaces, all pinned by one test.** `pyproject.toml` (canonical), `dendro.spec` (`Version:`, and `Release: 1` for a new version), the newest `<release>` in `data/io.github.xyasharx.Dendro.metainfo.xml`, and the `%changelog` entry in `dendro.spec`. `tests/test_entrypoint.py::test_release_version_is_consistent_across_python_rpm_and_appstream_metadata` fails if they disagree, so a partial bump cannot reach a tag.
- **Releasing.** The tag drives everything: pushing a `v*.*.*` tag runs `.github/workflows/release.yml`, which runs `tools/verify_release_tag.py` (tag must equal `project.version`), builds the AppImage and distributions, publishes the release with all assets, and triggers COPR. Publish a version bump through a PR and let CI pass **before** tagging; a tag that fails validation publishes nothing and the tag must then be deleted, which is a worse position than a red PR. Never re-use or move a pushed tag.
- **Release notes are curated, not generated.** The workflow creates the release with GitHub's auto-generated notes; the maintainer then replaces the body with hand-written notes. Match the existing shape: an opening paragraph, `### Highlights`, themed sections, `### Upgrade notes`, `## What's Changed` (one line per PR, in the `* title by @author in <url>` form), the `**Full Changelog**` link, and `### Special Thanks`. Never put a contributor's or tool's name in the title, body, or branch.
- **New Python modules or data files:** Verify inclusion in `pyproject.toml`/`setup.py`, `dendro.spec` (`%files`), `io.github.xyasharx.Dendro.yml` (`cp -a ...`), and `build_appimage.sh`.
- **Message catalogs:** every distribution form installs `data/locales/*.json` separately from the Python package (`core/i18n.py` discovers them beside the package directory, then falls back to `/usr/share/dendro/locales`). Add the install line to the same three manifests, or those builds ship English-only.
- **New runtime dependencies:** Update `requirements.txt`, `dendro.spec` (`Requires:`), `io.github.xyasharx.Dendro.yml` (offline hash-pinned source if not in `PyQt.BaseApp`), and `README.md`.
- **Search syntax or shortcuts changes:** Update `core/models.py`/`core/taxonomy.py`, tests, and `README.md` tables together.
- **Commit style:** Follow the repository's scoped conventional format (e.g., `fix(ui): ...`, `feat(taxonomy): ...`, `docs: ...`, `release: ...`), keeping changes focused and accompanied by regression tests.
