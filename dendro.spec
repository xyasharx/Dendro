Name:           dendro
Version:        1.8.3
Release:        1%{?dist}
Summary:        Visual package manager and dependency hierarchy explorer for Fedora Linux

License:        GPL-3.0-or-later
URL:            https://github.com/xyasharx/Dendro
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  desktop-file-utils
BuildRequires:  librsvg2-tools

# Runtime dependencies
Requires:       python3-pyqt6 >= 6.6.0
Requires:       python3-rpm
Requires:       (python3-libdnf5 or dnf5)
Requires:       polkit
Requires:       rpm
Requires:       (dnf5 or dnf)
Requires:       hicolor-icon-theme
Requires:       (adwaita-icon-theme or breeze-icon-theme)
Recommends:     (google-noto-color-emoji-fonts or gdouros-symbola-fonts)

%description
Dendro is a fast, graphical package manager and visual dependency explorer
designed specifically for Fedora Linux. It empowers users to inspect package
trees, remove orphaned libraries, and execute administrative actions safely
via native Polkit elevation, powered by native librpm and libdnf5 bindings.

%prep
%autosetup -n %{name}-%{version}
find . -type f -exec sed -i 's/\r$//' {} +
%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files core ui main

# Install Desktop launcher
install -D -m 0644 data/io.github.xyasharx.Dendro.desktop %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop

# Install Polkit Security Action
install -D -m 0644 data/org.dendro.policy %{buildroot}%{_datadir}/polkit-1/actions/org.dendro.policy

# Install AppStream Metadata
install -D -m 0644 data/io.github.xyasharx.Dendro.metainfo.xml %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml

# Install Scalable Vector Icon (Preferred by KDE Plasma and GNOME)
install -D -m 0644 io.github.xyasharx.Dendro.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg

