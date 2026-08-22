srpm:
	dnf install -y rpm-build tar
	tar --transform 's,^\.,dendro-1.0.0,' --exclude='.git*' --exclude='venv*' -czf dendro-1.0.0.tar.gz .
	rpmbuild -bs --define "_sourcedir $(CURDIR)" --define "_srcrpmdir $(outdir)" dendro.spec
