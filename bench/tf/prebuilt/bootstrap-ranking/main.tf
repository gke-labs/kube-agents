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

# The scenario driver for bench/tasks/bootstrap-inventory-ranking-delivery:
# plant a fixed INVENTORY.raw.md (inventory-raw.txt beside this file) on the
# shell sandbox's data volume, file the `bootstrap-inventory-prioritize` card
# the discovery sweep's worker files once it has written that file, and
# return once the card's worker has ended its run itself, or at `run_wait`
# with whatever it has written by then. The apply fails if no worker has
# picked the card up by then or no read of the board has succeeded.
#
# The raw report goes on the sandbox because that is where the sweep's worker
# writes it: a kanban worker's terminal and file tools both run there. Its
# owner is set to the data volume's, the user those tools run as.
#
# It refuses an install where a person has connected (`.user_aligned`) or
# onboarding already delivered (`.bootstrap_completed`): the ranked report
# this produces is the one onboarding delivers. It also refuses one whose
# gate has not filed its sweep (no `.bootstrap_scan_filed`), because a sweep
# filed during the run writes its own INVENTORY.raw.md over the planted one.
# Open `bootstrap-inventory-*` cards are archived before the plant, so an
# earlier sweep still running cannot do that either; the sweep marker stays,
# so the gate files no other.

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
  home     = "/opt/data"
  hermes   = "/opt/hermes/.venv/bin/hermes"
  python   = "/opt/hermes/.venv/bin/python3"
  key_like = "bootstrap-inventory-%"
  raw_file = "${local.home}/INVENTORY.raw.md"
  # Every file the prioritization stage and delivery write, on either pod.
  inventory = join(" ", [for name in [
    "INVENTORY.raw.md",
    "INVENTORY.md",
    "INVENTORY.md.tmp",
    "INVENTORY.items.json",
    "INVENTORY.scores.json",
    "INVENTORY.delivered.md",
  ] : "${local.home}/${name}"])
  # Base64, so the report and the card body cross two shells and kubectl
  # untouched.
  raw_b64 = base64encode(file("${path.module}/inventory-raw.txt"))
  # bootstrap_scan_gate.py's PRIORITIZE_IDEMPOTENCY_KEY and SCAN_ASSIGNEE, and
  # the card agents/platform/governance/inventory.md Step 5 tells the sweep's
  # worker to file, without the parent: this plant stands in for that worker.
  card_key      = "bootstrap-inventory-prioritize"
  card_assignee = "platform"
  card_title    = "Prioritize the onboarding inventory report"
  card_body_b64 = base64encode(<<-EOB
    Rank the onboarding discovery sweep's findings into the report the user receives.

    Follow the prioritization SOP, reading whichever of these exists:
      - /opt/data/profiles/platform/governance/inventory_prioritize_sop.md
      - /opt/platform-template/governance/inventory_prioritize_sop.md

    Read /opt/data/INVENTORY.raw.md as your only input, and write the ranked report to
    /opt/data/INVENTORY.md.
  EOB
  )
  run_wait = 900
  poll     = 15
  # deploy/docker/patches/kanban_guardrail_exit.py: RATE_LIMIT_REASON_PREFIX,
  # the start of the summary on a run the rate-limit guardrail blocked.
  rate_limit_block = "provider rate limit: API retries exhausted"
  # The exit trap's tries at listing the cards it archives, and the wait
  # between them: what failed the apply can fail one listing too.
  list_tries = 3
  list_wait  = 5
  # How long an exec into the agent Deployment waits for a pod when it has
  # none. A pod created in that time is not running yet and fails the exec
  # anyway, so kubectl's default of 60s only delays each failure by a minute.
  pod_wait = 5
}

