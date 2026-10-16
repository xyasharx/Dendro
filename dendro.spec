Name:           dendro
Version:        2.11.0
Release:        1%{?dist}
Summary:        Fedora DNF5 daemon package manager and dependency inspector

License:        GPL-3.0-or-later
URL:            https://github.com/xyasharx/Dendro
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

BuildArch:       noarch

BuildRequires:  appstream
BuildRequires:  desktop-file-utils
BuildRequires:  python3-devel
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
Requires:       python3-pyqt6-base >= 6.6.0
Requires:       python3-rpm

%description
Dendro is an unprivileged PyQt package-management frontend for Fedora.

It resolves, reviews, and executes package transactions through the system
DNF5 daemon on org.rpm.dnf.v0. The daemon owns RPM database access and uses
Polkit for authorization. Dendro does not run dnf5 through a generic privileged
shell command. Repository-file deletion uses the installed, argument-validated
Dendro helper because that operation is not exposed by the daemon API.

This RPM is supported on Fedora systems with dnf5daemon-server. It is not a
portable package manager for arbitrary Linux distributions.

%prep
%autosetup -n Dendro-%{version}

%build
%pyproject_wheel

%install
%pyproject_install

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
%{_libexecdir}/dendro-repo-helper
%{_datadir}/polkit-1/actions/org.dendro.policy
%{_datadir}/applications/io.github.xyasharx.Dendro.desktop
%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml
%{_datadir}/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png
%{_datadir}/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg

%changelog
* Fri Oct 16 2026 Dendro maintainers <maintainers@dendro.example> - 2.11.0-1
- Route package-changing operations through the Fedora DNF5 system daemon and its Polkit-mediated authority.
- Display and execute one retained daemon-resolved transaction plan rather than a generic privileged DNF command.
- Package the constrained repository-file deletion helper and its Polkit policy.
- Treat malformed and lookalike repository base URLs as non-official origins.
- Validate the offscreen Qt suite, desktop metadata, AppStream metadata, and packaged files during RPM builds.
