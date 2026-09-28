srpm:
	dnf install -y rpm-build rpmdevtools
	$(eval VERSION := $(shell grep -m1 '^Version:' dendro.spec | awk '{print $$2}'))
	$(eval SRCNAME := $(shell grep -m1 '^%global srcname' dendro.spec | awk '{print $$3}'))
	find . -type f -exec sed -i 's/\r$$//' {} +
	mkdir -p rpmbuild/SOURCES rpmbuild/SPECS
	tar --transform "s,^\.,$(SRCNAME)-$(VERSION)," --exclude=".git*" --exclude="rpmbuild*" -czf rpmbuild/SOURCES/$(SRCNAME)-$(VERSION).tar.gz .
	cp dendro.spec rpmbuild/SPECS/
	rpmbuild -D "_topdir $(CURDIR)/rpmbuild" -bs rpmbuild/SPECS/dendro.spec
	cp rpmbuild/SRPMS/*.src.rpm $(outdir)/
