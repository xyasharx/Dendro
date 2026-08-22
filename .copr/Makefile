srpm:
	mkdir -p $(outdir)
	mkdir -p $(HOME)/rpmbuild/SOURCES $(HOME)/rpmbuild/SPECS $(HOME)/rpmbuild/SRPMS
	tar --transform 's,^\.,dendro-1.2.0,' --exclude='.git*' -czf $(HOME)/rpmbuild/SOURCES/dendro-1.2.0.tar.gz .
	cp dendro.spec $(HOME)/rpmbuild/SPECS/
	rpmbuild -bs $(HOME)/rpmbuild/SPECS/dendro.spec
	cp $(HOME)/rpmbuild/SRPMS/*.src.rpm $(outdir)/
