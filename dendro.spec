Name:           dendro
Version:        1.2.0
Release:        1%{?dist}
Summary:        Visual package manager and dependency hierarchy explorer for Fedora Linux

License:        GPL-3.0-or-later
URL:            https://github.com/xyasharx/Dendro
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

# نیازمندی‌های زمان بیلد
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib

# نیازمندی‌های زمان اجرای برنامه
Requires:       python3-pyqt6 >= 6.6.0
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
# استخراج امن بدون وابستگی به حروف کوچک/بزرگ پوشه گیت‌هاب
%autosetup -c

# اگر محتوا داخل یک زیرپوشه بود، به آن وارد می‌شویم
if [ -d "Dendro-%{version}" ]; then
    cd Dendro-%{version}
elif [ -d "dendro-%{version}" ]; then
    cd dendro-%{version}
elif [ -d "Dendro" ]; then
    cd Dendro
fi

%build
# پیدا کردن دایرکتوری حاوی pyproject.toml
if [ ! -f "pyproject.toml" ]; then
    cd $(find . -maxdepth 2 -name "pyproject.toml" -exec dirname {} \;)
fi
%pyproject_wheel

%install
if [ ! -f "pyproject.toml" ]; then
    cd $(find . -maxdepth 2 -name "pyproject.toml" -exec dirname {} \;)
fi
%pyproject_install
%pyproject_save_files core ui main

# ۱. نصب لانچر دسکتاپ
install -D -m 0644 data/io.github.xyasharx.Dendro.desktop %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop

# ۲. نصب پالیسی امنیتی Polkit
install -D -m 0644 data/org.dendro.policy %{buildroot}%{_datadir}/polkit-1/actions/org.dendro.policy

# ۳. نصب متادیتای AppStream
install -D -m 0644 data/io.github.xyasharx.Dendro.metainfo.xml %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml

# ۴. نصب آیکون‌ها در ابعاد مختلف
install -D -m 0644 data/icons/128x128/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/128x128/apps/io.github.xyasharx.Dendro.png
install -D -m 0644 data/icons/256x256/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png
install -D -m 0644 data/icons/512x512/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/512x512/apps/io.github.xyasharx.Dendro.png

%check
# تست اعتبارسنجی لانچر دسکتاپ
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop

# تست اعتبارسنجی متادیتای AppStream
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
* Fri Aug 21 2026 Yashar <yashar@duck.com> - 1.2.0-1
- Fix resilient build directory extraction and AppStream validation for Fedora COPR
