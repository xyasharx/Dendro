SHELL := /usr/bin/bash

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
	tar --create --gzip \
	  --file="rpmbuild/SOURCES/$(SRCNAME)-$(VERSION).tar.gz" \
	  --transform="s,^\.$$,$(SRCNAME)-$(VERSION)," \
	  --transform="s,^\./,$(SRCNAME)-$(VERSION)/," \
	  --exclude-vcs \
	  --exclude="./rpmbuild" \
	  --exclude="./rpmbuild/*" \
	  .
	cp dendro.spec rpmbuild/SPECS/
	rpmbuild -D "_topdir $(CURDIR)/rpmbuild" -bs rpmbuild/SPECS/dendro.spec
	install -m 0644 rpmbuild/SRPMS/*.src.rpm "$(outdir)/"
