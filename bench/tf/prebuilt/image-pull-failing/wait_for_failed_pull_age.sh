#!/bin/bash
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

# The image-pull-failing stack's wait: it returns once each planted workload
# has had a pod in a failed image pull for WAIT_AGE_SECONDS, so the audit the
# case runs next reads a failure older than the check's minimum.
#
# It measures on the runner's own clock, from the moment it first sees every
# workload's pod waiting on the failed pull. A pod was failing at least that
# long, so the wait errs on the long side and reads no timestamp of the
# cluster's. The check counts each pod's own wait, so the pods seen at the
# start must be the pods still failing at the end: a pod replaced during the
# wait is a young failure, and the apply fails. Inputs arrive as WAIT_*
# variables from main.tf beside this file.
# Run against stub kubectl and gcloud by tests/test_image_pull_failing_stack.py.
set -euo pipefail

readonly WAIT_POLL_SECONDS="${WAIT_POLL_SECONDS:-10}"
readonly WAIT_FAILED_PULL_REASONS="ErrImagePull ImagePullBackOff"
readonly WAIT_WORKLOAD_LABEL="app"
# One line per pod: its uid, then the waiting reason of each container.
readonly WAIT_PODS_JSONPATH='{range .items[*]}{.metadata.uid}{range .status.containerStatuses[*]}{" "}{.state.waiting.reason}{end}{"\n"}{end}'

wait_require() { # <name>: the variable must be set and non-empty
  if [ -z "${!1:-}" ]; then
    echo "ERROR: $1 is not set; the stack's provisioner passes it." >&2
    exit 1
  fi
}

wait_credentials() {
  local project="${WAIT_PROJECT:-}"
  if [ -z "$project" ]; then
    project="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  if [ -z "$project" ]; then
    echo "ERROR: no project id. Pass project_id or set a gcloud default project." >&2
    exit 1
  fi
  gcloud container clusters get-credentials "$WAIT_HOST_CLUSTER" \
    --location "$WAIT_HOST_LOCATION" --project "$project" --quiet
}

# The uids of the workload's pods that wait on a failed image pull right now,
# sorted, one per line. Empty when none does.
wait_failing_pods() { # <workload>
  local uid reasons reason
  kubectl get pods --namespace "$WAIT_NAMESPACE" -l "$WAIT_WORKLOAD_LABEL=$1" -o jsonpath="$WAIT_PODS_JSONPATH" |
    while read -r uid reasons; do
      for reason in $reasons; do
        case " $WAIT_FAILED_PULL_REASONS " in
          *" $reason "*) echo "$uid"; break ;;
        esac
      done
    done | sort
}

# Every workload's failing pods, as "<workload> <uid>" lines; fails when a
# workload has none.
wait_all_failing() {
  local workload pods
  for workload in $WAIT_WORKLOADS; do
    pods="$(wait_failing_pods "$workload")"
    [ -n "$pods" ] || return 1
    printf '%s\n' "$pods" | sed "s/^/$workload /"
  done
}

wait_require WAIT_HOST_CLUSTER; wait_require WAIT_HOST_LOCATION; wait_require WAIT_NAMESPACE
wait_require WAIT_WORKLOADS; wait_require WAIT_AGE_SECONDS; wait_require WAIT_SEEN_TIMEOUT_SECONDS

wait_kubeconfig_dir="$(mktemp -d)"
trap 'rm -rf "$wait_kubeconfig_dir"' EXIT
KUBECONFIG="$wait_kubeconfig_dir/config"
export KUBECONFIG
wait_credentials

until wait_seen="$(wait_all_failing)"; do
  if [ "$SECONDS" -ge "$WAIT_SEEN_TIMEOUT_SECONDS" ]; then
    echo "ERROR: after ${WAIT_SEEN_TIMEOUT_SECONDS}s not every workload of '$WAIT_WORKLOADS' in $WAIT_NAMESPACE has a pod in a failed image pull; the plant did not take." >&2
    exit 1
  fi
  sleep "$WAIT_POLL_SECONDS"
done
echo "Every planted workload has a pod in a failed image pull; waiting ${WAIT_AGE_SECONDS}s so the failure is older than the check's minimum."
sleep "$WAIT_AGE_SECONDS"
wait_now="$(wait_all_failing || true)"
if [ "$wait_now" != "$wait_seen" ]; then
  echo "ERROR: the pods in a failed image pull are not the ones seen before the wait (a pod left the failed pull or was replaced); the fixture is not the one the case grades." >&2
  exit 1
fi
