#!/bin/bash
set -e

OUTDIR="$1"
mkdir -p "$OUTDIR"
mkdir -p rpmbuild/{RPMS,SRPMS,BUILD,SOURCES,SPECS}

# ۱. استخراج خودکار نسخه از تگ گیت (مثلاً v1.2.0 تبدیل به 1.2.0 می‌شود)
GIT_TAG=$(git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || echo "")

if [ -n "$GIT_TAG" ]; then
    VERSION="$GIT_TAG"
    # به‌روزرسانی خودکار نسخه داخل spec بر اساس تگ گیت
    sed -i "s/^Version:.*/Version:        ${VERSION}/" dendro.spec
else
    # اگر تگی وجود نداشت، نسخه را از خود فایل spec می‌خواند
    VERSION=$(grep -m1 '^Version:' dendro.spec | awk '{print $2}')
fi

echo "==> Building Dendro RPM for Version: ${VERSION}"

# ۲. فشرده‌سازی خودکار و داینامیک با نسخه تشخیص داده شده
tar --transform "s,^\.,dendro-${VERSION}," --exclude='.git*' -czf "rpmbuild/SOURCES/dendro-${VERSION}.tar.gz" .

# ۳. کپی فایل spec
cp dendro.spec rpmbuild/SPECS/

# ۴. اجرای کامپایل با تحمیل مسیر
rpmbuild -D "_topdir $(pwd)/rpmbuild" -bs rpmbuild/SPECS/dendro.spec

# ۵. انتقال فایل نهایی به خروجی COPR
cp rpmbuild/SRPMS/*.src.rpm "$OUTDIR"/
