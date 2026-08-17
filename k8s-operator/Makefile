# VERSION is the release version injected into the binary and, by default, the image tag.
VERSION ?= latest
# Image URL to use all building/pushing image targets
IMG ?= ghcr.io/gke-labs/kube-agents/k8s-operator:$(VERSION)
# Unlike the Dockerfile's ldflags, -s -w is deliberately omitted here so locally
# built binaries keep their symbol tables for dlv debugging.
LDFLAGS ?= -ldflags "-X github.com/gke-labs/kube-agents/k8s-operator/internal/controller.DefaultPlatformAgentVersion=$(VERSION)"
# YEAR defines the year value used for substituting the YEAR placeholder in the boilerplate header.
YEAR ?= $(shell date +%Y)

# Get the currently used golang install path (in GOPATH/bin, unless GOBIN is set)
ifeq (,$(shell go env GOBIN))
GOBIN=$(shell go env GOPATH)/bin
else
GOBIN=$(shell go env GOBIN)
endif

# CONTAINER_TOOL defines the container tool to be used for building images.
CONTAINER_TOOL ?= docker

# Prefer an installed `prettier` over `npx prettier`, same as the root
# Makefile: npx re-resolves the package against the npm registry on every
# invocation, so behind an authenticated mirror it can fail even though
# prettier is installed and on PATH. CI installs the pinned version before
# running the manifests gate (see .github/workflows/k8s-operator-test.yml).
PRETTIER := $(shell command -v prettier 2>/dev/null || echo npx prettier)

# Setting SHELL to bash allows bash commands to be executed by recipes.
# Options are set to exit when a recipe line exits non-zero or a piped command fails.
SHELL = /usr/bin/env bash -o pipefail
.SHELLFLAGS = -ec

.PHONY: all
all: build ## Default target: build the manager binary.

##@ General

.PHONY: help
help: ## Display this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_0-9][a-zA-Z_0-9 -]*:.*##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Development

.PHONY: manifests
manifests: controller-gen ## Generate WebhookConfiguration, ClusterRole and CustomResourceDefinition objects.
	$(CONTROLLER_GEN) rbac:roleName=manager-role crd webhook paths="./..." output:crd:artifacts:config=config/crd/bases
	@$(PRETTIER) --write "config/crd/bases/*.yaml" "config/rbac/*.yaml" "config/webhook/*.yaml" \
		|| echo "WARNING: prettier did not run (not installed, or npm registry unreachable?). The regenerated YAML is left unformatted, which CI's prettier check will reject. Re-run prettier --write over config/ once prettier is available." >&2

.PHONY: generate
generate: controller-gen ## Generate code containing DeepCopy, DeepCopyInto, and DeepCopyObject method implementations.
	$(CONTROLLER_GEN) object:headerFile="hack/boilerplate.go.txt",year=$(YEAR) paths="./..."

.PHONY: fmt
fmt: ## Run go fmt against code.
	go fmt ./...

.PHONY: vet
vet: ## Run go vet against code.
	go vet ./...

.PHONY: test
test: manifests generate fmt vet setup-envtest test-python ## Run tests.
	KUBEBUILDER_ASSETS="$(shell $(ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir $(LOCALBIN) -p path)" go test $$(go list ./... | grep -v /e2e) -coverprofile cover.out

.PHONY: test-python
test-python: ## Run Python tests.
	cd internal/controller && python3 -m unittest test_leader_elect.py
	cd ../agents/platform/scripts && python3 -m unittest test_slack_relay_patch.py test_credential_proxy.py test_sitecustomize.py

##@ Build

.PHONY: build
build: manifests generate fmt vet ## Build manager binary.
	go build $(LDFLAGS) -o bin/manager cmd/main.go

.PHONY: run
run: manifests generate fmt vet ## Run a controller from your host.
	go run $(LDFLAGS) ./cmd/main.go

.PHONY: docker-build
docker-build: ## Build docker image with the manager.
	$(CONTAINER_TOOL) build --build-arg VERSION=$(VERSION) -t ${IMG} .

.PHONY: docker-push
docker-push: ## Push docker image with the manager.
	$(CONTAINER_TOOL) push ${IMG}

##@ Deployment

ifndef ignore-not-found
  ignore-not-found = false
endif

# Deployment targets apply the manifests as committed. They deliberately do NOT
# depend on `manifests`: regenerating during a deploy means applying something
# that may differ from what is in git, and it rewrites tracked files as a side
# effect of shipping. Run `make manifests` yourself after changing the API types;
# CI fails if the committed output is stale.

