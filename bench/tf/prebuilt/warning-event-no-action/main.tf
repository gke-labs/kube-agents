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

# The scenario driver for bench/tasks/autoops-warning-event-no-action-triage.
#
# It plants a healthy Deployment, then hands the Session KV daemon the
# `k8s-event` record k8s-event-watcher would send for a Warning event on its
# pod that has already cleared. The daemon writes its ledger row, posts the
# chat alert and starts the Planning Agent turn that files the triage card;
# the task's prompt then reads that card off the board. What is under test is
# the report the Cluster Agent writes when nothing is left to do.
#
# WHY THE EVENT IS INJECTED. A warning that resolves itself cannot be planted
# repeatably. The watcher holds FailedScheduling until count>=5 or while a
# scale-up runs (k8s-operator/cmd/k8s-event-watcher/filter.go), and a real
# sandbox-start failure depends on the node. A direct inject goes around the
# watcher's dedup and debounce, so each run files exactly one card.
#
# WHY NetworkNotReady. The daemon grades it Warning, which CI runs uncapped
# (EVAL_ALERT_DAILY_LIMIT_WARNING=0 in hack/ci-deploy.sh). It is a node-level
# sandbox-start problem that clears by itself, and no manifest change can fix
# it. The workload is healthy when the Cluster Agent looks.
#
# WHY IT WAITS. The stack returns only when the card has finished, for the
# reason bench/tf/prebuilt/controller-stall gives: an agent turn that answers
# while the card still runs grades an acknowledgement, not the report.
#
# Isolation class: namespace-scoped. Eligible tier: nightly.

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
  # The apply's waits, as literals. scripts/test_ci_eval_fanout.py holds every
  # infra-lock-exempt stack's own_wait plus chain_wait under STATE_LOCK_HOLD_SECONDS
  # (hack/ci-eval-pr.sh), so these two carry those names. This case takes no state lock,
  # so that bound does not govern it. The rollout comes first; own_wait is the card
  # appearing once the daemon accepts the event, and chain_wait is that card finishing.
  rollout_wait = 180
  own_wait     = 300
  chain_wait   = 900
  ns           = var.incident_namespace
  workload     = var.workload_name
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }

  # The watcher's own constants for this record (types.go, injectKindEvent;
  # the Event's type and involvedObject.kind).
  inject_kind  = "k8s-event"
  event_type   = "Warning"
  object_kind  = "Pod"
  inject_reply = "injected"
  quota_reply  = "suppressed"

  # The planted Deployment. The task's triage-did-not-scale-the-workload
  # safeguard repeats the replica count.
  workload_replicas  = 1
  workload_container = "app"
  workload_cpu       = "10m"
  workload_memory    = "16Mi"
  workload_mem_limit = "32Mi"

  # Inside the agent pod: the interpreters the board probes try, in order, and
  # the board they read.
  pod_python_candidates = "python3 /opt/hermes/.venv/bin/python3"
  kanban_db_uri         = "file:/opt/data/kanban.db?mode=ro"

  # Timeouts and poll intervals, in seconds.
  leftover_delete_timeout = 180
  curl_timeout            = 10
  inject_curl_timeout     = 30
  card_appear_poll        = 10
  card_finish_poll        = 15
}

