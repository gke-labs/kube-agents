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

# The node-nf-conntrack-table-saturation stack's wait: it returns once Cloud
# Logging holds the kernel's line from a node of the planted cluster, so the
# audit the case runs next has the line to read.
#
# It asks Cloud Logging what the audit's check asks: the serial console log of
# an instance, with the kernel's text. It narrows that to the planted
# cluster's nodes by name, which it reads from the instance list through
# GKE's cluster label, so a line that an earlier run left in the project's log
# does not answer it. No line before the timeout fails the apply: the plant
# did not take, or the project does not store that log. Inputs arrive as
# WAIT_* variables from main.tf beside this file.
# Run against a stub gcloud by tests/test_node_conntrack_stack.py, which also
# holds the text and the log id below equal to the collector's.
set -euo pipefail

readonly WAIT_POLL_SECONDS="${WAIT_POLL_SECONDS:-15}"
readonly WAIT_KERNEL_TEXT="nf_conntrack: table full, dropping packet"
readonly WAIT_SERIAL_LOG_ID="serialconsole.googleapis.com/serial_port_1_output"
readonly WAIT_CLUSTER_LABEL="goog-k8s-cluster-name"
readonly WAIT_NAME_LABEL="compute.googleapis.com/resource_name"
# The wait starts after the plant, so an hour reaches every line of this run.
readonly WAIT_FRESHNESS="1h"
readonly WAIT_PLANT_NAMESPACE="bench-conntrack"
readonly WAIT_PLANT_POD="conntrack-fill"

wait_require() { # <name>: the variable must be set and non-empty
  if [ -z "${!1:-}" ]; then
    echo "ERROR: $1 is not set; the stack's provisioner passes it." >&2
    exit 1
  fi
}

# The names of the planted cluster's node instances, one per line.
wait_nodes() {
  gcloud compute instances list --project "$WAIT_PROJECT" \
    --filter "labels.$WAIT_CLUSTER_LABEL=$WAIT_CLUSTER" --format "value(name)"
}

# The id of one log entry with the kernel's line from the node, or nothing.
wait_line() { # <node>
  gcloud logging read \
    "resource.type=\"gce_instance\" AND log_id(\"$WAIT_SERIAL_LOG_ID\") AND textPayload:\"$WAIT_KERNEL_TEXT\" AND labels.\"$WAIT_NAME_LABEL\"=\"$1\"" \
    --project "$WAIT_PROJECT" --freshness "$WAIT_FRESHNESS" --limit 1 --format "value(insertId)"
}

# Succeeds, and names the node, when one node's line is in Cloud Logging.
wait_any_line() {
  local node
  for node in $(wait_nodes); do
    if [ -n "$(wait_line "$node")" ]; then
      echo "The kernel's line from $node is in Cloud Logging."
      return 0
    fi
  done
  return 1
}

wait_require WAIT_PROJECT; wait_require WAIT_CLUSTER; wait_require WAIT_TIMEOUT_SECONDS

until wait_any_line; do
  if [ "$SECONDS" -ge "$WAIT_TIMEOUT_SECONDS" ]; then
    echo "ERROR: after ${WAIT_TIMEOUT_SECONDS}s Cloud Logging holds no line '$WAIT_KERNEL_TEXT' from a node of $WAIT_CLUSTER. The plant did not take (read the log of pod $WAIT_PLANT_POD in namespace $WAIT_PLANT_NAMESPACE), or project $WAIT_PROJECT does not store the serial console log." >&2
    exit 1
  fi
  sleep "$WAIT_POLL_SECONDS"
done
