#!/bin/bash
set -e

OUTDIR="$1"
mkdir -p "$OUTDIR"
mkdir -p rpmbuild/{RPMS,SRPMS,BUILD,SOURCES,SPECS}

# ۱. خواندن خودکار نسخه از تگ گیت‌هاب (تبدیل v1.3.0 به 1.3.0)
GIT_TAG=$(git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || echo "")

if [ -n "$GIT_TAG" ]; then
    VERSION="$GIT_TAG"
    echo "==> Detected GitHub Release Tag: v${VERSION}"
    
    # آپدیت خودکار نسخه در فایل spec
    sed -i "s/^Version:.*/Version:        ${VERSION}/" dendro.spec
    
    # آپدیت خودکار نسخه در فایل pyproject.toml
    sed -i "s/^version = .*/version = \"${VERSION}\"/" pyproject.toml
else
    # اگر تگی نبود، از نسخه پیش‌فرض داخل فایل استفاده می‌کند
    VERSION=$(grep -m1 '^Version:' dendro.spec | awk '{print $2}')
    echo "==> Using default version from spec: ${VERSION}"
fi

echo "==> Building Dendro RPM for Version: ${VERSION}"

# ۲. فشرده‌سازی در مسیر امن /tmp بدون تداخل با پوشه جاری
tar --transform "s,^\.,dendro-${VERSION}," \
    --exclude='.git*' \
    --exclude='rpmbuild*' \
    --exclude='*.tar.gz' \
    -czf "/tmp/dendro-${VERSION}.tar.gz" .

# ۳. انتقال سورس آماده به پوشه بیلد
mv "/tmp/dendro-${VERSION}.tar.gz" "rpmbuild/SOURCES/"

# ۴. کپی فایل spec آپدیت‌شده
cp dendro.spec rpmbuild/SPECS/

# ۵. ساخت پکیج سورس SRPM
rpmbuild --define "_topdir $(pwd)/rpmbuild" -bs rpmbuild/SPECS/dendro.spec

# ۶. انتقال پکیج به خروجی COPR
cp rpmbuild/SRPMS/*.src.rpm "$OUTDIR"/
echo "==> SRPM Build Completed Successfully for version ${VERSION}!"
