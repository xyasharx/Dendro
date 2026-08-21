Name:           dendro
Version:        1.1.0
Release:        1%{?dist}
Summary:        Visual package manager and dependency hierarchy explorer for Fedora Linux

License:        GPL-3.0-or-later
URL:            https://github.com/xyasharx/Dendro
Source0:        %{url}/archive/v%{version}/Dendro-%{version}.tar.gz

BuildArch:      noarch

# ابزارهای مورد نیاز برای کامپایل و پکیج‌بندی
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib

# نیازمندی‌های زمان اجرای برنامه در سیستم کاربر
Requires:       python3-pyqt6 >= 6.11.0
Requires:       polkit
Requires:       rpm
Requires:       (dnf5 or dnf)
Requires:       hicolor-icon-theme

%description
Dendro is a fast, graphical package manager and visual dependency explorer
designed specifically for Fedora Linux. It empowers users to inspect package
trees, remove orphaned libraries, and execute administrative actions safely
via native Polkit elevation.

%prep
%autosetup -n Dendro-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files core ui main

# ۱. نصب فایل استاندارد دسکتاپ لانچر
install -D -m 0644 data/io.github.xyasharx.Dendro.desktop %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop

# ۲. نصب پالیسی امنیتی Polkit برای اجرای بدون پسورد روت با پنجره گرافیکی
install -D -m 0644 data/org.dendro.policy %{buildroot}%{_datadir}/polkit-1/actions/org.dendro.policy

# ۳. نصب فایل متادیتای AppStream برای نمایش در مرکز نرم‌افزار (GNOME Software / KDE Discover)
install -D -m 0644 data/io.github.xyasharx.Dendro.metainfo.xml %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml

# ۴. نصب آیکون با کیفیت‌های مختلف در تم Hicolor
install -D -m 0644 data/icons/128x128/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/128x128/apps/io.github.xyasharx.Dendro.png
install -D -m 0644 data/icons/256x256/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png
install -D -m 0644 data/icons/512x512/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/512x512/apps/io.github.xyasharx.Dendro.png

%check
# تست اعتبارسنجی فایل دسکتاپ
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop

# تست اعتبارسنجی متادیتای AppStream (بدون نیاز به دسترسی شبکه در محیط بیلد)
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/dendro
%{_datadir}/applications/io.github.xyasharx.Dendro.desktop
%{_datadir}/polkit-1/actions/org.dendro.policy
%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml
%{_datadir}/icons/hicolor/*/apps/io.github.xyasharx.Dendro.png

%changelog
* Fri Aug 21 2026 Yashar <yashar@duck.com> - 1.0.0-1
- Initial release for Fedora Linux with native Polkit and DNF/DNF5 support.
