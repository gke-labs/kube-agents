include tags.env

LOCATION ?= us-central1
REPO ?= $(eval REPO := $(LOCATION)-docker.pkg.dev/$(shell gcloud config get core/project)/kube-agents)$(REPO)

BAD_SKILLS := $(wildcard agents/*/defaults/skills/*)

.PHONY: default docker-build docker-build-agents docker-build-credential-proxy docker-push docker-push-agents docker-push-credential-proxy dev-rebuild-agent status prettier-check prettier-write test-python validate docs-generate docs-check docs-check-generated docs-check-links docs-check-terminology docs-check-map

AGENTS := $(notdir $(patsubst %/,%,$(wildcard agents/*/)))


default: docker-build

# Docker builds
docker-build: docker-build-agents docker-build-credential-proxy
docker-build-agents: $(foreach agent,$(AGENTS),docker-build-$(agent))

.PHONY: $(foreach agent,$(AGENTS),docker-build-$(agent))
$(foreach agent,$(AGENTS),docker-build-$(agent)): docker-build-%:
	docker build --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) --target $* -t $(REPO)/$*-agent:latest -f deploy/docker/Dockerfile .

docker-build-credential-proxy:
	docker build --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) --target credential-proxy -t $(REPO)/credential-proxy:latest -f deploy/docker/Dockerfile .

# Docker pushes
docker-push: docker-push-agents docker-push-credential-proxy
docker-push-agents: $(foreach agent,$(AGENTS),docker-push-$(agent))

.PHONY: $(foreach agent,$(AGENTS),docker-push-$(agent))
$(foreach agent,$(AGENTS),docker-push-$(agent)): docker-push-%: docker-build-%
	docker push $(REPO)/$*-agent:latest

docker-push-credential-proxy: docker-build-credential-proxy
	docker push $(REPO)/credential-proxy:latest

dev-rebuild-agent: ## Fast local iteration: rebuild and redeploy an agent image (e.g. make dev-rebuild-agent ARGS="platform").
	@$(MAKE) -C k8s-operator dev-rebuild-agent ARGS="$(ARGS)"


status:
	git status

prettier-check:
	npx prettier --check "**/*.md" "**/*.yaml" "**/*.yml"

prettier-write:
	npx prettier --write "**/*.md" "**/*.yaml" "**/*.yml"

# Unit tests for every Python helper outside k8s-operator/, which has its own
# target. Mostly stdlib-only -- the skill helpers shell out to gh/kubectl
# rather than importing SDKs -- but the agent scripts do import a few third
# party packages; CI installs those (.github/workflows/python-tests.yml) and
# a local run needs them on the path too.
#
# The wildcards are what keep this honest: a new skill's tests are picked up
# without editing this file. Five globs rather than one because the tests do
# not all live under skills -- the agent scripts the skills share, the Chat
# Agent plugins, the image patches and the repository's own tooling in
# scripts/ each hold their own. That last one is here because it was not: the
# tests for the upstream-skill sync sat in scripts/ outside every glob, so
# they had never once run in CI. Discovery is then run once per directory
# rather than once over the tree, because none of them are packages --
# `unittest discover` pointed at agents/platform/skills finds nothing and
# still exits 0, which reads as a passing suite.
PYTHON_TEST_DIRS := $(sort $(dir \
	$(wildcard agents/*/skills/*/scripts/test_*.py) \
	$(wildcard agents/*/scripts/test_*.py) \
	$(wildcard agents/*/defaults/plugins/*/test_*.py) \
	$(wildcard deploy/docker/patches/test_*.py) \
	$(wildcard scripts/test_*.py)))

test-python:
	@if [ -z "$(PYTHON_TEST_DIRS)" ]; then \
		echo "Error: no test_*.py files found under agents/, deploy/docker/patches or scripts/."; \
		echo "Either the tests moved or the globs are stale -- failing rather than reporting success."; \
		exit 1; \
	fi
	@set -e; for dir in $(PYTHON_TEST_DIRS); do \
		echo "==> $$dir"; \
		(cd $$dir && python3 -m unittest discover -p "test_*.py"); \
	done

# Documentation tables that mirror a machine-readable source (cron jobs, the
# skill catalogue, the provisioning steps) are generated rather than hand-kept.
docs-generate:
	@python3 scripts/generate_docs.py

# Everything CI enforces about the docs, in one command.
docs-check: docs-check-generated docs-check-links docs-check-terminology docs-check-map

docs-check-generated:
	@python3 scripts/generate_docs.py --check

docs-check-links:
	@python3 scripts/check_docs_links.py

docs-check-terminology:
	@./hack/check-docs-terminology.sh

docs-check-map:
	@python3 scripts/check_docs_map.py

validate:
	@if [ -n "$(BAD_SKILLS)" ]; then \
		echo "Error: Skills should not be placed under agents/*/defaults/skills. Move them to agents/*/skills/"; \
		set -- $(BAD_SKILLS); \
		for file; do echo "  $$file"; done; \
		exit 1; \
	else \
		echo "Validation passed: No skills found in invalid paths."; \
	fi



