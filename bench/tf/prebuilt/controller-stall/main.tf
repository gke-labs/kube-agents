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

# The scenario driver for bench/tasks/autoops-controller-stall-triage.
#
# It plants a Deployment that stalls without erroring, waits until the image's
# own stall_report.py reports it, and hands the Session KV daemon the
# `controller-stall` record the stall watch would send for it. The daemon writes
# its ledger row, posts the chat alert and starts the Planning Agent turn that
# files the triage card; the task's prompt then reads that card off the board.
# What is under test is the daemon end of the chain and everything downstream.
#
# WHY A READINESS GATE. The stall is a pod readiness gate whose condition nothing
# sets, so the pod runs and never turns Ready, and the Deployment's Available and
# Progressing conditions stay False. No Warning event is raised for it, so
# k8s-event-watcher files nothing for the namespace. A missing ConfigMap would
# be the more familiar stall, but its pods raise `Failed` events the watcher
# acts on within seconds, and the prompt's "most recent card concerning that
# namespace" would then have another candidate. (The install's own stall-watch
# tick can still raise one once the threshold passes; the task header says why
# the checks hold either way.)
#
# WHY THE RECORD IS POSTED RATHER THAN OBSERVED. A real stall-watch tick sweeps
# every namespace of every cluster on the Cluster Agent roster and opens up to
# three alerts for whatever it finds there, the seeded fleet's planted defects
# included. Posting the one record keeps the case to its own namespace. The
# record is built from a real scan, so it carries the rows the watch would send.
#
# WHY IT WAITS. stall_report.py reports a Deployment only once it has been stuck
# for ten minutes. The Cluster Agent runs the same scan when it works the card,
# so a record sent sooner would describe a stall the agent's own scan does not
# yet show. It then waits for the card to finish as well (step 5 says why).
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
  ns       = var.stall_namespace
  workload = var.workload_name
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