# Generate fresh, uncorrupted PNG icons directly from SVG source
for size in 128 256 512; do
    mkdir -p %{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps
    rsvg-convert -w ${size} -h ${size} io.github.xyasharx.Dendro.svg -o %{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps/io.github.xyasharx.Dendro.png
done

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop

if command -v appstreamcli &> /dev/null; then
    appstreamcli validate --no-net %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml || true
elif command -v appstream-util &> /dev/null; then
    appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml || true
fi

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/dendro
%{_datadir}/applications/io.github.xyasharx.Dendro.desktop
%{_datadir}/polkit-1/actions/org.dendro.policy
%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml
%{_datadir}/icons/hicolor/*/apps/io.github.xyasharx.Dendro.*

%changelog
* Sat Sep 26 2026 Yashar <yashar@duck.com> - 1.8.3-1
- Release 1.8.3: Critical libdnf5 & Qt Model Crash Fix
- Switched user-installed, leaf package, and history workers to isolated subprocesses to eliminate libdnf5 SWIG null pointer segfaults
- Synchronized layoutAboutToBeChanged and layoutChanged across all tree mutations
- Upgraded librpm mutex to reentrant RLock and sanitized iterator lifecycles
- Hooked dependency resolution strictly to on-demand tree expansion
- Fixed theme reactivity in Software Repositories dialog and RPM Changelog console

* Sat Sep 26 2026 Yashar <yashar@duck.com> - 1.8.2-1
- Release 1.8.2: Critical Stability & Theme Reactivity Hotfix
- Resolved startup segmentation fault (SIGSEGV) caused by Qt proxy model layout signal desynchronization
- Synchronized layoutAboutToBeChanged and layoutChanged in tree model mutations
- Enforced reentrant RLock protection and sanitized match iterator lifecycles across librpm calls
- Switched user-installed and leaf package queries to isolated subprocesses to prevent libdnf5 SWIG crashes
- Replaced eager recursive filtering with on-demand dependency resolution upon tree item expansion
- Fixed hardcoded dark styles in Software Repositories dialog (#CoprBox, search inputs, repository table)
- Fixed Changelog tab to dynamically re-render HTML entries, dividers, and CVE links across all themes
- Fixed additive upgrade status tracking in model updates (26/26 tests passing)\

* Sat Sep 26 2026 Yashar <yashar@duck.com> - 1.8.0-1
- Release 1.8.0: Security Auditing, Updates & Engine Reliability Update
- Fixed native segmentation fault on Fedora 44 caused by QProxyStyle recursion (#32)
- Added RPM_GLOBAL_LOCK mutex and resolved libdnf5 SWIG memory corruption (#32)
- Corrected desktop file name identifier in main.py (#32)
- Integrated package file integrity and tamper auditor (rpm -V engine)
- Added native RPM changelog viewer with automated Red Hat CVE and Bugzilla linkification
- Added live system updates and security advisories engine powered by dnf5 check-upgrade
- Introduced graphical Software Repository and COPR channel manager (/etc/yum.repos.d)
- Added dedicated "Available Updates" and "User-Installed Packages" sidebar channels
- Added status:update, status:upgradable search syntax and upgrade path indicators
- Fixed close button clipping defect across all desktop environments
- Expanded headless test suite to 24 comprehensive unit and integration tests

* Sat Sep 26 2026 Yashar <yashar@duck.com> - 1.7.0-1
- Release 1.7.0: Deterministic Classification & Engine Reliability Update
- Implemented full 10-component Freedesktop AppStream catalog taxonomy
- Implemented 5-pillar topological precedence matrix eliminating category bleeding
- Fixed NoDisplay=true settings trap and Terminal=true desktop launcher promotion
- Added user-installed package provenance tracking (status:user search syntax)
- Added sequential multi-stage Polkit transaction runner for concurrent operations
- Added human-readable ontology titles and ELF shared library iconography

- Release 1.6.2: Fix blank line carriage return in %prep (#29)

* Wed Sep 23 2026 Yashar <yashar@duck.com> - 1.6.1-1
- Fix carriage return error during %prep find execution

* Wed Sep 23 2026 Yashar <yashar@duck.com> - 1.6.0-1
- Release 1.6.0
- Added dynamic theming with 6 curated dark/light palettes and QSettings persistence
- Added automatic system light/dark mode adaptation via Qt 6 FreeDesktop portal integration
- Implemented strict path anchoring for typography, firmware, and graphics acceleration drivers
- Added top-down precedence hierarchy preventing feature keywords from overriding application categories
- Resolved misclassifications for Firefox, LibreOffice, 7zip, and PipeWire

* Wed Sep 23 2026 Yashar <yashar@duck.com> - 1.5.0-1
- Release 1.5.0
- Implemented fine-grained category taxonomy with 6 new dedicated categories
- Separated GPU/Mesa graphics drivers and PipeWire/ALSA audio stack from core pillars
- Added specialized categories for Media Plugins, Desktop Addons, GUI Toolkits, and Settings
- Enforced strict Desktop Application validation gate eliminating library and plugin false positives
- Fixed user-installed flag override in proxy models

* Wed Sep 23 2026 Yashar <yashar@duck.com> - 1.4.3-1
- Release 1.4.3
- Unified physical anatomy extraction between native librpm and CLI subprocess queries
- Achieved 100% categorization, package count, and confidence parity in AppImage builds
- Extended RPM CLI query format to parse directory footprints and exported SONAME arrays

* Wed Sep 23 2026 Yashar <yashar@duck.com> - 1.4.2-1
- Release 1.4.2
- Installed scalable vector SVG icon to /usr/share/icons/hicolor/scalable/apps/
- Added build-time PNG rendering via rsvg-convert to prevent image corruption
- Fixed CRLF line-ending validation in desktop entry and AppStream metadata
- Added scalable SVG icon to portable AppImage bundle

* Tue Sep 22 2026 Yashar <yashar@duck.com> - 1.4.1-1
- Release 1.4.1
- Adapted execution footprint to Fedora 42+ unified /usr/bin and /usr/sbin layout
- Added /usr/libexec detection to isolate internal background helpers from user CLI tools
- Integrated FHS manual page section analysis (man8 daemons vs man1 user commands)
- Added automated test cases for unified binary execution and internal helpers

* Tue Sep 22 2026 Yashar <yashar@duck.com> - 1.4.0-1
- Release 1.4.0
- Introduced intelligent multi-factor semantic decision engine
- Added natural language domain profiling and physical anatomy extraction
- Added explainable classification confidence and rationale card to inspector panel
- Added direct root execution bypass for containerized environments
- Solved false-positive categorization for language CLI tools and firmware GUIs
