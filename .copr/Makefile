srpm:
	dnf install -y rpm-build rpmdevtools
	$(eval VERSION := $(shell grep -m1 '^Version:' dendro.spec | awk '{print $$2}'))
	mkdir -p rpmbuild/SOURCES rpmbuild/SPECS
	tar --transform "s,^\.,dendro-$(VERSION)," --exclude=".git*" --exclude="rpmbuild*" -czf rpmbuild/SOURCES/dendro-$(VERSION).tar.gz .
	cp dendro.spec rpmbuild/SPECS/
	rpmbuild -D "_topdir $(CURDIR)/rpmbuild" -bs rpmbuild/SPECS/dendro.spec
	cp rpmbuild/SRPMS/*.src.rpm $(outdir)/
