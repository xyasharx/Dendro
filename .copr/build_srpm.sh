#!/bin/bash
set -e

OUTDIR="$1"
mkdir -p "$OUTDIR"
mkdir -p rpmbuild/{RPMS,SRPMS,BUILD,SOURCES,SPECS}

# ۱. استخراج دقیق نسخه از فایل spec
VERSION=$(grep -m1 '^Version:' dendro.spec | awk '{print $2}')
if [ -z "$VERSION" ]; then
    VERSION="1.2.0"
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

# ۴. کپی فایل spec
cp dendro.spec rpmbuild/SPECS/

# ۵. ساخت پکیج سورس SRPM
rpmbuild --define "_topdir $(pwd)/rpmbuild" -bs rpmbuild/SPECS/dendro.spec

# ۶. انتقال پکیج به خروجی COPR
cp rpmbuild/SRPMS/*.src.rpm "$OUTDIR"/
echo "==> SRPM Build Completed Successfully!"
