include tags.env

LOCATION ?= us-central1
REPO ?= $(eval REPO := $(LOCATION)-docker.pkg.dev/$(shell gcloud config get core/project)/kube-agents)$(REPO)

BAD_SKILLS := $(wildcard agents/*/defaults/skills/*)

# Base-image overrides for rebuilding where the public registries are
# unreachable. Each names a full mirrored reference without a tag (the tags
# stay pinned in the Dockerfile and images.json); unset ones are simply not
# passed, so an ordinary build is unchanged:
#   make docker-build HERMES_AGENT_IMAGE=registry.example.com/mirror/hermes-agent
BASE_IMAGE_VARS := HERMES_AGENT_IMAGE ENVOY_IMAGE GOLANG_IMAGE
BASE_IMAGE_ARGS := $(foreach v,$(BASE_IMAGE_VARS),$(if $($(v)),--build-arg $(v)=$($(v))))

# The sandbox image is built from a different Dockerfile and shares none of
# those bases, so it takes its own list. Passing the union to both would make
# every build warn about build args the Dockerfile never declares.
SANDBOX_IMAGE_VARS := PYTHON_IMAGE
SANDBOX_IMAGE_ARGS := $(foreach v,$(SANDBOX_IMAGE_VARS),$(if $($(v)),--build-arg $(v)=$($(v))))

.PHONY: default help docker-build docker-build-agents docker-build-credential-proxy docker-build-sandbox docker-smoke-sandbox docker-push docker-push-agents docker-push-credential-proxy docker-push-sandbox dev-rebuild-agent mirror-images images-check status prettier-check prettier-write test-python test-python-deps test-bench test-bench-deps bench-case-check e2e-tests e2e-test-deps test-e2e test-e2e-deps validate prompt-check docs-generate docs-check docs-check-generated docs-check-links docs-check-terminology docs-check-map docs-check-context-budget chart-sync chart-check tf-apply tf-destroy coverage coverage-check test-integration

# The agent images this repository builds -- one per `--target` stage in
# deploy/docker/Dockerfile, which is not the same thing as one per directory
# under agents/. This was `$(wildcard agents/*/)`, and every `make` at the
# repository root failed on the first stage it invented:
# `target stage "chat" could not be found`. There is no chat or cluster image.
# agents/chat/ is baked into this image as /opt/defaults (it is the `default`
# profile) and agents/cluster/ as /opt/cluster-template, which the Platform
# Agent scaffolds per cluster at runtime. Adding a genuinely new image means
# adding a Dockerfile stage, so name them here rather than guessing.
AGENTS := platform


default: docker-build

help: ## Display this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\n"} /^[a-zA-Z_0-9][a-zA-Z_0-9 -]*:.*##/ { printf "  \033[36m%-28s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# Docker builds
docker-build: docker-build-agents docker-build-credential-proxy docker-build-sandbox ## Build every image this repository ships (the default target).
docker-build-agents: $(foreach agent,$(AGENTS),docker-build-$(agent)) ## Build the agent images (see the AGENTS variable).

.PHONY: $(foreach agent,$(AGENTS),docker-build-$(agent))
# --platform linux/amd64 everywhere the agent images build: deployment targets
# are always amd64 GKE nodes, and the multi-arch bases (hermes-agent, envoy)
# otherwise resolve to the build host — an arm64 machine would silently produce
# an image that crashloops on the cluster (#560).
$(foreach agent,$(AGENTS),docker-build-$(agent)): docker-build-%:
	docker build --platform linux/amd64 $(BASE_IMAGE_ARGS) --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) --target $* -t $(REPO)/$*-agent:latest -f deploy/docker/Dockerfile .

docker-build-credential-proxy: ## Build the credential-proxy sidecar image.
	docker build --platform linux/amd64 $(BASE_IMAGE_ARGS) --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) --target credential-proxy -t $(REPO)/credential-proxy:latest -f deploy/docker/Dockerfile .

# Context is the repository root, not deploy/sandbox: the image ships the same
# credential-proxy client and PATH script the agent image does, and copying
# them into deploy/sandbox to narrow the context is how the two drift apart.
docker-build-sandbox: ## Build the agent shell sandbox image.
	docker build --platform linux/amd64 $(SANDBOX_IMAGE_ARGS) -t $(REPO)/agent-sandbox:latest -f deploy/sandbox/Dockerfile .

# Not folded into docker-build-sandbox: it runs containers and binds a port,
# which a plain build should not do. The script explains why the checks it makes
# cannot be made statically.
docker-smoke-sandbox: docker-build-sandbox ## Build the sandbox image and exercise it over ssh.
	deploy/sandbox/smoke-test.sh $(REPO)/agent-sandbox:latest

# Docker pushes
docker-push: docker-push-agents docker-push-credential-proxy docker-push-sandbox ## Build and push every image to $$REPO.
docker-push-agents: $(foreach agent,$(AGENTS),docker-push-$(agent)) ## Build and push the agent images.

.PHONY: $(foreach agent,$(AGENTS),docker-push-$(agent))
$(foreach agent,$(AGENTS),docker-push-$(agent)): docker-push-%: docker-build-%
	docker push $(REPO)/$*-agent:latest

docker-push-credential-proxy: docker-build-credential-proxy ## Build and push the credential-proxy image.
	docker push $(REPO)/credential-proxy:latest

docker-push-sandbox: docker-build-sandbox ## Build and push the agent shell sandbox image.
	docker push $(REPO)/agent-sandbox:latest

dev-rebuild-agent: ## Fast local iteration: rebuild and redeploy an agent image (e.g. make dev-rebuild-agent ARGS="platform").
	@chmod +x scripts/installer/*.sh scripts/dev/*.sh 2>/dev/null || true
	@./scripts/dev/dev_rebuild_agent.sh $(ARGS)

# Copy every image in images.json into a registry of your own, for installs
# that may only pull from an approved one. Run `./scripts/mirror_images.sh
# --help` for the full set of knobs.
mirror-images: ## Mirror the images in images.json into MIRROR_PREFIX (e.g. make mirror-images MIRROR_PREFIX=registry.example.com/kube-agents).
	@./scripts/mirror_images.sh $(ARGS)