resource "null_resource" "stall" {
  triggers = {
    namespace     = local.ns
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    scenario = sha256(join("|", [
      local.ns,
      local.workload,
      var.readiness_gate,
      tostring(var.progress_deadline_seconds),
      var.host_cluster_name,
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      work_dir="$(mktemp -d)"
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

      # A kubeconfig of its own, for the reason gitops-drift gives: an earlier
      # tofu task in the matrix may have left the ambient context elsewhere.
      KUBECONFIG="$work_dir/config"
      export KUBECONFIG

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
        kubectl delete namespace "${local.ns}" --ignore-not-found --wait=true --timeout=180s
      fi

      # --- 1. Plant the stall ------------------------------------------------
      planted_ns=1
      kubectl create namespace "${local.ns}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${local.ns}" --overwrite \
        managed-by="${local.ci_labels["managed-by"]}" \
        build-id="${local.ci_labels["build-id"]}" \
        pull-number="${local.ci_labels["pull-number"]}"

      kubectl apply -f - <<'MANIFEST'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: ${local.workload}
        namespace: ${local.ns}
        labels:
          app: ${local.workload}
      spec:
        replicas: 1
        progressDeadlineSeconds: ${var.progress_deadline_seconds}
        selector:
          matchLabels:
            app: ${local.workload}
        template:
          metadata:
            labels:
              app: ${local.workload}
          spec:
            automountServiceAccountToken: false
            readinessGates:
              - conditionType: ${var.readiness_gate}
            containers:
              - name: app
                image: busybox:1.36
                command: ["sh", "-c", "sleep 3600"]
                resources:
                  requests:
                    cpu: 10m
                    memory: 16Mi
                  limits:
                    memory: 32Mi
      MANIFEST
      first_seen="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
      echo "Planted ${local.ns}/deployments/${local.workload}, gated on ${var.readiness_gate}."

      # --- 2. Wait for the image's own scan to report it ---------------------
      pod="deployment/${var.agent_deployment}"
      exec_in_pod=(kubectl exec "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)
      exec_in_pod_stdin=(kubectl exec -i "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)

      # The deployed image's copy, the one stall_watch.py hands the sandbox, run
      # here with this stack's kubeconfig because the agent container has no
      # kubectl.
      report_src="$work_dir/stall_report.py"
      "$${exec_in_pod[@]}" cat /opt/defaults/scripts/stall_report.py > "$report_src"

      # The rows the stall watch would put in the record: the scan's Deployment
      # findings, without their detail, which the watch keeps out of the record.
      rows_file="$work_dir/rows.json"
      elapsed=0
      while :; do
        python3 -I "$report_src" --namespace "${local.ns}" --kind deployments --json 2>"$work_dir/scan.err" \
          | python3 -c 'import json, sys
      want = "Deployment/" + sys.argv[1]
      try:
          findings = json.load(sys.stdin).get("findings") or []
      except ValueError:
          findings = []
      rows = [{"object": f["object"], "heuristic": f["heuristic"], "stalled_for": f["stalled_for"]} for f in findings if f.get("object") == want]
      json.dump(rows, sys.stdout)' "${local.workload}" > "$rows_file" || echo '[]' > "$rows_file"
        if [ "$(cat "$rows_file")" != "[]" ]; then
          break
        fi
        if [ "$elapsed" -ge ${var.scan_ceiling_seconds} ]; then
          echo "ERROR: stall_report.py did not report Deployment/${local.workload} within $${elapsed}s. Its last stderr:" >&2
          cat "$work_dir/scan.err" >&2
          kubectl get deployment "${local.workload}" -n "${local.ns}" -o jsonpath='{.status.conditions}' >&2
          exit 1
        fi
        sleep 30
        elapsed=$((elapsed + 30))
      done
      echo "stall_report.py reports the Deployment after $${elapsed}s: $(cat "$rows_file")"

      # --- 3. Hand the daemon the record the stall watch would send ----------
      # Warn rather than stop when the kind is not advertised: on such a daemon
      # the record takes the event path, which is the red this case exists to
      # show, and a stack that refused to run there could not show it.
      kinds="$("$${exec_in_pod[@]}" sh -c 'curl -sS --max-time 10 "$1/healthz" | jq -r ".inject_kinds[]?"' sh "${var.daemon_url}" || true)"
      if ! printf '%s\n' "$kinds" | grep -qxF 'controller-stall'; then
        echo "WARNING: the Session KV daemon does not advertise the 'controller-stall' inject kind (it advertises: $kinds); the record will take the event path." >&2
      fi

      # The Cluster Agent profile the watch would name, resolved the way it
      # resolves it, and dropped if the profile is not there: the daemon then
      # tells the Planning Agent to find the cluster's agent itself.
      pod_python=""
      for candidate in python3 /opt/hermes/.venv/bin/python3; do
        if "$${exec_in_pod[@]}" "$candidate" -c 'pass' >/dev/null 2>&1; then
          pod_python="$candidate"
          break
        fi
      done
      if [ -z "$pod_python" ]; then
        echo "ERROR: no python interpreter found in ${var.agent_container}." >&2
        exit 1
      fi
      assignee="$("$${exec_in_pod[@]}" "$pod_python" -c 'import os, sys
      sys.path.insert(0, "/opt/defaults/scripts")
      from cluster_agent_profile import profile_name
      name = profile_name(*sys.argv[1:4])
      print(name if os.path.isdir(os.path.join("/opt/data/profiles", name)) else "")' \
        "$project" "${var.host_cluster_name}" "${var.host_cluster_location}" 2>/dev/null || true)"

      session_id="$("$${exec_in_pod[@]}" sh -c \
        'curl -sS --max-time 10 -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" "$1/sessions" | jq -r ".sessionID // empty"' \
        sh "${var.daemon_url}" || true)"
      if [ -z "$session_id" ]; then
        echo "ERROR: the daemon returned no sessionID. A 401 here means SESSION_KV_API_KEY on ${var.agent_container} does not match what the daemon expects; a 503 means it is unset." >&2
        exit 1
      fi

      envelope_file="$work_dir/envelope.json"
      STALL_CLUSTER="${var.host_cluster_name}" STALL_PROJECT="$project" STALL_LOCATION="${var.host_cluster_location}" \
        STALL_NAMESPACE="${local.ns}" STALL_ASSIGNEE="$assignee" STALL_FIRST_SEEN="$first_seen" \
        python3 -c 'import json, os, sys
      payload = {
          "kind": "controller-stall",
          "cluster": os.environ["STALL_CLUSTER"],
          "project": os.environ["STALL_PROJECT"],
          "location": os.environ["STALL_LOCATION"],
          "namespace": os.environ["STALL_NAMESPACE"],
          "first_seen": os.environ["STALL_FIRST_SEEN"],
          "objects": json.load(open(sys.argv[1])),
      }
      if os.environ["STALL_ASSIGNEE"]:
          payload["assignee"] = os.environ["STALL_ASSIGNEE"]
      sys.stdout.write(json.dumps({"message": json.dumps(payload)}))' "$rows_file" > "$envelope_file"

      inject_status="$("$${exec_in_pod_stdin[@]}" sh -c \
        'curl -sS --max-time 30 -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" -H "Content-Type: application/json" --data-binary @- "$1" | jq -r ".status // empty"' \
        sh "${var.daemon_url}/sessions/$session_id/inject" < "$envelope_file" || true)"
      if [ "$inject_status" != "injected" ]; then
        echo "ERROR: the daemon answered '$inject_status' rather than 'injected' for session $session_id." >&2
        exit 1
      fi
      echo "Daemon accepted the stall record (session $session_id, assignee '$assignee')."

      # --- 4. Wait for the card ----------------------------------------------
      # Found by the session that filed it: the Planning Agent's kanban_create
      # runs in the session the daemon opened, and Hermes stamps it on the card.
      board_probe="$work_dir/board_probe.py"
      cat > "$board_probe" <<'PROBE'
      import sqlite3
      import sys

      try:
          conn = sqlite3.connect("file:/opt/data/kanban.db?mode=ro", uri=True)
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
        if [ "$elapsed" -ge 300 ]; then
          echo "ERROR: the daemon accepted the record but no card from session $session_id appeared within $${elapsed}s. The Planning Agent turn either never ran or filed nothing." >&2
          exit 1
        fi
        sleep 10
        elapsed=$((elapsed + 10))
      done
      echo "Triage card for session $session_id appeared after $${elapsed}s."

      # --- 5. Wait for it to finish ------------------------------------------
      # Unlike gitops-drift, which leaves this wait to the agent turn: the stall
      # card runs a namespace scan first and takes minutes, and an agent turn
      # that answers while the card is still running grades an acknowledgement
      # instead of the pipeline's report. The case measures the pipeline, so the
      # stack hands the turn a finished card.
      status_probe="$work_dir/status_probe.py"
      cat > "$status_probe" <<'PROBE'
      import sqlite3
      import sys

      try:
          conn = sqlite3.connect("file:/opt/data/kanban.db?mode=ro", uri=True)
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
        if [ "$waited" -ge ${var.card_ceiling_seconds} ]; then
          echo "ERROR: the card from session $session_id did not finish within $${waited}s (statuses: $statuses)." >&2
          exit 1
        fi
        sleep 15
        waited=$((waited + 15))
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