.PHONY: install
install: kustomize ## Install CRDs into the K8s cluster specified in ~/.kube/config.
	@out="$$( $(KUSTOMIZE) build config/crd 2>/dev/null || true )"; \
	if [ -n "$$out" ]; then echo "$$out" | kubectl apply --server-side -f -; else echo "No CRDs to install; skipping."; fi

.PHONY: uninstall
uninstall: kustomize ## Uninstall CRDs from the K8s cluster specified in ~/.kube/config. Call with ignore-not-found=true to ignore resource not found errors during deletion.
	@out="$$( $(KUSTOMIZE) build config/crd 2>/dev/null || true )"; \
	if [ -n "$$out" ]; then echo "$$out" | kubectl delete --ignore-not-found=$(ignore-not-found) -f -; else echo "No CRDs to delete; skipping."; fi

# The image is pinned in a throwaway copy of config/ so that `kustomize edit`
# does not rewrite the tracked config/manager/kustomization.yaml.
.PHONY: deploy
deploy: kustomize ## Deploy controller to the K8s cluster specified in ~/.kube/config.
	@tmp="$$(mktemp -d)"; trap 'rm -rf "$$tmp"' EXIT; \
	cp -R config "$$tmp/config"; \
	(cd "$$tmp/config/manager" && $(KUSTOMIZE) edit set image controller=${IMG}); \
	$(KUSTOMIZE) build "$$tmp/config/default" | kubectl apply --server-side -f -

.PHONY: undeploy
undeploy: kustomize ## Undeploy controller from the K8s cluster specified in ~/.kube/config. Call with ignore-not-found=true to ignore resource not found errors during deletion.
	$(KUSTOMIZE) build config/default | kubectl delete --ignore-not-found=$(ignore-not-found) -f -

LITELLM_VARS = $$NAMESPACE $$MODEL_PROVIDER $$MODEL_DEFAULT_NAME
LITELLM_VERTEX_VARS = $(LITELLM_VARS) $$PROJECT_ID $$LITELLM_KSA_NAME $$LITELLM_GSA_NAME $$VERTEX_PROJECT_ID $$VERTEX_LOCATION

.PHONY: deploy-litellm
deploy-litellm: kustomize ## Deploy LiteLLM integration.
	@if [ "$$MODEL_PROVIDER" = "chatgpt" ]; then \
		$(KUSTOMIZE) build config/integrations/litellm/overlays/chatgpt | envsubst '$(LITELLM_VARS)' | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -; \
	elif [ "$$MODEL_PROVIDER" = "vertex_ai" ]; then \
		$(KUSTOMIZE) build config/integrations/litellm/overlays/vertex_ai | envsubst '$(LITELLM_VERTEX_VARS)' | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -; \
	else \
		$(KUSTOMIZE) build config/integrations/litellm/base | envsubst '$(LITELLM_VARS)' | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -; \
	fi

.PHONY: undeploy-litellm
undeploy-litellm: kustomize ## Undeploy LiteLLM integration.
	@if [ "$$MODEL_PROVIDER" = "chatgpt" ]; then \
		$(KUSTOMIZE) build config/integrations/litellm/overlays/chatgpt | envsubst '$(LITELLM_VARS)' | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -; \
	elif [ "$$MODEL_PROVIDER" = "vertex_ai" ]; then \
		$(KUSTOMIZE) build config/integrations/litellm/overlays/vertex_ai | envsubst '$(LITELLM_VERTEX_VARS)' | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -; \
	else \
		$(KUSTOMIZE) build config/integrations/litellm/base | envsubst '$(LITELLM_VARS)' | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -; \
	fi

.PHONY: deploy-hindsight
deploy-hindsight: kustomize ## Deploy the Hindsight memory store.
	$(KUSTOMIZE) build config/integrations/hindsight | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -

.PHONY: undeploy-hindsight
undeploy-hindsight: kustomize ## Undeploy the Hindsight memory store (keeps its PVC).
	$(KUSTOMIZE) build config/integrations/hindsight | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -

.PHONY: deploy-inference-replay
deploy-inference-replay: kustomize ## Deploy Inference Replay proxy integration.
	$(KUSTOMIZE) build config/integrations/inference-replay/base | envsubst '$$NAMESPACE $$REPLAY_IMAGE' | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -

