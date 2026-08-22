srpm:
	dnf install -y rpm-build rpmdevtools
	mkdir -p rpmbuild/SOURCES rpmbuild/SPECS
	tar --transform "s,^\.,dendro-1.2.0," --exclude=".git*" --exclude="rpmbuild*" -czf rpmbuild/SOURCES/dendro-1.2.0.tar.gz .
	cp dendro.spec rpmbuild/SPECS/
	rpmbuild -D "_topdir $(CURDIR)/rpmbuild" -bs rpmbuild/SPECS/dendro.spec
	cp rpmbuild/SRPMS/*.src.rpm $(outdir)/
