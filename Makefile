LOCATION ?= us-central1
REPO ?= $(LOCATION)-docker.pkg.dev/$(shell gcloud config get core/project)/kube-agents

.PHONY: default docker-build docker-build-agents status

# Only match directories under agents/
AGENTS := $(notdir $(patsubst %/,%,$(wildcard agents/*/)))

default: docker-build

# Docker builds
docker-build: docker-build-agents
docker-build-agents: $(foreach agent,$(AGENTS),docker-build-$(agent))

.PHONY: $(foreach agent,$(AGENTS),docker-build-$(agent))
$(foreach agent,$(AGENTS),docker-build-$(agent)): docker-build-%:
	docker build -t $(REPO)/$*-agent:latest -f agents/$*/Dockerfile .

status:
	git status