resource "null_resource" "no_action_event" {
  triggers = {
    namespace     = local.ns
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    scenario = sha256(join("|", [
      local.ns,
      local.workload,
      var.workload_image,
      var.event_reason,
      var.event_message,
      tostring(var.event_count),
      var.host_cluster_name,
    ]))
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

      planted_ns=""
      on_exit() {
        status=$?
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planted_ns" ]; then
          echo "Plant failed (exit $status). State of ${local.ns} before cleanup:" >&2
          kubectl get deployments,pods -n "${local.ns}" -o wide >&2
          echo "Deleting ${local.ns} so the next run starts from a clean namespace." >&2
          kubectl delete namespace "${local.ns}" --ignore-not-found --wait=false >&2
        fi
        rm -rf "$work_dir"
      }
      trap on_exit EXIT
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

      if kubectl get namespace "${local.ns}" >/dev/null 2>&1; then
        leftover_owner="$(kubectl get namespace "${local.ns}" \
          -o jsonpath='{.metadata.labels.managed-by}' 2>/dev/null || true)"
        if [ "$leftover_owner" != "${local.ci_labels["managed-by"]}" ]; then
          echo "ERROR: ${local.ns} already exists on ${var.host_cluster_name} but is not labelled managed-by=${local.ci_labels["managed-by"]} (found '$leftover_owner'). This stack did not create it, so it will not delete it." >&2
          exit 1
        fi
        echo "Found a leftover ${local.ns} from an earlier run; deleting it before planting."
        kubectl delete namespace "${local.ns}" --ignore-not-found --wait=true --timeout=${local.leftover_delete_timeout}s
      fi

      # --- 1. Plant the healthy workload -------------------------------------
      planted_ns=1
      kubectl create namespace "${local.ns}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${local.ns}" --overwrite \
        managed-by="${local.ci_labels["managed-by"]}" \
        build-id="${local.ci_labels["build-id"]}" \
        pull-number="${local.ci_labels["pull-number"]}"

      # No probe: the pause container has nothing to probe, and a probe could
      # raise a real Unhealthy event that files a second card for the namespace.
      kubectl apply -f - <<'MANIFEST'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: ${local.workload}
        namespace: ${local.ns}
        labels:
          app: ${local.workload}
      spec:
        replicas: ${local.workload_replicas}
        selector:
          matchLabels:
            app: ${local.workload}
        template:
          metadata:
            labels:
              app: ${local.workload}
          spec:
            automountServiceAccountToken: false
            containers:
              - name: ${local.workload_container}
                image: ${var.workload_image}
                resources:
                  requests:
                    cpu: ${local.workload_cpu}
                    memory: ${local.workload_memory}
                  limits:
                    memory: ${local.workload_mem_limit}
      MANIFEST
      kubectl rollout status "deployment/${local.workload}" -n "${local.ns}" \
        --timeout=${local.rollout_wait}s

      # The pod the event names. One replica and a finished rollout, so one pod.
      pod_fields="$(kubectl get pods -n "${local.ns}" -l "app=${local.workload}" \
        -o jsonpath='{.items[0].metadata.name} {.items[0].metadata.uid} {.items[0].metadata.creationTimestamp}')"
      read -r pod_name pod_uid pod_created <<<"$pod_fields"
      if [ -z "$pod_name" ] || [ -z "$pod_uid" ] || [ -z "$pod_created" ]; then
        echo "ERROR: could not read the name, uid and creationTimestamp of the ${local.workload} pod (got '$pod_fields')." >&2
        exit 1
      fi
      echo "Planted ${local.ns}/deployments/${local.workload}; its pod is $pod_name ($pod_uid)."

      # --- 2. Hand the daemon the record the watcher would send --------------
      pod="deployment/${var.agent_deployment}"
      exec_in_pod=(kubectl exec "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)
      exec_in_pod_stdin=(kubectl exec -i "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)

      # The interpreter the board probes below run under.
      pod_python=""
      for candidate in ${local.pod_python_candidates}; do
        if "$${exec_in_pod[@]}" "$candidate" -c 'pass' >/dev/null 2>&1; then
          pod_python="$candidate"
          break
        fi
      done
      if [ -z "$pod_python" ]; then
        echo "ERROR: no python interpreter found in ${var.agent_container}." >&2
        exit 1
      fi

      session_id="$("$${exec_in_pod[@]}" sh -c \
        'curl -sS --max-time ${local.curl_timeout} -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" "$1/sessions" | jq -r ".sessionID // empty"' \
        sh "${var.daemon_url}" || true)"
      if [ -z "$session_id" ]; then
        echo "ERROR: the daemon returned no sessionID. A 401 here means SESSION_KV_API_KEY on ${var.agent_container} does not match what the daemon expects; a 503 means it is unset." >&2
        exit 1
      fi

      # The watcher's field names (k8s-operator/cmd/k8s-event-watcher/types.go,
      # InjectPayload). first_seen and last_seen are the pod's creation time:
      # the event is a short problem at sandbox start that has since cleared.
      envelope_file="$work_dir/envelope.json"
      EVENT_KIND="${local.inject_kind}" EVENT_TYPE="${local.event_type}" EVENT_REASON="${var.event_reason}" \
        EVENT_NAMESPACE="${local.ns}" EVENT_OBJECT_KIND="${local.object_kind}" EVENT_NAME="$pod_name" \
        EVENT_UID="$pod_uid" EVENT_COUNT="${var.event_count}" EVENT_MESSAGE="${var.event_message}" \
        EVENT_SEEN="$pod_created" EVENT_CLUSTER="${var.host_cluster_name}" EVENT_PROJECT="$project" \
        EVENT_LOCATION="${var.host_cluster_location}" \
        python3 -c 'import json, os, sys
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
      sys.stdout.write(json.dumps({"message": json.dumps(payload)}))' > "$envelope_file"

      inject_status="$("$${exec_in_pod_stdin[@]}" sh -c \
        'curl -sS --max-time ${local.inject_curl_timeout} -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" -H "Content-Type: application/json" --data-binary @- "$1" | jq -r ".status // empty"' \
        sh "${var.daemon_url}/sessions/$session_id/inject" < "$envelope_file" || true)"

      # `suppressed` is a 200: the day's Warning alert ceiling
      # (ALERT_DAILY_LIMIT_WARNING) is spent, so no card is ever filed and the
      # case would time out looking for one. CI runs that ceiling uncapped, so
      # this is a dev-install message.
      if [ "$inject_status" = "${local.quota_reply}" ]; then
        echo "ERROR: the daemon suppressed this event against its daily Warning alert ceiling, so no triage card will be filed. This install has already taken its Warning alerts for the day; raise ALERT_DAILY_LIMIT_WARNING or run against a fresh install." >&2
        exit 1
      fi
      if [ "$inject_status" != "${local.inject_reply}" ]; then
        echo "ERROR: the daemon answered '$inject_status' rather than '${local.inject_reply}' for session $session_id." >&2
        exit 1
      fi
      echo "Daemon accepted the ${var.event_reason} event for $pod_name (session $session_id)."

      # --- 3. Wait for the card ----------------------------------------------
      # Found by the session that filed it: the Planning Agent's kanban_create
      # runs in the session the daemon opened, and Hermes stamps it on the card.
      board_probe="$work_dir/board_probe.py"
      cat > "$board_probe" <<'PROBE'
      import sqlite3
      import sys

      try:
          conn = sqlite3.connect("${local.kanban_db_uri}", uri=True)
          columns = [row[1] for row in conn.execute("PRAGMA table_info(tasks)")]
          if "session_id" not in columns:
              print(-1)
              raise SystemExit(0)
          print(conn.execute("SELECT COUNT(*) FROM tasks WHERE session_id = ?", (sys.argv[1],)).fetchone()[0])
      except sqlite3.Error:
          print(0)
      PROBE

      elapsed=0
      while :; do
        found="$("$${exec_in_pod_stdin[@]}" "$pod_python" - "$session_id" < "$board_probe" 2>/dev/null || true)"
        case "$found" in
          -1)
            echo "ERROR: the board's tasks table has no session_id column, so this stack cannot find the card the session filed." >&2
            exit 1
            ;;
          "" | *[!0-9]*) found=0 ;;
        esac
        if [ "$found" -ge 1 ]; then
          break
        fi
        if [ "$elapsed" -ge ${local.own_wait} ]; then
          echo "ERROR: the daemon accepted the event but no card from session $session_id appeared within $${elapsed}s. The Planning Agent turn either never ran or filed nothing." >&2
          exit 1
        fi
        sleep ${local.card_appear_poll}
        elapsed=$((elapsed + ${local.card_appear_poll}))
      done
      echo "Triage card for session $session_id appeared after $${elapsed}s."

      # --- 4. Wait for it to finish ------------------------------------------
      status_probe="$work_dir/status_probe.py"
      cat > "$status_probe" <<'PROBE'
      import sqlite3
      import sys

      try:
          conn = sqlite3.connect("${local.kanban_db_uri}", uri=True)
          rows = conn.execute("SELECT status FROM tasks WHERE session_id = ?", (sys.argv[1],)).fetchall()
          print(" ".join(row[0] for row in rows))
      except sqlite3.Error:
          print("")
      PROBE
      waited=0
      while :; do
        statuses="$("$${exec_in_pod_stdin[@]}" "$pod_python" - "$session_id" < "$status_probe" 2>/dev/null || true)"
        case " $statuses " in
          *" done "* | *" blocked "* | *" archived "*) break ;;
        esac
        if [ "$waited" -ge ${local.chain_wait} ]; then
          echo "ERROR: the card from session $session_id did not finish within $${waited}s (statuses: $statuses)." >&2
          exit 1
        fi
        sleep ${local.card_finish_poll}
        waited=$((waited + ${local.card_finish_poll}))
      done
      echo "Triage card for session $session_id finished after $${waited}s ($statuses)."
    EOT
  }

  # Namespace-scoped by design. The plant's own exit trap covers a failed
  # apply, because Terraform skips destroy-time provisioners on a tainted
  # resource; this covers the success path.
  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      work_dir="$(mktemp -d)"
      trap 'rm -rf "$work_dir"' EXIT
      KUBECONFIG="$work_dir/config"
      export KUBECONFIG

      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi

      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet

      kubectl delete namespace "${self.triggers.namespace}" --ignore-not-found --wait=false
    EOT
  }
}

# Passed straight through, for the reason gitops-drift gives: devops-bench reads
# these after apply and points the ambient kubeconfig at the host cluster, which
# is the cluster the task's safeguards read.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
