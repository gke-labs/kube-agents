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
#               whatever happens, and the records go to $ARTIFACTS and the bucket
#
# Runs main only, as its own identity, sized-fleet-runner: never a presubmit. The
# identity's roles on the scale project are in the design, "How the fleet is built,
# and who may build it". A run by hand works too, from a main checkout:
#
#   SCALE_PROJECT_ID=<project> SCALE_FLEET_SIZE=S hack/ci-scale-nightly.sh
#
# Exit: the run's own, 0 green and 1 red; any other value is a step that could not run.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIZE="${SCALE_FLEET_SIZE:-M}"
PROJECT="${SCALE_PROJECT_ID:?SCALE_PROJECT_ID must name the scale project}"
HOST_CLUSTER="${SCALE_HOST_CLUSTER:-scale-host}"
HOST_LOCATION="${SCALE_HOST_LOCATION:-us-central1-c}"
RESULTS_BUCKET="${SCALE_RESULTS_BUCKET:-}"
# Prow sets BUILD_ID; a run by hand gets the time. Both are valid GCP label values.
RUN_ID="nightly-${BUILD_ID:-manual-$(date -u +%Y%m%d%H%M%S)}"
OUT="${ARTIFACTS:-$(mktemp -d)}/scale"
# Pinned as the seeded fleet's reconcile pins its OpenTofu install: the standalone
# installer from get.opentofu.org.
TOFU_INSTALLER_URL="https://get.opentofu.org/install-opentofu.sh"
HELM_INSTALLER_URL="https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3"

log() { printf '%s ci-scale-nightly: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

# The CI image has gcloud, kubectl and python3; OpenTofu, Helm and PyYAML arrive here.
ensure_tools() {
  if ! command -v tofu >/dev/null; then
    log "installing OpenTofu"
    curl --proto '=https' --tlsv1.2 -fsSL "$TOFU_INSTALLER_URL" -o /tmp/install-opentofu.sh
    chmod +x /tmp/install-opentofu.sh
    /tmp/install-opentofu.sh --install-method standalone >/dev/null
  fi
  if ! command -v helm >/dev/null; then
    log "installing Helm"
    curl --proto '=https' --tlsv1.2 -fsSL "$HELM_INSTALLER_URL" | bash >/dev/null
  fi
  if ! python3 -c 'import yaml' 2>/dev/null; then
    log "installing PyYAML"
    python3 -m pip install --quiet pyyaml
  fi
  command -v gke-gcloud-auth-plugin >/dev/null
}

fleet() {
  python3 "$REPO/hack/sized_fleet.py" "$1" --size "$SIZE" --project "$PROJECT" --run-id "$RUN_ID" \
    --host-cluster "$HOST_CLUSTER" --host-location "$HOST_LOCATION" "${@:2}"
}

main() {
  ensure_tools
  mkdir -p "$OUT"
  log "run $RUN_ID: size $SIZE in $PROJECT, host $HOST_CLUSTER"
  fleet sweep
  fleet install
  local rc=0
  # `up` and `run` share the trap through `run --down`; an `up` that fails part-way is
  # deleted by the explicit `down` below.
  if ! fleet up; then
    log "the fleet did not come up; deleting what was built"
    fleet down || true
    exit 2
  fi
  local bucket=()
  if [[ -n "$RESULTS_BUCKET" ]]; then bucket=(--results-bucket "$RESULTS_BUCKET"); fi
  fleet run --down --out "$OUT" ${bucket[@]+"${bucket[@]}"} || rc=$?
  log "run $RUN_ID finished with $rc; records in $OUT"
  exit "$rc"
}

main "$@"