.PHONY: undeploy-inference-replay
undeploy-inference-replay: kustomize ## Undeploy Inference Replay proxy integration.
	$(KUSTOMIZE) build config/integrations/inference-replay/base | envsubst '$$NAMESPACE $$REPLAY_IMAGE' | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -

.PHONY: deploy-github
deploy-github: kustomize ## Deploy GitHub integration.
	$(KUSTOMIZE) build config/integrations/github | envsubst '$$NAMESPACE $$PROJECT_ID $$REGION $$CLUSTER_NAME $$KMS_KEYRING $$KMS_KEY $$KMS_KEY_VERSION $$KMS_LOCATION $$GITHUB_ORG $$GITHUB_REPO $$GITHUB_MINTER_KSA_NAME $$GITHUB_MINTER_GSA_NAME $$PLATFORM_AGENT_GSA_NAME' | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -

.PHONY: undeploy-github
undeploy-github: kustomize ## Undeploy GitHub integration.
	$(KUSTOMIZE) build config/integrations/github | envsubst '$$NAMESPACE $$PROJECT_ID $$REGION $$CLUSTER_NAME $$KMS_KEYRING $$KMS_KEY $$KMS_KEY_VERSION $$KMS_LOCATION $$GITHUB_ORG $$GITHUB_REPO $$GITHUB_MINTER_KSA_NAME $$GITHUB_MINTER_GSA_NAME $$PLATFORM_AGENT_GSA_NAME' | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -



##@ Dependencies

## Location to install dependencies to
LOCALBIN ?= $(shell pwd)/bin
$(LOCALBIN):
	mkdir -p "$(LOCALBIN)"

## Tool Binaries
KUSTOMIZE ?= $(LOCALBIN)/kustomize
CONTROLLER_GEN ?= $(LOCALBIN)/controller-gen
ENVTEST ?= $(LOCALBIN)/setup-envtest

## Tool Versions
KUSTOMIZE_VERSION ?= v5.4.2
CONTROLLER_TOOLS_VERSION ?= v0.20.1
ENVTEST_VERSION ?= release-0.19

# ENVTEST_K8S_VERSION is the version of Kubernetes to use for setting up ENVTEST binaries (i.e. 1.31)
ENVTEST_K8S_VERSION ?= 1.31.0

.PHONY: kustomize
kustomize: $(KUSTOMIZE) ## Download kustomize locally if necessary.
$(KUSTOMIZE): $(LOCALBIN)
	$(call go-install-tool,$(KUSTOMIZE),sigs.k8s.io/kustomize/kustomize/v5,$(KUSTOMIZE_VERSION))

.PHONY: controller-gen
controller-gen: $(CONTROLLER_GEN) ## Download controller-gen locally if necessary.
$(CONTROLLER_GEN): $(LOCALBIN)
	$(call go-install-tool,$(CONTROLLER_GEN),sigs.k8s.io/controller-tools/cmd/controller-gen,$(CONTROLLER_TOOLS_VERSION))

