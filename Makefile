SHELL := /bin/bash
.DEFAULT_GOAL := help
export PATH := $(CURDIR)/.tools:$(PATH)
export KUBECONFIG := $(CURDIR)/.state/kubeconfig
.PHONY: help tools kind deploy render static verify demo canary log-delivery operation-check acceptance ci-acceptance ui lock vendor validate-containers package
help:
	@printf '%s\n' 'SIGNAL — run on a dedicated Ubuntu 24.04 amd64 VM.' 'Cluster: sudo bash scripts/bootstrap-ubuntu.sh --dedicated-node' 'Then: make deploy && make verify && make acceptance' 'Alternative local CI cluster: make kind' 'Other targets: static render demo canary ui lock vendor validate-containers package'
tools:
	bash scripts/install-tools.sh
kind:
	bash scripts/kind-up.sh
deploy:
	bash scripts/deploy.sh
render:
	python3 scripts/render.py > manifests/application.json
	python3 scripts/render.py --stage bootstrap > manifests/bootstrap.example.json
static:
	python3 scripts/local-checks.py
verify:
	python3 scripts/verify.py
demo:
	python3 scripts/canary.py --demo
canary:
	python3 scripts/canary.py
log-delivery:
	python3 scripts/log_delivery.py --run
operation-check:
	python3 scripts/operation_acceptance.py --run
acceptance:
	python3 scripts/acceptance.py
ci-acceptance:
	python3 scripts/acceptance.py --ci
ui:
	bash scripts/ui.sh
lock:
	python3 scripts/freeze-images.py
vendor:
	bash scripts/vendor.sh
validate-containers:
	bash scripts/validate-containers.sh
package:
	python3 scripts/package.py --repo-url "$(REPO_URL)" --surname "$(SURNAME)" --passport "$(or $(PASSPORT),docs/Паспорт.pdf)"
