Name:           dendro
Version:        2.20.0
Release:        1%{?dist}
Summary:        Fedora DNF5 daemon package manager and dependency inspector

License:        GPL-3.0-or-later
URL:            https://github.com/xyasharx/Dendro
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

BuildArch:       noarch

BuildRequires:  appstream
BuildRequires:  bash-completion
BuildRequires:  desktop-file-utils
BuildRequires:  python3-defusedxml
BuildRequires:  python3-gobject
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-pyqt6-base >= 6.6.0
BuildRequires:  python3-pytest
BuildRequires:  python3-pytest-qt
BuildRequires:  python3-rpm
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-setuptools >= 77
BuildRequires:  python3-wheel

# The application is an unprivileged D-Bus client.  dnf5daemon-server owns
# package-manager authority and invokes Polkit for privileged operations.
Requires:       dnf5daemon-server >= 5.2.7
Requires:       dnf5daemon-server-polkit
Requires:       polkit
Requires:       ca-certificates
Requires:       python3-pyqt6-base >= 6.6.0
Requires:       python3-defusedxml
Requires:       python3-gobject
Requires:       python3-rpm
# Hindi ships as a translated locale. Without a Devanagari-capable font the UI
# renders placeholder boxes on installations that omit desktop fonts (minimal
# spins, containers, and the Flatpak runtime). Fedora's default font package
# pulls this in; naming it here keeps the translated locale usable on its own.
Recommends:     google-noto-sans-devanagari-fonts
Recommends:     bash-completion

%description
Dendro is an unprivileged PyQt package-management frontend for Fedora.

It resolves, reviews, and executes package transactions through the system
DNF5 daemon on org.rpm.dnf.v0. The daemon owns RPM database access and uses
Polkit for authorization. Dendro does not run dnf5 through a generic privileged
shell command. Direct HTTPS repository configuration addition and repository-file
deletion use the installed, argument-validated Dendro helper because persistent
repository-file creation and deletion are not exposed by the daemon API.

This RPM is supported on Fedora systems with dnf5daemon-server. It is not a
portable package manager for arbitrary Linux distributions.

%prep
%autosetup -n Dendro-%{version}

%build
%pyproject_wheel

%install
%pyproject_install

install -Dpm 0644 data/dendro.bash \
    %{buildroot}%{bash_completions_dir}/dendro

install -Dpm 0755 tools/dendro-repo-helper.py \
    %{buildroot}%{_libexecdir}/dendro-repo-helper
install -Dpm 0644 data/org.dendro.policy \
    %{buildroot}%{_datadir}/polkit-1/actions/org.dendro.policy
install -Dpm 0644 data/io.github.xyasharx.Dendro.desktop \
    %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop
install -Dpm 0644 data/io.github.xyasharx.Dendro.metainfo.xml \
    %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml
install -Dpm 0644 data/icons/256x256/io.github.xyasharx.Dendro.png \
    %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png
install -Dpm 0644 io.github.xyasharx.Dendro.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg

