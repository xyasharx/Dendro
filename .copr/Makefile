SHELL := /usr/bin/bash
.ONESHELL:

PKGNAME := dendro
SRCNAME := Dendro
VERSION := $(shell awk '/^Version:[[:space:]]*/ { print $$2; exit }' dendro.spec)

.PHONY: srpm

srpm:
	set -euo pipefail
	test -n "$(VERSION)"
	test -n "$(outdir)"
	if ! command -v git >/dev/null 2>&1 || ! command -v rpmbuild >/dev/null 2>&1; then
		dnf install -y git-core rpm-build
	fi
	rm -rf rpmbuild
	install -d rpmbuild/SOURCES rpmbuild/SPECS "$(outdir)"
	if [ -e .git ]; then
		git -c safe.directory="$(CURDIR)" -c safe.directory='*' archive \
			--format=tar.gz --prefix="$(SRCNAME)-$(VERSION)/" HEAD \
			> "rpmbuild/SOURCES/$(PKGNAME)-$(VERSION).tar.gz"
	else
		tar --create --gzip \
			--file="rpmbuild/SOURCES/$(PKGNAME)-$(VERSION).tar.gz" \
			--transform="s,^\.$$,$(SRCNAME)-$(VERSION)," \
			--transform="s,^\./,$(SRCNAME)-$(VERSION)/," \
			--exclude="./.git" \
			--exclude="./.git/*" \
			--exclude="./rpmbuild" \
			--exclude="./rpmbuild/*" \
			.
	fi
	cp dendro.spec rpmbuild/SPECS/
	rpmbuild -D "_topdir $(CURDIR)/rpmbuild" -bs rpmbuild/SPECS/dendro.spec
	install -m 0644 rpmbuild/SRPMS/*.src.rpm "$(outdir)/"
