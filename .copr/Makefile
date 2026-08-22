srpm:
	mkdir -p $(outdir)
	tar -czf $(outdir)/Dendro-1.2.0.tar.gz --exclude='.git*' --exclude='*.tar.gz' --transform 's,^\.,Dendro-1.2.0,' .
	rpmbuild -bs --define "_sourcedir $(outdir)" --define "_srcrpmdir $(outdir)" $(spec)
