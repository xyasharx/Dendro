srpm:
	mkdir -p $(outdir)
	mkdir -p rpmbuild/SOURCES
	tar -czf rpmbuild/SOURCES/dendro-1.2.0.tar.gz --exclude=".git*" --exclude="rpmbuild*" --transform "s,^\.,dendro-1.2.0," .
	rpmbuild -D "_srcrpmdir $(outdir)" -D "_sourcedir $(CURDIR)/rpmbuild/SOURCES" -bs $(spec)
