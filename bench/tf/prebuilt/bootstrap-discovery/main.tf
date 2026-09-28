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

# The scenario driver for bench/tasks/bootstrap-discovery-fanout: re-arm the
# onboarding discovery gate on the install under test, wait for the
# `bootstrap-inventory-scan` cron job to file a fresh sweep card, and return
# once the sweep's worker has filed cards and ended the run that filed them --
# by then it has filed whatever Cluster Agent cards it is going to file, which
# is what the case grades. It does not wait for those cards to finish: the
# destroy archives them, and archiving a running card ends its worker.
#
# Re-arming is the runbook in agents/chat/defaults/plugins/bootstrap_onboarding/
# README.md §5 plus one step: the board deduplicates kanban_create on
# idempotency key against every card that is not archived, so the previous
# run's `bootstrap-inventory-*` cards are archived first, or the gate's
# create returns the old card and no sweep runs. Newest first: archiving a
# card promotes whatever was waiting on it, so the sweep goes last or its
# prioritize card is dispatched in between.
#
# It refuses an install where a person has connected (`.user_aligned`) or
# onboarding already delivered (`.bootstrap_completed`): a fresh sweep there
# ends in a report sent to a real chat.

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
  home      = "/opt/data"
  hermes    = "/opt/hermes/.venv/bin/hermes"
  python    = "/opt/hermes/.venv/bin/python3"
  key_like  = "bootstrap-inventory-%"
  file_wait = 600
  run_wait  = 900
  poll      = 15
  inventory = "${local.home}/INVENTORY.raw.md ${local.home}/INVENTORY.md"
  # The gate as the cron job launches it, and the longest one run of it can
  # take: bootstrap_scan_gate.py's RECONCILE_TIMEOUT_SECONDS (240) plus one
  # cron tick.
  gate_script = "bootstrap_scan_gate.py"
  gate_wait   = 300
}

