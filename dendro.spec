Name:           dendro
Version:        2.15.0
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
BuildRequires:  python3-setuptools >= 69
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

%check
bash -n data/dendro.bash

# Keep Qt settings and caches inside the build tree; tests must not read or
# modify the builder user's desktop state.
export QT_QPA_PLATFORM=offscreen
export PYTHONPATH=.
export XDG_CONFIG_HOME=%{_builddir}/dendro-xdg-config
export XDG_CACHE_HOME=%{_builddir}/dendro-xdg-cache
%{__python3} -m pytest -q tests

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

%changelog
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
