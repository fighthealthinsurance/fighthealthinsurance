.PHONY: run-local install build-static

run-local:
	scripts/run_local.sh

install:
	./scripts/install.sh

build-static:
	./scripts/build_static.sh