resource "null_resource" "sweep" {
  triggers = {
    host_cluster      = var.host_cluster_name
    host_location     = var.host_cluster_location
    host_project      = var.project_id
    namespace         = var.agent_namespace
    deployment        = var.agent_deployment
    container         = var.agent_container
    sandbox_selector  = var.sandbox_selector
    sandbox_container = var.sandbox_container
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # Own kubeconfig: by the time this apply runs, an earlier tofu task may
      # have pointed the ambient context at its own cluster.
      kubeconfig_dir="$(mktemp -d)"

      # Terraform taints a resource whose create-time provisioner failed and
      # skips its destroy-time provisioners, so a failure after the re-arm
      # cleans up here or the sweep's cards keep their dispatcher slots.
      # errexit stays in force inside a trap, hence `set +e`.
      rearmed=""
      on_exit() {
        status=$?
        set +e
        if [ "$status" -ne 0 ] && [ -n "$rearmed" ]; then
          echo "Plant failed (exit $status); archiving the bootstrap-inventory cards it left open." >&2
          # The gate files only while this marker is absent, and a sweep filed
          # after this exit would run with nothing to archive it, so the
          # marker goes back before the cards are listed. A gate run that read
          # the marker before it went back files once its reconcile ends, so
          # the listing also waits for any gate run to exit.
          agent sh -c 'test -e ${local.home}/.bootstrap_scan_filed || echo "task_id=$1" > ${local.home}/.bootstrap_scan_filed' sh "$${old_id:-none}" >&2
          waited=0
          until [ "$(gate_running)" = idle ] || [ "$waited" -ge ${local.gate_wait} ]; do
            sleep 5
            waited=$((waited + 5))
          done
          for id in $(open_cards); do
            agent ${local.hermes} kanban archive "$id" >&2
          done
          clear_inventory
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT
      # bash skips the EXIT trap when an untrapped signal kills it, and a
      # Prow deadline arrives as SIGTERM.
      trap 'exit 143' TERM INT
      KUBECONFIG="$kubeconfig_dir/config"
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

      agent() {
        kubectl exec -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
          -c "${var.agent_container}" -- "$@"
      }
      agent_py() {
        kubectl exec -i -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
          -c "${var.agent_container}" -- ${local.python} - "$@"
      }
      open_cards() {
        agent_py "${local.key_like}" <<'PY'
      import sqlite3, sys
      c = sqlite3.connect("file:${local.home}/kanban.db?mode=ro", uri=True)
      rows = c.execute("SELECT id FROM tasks WHERE idempotency_key LIKE ? AND status != 'archived' ORDER BY created_at DESC", (sys.argv[1],))
      print(" ".join(r[0] for r in rows))
      PY
      }
      # Anything but `idle`, a failed exec included, reads as running. Every
      # pod is checked because above one replica only the leader runs cron.
      gate_running() {
        selector="$(kubectl get deployment -n "${var.agent_namespace}" "${var.agent_deployment}" \
          -o go-template='{{range $k, $v := .spec.selector.matchLabels}}{{$k}}={{$v}},{{end}}' || true)"
        pods=""
        if [ -n "$selector" ]; then
          pods="$(kubectl get pods -n "${var.agent_namespace}" -l "$${selector%,}" -o name || true)"
        fi
        if [ -z "$pods" ]; then
          echo running
          return
        fi
        for pod in $pods; do
          state="$(kubectl exec -i -n "${var.agent_namespace}" "$pod" -c "${var.agent_container}" -- \
            ${local.python} - "${local.gate_script}" <<'PY' || true
      import os, sys
      me = os.getpid()
      for pid in filter(str.isdigit, os.listdir("/proc")):
          try:
              with open("/proc/%s/cmdline" % pid, "rb") as fh:
                  argv = fh.read().decode(errors="replace").split("\0")
          except OSError:
              continue
          if int(pid) != me and any(os.path.basename(a) == sys.argv[1] for a in argv[1:]):
              print("running")
              break
      else:
          print("idle")
      PY
      )"
          if [ "$state" != idle ]; then
            echo running
            return
          fi
        done
        echo idle
      }
      sweep_id() {
        agent sh -c 'sed -n "s/^task_id=//p" ${local.home}/.bootstrap_scan_filed 2>/dev/null; true' || true
      }
      clear_inventory() {
        for pod in $(kubectl get pods -n "${var.agent_namespace}" -l "${var.sandbox_selector}" -o name); do
          kubectl exec -n "${var.agent_namespace}" "$pod" -c "${var.sandbox_container}" -- rm -f ${local.inventory}
        done
        agent rm -f ${local.inventory}
      }

      # ---- 1. Refuse an install where the sweep would reach a person -------
      # One read that has to answer `clear`, so a failed exec refuses rather
      # than reading as "no marker".
      state="$(agent sh -c 'if [ -e ${local.home}/.user_aligned ]; then echo aligned; elif [ -e ${local.home}/.bootstrap_completed ]; then echo completed; elif grep -q "\"bootstrap-inventory-scan\"" ${local.home}/cron/jobs.json 2>/dev/null; then echo clear; else echo nojob; fi' || true)"
      case "$state" in
        clear) ;;
        aligned)
          echo "ERROR: ${local.home}/.user_aligned exists on ${var.host_cluster_name}: a person has connected, and the report a fresh sweep writes would be delivered to their chat. Run this case on an install nobody is chatting with." >&2
          exit 1 ;;
        completed)
          echo "ERROR: onboarding already delivered on ${var.host_cluster_name} (${local.home}/.bootstrap_completed), and delivery removed the bootstrap-inventory-scan job with it. There is no gate left to re-arm." >&2
          exit 1 ;;
        nojob)
          echo "ERROR: the bootstrap-inventory-scan cron job is not in ${local.home}/cron/jobs.json on ${var.host_cluster_name}, so nothing will file a sweep." >&2
          exit 1 ;;
        *)
          echo "ERROR: could not read the onboarding markers on ${var.host_cluster_name} (got '$state')." >&2
          exit 1 ;;
      esac

      # ---- 2. Clear the previous sweep -------------------------------------
      old_id="$(sweep_id)"
      rearmed=1
      for id in $(open_cards); do
        agent ${local.hermes} kanban archive "$id"
      done
      leftover="$(open_cards)"
      if [ -n "$leftover" ]; then
        echo "ERROR: bootstrap-inventory cards still open after archiving: $leftover. The gate's create would return one of them instead of filing a sweep." >&2
        exit 1
      fi
      clear_inventory
      agent rm -f "${local.home}/.bootstrap_scan_filed" "${local.home}/.bootstrap_reconcile_attempts"
      echo "Re-armed discovery (previous sweep card: $${old_id:-none})."

      # ---- 3. Wait for the gate to file a new sweep ------------------------
      # The gate runs the Cluster Agent reconcile to completion first, so
      # this is one cron tick plus however long that takes.
      elapsed=0
      sweep="$(sweep_id)"
      until [ -n "$sweep" ] && [ "$sweep" != "$old_id" ]; do
        if [ "$elapsed" -ge ${local.file_wait} ]; then
          echo "ERROR: the gate filed no sweep card within $${elapsed}s of re-arming. Onboarding markers on the agent:" >&2
          agent sh -c 'ls -la ${local.home}/.bootstrap* 2>&1; cat ${local.home}/.bootstrap_reconcile_attempts 2>/dev/null' >&2 || true
          exit 1
        fi
        sleep 10
        elapsed=$((elapsed + 10))
        sweep="$(sweep_id)"
      done
      echo "The gate filed sweep card $sweep after $${elapsed}s."

      # ---- 4. Wait for the sweep worker to file its cards -----------------
      # The worker blocks to wait for its children or completes once it has
      # filed them, and either ends its run. A run can also end before the
      # worker files anything -- a rate-limit block, retries exhausted, a
      # crashed worker reclaimed and re-dispatched -- so only a run that ended
      # after the sweep's newest card was filed counts. Counting ended runs
      # rather than reading the card's status cannot miss a block the
      # dispatcher lifts between two polls. The board creates
      # kanban_worker_children when a worker first files a card.
      run_state() {
        agent_py "$sweep" <<'PY'
      import sqlite3, sys
      c = sqlite3.connect("file:${local.home}/kanban.db?mode=ro", uri=True)
      runs = c.execute("SELECT count(*), count(ended_at) FROM task_runs WHERE task_id = ?", (sys.argv[1],)).fetchone()
      filed, newest, after = 0, None, 0
      if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kanban_worker_children'").fetchone():
          filed, newest = c.execute("SELECT count(*), max(created_at) FROM kanban_worker_children WHERE creator_id = ?", (sys.argv[1],)).fetchone()
      if newest is not None:
          after = c.execute("SELECT count(*) FROM task_runs WHERE task_id = ? AND ended_at >= ?", (sys.argv[1], newest)).fetchone()[0]
      print(runs[0], runs[1], filed, after)
      PY
      }
      elapsed=0
      read -r started ended filed after <<<"$(run_state)"
      until [ "$${after:-0}" -ge 1 ]; do
        if [ "$elapsed" -ge ${local.run_wait} ]; then
          if [ "$${started:-0}" -eq 0 ]; then
            echo "ERROR: no worker picked up sweep card $sweep within $${elapsed}s, so there is no fan-out to grade." >&2
            agent ${local.hermes} kanban show "$sweep" >&2 || true
            exit 1
          fi
          echo "Sweep card $sweep has $${ended:-0} ended run(s) and $${filed:-0} filed card(s) after $${elapsed}s; handing over to the verifier."
          exit 0
        fi
        sleep ${local.poll}
        elapsed=$((elapsed + ${local.poll}))
        read -r started ended filed after <<<"$(run_state)"
      done
      echo "Sweep card $sweep filed $filed card(s) and ended a run after $${elapsed}s."
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet

      ns="${self.triggers.namespace}"
      target="deployment/${self.triggers.deployment}"
      ids="$(kubectl exec -i -n "$ns" "$target" -c "${self.triggers.container}" -- \
        /opt/hermes/.venv/bin/python3 - "bootstrap-inventory-%" <<'PY'
      import sqlite3, sys
      c = sqlite3.connect("file:/opt/data/kanban.db?mode=ro", uri=True)
      rows = c.execute("SELECT id FROM tasks WHERE idempotency_key LIKE ? AND status != 'archived' ORDER BY created_at DESC", (sys.argv[1],))
      print(" ".join(r[0] for r in rows))
      PY
      )"
      for id in $ids; do
        kubectl exec -n "$ns" "$target" -c "${self.triggers.container}" -- \
          /opt/hermes/.venv/bin/hermes kanban archive "$id" || true
      done
      # The sweep marker stays, so the gate does not file again once this
      # case is gone.
      kubectl exec -n "$ns" "$target" -c "${self.triggers.container}" -- \
        rm -f /opt/data/INVENTORY.raw.md /opt/data/INVENTORY.md
      for pod in $(kubectl get pods -n "$ns" -l "${self.triggers.sandbox_selector}" -o name); do
        kubectl exec -n "$ns" "$pod" -c "${self.triggers.sandbox_container}" -- \
          rm -f /opt/data/INVENTORY.raw.md /opt/data/INVENTORY.md || true
      done
    EOT
  }
}

# Passed straight through: devops-bench reads these after apply and points the
# ambient kubeconfig at cluster_name.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
