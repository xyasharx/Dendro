srpm:
	dnf -y install rpmdevtools rpm-build
	mkdir -p $(outdir)
	spectool -g -C $(outdir) $(spec)
	rpmbuild -bs --define "_sourcedir $(outdir)" --define "_srcrpmdir $(outdir)" $(spec)