.PHONY: setup-envtest
setup-envtest: envtest ## Download the binaries required for ENVTEST in the local bin directory.
	@echo "Setting up envtest binaries for Kubernetes version $(ENVTEST_K8S_VERSION)..."
	@$(ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir "$(LOCALBIN)" -p path || { \
		echo "Error: Failed to set up envtest binaries for version $(ENVTEST_K8S_VERSION)."; \
		exit 1; \
	}

.PHONY: envtest
envtest: $(ENVTEST) ## Download setup-envtest locally if necessary.
$(ENVTEST): $(LOCALBIN)
	$(call go-install-tool,$(ENVTEST),sigs.k8s.io/controller-runtime/tools/setup-envtest,$(ENVTEST_VERSION))

# go-install-tool will 'go install' any package with custom target and name of binary, if it doesn't exist
# $1 - target path with name of binary
# $2 - package url which can be installed
# $3 - specific version of package
define go-install-tool
@[ -f "$(1)-$(3)" ] && [ "$$(readlink -- "$(1)" 2>/dev/null)" = "$(1)-$(3)" ] || { \
set -e; \
package=$(2)@$(3) ;\
echo "Downloading $${package}" ;\
rm -f "$(1)" ;\
GOBIN="$(LOCALBIN)" go install $${package} ;\
mv "$(LOCALBIN)/$$(basename "$(1)")" "$(1)-$(3)" ;\
} ;\
ln -sf "$$(realpath "$(1)-$(3)")" "$(1)"
endef

##@ Setup & Cleanup

.PHONY: provision gcp-provision
provision gcp-provision: ## Bootstrap GCP, GKE resources, and deploy PlatformAgent.
	@chmod +x scripts/*.sh scripts/dev/*.sh 2>/dev/null || true
	@./scripts/provision.sh $(ARGS)

.PHONY: teardown gcp-teardown
teardown gcp-teardown: ## Clean up and delete all provisioned GCP and GKE resources.
	@chmod +x scripts/*.sh scripts/dev/*.sh 2>/dev/null || true
	@./scripts/teardown.sh

.PHONY: gcp-provision-01-cluster
gcp-provision-01-cluster: ## Step 1: Provision GKE cluster and initial GCP environment.
	@chmod +x scripts/*.sh
	@./scripts/provision_01_gcp_cluster.sh $(ARGS)

.PHONY: gcp-provision-02-gvisor
gcp-provision-02-gvisor: ## Step 2: Provision gVisor node pool for GKE Sandbox.
	@chmod +x scripts/*.sh
	@./scripts/provision_02_gvisor_nodepool.sh $(ARGS)

.PHONY: gcp-provision-03-operator
gcp-provision-03-operator: ## Step 3: Install operator CRDs and deploy controller manager.
	@chmod +x scripts/*.sh
	@./scripts/provision_03_gcp_gke_operator.sh $(ARGS)

.PHONY: gcp-provision-04-iam
gcp-provision-04-iam: ## Step 4: Configure IAM service accounts and Workload Identity.
	@chmod +x scripts/*.sh
	@./scripts/provision_04_gcp_iam.sh $(ARGS)

.PHONY: gcp-provision-05-gchat
gcp-provision-05-gchat: ## Step 5: Setup Google Chat Pub/Sub topic and subscription.
	@chmod +x scripts/*.sh
	@./scripts/provision_05_gcp_gchat.sh $(ARGS)

.PHONY: gcp-provision-06-slack
gcp-provision-06-slack: ## Step 6: Setup Slack integration configuration.
	@chmod +x scripts/*.sh
	@./scripts/provision_06_slack.sh $(ARGS)

.PHONY: gcp-provision-07-secrets
gcp-provision-07-secrets: ## Step 7: Configure secrets directly in GKE.
	@chmod +x scripts/*.sh
	@./scripts/provision_07_gcp_k8s_secrets.sh $(ARGS)

.PHONY: gcp-provision-08-deploy
gcp-provision-08-deploy: ## Step 8: Deploy the PlatformAgent Custom Resource.
	@chmod +x scripts/*.sh
	@./scripts/provision_08_deploy_platform_agent.sh $(ARGS)

.PHONY: gcp-provision-09-litellm
gcp-provision-09-litellm: ## Step 9: Deploy LiteLLM Gateway.
	@chmod +x scripts/*.sh
	@./scripts/provision_09_deploy_litellm.sh $(ARGS)

.PHONY: gcp-provision-10-github
gcp-provision-10-github: ## Step 10: Deploy GitHub Token Minter.
	@chmod +x scripts/*.sh
	@./scripts/provision_10_deploy_github_minter.sh $(ARGS)

.PHONY: gcp-provision-11-inference-replay
gcp-provision-11-inference-replay: ## Step 11: Deploy Inference Replay proxy.
	@chmod +x scripts/*.sh
	@./scripts/provision_11_deploy_inference_replay.sh $(ARGS)

.PHONY: gcp-provision-12-gke-backup-plan
gcp-provision-12-gke-backup-plan: ## Step 12: Provision GKE Backup Plan for disaster recovery.
	@chmod +x scripts/*.sh
	@./scripts/provision_12_gke_backup_plan.sh $(ARGS)

.PHONY: gcp-provision-13-hindsight
gcp-provision-13-hindsight: ## Step 13: Deploy Hindsight memory store.
	@chmod +x scripts/*.sh
	@./scripts/provision_13_deploy_hindsight.sh $(ARGS)

.PHONY: gcp-provision-14-verify-rollout
gcp-provision-14-verify-rollout: ## Step 14: Verify the PlatformAgent Deployment rollout.
	@chmod +x scripts/*.sh
	@./scripts/provision_14_verify_agent_rollout.sh $(ARGS)

.PHONY: dev-rebuild-agent
dev-rebuild-agent: ## Fast local iteration: rebuild and redeploy an agent image (e.g. make dev-rebuild-agent ARGS="platform").
	@chmod +x scripts/*.sh scripts/dev/*.sh 2>/dev/null || true
	@./scripts/dev/dev_rebuild_agent.sh $(ARGS)

.PHONY: gcp-teardown-13-hindsight
gcp-teardown-13-hindsight: ## Teardown Step 13: Undeploy Hindsight memory store.
	@chmod +x scripts/*.sh
	@./scripts/teardown_13_deploy_hindsight.sh $(ARGS)

.PHONY: gcp-teardown-12-gke-backup-plan
gcp-teardown-12-gke-backup-plan: ## Teardown Step 12: Delete GKE Backup Plan and snapshots.
	@chmod +x scripts/*.sh
	@./scripts/teardown_12_gke_backup_plan.sh $(ARGS)

.PHONY: gcp-teardown-11-inference-replay
gcp-teardown-11-inference-replay: ## Teardown Step 11: Undeploy Inference Replay proxy.
	@chmod +x scripts/*.sh
	@./scripts/teardown_11_deploy_inference_replay.sh $(ARGS)

.PHONY: gcp-teardown-10-github
gcp-teardown-10-github: ## Teardown Step 10: Delete GitHub Token Minter resources.
	@chmod +x scripts/*.sh
	@./scripts/teardown_10_deploy_github_minter.sh $(ARGS)

.PHONY: gcp-teardown-09-litellm
gcp-teardown-09-litellm: ## Teardown Step 9: Undeploy LiteLLM Gateway.
	@chmod +x scripts/*.sh
	@./scripts/teardown_09_deploy_litellm.sh $(ARGS)

.PHONY: gcp-teardown-08-deploy
gcp-teardown-08-deploy: ## Teardown Step 8: Delete the PlatformAgent Custom Resource.
	@chmod +x scripts/*.sh
	@./scripts/teardown_08_deploy_platform_agent.sh $(ARGS)

.PHONY: gcp-teardown-07-secrets
gcp-teardown-07-secrets: ## Teardown Step 7: Clean up Kubernetes secrets.
	@chmod +x scripts/*.sh
	@./scripts/teardown_07_gcp_k8s_secrets.sh $(ARGS)

.PHONY: gcp-teardown-06-slack
gcp-teardown-06-slack: ## Teardown Step 6: Reset Slack integration settings.
	@chmod +x scripts/*.sh
	@./scripts/teardown_06_slack.sh $(ARGS)

.PHONY: gcp-teardown-05-gchat
gcp-teardown-05-gchat: ## Teardown Step 5: Delete Google Chat Pub/Sub resources.
	@chmod +x scripts/*.sh
	@./scripts/teardown_05_gcp_gchat.sh $(ARGS)

.PHONY: gcp-teardown-04-iam
gcp-teardown-04-iam: ## Teardown Step 4: Remove IAM service accounts and policies.
	@chmod +x scripts/*.sh
	@./scripts/teardown_04_gcp_iam.sh $(ARGS)

.PHONY: gcp-teardown-03-operator
gcp-teardown-03-operator: ## Teardown Step 3: Undeploy the operator and CRDs.
	@chmod +x scripts/*.sh
	@./scripts/teardown_03_gcp_gke_operator.sh $(ARGS)

.PHONY: gcp-teardown-02-gvisor
gcp-teardown-02-gvisor: ## Teardown Step 2: Delete gVisor node pool without destroying cluster.
	@chmod +x scripts/*.sh
	@./scripts/teardown_02_gvisor_nodepool.sh $(ARGS)

.PHONY: gcp-teardown-dev-artifact-registry
gcp-teardown-dev-artifact-registry: ## Teardown Dev Step: Delete the Artifact Registry repository created during local rebuilds.
	@chmod +x scripts/*.sh scripts/dev/*.sh 2>/dev/null || true
	@./scripts/dev/teardown_dev_01_gcp_artifact_registry.sh $(ARGS)

.PHONY: gcp-teardown-01-cluster
gcp-teardown-01-cluster: ## Teardown Step 1: Delete GKE cluster and local configuration state.
	@chmod +x scripts/*.sh
	@./scripts/teardown_01_gcp_cluster.sh $(ARGS)

##@ Formatting

prettier-check: ## Check Markdown/YAML formatting.
	$(PRETTIER) --check "**/*.{md,yaml,yml}"

prettier-write: ## Apply Markdown/YAML formatting.
	$(PRETTIER) --write "**/*.{md,yaml,yml}"
