# AGENTS.md

Instructions for AI coding agents working in **Dendro** (`io.github.xyasharx.Dendro`), a native PyQt6 graphical package manager and dependency tree explorer for Fedora Linux (Python 3.12+, Fedora 43+, targeting `dnf5daemon-server >= 5.2.7`).

---

## 1. Grounding & Discovery Before Editing

- **Inspect live files before assuming layout:** Verify the current contents of `core/`, `ui/`, `tests/`, and `.github/workflows/ci.yml` at the start of a task. Newer releases (e.g., `v2.16.0` Flatpak management additions) may add modules or CI lint targets beyond older README listings.
- **Check both `ci.yml` and `README.md`:** `.github/workflows/ci.yml` is the source of truth for required CI gates. Always run the full check suite (or every check covering touched files) before finishing.
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
├── io.github.xyasharx.Dendro.svg  # Scalable application icon
├── build_appimage.sh              # x86_64 AppImage builder (pinned downloads)
├── .editorconfig / .gitattributes # Formatting & LF line-ending enforcement
├── .copr/                         # COPR SRPM build hooks
├── .github/workflows/             # ci.yml, release.yml
├── ci/                            # CI helper scripts
├── core/                          # Domain logic, bindings, D-Bus, models, gates
│   ├── backend.py                 # librpm / optional libdnf5 inspection & workers
│   ├── build_provenance.py        # Git commit/tree ID stamping into _build_info.py
│   ├── classification_audit.py    # Corpus coverage & evidence-gap audit engine
│   ├── dnf5daemon.py              # Typed org.rpm.dnf.v0 D-Bus client
│   ├── ecosystems.py              # Declarative RPM ecosystem evidence registry
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
│   └── transaction_controller.py  # Daemon plan resolution, review, & execution
├── ui/                            # PyQt6 UI layer (never blocks event loop)
│   ├── delegates.py               # Tree rendering & status delegates
│   ├── header.py                  # Search bar, theme toggle, update/action bar
│   ├── history_dialog.py          # DNF transaction history viewer
│   ├── i18n.py                    # Locale manager & live retranslation
│   ├── inspector_panel.py         # Metadata, rpm -V, scriptlets, CVEs, deps
│   ├── main_window.py             # Main controller, QThreadPool workers, tray
│   ├── repo_dialog.py             # DNF5 repository manager & cache controls
│   ├── settings_dialog.py         # Application preferences
│   ├── sidebar.py                 # Category tree & package counters
│   ├── styles.py                  # Palettes, QSS, desktop portal theme sync
│   ├── transaction_drawer.py      # Progress & Polkit authorization status
│   └── transaction_plan_dialog.py # Pre-execution transaction plan review
├── data/
│   ├── locales/                   # Shipped JSON message catalogs (es, pt_BR, …)
│   ├── icons/                     # High-DPI raster application icons
│   ├── io.github.xyasharx.Dendro.desktop
│   ├── io.github.xyasharx.Dendro.metainfo.xml
│   └── org.dendro.policy          # Polkit policy for constrained repo helper
├── tools/
│   ├── dendro-i18n.py             # Translation extractor & catalog coverage check
│   ├── dendro-repo-helper.py      # Root helper for /etc/yum.repos.d/ add/delete
│   └── dendro-taxonomy-audit.py   # Read-only metadata corpus audit CLI
└── tests/                         # Unit & Qt tests (fake D-Bus transport)
```

*(Note: Inspect `core/`, `ui/`, and `tests/` for any Flatpak management modules added in `v2.16.0+`.)*

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
8. **Preserve Fedora Atomic, read-only `/usr`, and unbooted-container guardrails.**
   - Classify host mode strictly through kernel/filesystem facts in `core/host_environment.py` (never environment variables or `os-release` strings).
   - Never run `org.rpm.dnf.v0` mutations/overrides, `dnf5 --transient`, `usroverlay`, `pkexec rpm-ostree`, or `tools/dendro-repo-helper.py` on Fedora Atomic / OSTree or read-only `/usr` hosts, and never forward container calls to a host system bus when PID 1 is not booted `systemd`.
   - Keep `core/rpmostree_status.py` strictly unprivileged and read-only (`Properties.Get` on `org.projectatomic.rpmostree1.Sysroot.Deployments` outside Flatpak).

---

## 4. Runtime, Threading & Backend Invariants

- **Never block the Qt main/event loop:** Run RPM/libdnf5 inspections, catalog parsing, `rpm -V`, D-Bus daemon calls, and Flatpak CLI/helper actions off the UI thread via `QThreadPool` workers (`core/backend.py`, `ui/main_window.py`).
- **Guard native bindings with `RPM_GLOBAL_LOCK`:** Every native `librpm` / `libdnf5` call must hold `RPM_GLOBAL_LOCK`.
- **Degrade optional bindings explicitly:** Missing `libdnf5` (e.g., in AppImage/Flatpak/non-host environments) must result in explicit reduced capability, never silent failure or crashes.
- **Handle RPM pseudo-packages cleanly:** In `_query_native_librpm` (`core/backend.py`), skip `gpg-pubkey` pseudo-package records (which intentionally lack an architecture) rather than treating them as malformed and triggering a full `'rpm -qa'` fallback. Reserve fallback for genuinely incomplete package records.
- **Keep DNF5 vs DNF4 `Provides:` framing honest:** DNF5 remote search safely record-frames newline-valued `Provides:` records before classification; keep legacy DNF4 scalar-only rather than guessing list boundaries.
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
- **Theme integration:** Preserve `Follow System`, `Light`, and `Dark` theme handling in `ui/styles.py`, including XDG Desktop Portal color-scheme subscriptions.
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
python3 -m compileall -q core ui tools main.py

# Broad syntax & pyflakes pass
ruff check core ui main.py tests tools --select=F,E9

# Strict lint pass (check .github/workflows/ci.yml for any newly added files)
ruff check --select=E,F,I,UP,B,SIM,PERF,RUF --ignore=E501 \
  core/taxonomy.py core/package_metadata.py core/package_classifier.py \
  core/ecosystems.py core/classification_audit.py tools/dendro-taxonomy-audit.py \
  tests/test_taxonomy_registry.py tests/test_package_metadata_parsers.py \
  tests/test_ecosystem_registry.py tests/test_classification_audit.py

# Format check (verify against .github/workflows/ci.yml for expanded file lists)
ruff format --check \
  main.py core/backend.py core/models.py core/taxonomy.py \
  core/package_metadata.py core/package_classifier.py core/ecosystems.py \
  core/classification_audit.py tools/dendro-taxonomy-audit.py \
  ui/header.py ui/inspector_panel.py ui/main_window.py ui/sidebar.py \
  tests/test_entrypoint.py tests/test_taxonomy_registry.py \
  tests/test_package_metadata_parsers.py tests/test_ecosystem_registry.py \
  tests/test_classification_audit.py

# Bandit security scan (on supported Fedora releases use an isolated venv
# with --system-site-packages, since Fedora no longer ships the Bandit RPM)
.dendro-ci-venv/bin/python -m bandit -q -r \
  core/taxonomy.py core/package_metadata.py core/package_classifier.py \
  core/ecosystems.py core/classification_audit.py -lll
```

