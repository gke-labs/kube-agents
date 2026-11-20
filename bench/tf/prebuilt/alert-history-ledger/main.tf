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

# The scenario driver for bench/tasks/alert-history-overnight-from-ledger.
#
# It hands the Session KV daemon two `k8s-event` records, the shape
# k8s-event-watcher sends, for two workloads in a namespace nothing is planted
# in. The daemon writes one event-ledger row for each; the task then asks what
# alerts fired and grades that the answer names both, read through the
# `get_alert_history` tool.
#
# WHY NORMAL-TYPE EVENTS. The daemon grades a Normal-type event Info and holds
# it back at the severity gate: a ledger row with status `filtered`, no chat
# post, no triage turn, no card. That is the cheapest row the ledger takes and
# the one that proves the route, since nothing but the ledger knows the event
# happened. A Warning record would file a triage card per event per
# repetition against a namespace that does not exist. The reply must still name
# both workloads: a held-back event is exactly what the recap counts and a
# person cannot see.
#
# WHY THE FEATURES HEADER. Without `X-Watcher-Features: policy-filtered` the
# daemon answers an Info event `suppressed` for a watcher that cannot hold the
# status (the skew paragraph in k8s-operator/cmd/k8s-event-watcher/README.md);
# with it, `filtered`. The row is written either way; the header makes the
# expected answer the one a current watcher gets.
#
# Isolation class: read-only against the cluster; two rows in the install's
# ledger, which age out under the daemon's own retention and which no other
# stack rewrites. Eligible tier: nightly.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

locals {
  # The watcher's own constants for this record (types.go, injectKindEvent;
  # the Event's type and involvedObject.kind), and the daemon's answers.
  inject_kind      = "k8s-event"
  event_type       = "Normal"
  object_kind      = "Pod"
  features_header  = "X-Watcher-Features: policy-filtered"
  filtered_reply   = "filtered"
  event_count      = 3
  # A ReplicaSet pod suffix in the shape session_kv_server.DEPLOYMENT_POD_NAME_RE
  # folds (`-[a-f0-9]{8,10}-[a-z0-9]{5}$`), so the ledger's workload name is the
  # bare name the case grades.
  pod_suffix       = "7d9f8b6c5-x2k4q"
  # The tool's default page (platform_mcp_server.ALERT_HISTORY_DEFAULT_LIMIT):
  # the rows must sit on it, or the case fails for the install's other producers.
  first_page_limit = 50
  # Timeouts, in seconds.
  curl_timeout        = 10
  inject_curl_timeout = 30
}