images-check: ## Verify images.json still matches every pin it mirrors, and that the chart renders nothing off a public registry when mirrored (CI runs this).
	@./hack/check-image-inventory.sh


status: ## Show the working tree status.
	git status

# Prefer an installed `prettier` over `npx prettier`, falling back to npx where
# there is none (CI installs a pinned version first). npx re-resolves the
# package against the npm registry on every invocation, so on a machine whose
# registry is an authenticated mirror these targets failed with an auth error
# even though prettier was installed and on PATH -- which is how the
# formatting check came to be skipped by hand rather than run.
#
# Install the version CI pins (see the Install Prettier step in
# .github/workflows/prettier.yml), e.g. `npm install -g prettier@<that
# version>`. The k8s-operator manifests gate asserts byte-equality against
# that version's output, so a version skew shows up as a check that passes
# locally and fails in CI, or the reverse.
PRETTIER := $(shell command -v prettier 2>/dev/null || echo npx prettier)

prettier-check: ## Check Markdown/YAML formatting (CI runs this).
	$(PRETTIER) --check "**/*.md" "**/*.yaml" "**/*.yml"

prettier-write: ## Reformat all Markdown/YAML in place.
	$(PRETTIER) --write "**/*.md" "**/*.yaml" "**/*.yml"

# Unit tests for every Python helper outside k8s-operator/, which has its own
# target. Mostly stdlib-only -- the skill helpers shell out to gh/kubectl
# rather than importing SDKs -- but the agent scripts do import a few third
# party packages, listed in requirements-test.txt and installed by
# `make test-python-deps`. CI installs the same file.
#
# The wildcards are what keep this honest: a new skill's tests are picked up
# without editing this file. Thirteen globs rather than one because the tests do
# not all live under skills -- the admin console, the shared agent scripts,
# Chat Agent plugins and hooks, image patches, image build and repository
# tooling in scripts/ each hold their own. scripts/ is here
# because it was not: the tests for the upstream-skill sync sat outside every
# glob, so they had never once run in CI. defaults/hooks is here for the same
# reason -- the plugins glob does not reach it, so the chat_message_audit hook
# was untestable-by-CI however many tests it grew. Discovery is then run once
# per directory rather than once over the tree, because most of them are not
# packages (incident_context is the exception, and per-directory discovery
# still collects it) -- `unittest discover` pointed at agents/platform/skills finds
# nothing and still exits 0, which reads as a passing suite. That also keeps
# deploy/docker, deploy/docker/patches and each deploy/docker/plugins/<name>
# separate, which they must be: those tests import their subject by bare module
# name, which only resolves with their own directory as the discovery root.
#
# tests/integration is the newest entry and the only one that is not a unit
# suite. It ran alone in its own CI job through a probation period, so that a
# flake in a young seam test could not red an already-gating job; it finished
# that period without a single failure, and a tier nothing gates on is a tier
# people learn to merge around. It is deterministic by construction -- real
# components, no model calls -- so it belongs in the sweep the `test` job runs
# rather than beside it. The one thing that costs: the injector seam shells out
# to `go test`, so every job that expands this list needs a Go toolchain on
# PATH or those tests skip themselves and the sweep reports green without them.
PYTHON_TEST_DIRS := $(sort $(dir \
	$(wildcard admin_console/tests/test_*.py) \
	$(wildcard agents/*/skills/*/scripts/test_*.py) \
	$(wildcard agents/*/scripts/test_*.py) \
	$(wildcard agents/*/defaults/plugins/*/test_*.py) \
	$(wildcard agents/*/plugins/*/test_*.py) \
	$(wildcard agents/*/defaults/hooks/*/test_*.py) \
	$(wildcard agentplugins/*/tests/test_*.py) \
	$(wildcard agentplugins/lib/tests/test_*.py) \
	$(wildcard deploy/docker/test_*.py) \
	$(wildcard deploy/docker/patches/test_*.py) \
	$(wildcard deploy/docker/plugins/*/test_*.py) \
	$(wildcard scripts/test_*.py) \
	$(wildcard tests/integration/test_*.py) \
	$(wildcard tests/test_*.py) \
	$(wildcard tests/memory/test_*.py)))

# What both callers of the sweep below -- test-python and coverage -- export as
# PYTHONPATH, prepended to whatever the caller already has. Declared once
# because the two had already drifted: coverage exported only $(CURDIR), so it
# resolved imports differently from the suite it claims to mirror. Nothing
# depended on the difference yet, which is exactly why it went unnoticed -- the
# coverage target tolerates a failing directory, so a test that needed the
# missing entries would have been swallowed by the "failing test directories"
# note. Now that the same run produces the required verdict, that drift would
# read as a test that passes locally under `make test-python` and fails in CI.
#
# Sharing the sweep made the discovery structural; this makes the environment it
# runs in structural too, which is the other half of measuring the same suite.
PYTHON_TEST_PATH := $(CURDIR):$(CURDIR)/agentplugins/lib:$(CURDIR)/agentplugins/pubsub-platform

# How many of those directories `test-python` and `coverage` run at once. They
# are separate `python3` processes that share nothing -- each cd's into its own
# directory, servers in the seam tier bind port 0, and fixtures go through
# tempfile -- so the sweep costs its slowest single directory rather than the
# sum of all of them. Four directories are most of that sum, so the win is
# large and then flat: raising this past a handful buys nothing.
#
# Set it to 1 to serialise. That is for reproducing a failure you suspect is
# concurrency's doing, or for a machine you need the cores back on -- not for
# readability, since the sweep captures each directory's output either way:
#   make test-python PYTHON_TEST_JOBS=1
PYTHON_TEST_JOBS ?= $(shell nproc 2>/dev/null || echo 4)

# The sweep over PYTHON_TEST_DIRS that `test-python` and `coverage` both run.
# $(1) is the command executed inside each directory, and it is the only thing
# the two callers differ by.
#
# One macro rather than two loops because that mirroring is load-bearing and
# used to be only asserted in a comment: a coverage target that walks the
# directories differently measures a different suite than the one that gates,
# and nothing would say so. Sharing the sweep makes it structural.
#
# Contract: leaves `$$failed` set to a space-separated list of the directories
# whose command exited non-zero, empty when none did. The caller decides what
# that means -- test-python exits 1, coverage prints a note and carries on.
#
# Two properties the sequential loop had, kept by different means now that the
# directories run concurrently. Every directory still runs even after another
# fails: xargs keeps going, and each worker records its own verdict in a file
# because a variable assigned in a subprocess cannot come back to the parent.
# And each directory's output still arrives as a labelled block, because it is
# captured to its own file and printed afterwards in PYTHON_TEST_DIRS order --
# concurrent writers to one stream interleave mid-line and the "==> dir" headers
# stop meaning anything. What that costs is progress, and a run killed mid-sweep
# (a CI cancellation, a step timeout) loses the captured output entirely, so
# each worker prints a line as it finishes to leave something behind.
#
# The verdict file is per-directory and read as fail-closed: a directory whose
# .rc is missing or non-zero counts as failed. One shared append-only file would
# be shorter, but a failed append -- a full TMPDIR, which 25 concurrent suites
# make likelier than the old loop did -- would silently drop a red directory and
# let the gate pass. Absence has to mean failure, not success.
#
# $(1) is interpolated into a single-quoted sh -c string, so it must not contain
# a single quote. It also must not contain a comma: $(call) splits arguments on
# commas before the body ever sees them, so `python3 -c "import os, sys"` would
# arrive silently truncated at the comma. Both callers avoid each.
define sweep_python_test_dirs
work=$$(mktemp -d); \
trap 'rm -rf "$$work"' EXIT INT TERM; \
export work; \
printf '%s\n' $(PYTHON_TEST_DIRS) | xargs -P $(PYTHON_TEST_JOBS) -I{} sh -c ' \
	dir="$$1"; \
	stem="$$work/$$(printf "%s" "$$dir" | tr "/" "_")"; \
	if (cd "$$dir" && $(1)) >"$$stem.log" 2>&1; then \
		rc=0; printf "    ok  %s\n" "$$dir"; \
	else \
		rc=1; printf "  FAIL  %s\n" "$$dir"; \
	fi; \
	echo "$$rc" >"$$stem.rc" \
' _ {}; \
echo; \
failed=""; \
for dir in $(PYTHON_TEST_DIRS); do \
	echo "==> $$dir"; \
	stem="$$work/$$(printf "%s" "$$dir" | tr "/" "_")"; \
	if [ -f "$$stem.log" ]; then \
		cat "$$stem.log"; \
	else \
		echo "no output captured -- this directory never ran"; \
	fi; \
	[ "$$(cat "$$stem.rc" 2>/dev/null)" = "0" ] || failed="$$failed $$dir"; \
done; \
failed=$${failed# }
endef

# The same packages as `import` names rather than distribution names, because
# that is what the preflight below can actually test for: python-dotenv imports
# as `dotenv` and pyyaml as `yaml`.
PYTHON_TEST_IMPORTS := fastapi httpx mcp dotenv plotly pydantic streamlit uvicorn websockets yaml

test-python-deps: ## Install the third-party imports `make test-python` needs.
	@python3 -m pip install -r requirements-test.txt

e2e-tests: ## Run the live E2E promotion test suite against the target GKE cluster.
	@./scripts/release/execute_e2e_tests.sh

test-e2e: e2e-tests ## Alias for e2e-tests.

test-e2e-deps: ## Install dependencies required to run the E2E test suite.
	@python3 -m pip install -r tests/e2e/requirements.txt

e2e-test-deps: test-e2e-deps ## Alias for test-e2e-deps.

# One command for "is this branch landable": everything a PR must pass, ordered
# so the cheapest check fails first.
#
# Added because the answer used to be three commands nobody could remember, and
# a handoff doc had to carry the recipe. If you add a suite, add it here.
# test-bench is deliberately not here: its deps target installs bench/
# editable, which pulls devops-bench from a pinned git SHA over the network.
# verify stays offline-runnable; the bench suite gates in CI (bench-tests job)
# and runs locally with `make test-bench`.
verify: ## Run everything a PR must pass offline: go build, go vet, go test, python tests. The bench suite needs network; run `make test-bench` separately.
	@echo "==> go build"; cd k8s-operator && go build ./...
	@echo "==> go vet";   cd k8s-operator && go vet ./...
	@echo "==> go test";  cd k8s-operator && go test ./...
	@echo "==> go build (a2a)"; cd a2a && go build ./...
	@echo "==> go vet (a2a)";   cd a2a && go vet ./...
	@echo "==> go test (a2a)";  cd a2a && go test ./...
	@echo "==> python (k8s-operator)"; $(MAKE) --no-print-directory -C k8s-operator test-python
	@echo "==> python (everything else)"; $(MAKE) --no-print-directory test-python
	@echo "==> verify OK"

test-python: ## Run the Python unit tests outside k8s-operator/.
	@if [ -z "$(PYTHON_TEST_DIRS)" ]; then \
		echo "Error: no test_*.py files found under agents/, deploy/docker or scripts/."; \
		echo "Either the tests moved or the globs are stale -- failing rather than reporting success."; \
		exit 1; \
	fi
# Named up front rather than left to surface as an ImportError inside one
# directory's discovery, where a missing package reads like a broken test. This
# is a warning and not a hard stop because the two failures are independent: a
# machine that cannot install `mcp` can still run every other directory, and
# refusing to start would throw away that signal to report something the
# developer already knows. The exit status below still fails, so CI cannot go
# green on a suite whose modules never imported.
	@missing=""; \
	for mod in $(PYTHON_TEST_IMPORTS); do \
		python3 -c "import $$mod" >/dev/null 2>&1 || missing="$$missing $$mod"; \
	done; \
	if [ -n "$$missing" ]; then \
		echo "Warning: missing third-party imports:$$missing"; \
		echo "         The agent scripts import these at module scope, so the test"; \
		echo "         modules that import those scripts will fail to load."; \
		echo "         Install them with:  make test-python-deps"; \
		echo; \
	fi
# Every directory runs even after one fails, and the failures are named again at
# the end. This loop was `set -e` over a plain `for`, which stopped at the first
# failing directory -- and since the list is sorted, agents/platform/scripts
# failing meant deploy/docker/patches (the largest suite in the repository, 599
# tests) never ran at all, while the output still ended in a familiar-looking
# failure. A red run that hides four green directories is survivable; one that
# hides an untested directory is not.
#
# Both survive the move to concurrency; sweep_python_test_dirs says how.
	@export PYTHONPATH="$(PYTHON_TEST_PATH):$${PYTHONPATH:-}"; \
	$(call sweep_python_test_dirs,python3 -m unittest discover -p "test_*.py"); \
	missing=""; \
	for mod in $(PYTHON_TEST_IMPORTS); do \
		python3 -c "import $$mod" >/dev/null 2>&1 || missing="$$missing $$mod"; \
	done; \
	if [ -n "$$failed" ]; then \
		echo; \
		echo "Failing test directories: $$failed"; \
		if [ -n "$$missing" ]; then \
			echo "Missing third-party imports:$$missing -- run: make test-python-deps"; \
		fi; \
		exit 1; \
	fi

# Coverage runs the same suite the same way -- literally the same sweep as
# test-python, through sweep_python_test_dirs, with `coverage run` in place of
# `python3`. That mirroring is the point: a coverage target that discovers tests
# any other way measures a different suite. Two things differ. COVERAGE_ROOT
# pins the measured tree to the repository root (the sweep cd's into each
# directory, and .coveragerc reads the variable because `source` cannot be
# relative from seventeen places), and COVERAGE_FILE parks every per-directory
# data file in one place for `coverage combine`. By default a failing directory
# is reported but does not stop the measurement, so a red directory cannot hide
# the number for the others.
#
# Concurrency needs nothing extra here: .coveragerc already sets parallel = True,
# so each process writes its own data file suffixed with host and pid and the
# `coverage combine` below merges them. That setting was there for the
# per-directory loop, and it is the same property concurrent directories need.
#
# COVERAGE_STRICT=1 makes the target fail at the end when any directory failed,
# which is what lets one run serve as both the verdict and the meter. CI's
# required job sets it, so a pull request pays for the 5638 tests once instead
# of running them again unmeasured in a second job. The default stays 0 because
# a local run against a tree with known-red directories should still print a
# total. The failing list travels through a file because each recipe line is its
# own shell: the sweep leaves `$$failed` set in the shell that called it, and
# the check at the bottom of the target runs in a different one.
COVERAGE_DIR := .coverage-data
COVERAGE_STRICT ?= 0
COVERAGE_FAILED_FILE := failed-dirs.txt
# Named rather than written literally in the four places below so a test can
# point one run's output somewhere else. tests/ is itself a PYTHON_TEST_DIR, so
# a test that invoked this target with the defaults would `rm -rf` the data
# directory of the very sweep it is running inside -- concurrently, and with no
# error, leaving the outer run to combine whatever survived.
COVERAGE_XML ?= coverage.xml
COVERAGE_GO_XML ?= coverage-go.xml

coverage: ## Measure unit-test coverage; writes coverage.xml (and coverage-go.xml when tooling allows).
	@rm -rf $(COVERAGE_DIR) $(COVERAGE_XML) $(COVERAGE_GO_XML)
	@mkdir -p $(COVERAGE_DIR)
	@if [ -z "$(strip $(PYTHON_TEST_DIRS))" ]; then \
		echo "ERROR: PYTHON_TEST_DIRS expanded to nothing; the globs above are stale."; \
		exit 1; \
	fi
# Validated here rather than beside the gate it controls, which runs last: a
# typo would otherwise cost the whole suite before saying so. Anything but 0 or
# 1 is refused instead of guessed -- every truthy-looking spelling (true, yes,
# on) reads as "not 1" to the test below, which turns the gate off silently and
# is the one failure this flag exists to prevent.
	@case "$(COVERAGE_STRICT)" in \
		0|1) ;; \
		*) echo "ERROR: COVERAGE_STRICT must be 0 or 1, got '$(COVERAGE_STRICT)'."; \
		   exit 1;; \
	esac
# The same preflight test-python runs, and here for the same reason: a missing
# package surfaces as an ImportError inside one directory's discovery, where it
# reads like a broken test rather than a missing install. Duplicated rather than
# factored out because a `define` would have to be expanded by both targets and
# the indirection costs more than the six lines. A warning, not a hard stop --
# the sweep's own exit status is what fails the run. It also keeps
# PYTHON_TEST_IMPORTS exercised by CI: this target is the one CI invokes, so
# without this the list could drift out of step with requirements-test.txt and
# nothing would notice.
	@missing=""; \
	for mod in $(PYTHON_TEST_IMPORTS); do \
		python3 -c "import $$mod" >/dev/null 2>&1 || missing="$$missing $$mod"; \
	done; \
	if [ -n "$$missing" ]; then \
		echo "Warning: missing third-party imports:$$missing"; \
		echo "         Install them with:  make test-python-deps"; \
		echo; \
	fi
	@export COVERAGE_ROOT=$(CURDIR) COVERAGE_FILE=$(CURDIR)/$(COVERAGE_DIR)/.coverage; \
	export PYTHONPATH="$(PYTHON_TEST_PATH):$${PYTHONPATH:-}"; \
	$(call sweep_python_test_dirs,python3 -m coverage run --rcfile=$(CURDIR)/.coveragerc -m unittest discover -p "test_*.py"); \
	if [ -n "$$failed" ]; then \
		echo "Note: failing test directories (their coverage is still recorded): $$failed"; \
	fi; \
	printf '%s' "$$failed" > $(COVERAGE_DIR)/$(COVERAGE_FAILED_FILE)
	@COVERAGE_ROOT=$(CURDIR) COVERAGE_FILE=$(CURDIR)/$(COVERAGE_DIR)/.coverage \
		python3 -m coverage combine --rcfile=$(CURDIR)/.coveragerc
	@COVERAGE_ROOT=$(CURDIR) COVERAGE_FILE=$(CURDIR)/$(COVERAGE_DIR)/.coverage \
		python3 -m coverage xml --rcfile=$(CURDIR)/.coveragerc -o $(COVERAGE_XML)
	@COVERAGE_ROOT=$(CURDIR) COVERAGE_FILE=$(CURDIR)/$(COVERAGE_DIR)/.coverage \
		python3 -m coverage report --rcfile=$(CURDIR)/.coveragerc | grep '^TOTAL'
# The Go half is best-effort: it needs gocover-cobertura for the XML diff-cover
# reads, and the operator's envtest binaries to run at all. CI skips it with
# COVERAGE_SKIP_GO=1 because k8s-operator-test.yml already runs that suite.
# -coverpkg=./... matters: without it, packages with no test files of their own
# drop out of the denominator and the number reads ~10 points high.
	@if [ "$(COVERAGE_SKIP_GO)" = "1" ]; then \
		echo "Skipping Go coverage (COVERAGE_SKIP_GO=1)."; \
	elif ! command -v gocover-cobertura >/dev/null 2>&1; then \
		echo "Skipping Go coverage: gocover-cobertura not installed."; \
		echo "  go install github.com/boumenot/gocover-cobertura@latest"; \
	else \
		$(MAKE) -C k8s-operator setup-envtest && \
		(cd k8s-operator && \
			ENVTEST_V="$$(sed -n 's/^ENVTEST_K8S_VERSION ?= //p' Makefile)" && \
			test -n "$$ENVTEST_V" && \
			KUBEBUILDER_ASSETS="$$(bin/setup-envtest use "$$ENVTEST_V" --bin-dir bin -p path)" && \
			test -n "$$KUBEBUILDER_ASSETS" && \
			KUBEBUILDER_ASSETS="$$KUBEBUILDER_ASSETS" \
			go test -coverpkg=./... $$(go list ./... | grep -v /e2e) -coverprofile=$(CURDIR)/$(COVERAGE_DIR)/go-cover.out && \
			gocover-cobertura < $(CURDIR)/$(COVERAGE_DIR)/go-cover.out > $(COVERAGE_GO_XML)) \
		|| echo "Go coverage failed; the Python half above is unaffected."; \
	fi
# The envtest version is read from k8s-operator/Makefile's own pin rather than
# repeated here: a hardcoded copy drifted once already (1.31.0 against the
# operator's 1.36.0), and the empty-string failure mode -- setup-envtest
# failing, KUBEBUILDER_ASSETS="" exported, every suite red, all of it
# swallowed by the || echo above -- is why both reads are guarded with test -n.
#
# The strict gate, last in the target on purpose: combine, xml and report have
# all run by the time it fails, so a red run still leaves coverage.xml on disk
# for the CI job to upload and the coverage comment to be posted from.
	@if [ "$(COVERAGE_STRICT)" = "1" ] && [ -s $(COVERAGE_DIR)/$(COVERAGE_FAILED_FILE) ]; then \
		echo "FAIL (COVERAGE_STRICT=1) -- failing test directories: $$(cat $(COVERAGE_DIR)/$(COVERAGE_FAILED_FILE))"; \
		exit 1; \
	fi

# 55 is a deliberately loose placeholder: the real floor gets committed from the
# first green CI run of the `test` job's coverage sweep, not from a laptop
# measurement, because CI's Python and dependency set produce a different number.
COVERAGE_FLOOR ?= 55

coverage-check: ## Fail if total Python coverage is below COVERAGE_FLOOR. Run `make coverage` first.
	@if [ ! -f $(COVERAGE_DIR)/.coverage ]; then \
		echo "No coverage data. Run: make coverage"; \
		exit 1; \
	fi
	@COVERAGE_ROOT=$(CURDIR) COVERAGE_FILE=$(CURDIR)/$(COVERAGE_DIR)/.coverage \
		python3 -m coverage report --rcfile=$(CURDIR)/.coveragerc --fail-under=$(COVERAGE_FLOOR) >/dev/null \
		&& echo "Coverage is at or above the $(COVERAGE_FLOOR)% floor." \
		|| { echo "Coverage fell below the $(COVERAGE_FLOOR)% floor."; exit 1; }

# bench/tests is the one Python suite that cannot join PYTHON_TEST_DIRS: it is
# pytest-native (fixtures, parametrize), and `unittest discover` collects two
# of its tests and errors on both. So it runs under its own target, and
# scripts/test_test_discovery.py keeps the exclusion explicit rather than an
# accident of the globs above.
# pyyaml is a test-only dependency and is named here rather than in bench's
# runtime `dependencies`: the harness never parses a task.yaml itself (devops-
# bench does that before any of this package is imported). One test reads the
# specs directly -- the roster-collision sweep in tests/test_verifiers.py,
# which has to see every task's phrases at once -- so the parser belongs with
# the test runner. Keep in step with bench/pyproject.toml's `dev` group.
test-bench-deps: ## Install what `make test-bench` needs: bench/ editable plus pytest and pyyaml. Resolves devops-bench from the git SHA pinned in bench/pyproject.toml, so the first run needs network.
	@python3 -m pip install -e bench/ pytest pyyaml

test-bench: ## Run the bench harness tests under pytest.
	@python3 -m pytest bench/tests/

# A broken bench case otherwise costs a full presubmit to discover: provision,
# deploy, run the agent, score, read the log. Most of the ways a task.yaml is
# broken are static -- an unknown domain slug, a fixture role the seeded fleet
# never planted, a check with no assertion, a case in no TASKS entry -- so they
# fail here in a second instead. The same rules gate in CI through
# scripts/test_task_registration.py; this target is the fast path to them.
bench-case-check: ## Validate every bench task.yaml against the case-format contract (no cluster).
	@python3 -c "import yaml" 2>/dev/null || { \
		echo "bench-case-check needs PyYAML: python3 -m pip install pyyaml (or make test-python-deps)"; \
		exit 1; \
	}
	@python3 scripts/validate_bench_cases.py

# The integration tier: real components wired together with the agent replaced
# by a fake -- no model calls, deterministic by construction (strategy 4.1b).
# The tier now gates: tests/integration is in PYTHON_TEST_DIRS, so `make
# test-python` and the CI `test` job both run it, and a red seam test is a red
# pull request. This target is a convenience for running that one tier while
# you work on a seam -- seconds instead of the whole sweep -- and is not what
# CI invokes, so do not reach for it as the definition of what must pass.
# Install a Go toolchain before trusting a green run here: the injector seam
# compiles the real Go client, and without `go` on PATH it skips itself rather
# than failing, which reads exactly like a pass.
test-integration: ## Run just the integration seam tests; CI reaches them through the PYTHON_TEST_DIRS sweep.
	@cd tests/integration && PYTHONPATH="$(CURDIR):$${PYTHONPATH:-}" python3 -m unittest discover -p "test_*.py"

# The agent's own instructions are prose, and prose is not compiled: a persona
# that cites a renamed skill or an SOP that names a moved script merges clean
# and fails at 06:20 inside an agent, as a slightly worse answer rather than an
# error. This is the compiler for that layer.
#
# Not folded into docs-check: these files are runtime assets rather than
# documents (the docs map does not inventory them), and the resolution rules are
# not the same either -- a path here resolves against a profile home and the
# /opt/defaults layer the entrypoint copies over it, not against the file that
# cites them. CI runs it as its own job in validate.yml, alongside the other
# repository-structure invariants.
prompt-check: ## Verify the agent's instructions cite skills and files that exist.
	@python3 scripts/check_prompt_assets.py

# Documentation that mirrors a machine-readable source is generated rather than
# hand-kept: the cron jobs, the skill catalogue and the image inventory as
# <!-- BEGIN GENERATED --> regions, plus docs/family-roster.txt written whole.
docs-generate: ## Regenerate the generated doc regions and files from their sources.
	@python3 scripts/generate_docs.py

# Everything CI enforces about the docs, in one command.
docs-check: docs-check-generated docs-check-links docs-check-terminology docs-check-map docs-check-context-budget ## Run every documentation check CI runs.

docs-check-generated:
	@python3 scripts/generate_docs.py --check

docs-check-links:
	@python3 scripts/check_docs_links.py

docs-check-terminology:
	@./hack/check-docs-terminology.sh

docs-check-map:
	@python3 scripts/check_docs_map.py

docs-check-context-budget:
	@python3 scripts/check_context_budget.py

chart-sync: ## Sync the Helm chart's CRD copies and operator ClusterRole rules from k8s-operator/config.
	@./hack/sync-chart-manifests.sh

chart-check: ## Verify the chart's CRD/RBAC copies match k8s-operator/config (CI runs this).
	@./hack/sync-chart-manifests.sh --check

tf-apply: ## Apply terraform/examples/full-install, adopting KMS resources a previous destroy left behind.
	@./terraform/examples/full-install/lifecycle.sh apply $(ARGS)

tf-destroy: ## Destroy terraform/examples/full-install, clearing the finalizer, backups, and deletion protection first.
	@./terraform/examples/full-install/lifecycle.sh destroy $(ARGS)

validate: ## Fail if any skill sits under agents/*/defaults/skills/.
	@if [ -n "$(BAD_SKILLS)" ]; then \
		echo "Error: Skills should not be placed under agents/*/defaults/skills. Move them to agents/*/skills/"; \
		set -- $(BAD_SKILLS); \
		for file; do echo "  $$file"; done; \
		exit 1; \
	else \
		echo "Validation passed: No skills found in invalid paths."; \
	fi


