srpm:
	mkdir -p $(outdir)
	tar --transform 's,^\.,dendro-1.0.0,' --exclude='.git*' --exclude='*.tar.gz' -czf /tmp/dendro-1.0.0.tar.gz .
	rpmbuild -bs --define "_sourcedir /tmp" --define "_srcrpmdir $(outdir)" $(spec)