resource "null_resource" "ledger_rows" {
  triggers = {
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    # Every apply is a fresh pair of rows: the case grades the newest page of
    # the ledger, and a repetition must find its own rows there.
    run = timestamp()
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      work_dir="$(mktemp -d)"
      # A kubeconfig of its own, for the reason gitops-drift gives: an earlier
      # tofu task in the matrix may have left the ambient context elsewhere.
      KUBECONFIG="$work_dir/config"
      export KUBECONFIG
      trap 'rm -rf "$work_dir"' EXIT
      trap 'exit 143' TERM INT

      project="${var.project_id}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project; this stack needs one to fetch credentials for ${var.host_cluster_name}." >&2
        exit 1
      fi
      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet

      pod="deployment/${var.agent_deployment}"
      exec_in_pod=(kubectl exec "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)
      exec_in_pod_stdin=(kubectl exec -i "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)

      session_id="$("$${exec_in_pod[@]}" sh -c \
        'curl -sS --max-time ${local.curl_timeout} -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" "$1/sessions" | jq -r ".sessionID // empty"' \
        sh "${var.daemon_url}" || true)"
      if [ -z "$session_id" ]; then
        echo "ERROR: the daemon returned no sessionID. A 401 here means SESSION_KV_API_KEY on ${var.agent_container} does not match what the daemon expects; a 503 means it is unset." >&2
        exit 1
      fi

      # The watcher's field names (k8s-operator/cmd/k8s-event-watcher/types.go,
      # InjectPayload). The uid is made up: nothing is planted, and the daemon
      # looks nothing up for an Info event. A script file rather than `-c`:
      # this heredoc is de-indented to its shallowest line, which is this one.
      envelope_py="$work_dir/envelope.py"
      cat > "$envelope_py" <<'PY'
      import json, os, sys
      env = os.environ
      payload = {
          "kind": env["EVENT_KIND"],
          "type": env["EVENT_TYPE"],
          "reason": env["EVENT_REASON"],
          "namespace": env["EVENT_NAMESPACE"],
          "kind_of_object": env["EVENT_OBJECT_KIND"],
          "name": env["EVENT_NAME"],
          "uid": env["EVENT_UID"],
          "count": int(env["EVENT_COUNT"]),
          "message": env["EVENT_MESSAGE"],
          "first_seen": env["EVENT_SEEN"],
          "last_seen": env["EVENT_SEEN"],
          "cluster": env["EVENT_CLUSTER"],
          "project": env["EVENT_PROJECT"],
          "location": env["EVENT_LOCATION"],
      }
      sys.stdout.write(json.dumps({"message": json.dumps(payload)}))
      PY
      seen="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      for workload in ${join(" ", var.workload_names)}; do
        envelope_file="$work_dir/envelope-$workload.json"
        # The pod name folds to the workload name in the ledger
        # (clean_workload_name): a ReplicaSet hash and a pod suffix.
        EVENT_KIND="${local.inject_kind}" EVENT_TYPE="${local.event_type}" EVENT_REASON="${var.event_reason}" \
          EVENT_NAMESPACE="${var.event_namespace}" EVENT_OBJECT_KIND="${local.object_kind}" EVENT_NAME="$workload-${local.pod_suffix}" \
          EVENT_UID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen | tr 'A-Z' 'a-z')" \
          EVENT_COUNT="${local.event_count}" EVENT_MESSAGE="${var.event_message}" EVENT_SEEN="$seen" \
          EVENT_CLUSTER="${var.host_cluster_name}" EVENT_PROJECT="$project" EVENT_LOCATION="${var.host_cluster_location}" \
          python3 "$envelope_py" > "$envelope_file"

        inject_status="$("$${exec_in_pod_stdin[@]}" sh -c \
          'curl -sS --max-time ${local.inject_curl_timeout} -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" -H "Content-Type: application/json" -H "${local.features_header}" --data-binary @- "$1" | jq -r ".status // empty"' \
          sh "${var.daemon_url}/sessions/$session_id/inject" < "$envelope_file" || true)"
        if [ "$inject_status" != "${local.filtered_reply}" ]; then
          echo "ERROR: the daemon answered '$inject_status' rather than '${local.filtered_reply}' for the $workload event (session $session_id). An Info event is held back at the severity gate; another answer means the daemon did not take the record as this stack sends it." >&2
          exit 1
        fi
        echo "Daemon recorded the ${var.event_reason} event for ${var.event_namespace}/$workload (session $session_id, $inject_status)."
      done

      # The case reads the tool's default page. Other producers write to the
      # same ledger, so prove both rows are on that page before handing off;
      # a miss here is this stack's failure, not the agent's.
      first_page="$("$${exec_in_pod[@]}" sh -c \
        'curl -sS --max-time ${local.curl_timeout} -H "Authorization: Bearer $SESSION_KV_API_KEY" "$1/v1/intercepted-events?limit=${local.first_page_limit}"' \
        sh "${var.daemon_url}" || true)"
      for workload in ${join(" ", var.workload_names)}; do
        if ! grep -q "\"name\": *\"$workload\"" <<<"$first_page"; then
          echo "ERROR: ${var.event_namespace}/$workload is not on the ledger's first ${local.first_page_limit} rows after the inject; the install's other producers outpaced this stack, so the case cannot grade the tool's default page." >&2
          exit 1
        fi
      done
      echo "Both rows are on the ledger's first ${local.first_page_limit} rows."
    EOT
  }
}

# Passed straight through, for the reason gitops-drift gives: devops-bench reads
# these after apply and points the ambient kubeconfig at the host cluster.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