# Message catalogs live outside the Python package so translators can rebuild
# and install them without touching the wheel's installed module tree.
install -d %{buildroot}%{_datadir}/dendro/locales
install -pm 0644 data/locales/*.json \
    %{buildroot}%{_datadir}/dendro/locales/

%check
bash -n data/dendro.bash

# Keep Qt settings and caches inside the build tree; tests must not read or
# modify the builder user's desktop state.
export QT_QPA_PLATFORM=offscreen
export PYTHONPATH=.
export XDG_CONFIG_HOME=%{_builddir}/dendro-xdg-config
export XDG_CACHE_HOME=%{_builddir}/dendro-xdg-cache
# rpmbuild abstracts %check into a script and reports only "Bad exit status",
# which names neither the test nor the file. Run one file per process so a Qt
# fault in one file is attributed to that file and the rest still report, then
# fail with the file name in the build log where the CI annotation can find it.
set +e
check_failed=""
for test_file in tests/test_*.py; do
    %{__python3} -u -X faulthandler -m pytest -q "$test_file" > "pytest-$(basename "$test_file").log" 2>&1
    file_status=$?
    if [ $file_status -ne 0 ]; then
        check_failed="$check_failed $test_file"
        echo "--- $test_file exited $file_status ---"
        # A native fault prints "Binary file ..., at <symbol>" frames; keep them,
        # they are the only thing that names where the process died.
        grep -E "^(FAILED|ERROR)|^E   |Traceback|Fatal Python error|Current thread|^  Binary file" \
            "pytest-$(basename "$test_file").log" | head -30
        tail -25 "pytest-$(basename "$test_file").log"
    fi
done
set -e
if [ -n "$check_failed" ]; then
    echo "--- test files that failed:$check_failed ---"
    exit 1
fi

# Validate the exact files that will be packaged. Do not suppress validation
# failures: launcher and AppStream metadata are part of the runtime contract.
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop
appstreamcli validate --no-net %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml

%files
%license LICENSE
%doc README.md
%{python3_sitelib}/core/
%{python3_sitelib}/ui/
%{python3_sitelib}/main.py*
%{python3_sitelib}/__pycache__/main.*.pyc
%{python3_sitelib}/dendro-%{version}.dist-info/
%{_bindir}/dendro
%{bash_completions_dir}/dendro
%{_libexecdir}/dendro-repo-helper
%{_datadir}/polkit-1/actions/org.dendro.policy
%{_datadir}/applications/io.github.xyasharx.Dendro.desktop
%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml
%{_datadir}/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png
%{_datadir}/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg
%{_datadir}/dendro/locales/*.json

%changelog
* Sun Nov 22 2026 Dendro maintainers <yashar@duck.com> - 2.20.0-1
- Rebuild the inspector's file manifest as one card: a single frame, one vertical scrollbar at the card's right edge, and no row ever painted cut in half.
- Make every table's column headers translatable, covering the transaction history, repository manager, Flatpak inventory, and manifest, which had shipped in English in all seven languages.
- Translate the window title without changing the window's identity, translate DNF5 daemon severities, and ask the daemon for the session's language.
- Give every security advisory reference a reachable, translated link, and fix advisory text rendering, link contrast, and markup injection.
- Fix clipped repository status checkboxes, mismatched control heights, unstyled settings tabs, and tables that cut their last row in half.
- Split the main window along testable boundaries, adding theme coordination, window presentation, and daemon locale modules.
- Stop CI from failing during Qt interpreter shutdown rather than on a test.
- Remove the manifest's duplicate scroll synchronisation and row-size copying that caused intermittent segfaults.
* Fri Nov 20 2026 Dendro maintainers <yashar@duck.com> - 2.19.0-1
- Ship the interface in Spanish, Portuguese (Brazil and Portugal), French, German, Hindi, and Russian, switching live without a restart from Preferences.
- Translate every header label and tooltip, including the counted queue, update, and orphan buttons and the search filter reference.
- Keep the window title and the running status message in the selected language.
- Test Fedora 45 alongside 43 and 44.

* Wed Nov 18 2026 Dendro maintainers <yashar@duck.com> - 2.18.0-2
- Keep label sizes in the unit the platform reports, so a pixel-sized application font is no longer guessed larger than the user's setting.
- Tighten the instance metadata card's spacing so it stays compact at a larger font.
- Fix a malformed %changelog date.

* Tue Nov 17 2026 Dendro maintainers <yashar@duck.com> - 2.18.0-1
- Scale interface text from the user's own font instead of pinning pixel sizes in the stylesheet, so labels grow with the desktop's font setting.
- Measure the metadata field-name gutter from the font it paints in, which had been clipping "Architecture".
- Rename the session queue view to "Transaction Queue", and split the sidebar's bottom views into "Maintenance & Queue" and "Sources & Catalog" with repository sources as a top-level row.
- Enforce the sidebar umbrella-parent hierarchy in taxonomy registry validation, and add theme-aware empty-state guidance to the package tree.
- Answer DNF5 repository signing key prompts instead of letting the daemon stall for five minutes, and ask before trusting a key while showing its fingerprint, identities, and download source.
- Remove six unused interface style rules.

* Tue Nov 17 2026 Dendro maintainers <yashar@duck.com> - 2.17.1-1
- Ask before trusting a repository's OpenPGP signing key with a dialog that shows its fingerprint, signing identities, and download source, instead of declining the key silently when automatic import is off.
- Keep the safe answer the default: the dialog declines on Escape or Enter, and a prompt that cannot be displayed declines rather than trusting an unseen key.
- Only store the "stop asking" choice when the key was actually trusted, and describe the new prompt in Settings.

* Tue Nov 17 2026 Dendro maintainers <yashar@duck.com> - 2.17.0-1
- Add kernel and filesystem host-environment classification for mutable Fedora, Fedora Atomic and OSTree hosts, read-only /usr systems, Flatpak, and unbooted containers.
- Block live DNF5 daemon operations and repository-helper file mutations on Fedora Atomic, OSTree (including unlocked usroverlay), and read-only /usr hosts, and refuse forwarded host system D-Bus access in unbooted containers.
- Add unprivileged read-only org.projectatomic.rpmostree1 deployment and package-provenance inspection, mtime-safe rpm -V --nomtime checks, rpm-ostree Comps catalog indexing, and status:layered, status:override, and status:base search filters.
- Add a host-mode banner, OSTree package provenance labels, and read-only dialog and toolbar states while keeping host Flatpak app management available.

* Tue Nov 17 2026 Dendro maintainers <yashar@duck.com> - 2.16.1-1
- Align repository-helper .repo validation with libdnf5 INI and URL-list parsing to block comma-separated, quoted, indented-continuation, colon-delimited, and mixed-case option bypasses.
- Reject DEL and C1 control characters across repository URLs and configurations, and harden existing repository-ID collision scanning.
- Bound HTTP 30x redirect response bodies, enforce whole-download deadlines across redirects, and strip LD_PRELOAD and LD_AUDIT before launching the repository helper.
- Fix DNF5 daemon Repo.list string-array baseurl handling, Goal.resolve Skipped item parsing, Reason Change labels, and strict boolean/integer D-Bus reply checks.

* Sun Nov 01 2026 Dendro maintainers <yashar@duck.com> - 2.16.0-1
- Add a separate Flatpak window for apps and runtimes, opened from the header. Flatpak packages stay out of the RPM package tree and DNF transactions.
- Update and uninstall apps installed for your user, reporting the version change from before and after each update.
- Update and uninstall system-wide apps through Flatpak's system helper with Polkit prompts; requires Flatpak 1.16.4 or newer.
- Show a progress bar with steps, percentage, speed, and time left during Flatpak updates; add Cancel for system-wide changes.
- Offer unused runtime removal when uninstalling a Flatpak app. Runtime updates and standalone runtime removal are not included.
- Add bundled SVG icons for the header and sidebar, and harden repository-file download, repository helper, and portal integration paths.
- No RPM package-management behaviour changed.

* Fri Oct 30 2026 Dendro maintainers <yashar@duck.com> - 2.15.0-1
- Add manifest-first, evidence-driven RPM classification for installed, remote, local-file, and third-party RPM metadata.
- Add declarative ecosystem evidence, searchable ecosystem facets, system/reference/repository categories, and constrained fallback policies for sparse and support RPMs.
- Classify local RPM inspection headers and safely frame DNF5 remote Provides data; retain explicit conservative DNF4 compatibility.
- Add reproducible taxonomy-coverage auditing and documentation for evidence gaps and ambiguity handling.
- Repair DNF5 History loading through documented JSON output, improve visible error feedback, fix Qt themed-icon fallbacks, and harden malformed transaction-result handling.

* Thu Oct 29 2026 Dendro maintainers <yashar@duck.com> - 2.14.2-1
- Preserve exact RPM identity across multilib, install-only, update, inspection, file-owner, and queued transaction workflows.
- Reject malformed RPM, DNF, and DNF5 data; improve safe command-line fallbacks and visible background-query failure feedback.
- Handle gpg-pubkey database pseudo-packages safely, preserve files with spaces during inspection and verification, and recognize legacy /lib/firmware manifests.
- Show clear feedback when an external web handler is unavailable, and protect the UI from stale asynchronous results.
- Thank Ricky-Tigg for detailed reports, reproduction information, suggested wording, and validation for #43, #44, and #45.

* Tue Oct 27 2026 Dendro maintainers <yashar@duck.com> - 2.14.1-1
- Repair desktop-portal theme synchronization and add explicit Follow System, Light, and Dark appearance choices.
- Restore a Details inspector that was hidden by a saved collapsed splitter layout; improve main-window and About dialog icon handling.
- Classify Steam and similar game launchers as Games when their desktop metadata also declares broad network or file-transfer categories.
- Report labelled Git commit and tree identity for source and packaged builds; improve Bash completion.
- Thank Ricky-Tigg for detailed Fedora, GNOME, Wayland, Qt, packaging, completion, and UI-layout reports and validation.

* Thu Oct 22 2026 Dendro maintainers <yashar@duck.com> - 2.14.0-1
- Add an evidence-aware central package taxonomy with a neutral fallback for packages that lack trustworthy metadata.
- Harden AppStream and Fedora Comps metadata parsing, including multi-package AppStream components and protected XML handling.
- Improve package-removal guidance, sidebar/filter parity, search performance, and taxonomy regression coverage.
- Fix native titlebar and first system light/dark theme synchronization behavior on GNOME Wayland.
- Thank Ricky-Tigg for the detailed theme-regression report and validation feedback.

* Mon Oct 19 2026 Dendro maintainers <yashar@duck.com> - 2.13.0-1
- Support multiple search-filter expressions with context-aware completion,
  complete validation, and updated search guidance.
- Decode supported Zstandard-compressed Fedora Comps metadata before XML parsing.
- Preserve CLI classification for packages such as htop that overlap Fedora's
  Security Lab and System Tools Comps groups.
- Correct missing dependency-provider state, invalidate stale provider cache
  data after completed transactions, and protect DNF search option boundaries.
- Improve native titlebar colour-scheme synchronisation and release validation.

* Sat Oct 17 2026 Dendro maintainers <yashar@duck.com> - 2.12.0-1
- Add bounded direct HTTPS repository-file installation through the constrained
  Polkit helper; validate content, repository IDs, and package-signature policy.
- Keep repository persistence atomic and no-overwrite, without a privileged
  downloader, generic shell bridge, or DNF command execution.
- Bundle the Qt XCB/XKB runtime closure required by the AppImage and verify
  offline graphical startup in release CI.
  
* Fri Oct 16 2026 Dendro maintainers <yashar@duck.com> - 2.11.0-1
- Route package-changing operations through the Fedora DNF5 system daemon and its Polkit-mediated authority.
- Display and execute one retained daemon-resolved transaction plan rather than a generic privileged DNF command.
- Package the constrained repository-file deletion helper and its Polkit policy.
- Treat malformed and lookalike repository base URLs as non-official origins.
- Declare Python RPM bindings and python3-pip for the build check and %pyproject_wheel in minimal buildroots.
- Install the source icon assets correctly and validate the complete offscreen test suite and installed metadata during RPM builds.