resource "null_resource" "ranking" {
  triggers = {
    host_cluster      = var.host_cluster_name
    host_location     = var.host_cluster_location
    host_project      = var.project_id
    namespace         = var.agent_namespace
    deployment        = var.agent_deployment
    container         = var.agent_container
    sandbox_selector  = var.sandbox_selector
    sandbox_container = var.sandbox_container
    pod_wait          = local.pod_wait
    home              = local.home
    hermes            = local.hermes
    python            = local.python
    key_like          = local.key_like
    inventory         = local.inventory
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # Own kubeconfig: by the time this apply runs, an earlier tofu task may
      # have pointed the ambient context at its own cluster.
      kubeconfig_dir="$(mktemp -d)"

      # Terraform taints a resource whose create-time provisioner failed and
      # skips its destroy-time provisioners, so a failure after step 2 cleans
      # up here or the card keeps its dispatcher slot and the planted report
      # stays where the next sweep would find it. errexit stays in force
      # inside a trap, hence `set +e`.
      planted=""
      on_exit() {
        status=$?
        # A second signal would end the cleanup part-way.
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planted" ]; then
          echo "Plant failed (exit $status); archiving the bootstrap-inventory cards and removing the INVENTORY files." >&2
          failed=""
          tries=1
          until ids="$(open_cards)"; do
            if [ "$tries" -ge ${local.list_tries} ]; then
              ids=""
              failed="$failed, list the open cards"
              break
            fi
            sleep ${local.list_wait}
            tries=$((tries + 1))
          done
          for id in $ids; do
            agent ${local.hermes} kanban archive "$id" >&2 || failed="$failed, archive $id"
          done
          clear_inventory || failed="$failed, remove the INVENTORY files"
          if [ -n "$failed" ]; then
            echo "Cleanup incomplete: could not$${failed#,}. The next run's step 2 archives the cards and removes the files left behind." >&2
          fi
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
          -c "${var.agent_container}" --pod-running-timeout=${local.pod_wait}s -- "$@"
      }
      agent_py() {
        kubectl exec -i -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
          -c "${var.agent_container}" --pod-running-timeout=${local.pod_wait}s -- ${local.python} - "$@"
      }
      open_cards() {
        agent_py "${local.key_like}" <<'PY'
      import sqlite3, sys
      c = sqlite3.connect("file:${local.home}/kanban.db?mode=ro", uri=True)
      rows = c.execute("SELECT id FROM tasks WHERE idempotency_key LIKE ? AND status != 'archived' ORDER BY created_at DESC", (sys.argv[1],))
      print(" ".join(r[0] for r in rows))
      PY
      }
      # Every step runs and the status covers them all, so step 2 still stops
      # and the trap can name what it left. The listing is checked on its own
      # because a failure inside a `for` word list fails nothing.
      clear_inventory() {
        clear_status=0
        if sandbox_pods="$(kubectl get pods -n "${var.agent_namespace}" -l "${var.sandbox_selector}" -o name)"; then
          for pod in $sandbox_pods; do
            kubectl exec -n "${var.agent_namespace}" "$pod" -c "${var.sandbox_container}" -- rm -f ${local.inventory} || clear_status=1
          done
        else
          clear_status=1
        fi
        agent rm -f ${local.inventory} || clear_status=1
        return "$clear_status"
      }

      # ---- 1. Refuse an install where the report would reach a person -----
      # One read that has to answer `clear`, so a failed exec refuses rather
      # than reading as "no marker".
      state="$(agent_py <<'PY' || true
      import os
      home = "${local.home}"
      if os.path.exists(home + "/.user_aligned"):
          print("aligned")
      elif os.path.exists(home + "/.bootstrap_completed"):
          print("completed")
      elif not os.path.exists(home + "/.bootstrap_scan_filed"):
          print("unfiled")
      else:
          print("clear")
      PY
      )"
      case "$state" in
        clear) ;;
        aligned)
          echo "ERROR: ${local.home}/.user_aligned exists on ${var.host_cluster_name}: a person has connected, and the ranked report this case produces is the one onboarding delivers to their chat. Run this case on an install nobody is chatting with." >&2
          exit 1 ;;
        completed)
          echo "ERROR: onboarding already delivered on ${var.host_cluster_name} (${local.home}/.bootstrap_completed). Clearing the INVENTORY files would delete the report that was delivered." >&2
          exit 1 ;;
        unfiled)
          echo "ERROR: the onboarding gate on ${var.host_cluster_name} has not filed its discovery sweep (no ${local.home}/.bootstrap_scan_filed). A sweep filed during this case writes its own INVENTORY.raw.md over the planted one; wait for the gate to file, or run the case elsewhere." >&2
          exit 1 ;;
        *)
          echo "ERROR: could not read the onboarding markers on ${var.host_cluster_name} (got '$state')." >&2
          exit 1 ;;
      esac

      # One sandbox pod to plant on. Listed before step 2 changes anything,
      # since without one there is nothing this case can grade.
      sandbox_pods="$(kubectl get pods -n "${var.agent_namespace}" -l "${var.sandbox_selector}" -o name)"
      set -- $sandbox_pods
      if [ "$#" -ne 1 ]; then
        echo "ERROR: expected one shell sandbox pod matching ${var.sandbox_selector} in ${var.agent_namespace}, found $#. This case grades what the card's worker writes on the sandbox." >&2
        exit 1
      fi
      sandbox_pod="$1"

      # ---- 2. Clear the previous run -------------------------------------
      planted=1
      # Listed first: a failure inside a `for` word list fails nothing.
      ids="$(open_cards)"
      for id in $ids; do
        agent ${local.hermes} kanban archive "$id"
      done
      leftover="$(open_cards)"
      if [ -n "$leftover" ]; then
        echo "ERROR: bootstrap-inventory cards still open after archiving: $leftover. The create below would return one of them instead of filing a card." >&2
        exit 1
      fi
      clear_inventory

      # ---- 3. Plant the raw report on the sandbox ------------------------
      printf '%s' '${local.raw_b64}' | kubectl exec -i -n "${var.agent_namespace}" "$sandbox_pod" -c "${var.sandbox_container}" -- \
        sh -c 'base64 -d > "$1.tmp" && chown "$(stat -c %u:%g "$2")" "$1.tmp" && mv -f "$1.tmp" "$1"' sh "${local.raw_file}" "${local.home}"
      echo "Planted ${local.raw_file} on $sandbox_pod."

      # ---- 4. File the prioritization card -------------------------------
      card="$(agent_py "${local.card_body_b64}" <<'PY'
      import base64, json, subprocess, sys
      body = base64.b64decode(sys.argv[1]).decode()
      out = subprocess.run(
          ["${local.hermes}", "kanban", "create", "--json", "--assignee", "${local.card_assignee}",
           "--idempotency-key", "${local.card_key}", "--body", body, "${local.card_title}"],
          capture_output=True, text=True, check=True,
      ).stdout
      print(json.loads(out[out.find("{"):out.rfind("}") + 1])["id"])
      PY
      )"
      if [ -z "$card" ]; then
        echo "ERROR: the board returned no card id for ${local.card_key}." >&2
        exit 1
      fi
      echo "Filed prioritization card $card."

      # ---- 5. Wait for the card's worker to end its run ------------------
      # Only a run the worker ended itself, completed or blocked other than by
      # the rate-limit guardrail, counts: a run the guardrail blocked, that
      # timed out, or whose worker crashed is retried and has more to write.
      run_state() {
        agent_py "$card" "${local.rate_limit_block}" <<'PY'
      import sqlite3, sys
      card, rate_limit = sys.argv[1:3]
      c = sqlite3.connect("file:${local.home}/kanban.db?mode=ro", uri=True)
      started, ended = c.execute("SELECT count(*), count(ended_at) FROM task_runs WHERE task_id = ?", (card,)).fetchone()
      rows = c.execute("SELECT outcome, coalesce(summary, '') FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL", (card,))
      own = sum(o == "completed" or (o == "blocked" and not s.startswith(rate_limit)) for o, s in rows)
      print(started, ended, own)
      PY
      }
      # Only a read of three counts is used: a failed exec or query prints
      # nothing, and that is not a card with no runs. read_at is when the last
      # one succeeded.
      counts='^[0-9]+ [0-9]+ [0-9]+$'
      read_at=""
      read_run_state() {
        out="$(run_state)" || out=""
        if [[ "$out" =~ $counts ]]; then
          read -r started ended own <<<"$out"
          read_at=$elapsed
        fi
      }
      elapsed=0
      own=0
      read_run_state
      until [ "$own" -ge 1 ]; do
        if [ "$elapsed" -ge ${local.run_wait} ]; then
          if [ -z "$read_at" ]; then
            echo "ERROR: no read of card $card's runs from the board succeeded in $${elapsed}s, so there is nothing to grade. The read errors are above." >&2
            exit 1
          fi
          if [ "$read_at" -lt "$elapsed" ]; then
            echo "Board reads after $${read_at}s failed; what follows is from the read at $${read_at}s." >&2
          fi
          if [ "$started" -eq 0 ]; then
            echo "ERROR: no worker picked up card $card within $${read_at}s, so nothing ran the prioritization." >&2
            agent ${local.hermes} kanban show "$card" >&2 || true
            exit 1
          fi
          echo "Card $card has $ended ended run(s), none ended by its worker, after $${elapsed}s; handing over to the verifier."
          exit 0
        fi
        sleep ${local.poll}
        elapsed=$((elapsed + ${local.poll}))
        read_run_state
      done
      echo "Card $card's worker ended its run after $${elapsed}s."
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
      # A failed step does not skip the ones after it, and what failed is named
      # at the end: with on_failure = continue, Terraform counts this destroy
      # done whatever its exit status.
      failed=""
      ids="$(kubectl exec -i -n "$ns" "$target" -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
        ${self.triggers.python} - "${self.triggers.key_like}" <<'PY'
      import sqlite3, sys
      c = sqlite3.connect("file:${self.triggers.home}/kanban.db?mode=ro", uri=True)
      rows = c.execute("SELECT id FROM tasks WHERE idempotency_key LIKE ? AND status != 'archived' ORDER BY created_at DESC", (sys.argv[1],))
      print(" ".join(r[0] for r in rows))
      PY
      )" || failed="$failed, list the open cards"
      for id in $ids; do
        kubectl exec -n "$ns" "$target" -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.hermes} kanban archive "$id" || failed="$failed, archive $id"
      done
      kubectl exec -n "$ns" "$target" -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
        rm -f ${self.triggers.inventory} || failed="$failed, remove the agent's INVENTORY files"
      if sandbox_pods="$(kubectl get pods -n "$ns" -l "${self.triggers.sandbox_selector}" -o name)"; then
        for pod in $sandbox_pods; do
          kubectl exec -n "$ns" "$pod" -c "${self.triggers.sandbox_container}" -- \
            rm -f ${self.triggers.inventory} || failed="$failed, remove the INVENTORY files from $pod"
        done
      else
        failed="$failed, list the sandbox pods"
      fi
      if [ -n "$failed" ]; then
        echo "Cleanup incomplete: could not$${failed#,}. The next run's step 2 archives the cards and removes the files left behind." >&2
        exit 1
      fi
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
