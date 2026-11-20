#!/usr/bin/env bash
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The sized test fleet's nightly run (docs/designs/sized-test-fleet.md), as the Prow
# periodics ci-kube-agents-scale-nightly-m and ci-kube-agents-scale-weekly-l run it:
#
#   1. sweep    delete any sized fleet an aborted run left
#   2. install  the host cluster's install on the newest main commit with published
#               images, at the size's tier
#   3. up       build the fleet and its workloads
#   4. run      the burst and soak windows and the verdict; --down deletes the fleet
#               when the run ends, and an EXIT trap here deletes it whenever `run`
#               stops before its own cleanup. The records go to $ARTIFACTS and the bucket.
#               The soak window runs the eval cases hack/eval/sized-fleet-cases.txt
#               lists, once each; SCALE_EVAL_CASES=false skips them
#
# Runs main only, as its own identity, sized-fleet-runner: never a presubmit. The
# identity's roles on the scale project are in the design, "How the fleet is built,
# and who may build it". A run by hand works too, from a main checkout:
#
#   SCALE_PROJECT_ID=<project> SCALE_FLEET_SIZE=S hack/ci-scale-nightly.sh
#
# Exit: the run's own, 0 green and 1 red; 2 is a step before the run that could not run
# (sweep, install, up); 143 is a signal.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIZE="${SCALE_FLEET_SIZE:-M}"
PROJECT="${SCALE_PROJECT_ID:?SCALE_PROJECT_ID must name the scale project}"
HOST_CLUSTER="${SCALE_HOST_CLUSTER:-scale-host}"
HOST_LOCATION="${SCALE_HOST_LOCATION:-us-central1-c}"
RESULTS_BUCKET="${SCALE_RESULTS_BUCKET:-}"
EVAL_CASES="${SCALE_EVAL_CASES:-true}"
# Prow sets BUILD_ID; a run by hand gets the time. Both are valid GCP label values.
RUN_ID="nightly-${BUILD_ID:-manual-$(date -u +%Y%m%d%H%M%S)}"
OUT="${ARTIFACTS:-$(mktemp -d)}/scale"
# The tools the CI image lacks, each installed at a pinned version.
TOFU_INSTALLER_URL="https://get.opentofu.org/install-opentofu.sh"
TOFU_VERSION="1.12.6"
HELM_VERSION="v3.19.0"
HELM_INSTALLER_URL="https://raw.githubusercontent.com/helm/helm/${HELM_VERSION}/scripts/get-helm-3"
UV_VERSION="0.11.7"
UV_INSTALLER_URL="https://astral.sh/uv/${UV_VERSION}/install.sh"
EXIT_SETUP=2
EXIT_SIGNAL=143
FLEET_UP=0
RUN_PID=""

log() { printf '%s ci-scale-nightly: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

# The CI image has gcloud, kubectl and python3; OpenTofu, Helm, PyYAML and uv (the eval
# harness's runner) arrive here.
ensure_tools() {
  if ! command -v tofu >/dev/null; then
    log "installing OpenTofu"
    curl --proto '=https' --tlsv1.2 -fsSL "$TOFU_INSTALLER_URL" -o /tmp/install-opentofu.sh
    chmod +x /tmp/install-opentofu.sh
    /tmp/install-opentofu.sh --install-method standalone --opentofu-version "$TOFU_VERSION" >/dev/null
  fi
  if ! command -v helm >/dev/null; then
    log "installing Helm"
    curl --proto '=https' --tlsv1.2 -fsSL "$HELM_INSTALLER_URL" | DESIRED_VERSION="$HELM_VERSION" bash >/dev/null
  fi
  if ! python3 -c 'import yaml' 2>/dev/null; then
    log "installing PyYAML"
    python3 -m pip install --quiet pyyaml
  fi
  if [[ "$EVAL_CASES" == "true" ]] && ! command -v uv >/dev/null; then
    log "installing uv"
    curl --proto '=https' --tlsv1.2 -LsSf "$UV_INSTALLER_URL" | sh >/dev/null
    export PATH="$HOME/.local/bin:$PATH"
  fi
  command -v gke-gcloud-auth-plugin >/dev/null
}

fleet() {
  python3 "$REPO/hack/sized_fleet.py" "$1" --size "$SIZE" --project "$PROJECT" --run-id "$RUN_ID" \
    --host-cluster "$HOST_CLUSTER" --host-location "$HOST_LOCATION" "${@:2}"
}

# Once `up` has started, the fleet is deleted on any exit. `run --down` normally does it
# first; a second destroy of an empty state changes nothing.
# shellcheck disable=SC2329 # invoked by the EXIT trap
cleanup() {
  if [[ "$FLEET_UP" == "1" ]]; then
    log "deleting the fleet"
    fleet down || log "the fleet's delete failed; the next run's sweep retries it"
  fi
}

# Prow signals this shell only: pass the signal to `run`, whose own handler deletes the
# fleet, and wait for it before the EXIT trap runs.
# shellcheck disable=SC2329 # invoked by the TERM and INT trap
on_signal() {
  if [[ -n "$RUN_PID" ]]; then
    kill -TERM "$RUN_PID" 2>/dev/null || true
    wait "$RUN_PID" || true
  fi
  exit "$EXIT_SIGNAL"
}

main() {
  ensure_tools
  mkdir -p "$OUT"
  log "run $RUN_ID: size $SIZE in $PROJECT, host $HOST_CLUSTER"
  trap cleanup EXIT
  trap on_signal TERM INT
  fleet sweep || exit "$EXIT_SETUP"
  fleet install || exit "$EXIT_SETUP"
  local rc=0
  FLEET_UP=1
  if ! fleet up; then
    log "the fleet did not come up"
    exit "$EXIT_SETUP"
  fi
  local extra=()
  if [[ -n "$RESULTS_BUCKET" ]]; then extra=(--results-bucket "$RESULTS_BUCKET"); fi
  if [[ "$EVAL_CASES" == "true" ]]; then extra+=(--eval-cases); fi
  fleet run --down --out "$OUT" ${extra[@]+"${extra[@]}"} &
  RUN_PID=$!
  wait "$RUN_PID" || rc=$?
  RUN_PID=""
  log "run $RUN_ID finished with $rc; records in $OUT"
  exit "$rc"
}

main "$@"
