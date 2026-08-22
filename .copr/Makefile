srpm:
	dnf install -y rpm-build
	mkdir -p $(outdir)
	mkdir -p rpmbuild/SOURCES
	tar --transform "s,^\.,dendro-1.2.0," --exclude='.git*' --exclude='rpmbuild*' --exclude='*.tar.gz' -czf rpmbuild/SOURCES/dendro-1.2.0.tar.gz .
	rpmbuild --define "_sourcedir $(CURDIR)/rpmbuild/SOURCES" --define "_srcrpmdir $(outdir)" -bs dendro.spec
