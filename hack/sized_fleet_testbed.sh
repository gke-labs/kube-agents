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

# Prepares a project as the sized test fleet's test bed (docs/designs/sized-test-fleet.md,
# "Rollout", stage 1): the one-time pieces bench/tf/sized-fleet/README.md lists, done
# once and idempotently. It creates only what is missing and changes nothing that exists.
#
#   1. the APIs the fleet, its records and the install use
#   2. the OpenTofu state bucket, <project>-tf-state
#   3. the nodes' service account, sized-fleet-nodes, with the node-agent roles
#   4. the standing network, sized-fleet
#   5. the host cluster's label, purpose=harness-scale, when the host cluster exists
#
# The kube-agents install on the host cluster is the installer's
# (scripts/installer/, INSTALL.md), run before or after this script; step 5 then
# marks that cluster as one a run may reset. The project and its billing link are
# preconditions, as for the eval pool's projects.
#
#   hack/sized_fleet_testbed.sh --project-id=<project> [--host-cluster=scale-host] [--host-location=us-central1-c]
set -euo pipefail

HOST_CLUSTER="scale-host"
HOST_LOCATION="us-central1-c"
BUCKET_LOCATION="us-central1"
NETWORK="sized-fleet"
NODE_SA_NAME="sized-fleet-nodes"
HOST_LABEL="purpose=harness-scale"
APIS=(compute.googleapis.com container.googleapis.com storage.googleapis.com iam.googleapis.com
  logging.googleapis.com monitoring.googleapis.com cloudbilling.googleapis.com
  cloudresourcemanager.googleapis.com aiplatform.googleapis.com)
NODE_ROLES=(roles/logging.logWriter roles/monitoring.metricWriter roles/monitoring.viewer
  roles/stackdriver.resourceMetadata.writer roles/artifactregistry.reader)

log() { printf '%s sized-fleet-testbed: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

PROJECT=""
for arg in "$@"; do
  case "$arg" in
  --project-id=*) PROJECT="${arg#*=}" ;;
  --host-cluster=*) HOST_CLUSTER="${arg#*=}" ;;
  --host-location=*) HOST_LOCATION="${arg#*=}" ;;
  *)
    echo "unknown flag: $arg" >&2
    exit 2
    ;;
  esac
done
if [[ -z "$PROJECT" ]]; then
  echo "usage: $0 --project-id=<project> [--host-cluster=NAME] [--host-location=ZONE]" >&2
  exit 2
fi

gcloud projects describe "$PROJECT" --format='value(projectId)' >/dev/null
if [[ "$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null || true)" == "False" ]]; then
  log "billing is off on $PROJECT; link a billing account first"
  exit 1
fi

log "1. APIs"
gcloud services enable "${APIS[@]}" --project "$PROJECT"

BUCKET="gs://${PROJECT}-tf-state"
log "2. state bucket $BUCKET"
if ! gcloud storage buckets describe "$BUCKET" --format='value(name)' >/dev/null 2>&1; then
  gcloud storage buckets create "$BUCKET" --project "$PROJECT" --location "$BUCKET_LOCATION" \
    --uniform-bucket-level-access --public-access-prevention
  gcloud storage buckets update "$BUCKET" --versioning
fi

NODE_SA="${NODE_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
log "3. node service account $NODE_SA"
if ! gcloud iam service-accounts describe "$NODE_SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$NODE_SA_NAME" --project "$PROJECT" \
    --display-name="Node service account for the sized test fleet"
fi
for role in "${NODE_ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "$PROJECT" --condition=None --format=none \
    --member="serviceAccount:${NODE_SA}" --role="$role"
done

log "4. network $NETWORK"
if ! gcloud compute networks describe "$NETWORK" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud compute networks create "$NETWORK" --project "$PROJECT" --subnet-mode=custom
fi

log "5. host cluster label"
if gcloud container clusters describe "$HOST_CLUSTER" --location "$HOST_LOCATION" --project "$PROJECT" \
  --format='value(name)' >/dev/null 2>&1; then
  gcloud container clusters update "$HOST_CLUSTER" --location "$HOST_LOCATION" --project "$PROJECT" \
    --update-labels="$HOST_LABEL"
else
  log "no $HOST_CLUSTER in $HOST_LOCATION yet: install kube-agents there (INSTALL.md), then run this again"
fi

log "done; next: SCALE_PROJECT_ID=$PROJECT SCALE_FLEET_SIZE=S hack/ci-scale-nightly.sh"