*Tip:* If you modify security-sensitive modules outside the list above (e.g., `tools/dendro-repo-helper.py`, `core/repository_addition.py`, `core/repository_deletion.py`, `core/dnf5daemon.py`, `core/transaction_controller.py`, or Flatpak helpers), also run `ruff check`, `ruff format --check`, and `bandit -lll` directly on those files.

### Unit & Headless Qt Tests

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest -q tests
```

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
- **Keep the CI matrix on the supported Fedora window:** `ci.yml` runs the job in `fedora:<version>` containers for every release the project claims to support. The matrix is not cosmetic: COPR (`xyasharx/dendro`) publishes the RPM to `fedora-43`, `fedora-44`, `fedora-45`, and `fedora-rawhide` chroots, so any release users can install from must be one CI actually exercises. When a release reaches GA, add it here and to the COPR chroots. When a release reaches end-of-life, move the supported floor in `README.md`/`SECURITY.md`, this matrix, and the COPR chroots together in one change; an expired image tag makes the job fail at `dnf install` before any test runs. Never leave a release published but untested, and never quietly drop a release that is still supported.

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
- **Live switching:** `ui/i18n.py` owns a `LocaleManager`. `install(locale)` walks `QApplication.allWidgets()` and calls `retranslate()` on every live widget that defines one. There is deliberately **no registration list**: an opt-in registry only covers objects that existed at startup, so a dialog opened after the switch, or a panel whose author forgot to register it, silently keeps the old language. Membership is "is this widget alive", which cannot be forgotten. `MainWindow._apply_language_choice` therefore does not enumerate children; it only refreshes the things with no widget of their own (the window title and the running status message).
- **`retranslate()` must not destroy widgets, and must not re-enter.** Patch text in place. The first implementation tore the tab pages down and re-ran `_init_ui()`; on Fedora 45 that segfaulted, because the combo box delivering the `currentIndexChanged` signal that triggered the rebuild was deleted underneath the handler. It crashed on one Fedora version and not the others, which is the worst kind of bug to leave in. Two further rules follow from the same class of bug, and both are enforced in `ui/i18n.py::LocaleManager.retranslate_all` rather than left to each hook:
  - A widget already being retranslated is skipped, so a hook that ends up calling `install()` again cannot call itself. The Preferences dialog sits on the signal path that starts the sweep, so "retranslate while retranslating" is a reachable state, not a hypothetical; without the guard it recurses until the stack runs out.
  - `sip.isdeleted(widget)` is checked before the hook is looked up, and a `RuntimeError` from a widget torn down mid-sweep ends that widget rather than the sweep. `allWidgets()` is allowed to still list a destroyed C++ object.
- **A `retranslate()` hook must not resize its own window synchronously.** Re-fitting the layout while a combo box is delivering the signal that changed the language is a re-entrant layout change, and the text it measures is a frame behind in some Qt builds. `DendroSettingsDialog` defers the re-fit with `QTimer.singleShot(0, ...)` behind a `sip.isdeleted` check. Translated text is a different length, so the re-fit itself is required, not optional: the dialog widens from 563px in German to 714px in Hindi.
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
- **Version bumps:** `pyproject.toml`, `dendro.spec`, `data/io.github.xyasharx.Dendro.metainfo.xml` (release notes), and `README.md` if referenced.
- **New Python modules or data files:** Verify inclusion in `pyproject.toml`/`setup.py`, `dendro.spec` (`%files`), `io.github.xyasharx.Dendro.yml` (`cp -a ...`), and `build_appimage.sh`.
- **Message catalogs:** every distribution form installs `data/locales/*.json` separately from the Python package (`core/i18n.py` discovers them beside the package directory, then falls back to `/usr/share/dendro/locales`). Add the install line to the same three manifests, or those builds ship English-only.
- **New runtime dependencies:** Update `requirements.txt`, `dendro.spec` (`Requires:`), `io.github.xyasharx.Dendro.yml` (offline hash-pinned source if not in `PyQt.BaseApp`), and `README.md`.
- **Search syntax or shortcuts changes:** Update `core/models.py`/`core/taxonomy.py`, tests, and `README.md` tables together.
- **Commit style:** Follow the repository's scoped conventional format (e.g., `fix(ui): ...`, `feat(taxonomy): ...`, `docs: ...`, `release: ...`), keeping changes focused and accompanied by regression tests.
