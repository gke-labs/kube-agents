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
# Discovery, not a hand-maintained list. The list version was stale: it named
# three files and missed test_scoped_sa_pool.py and test_session_kv_server.py,
# so tests that existed and passed locally were never run by this target.
test-python: ## Run Python tests.
	cd internal/controller && python3 -m unittest discover -s . -p 'test_*.py'
	cd ../agents/platform/scripts && python3 -m unittest discover -s . -p 'test_*.py'

##@ Build

.PHONY: build
build: manifests generate fmt vet ## Build manager binary.
	go build $(LDFLAGS) -o bin/manager cmd/main.go

.PHONY: run
run: manifests generate fmt vet ## Run a controller from your host.
	go run $(LDFLAGS) ./cmd/main.go

# Base-image overrides for rebuilding where the public registries are
# unreachable; unset ones are not passed, so an ordinary build is unchanged.
# The root Makefile carries the same mechanism for the agent images.
BASE_IMAGE_VARS := GOLANG_IMAGE DISTROLESS_IMAGE
BASE_IMAGE_ARGS := $(foreach v,$(BASE_IMAGE_VARS),$(if $($(v)),--build-arg $(v)=$($(v))))

.PHONY: docker-build
docker-build: ## Build docker image with the manager.
	$(CONTAINER_TOOL) build $(BASE_IMAGE_ARGS) --build-arg VERSION=$(VERSION) -t ${IMG} .

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

# Tags that float. A controller deployed from one is upgraded on its next pod
# reschedule while the RBAC and CRDs applied beside it stay where they were,
# and the first verb the newer controller needs that the older ClusterRole
# lacks becomes an unbounded `Reconciler error` loop (#1009). The supported
# installers go further (validate_immutable_ref in install.sh allows only a
# 40-hex commit SHA or a numeric SemVer tag); this is a denylist of the tags
# that are known to float, deliberately looser so the short SHAs and dev tags
# a local build uses still deploy, with an escape hatch for a cluster that will
# be thrown away before the next reschedule.
MUTABLE_IMAGE_TAGS := latest main master HEAD dev
ALLOW_MUTABLE_IMG ?=

