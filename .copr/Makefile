SHELL := /usr/bin/bash

PKGNAME := dendro
SRCNAME := Dendro
VERSION := $(shell awk '/^Version:[[:space:]]*/ { print $$2; exit }' dendro.spec)

.PHONY: srpm

srpm:
	set -euo pipefail
	test -n "$(VERSION)"
	test -n "$(outdir)"
	dnf install -y rpm-build
	rm -rf rpmbuild
	install -d rpmbuild/SOURCES rpmbuild/SPECS "$(outdir)"
	if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then \
	  git archive --format=tar.gz --prefix="$(SRCNAME)-$(VERSION)/" HEAD \
	    > "rpmbuild/SOURCES/$(PKGNAME)-$(VERSION).tar.gz"; \
	else \
	  tar --create --gzip \
	    --file="rpmbuild/SOURCES/$(PKGNAME)-$(VERSION).tar.gz" \
	    --transform="s,^\.$$,$(SRCNAME)-$(VERSION)," \
	    --transform="s,^\./,$(SRCNAME)-$(VERSION)/," \
	    --exclude-vcs \
	    --exclude="./rpmbuild" \
	    --exclude="./rpmbuild/*" \
	    .; \
	fi
	cp dendro.spec rpmbuild/SPECS/
	rpmbuild -D "_topdir $(CURDIR)/rpmbuild" -bs rpmbuild/SPECS/dendro.spec
	install -m 0644 rpmbuild/SRPMS/*.src.rpm "$(outdir)/"
