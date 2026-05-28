include tags.env

LOCATION ?= us-central1
REPO ?= $(LOCATION)-docker.pkg.dev/$(shell gcloud config get core/project)/kube-agents

.PHONY: default docker-build docker-build-agents status prettier-check prettier-write

# Only match directories under agents/
AGENTS := $(filter-out shared,$(notdir $(patsubst %/,%,$(wildcard agents/*/))))


default: docker-build

# Docker builds
docker-build: docker-build-agents
docker-build-agents: $(foreach agent,$(AGENTS),docker-build-$(agent))

.PHONY: $(foreach agent,$(AGENTS),docker-build-$(agent))
$(foreach agent,$(AGENTS),docker-build-$(agent)): docker-build-%:
	docker build --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) -t $(REPO)/$*-agent:latest -f agents/$*/Dockerfile .

status:
	git status

prettier-check:
	npx prettier --check "**/*.md" "**/*.yaml" "**/*.yml"

prettier-write:
	npx prettier --write "**/*.md" "**/*.yaml" "**/*.yml"