.PHONY: check-img
check-img: ## Refuse a mutable image tag in IMG for deploy (ALLOW_MUTABLE_IMG=1 overrides).
	@ref='$(IMG)'; \
	case "$$ref" in *@sha256:*) exit 0 ;; esac; \
	tag="$${ref##*:}"; \
	case "$$tag" in "$$ref"|*/*) tag="" ;; esac; \
	mutable=""; \
	if [ -z "$$tag" ]; then mutable="an untagged reference"; fi; \
	for t in $(MUTABLE_IMAGE_TAGS); do [ "$$tag" = "$$t" ] && mutable="the floating tag ':$$t'"; done; \
	if [ -n "$$mutable" ] && [ "$(ALLOW_MUTABLE_IMG)" != "1" ]; then \
	  echo "ERROR: IMG=$$ref is $$mutable. A controller deployed from it is silently upgraded on its next" >&2; \
	  echo "       pod reschedule while the ClusterRole and CRDs applied with it stay put, and the newer" >&2; \
	  echo "       controller then fails every reconcile with 'forbidden' (see issue #1009)." >&2; \
	  echo "       Deploy an immutable ref instead, e.g. IMG=<registry>/k8s-operator:\$$(git rev-parse HEAD)," >&2; \
	  echo "       or set ALLOW_MUTABLE_IMG=1 for a cluster you will throw away before then." >&2; \
	  exit 1; \
	fi

# The image is pinned in a throwaway copy of config/ so that `kustomize edit`
# does not rewrite the tracked config/manager/kustomization.yaml.
.PHONY: deploy
deploy: kustomize check-img ## Deploy controller to the K8s cluster specified in ~/.kube/config.
	@tmp="$$(mktemp -d)"; trap 'rm -rf "$$tmp"' EXIT; \
	cp -R config "$$tmp/config"; \
	(cd "$$tmp/config/manager" && $(KUSTOMIZE) edit set image controller=${IMG}); \
	$(KUSTOMIZE) build "$$tmp/config/default" | kubectl apply --server-side -f -

.PHONY: undeploy
undeploy: kustomize ## Undeploy controller from the K8s cluster specified in ~/.kube/config. Call with ignore-not-found=true to ignore resource not found errors during deletion.
	$(KUSTOMIZE) build config/default | kubectl delete --ignore-not-found=$(ignore-not-found) -f -

# The integration manifests carry `image: $${..._IMAGE}` so a mirrored install
# can point them at its own registry. envsubst leaves an unset variable as the
# empty string rather than failing, which renders `image:` — a null the API
# server rejects with a message that says nothing about the missing variable.
# Check first, and name where the value comes from.
# $(2) says where the value lives, because the integrations do not agree: the
# third-party pins are in images.json, while the replay proxy is built here
# and follows REGISTRY_PREFIX.
require-var = @test -n "$${$(1)}" || { echo "$(1) is not set. Export it; the reference lives in $(2)." >&2; exit 1; }

# The third-party images default to their upstream pin straight from the
# inventory, so the documented manual sequences (INSTALL.md Step 5, README
# below) keep working without exporting anything. `?=` means an export still
# wins for a mirrored deploy that sets its own references. Resolving here
# rather than
# hardcoding keeps images.json the only place any of these pins is written; if
# jq is missing or the entry is gone the variable stays empty and require-var
# reports it by name.
IMAGES_JSON ?= $(CURDIR)/../images.json
image_from_inventory = $(shell jq -r --arg n "$(1)" '.images[] | select(.name == $$n) | "\(.repository):\(.tag)"' "$(IMAGES_JSON)" 2>/dev/null)
LITELLM_IMAGE ?= $(call image_from_inventory,litellm)
GITHUB_MINTER_IMAGE ?= $(call image_from_inventory,github-token-minter-server)
HINDSIGHT_API_IMAGE ?= $(call image_from_inventory,hindsight-api)
HINDSIGHT_POSTGRES_IMAGE ?= $(call image_from_inventory,hindsight-postgresql)
# The recipes read these through the shell (envsubst and require-var both want
# them in the environment), which a plain make variable would not reach.
export LITELLM_IMAGE
export GITHUB_MINTER_IMAGE
export HINDSIGHT_API_IMAGE
export HINDSIGHT_POSTGRES_IMAGE

LITELLM_VARS = $$NAMESPACE $$MODEL_PROVIDER $$MODEL_DEFAULT_NAME $$LITELLM_IMAGE
LITELLM_VERTEX_VARS = $(LITELLM_VARS) $$PROJECT_ID $$LITELLM_KSA_NAME $$LITELLM_GSA_NAME $$VERTEX_PROJECT_ID $$VERTEX_LOCATION
# Hindsight's manifests need no other substitution: the namespace comes from
# `kubectl apply -n`, and every in-cluster address is a short service name.
HINDSIGHT_VARS = $$HINDSIGHT_API_IMAGE $$HINDSIGHT_POSTGRES_IMAGE

.PHONY: deploy-litellm
deploy-litellm: kustomize ## Deploy LiteLLM integration.
	$(call require-var,LITELLM_IMAGE,images.json)
	@if [ "$$MODEL_PROVIDER" = "vertex_ai" ]; then \
		$(KUSTOMIZE) build config/integrations/litellm/overlays/vertex_ai | envsubst '$(LITELLM_VERTEX_VARS)' | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -; \
	else \
		$(KUSTOMIZE) build config/integrations/litellm/base | envsubst '$(LITELLM_VARS)' | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -; \
	fi

.PHONY: undeploy-litellm
undeploy-litellm: kustomize ## Undeploy LiteLLM integration.
	@if [ "$$MODEL_PROVIDER" = "vertex_ai" ]; then \
		$(KUSTOMIZE) build config/integrations/litellm/overlays/vertex_ai | envsubst '$(LITELLM_VERTEX_VARS)' | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -; \
	else \
		$(KUSTOMIZE) build config/integrations/litellm/base | envsubst '$(LITELLM_VARS)' | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -; \
	fi

.PHONY: deploy-hindsight
deploy-hindsight: kustomize ## Deploy the Hindsight memory store.
	$(call require-var,HINDSIGHT_API_IMAGE,images.json)
	$(call require-var,HINDSIGHT_POSTGRES_IMAGE,images.json)
	$(KUSTOMIZE) build config/integrations/hindsight | envsubst '$(HINDSIGHT_VARS)' | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -

.PHONY: undeploy-hindsight
undeploy-hindsight: kustomize ## Undeploy the Hindsight memory store (keeps its PVC).
	$(KUSTOMIZE) build config/integrations/hindsight | envsubst '$(HINDSIGHT_VARS)' | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -

.PHONY: deploy-inference-replay
deploy-inference-replay: kustomize ## Deploy Inference Replay proxy integration.
	$(call require-var,REPLAY_IMAGE,REGISTRY_PREFIX)
	$(KUSTOMIZE) build config/integrations/inference-replay/base | envsubst '$$NAMESPACE $$REPLAY_IMAGE' | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -

.PHONY: undeploy-inference-replay
undeploy-inference-replay: kustomize ## Undeploy Inference Replay proxy integration.
	$(KUSTOMIZE) build config/integrations/inference-replay/base | envsubst '$$NAMESPACE $$REPLAY_IMAGE' | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -

.PHONY: deploy-github
deploy-github: kustomize ## Deploy GitHub integration.
	$(call require-var,GITHUB_MINTER_IMAGE,images.json)
	$(KUSTOMIZE) build config/integrations/github | envsubst '$$NAMESPACE $$PROJECT_ID $$REGION $$CLUSTER_NAME $$KMS_KEYRING $$KMS_KEY $$KMS_KEY_VERSION $$KMS_LOCATION $$GITHUB_ORG $$GITHUB_REPO $$GITHUB_MINTER_KSA_NAME $$GITHUB_MINTER_GSA_NAME $$PLATFORM_AGENT_GSA_NAME $$GITHUB_MINTER_IMAGE' | kubectl apply -n $${NAMESPACE:-kubeagents-system} -f -

.PHONY: undeploy-github
undeploy-github: kustomize ## Undeploy GitHub integration.
	$(KUSTOMIZE) build config/integrations/github | envsubst '$$NAMESPACE $$PROJECT_ID $$REGION $$CLUSTER_NAME $$KMS_KEYRING $$KMS_KEY $$KMS_KEY_VERSION $$KMS_LOCATION $$GITHUB_ORG $$GITHUB_MINTER_KSA_NAME $$GITHUB_MINTER_GSA_NAME $$PLATFORM_AGENT_GSA_NAME $$GITHUB_MINTER_IMAGE' | kubectl delete --ignore-not-found=$(ignore-not-found) -n $${NAMESPACE:-kubeagents-system} -f -



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
ENVTEST_VERSION ?= release-0.24

# ENVTEST_K8S_VERSION is the version of Kubernetes to use for setting up ENVTEST binaries (i.e. 1.36)
ENVTEST_K8S_VERSION ?= 1.36.0

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

.PHONY: dev-rebuild-agent
# The script moved to the repository root's scripts/dev/, so the root Makefile
# owns this target now and this one delegates up. Kept because the target has
# always been reachable from inside k8s-operator/.
dev-rebuild-agent: ## Fast local iteration: rebuild and redeploy an agent image (e.g. make dev-rebuild-agent ARGS="platform").
	@$(MAKE) -C .. dev-rebuild-agent ARGS="$(ARGS)"

##@ Formatting

prettier-check: ## Check Markdown/YAML formatting.
	$(PRETTIER) --check "**/*.{md,yaml,yml}"

prettier-write: ## Apply Markdown/YAML formatting.
	$(PRETTIER) --write "**/*.{md,yaml,yml}"
