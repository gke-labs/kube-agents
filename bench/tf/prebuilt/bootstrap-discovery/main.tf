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

# The scenario driver for bench/tasks/bootstrap-discovery-fanout and
# bench/tasks/bootstrap-discovery-capped: re-arm the onboarding discovery gate on the install under test, wait for the
# `bootstrap-inventory-scan` cron job to file a fresh sweep card, then wait for
# the hand-off to the ranking stage: return once the hand-off's own card keyed
# `bootstrap-inventory-prioritize` is on the board, or once the sweep and its
# Cluster Agent cards have all settled without one for `settle_hold`, or at
# `handoff_wait`. Fail the apply if no worker has picked the sweep up by
# `run_wait` or no read of the board has succeeded. The destroy archives every
# card, and archiving a running card ends its worker.
#
# Re-arming is the runbook in agents/chat/defaults/plugins/bootstrap_onboarding/
# README.md §5: the previous run's `bootstrap-inventory-*` cards are archived,
# newest first, and then the markers are removed.
#
# With `first_run_cluster_cards` set, step 2 first writes it to the agent's
# `.first_run_cluster_cards`, the gate's first-run cluster card cap (#3702), so
# the sweep the re-arm brings on is filed under it, after a sentinel beside it
# (`.first_run_cluster_cards.bench`) holding the same value, which marks the cap
# as this stack's. The exit trap and the destroy remove the cap and then the
# sentinel; a cap whose removal failed keeps its sentinel, and every run's step
# 2, capped or not, first removes a cap its sentinel matches, and a sentinel
# with no cap or beside a cap of another value on its own. A cap file without a
# matching sentinel is an operator's setting, and it is neither overwritten nor
# removed: a capped run refuses the install, and an uncapped run says its sweep
# is bounded by that cap.
#
# It refuses an install where a person has connected (`.user_aligned`) or
# onboarding already delivered (`.bootstrap_completed`): a fresh sweep there
# ends in a report sent to a real chat. It also refuses one whose `oobe` job,
# which runs the scan, is missing or paused, where nothing would file the sweep.

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
  # The longest one run of the gate can
  # take at the default scope cap, which is what this stack installs:
  # bootstrap_scan_gate.py's RECONCILE_TIMEOUT_SECONDS (390) plus one cron
  # tick. A declared spec.scope.maxProjects raises the gate's ceiling with
  # the reconcile's budget.
  # The processes the gate runs in: oobe.py, whose scan stage calls it in-process, and the
  # gate script itself, which the old scan job still runs on an install with no oobe job.
  gate_script = "oobe.py bootstrap_scan_gate.py"
  gate_wait   = 450
  # The job whose scan stage files the sweep (agents/chat/scripts/oobe.py); the old
  # bootstrap-inventory-scan entry ships disabled.
  scan_job    = "oobe"
  # bootstrap_scan_gate.py's CLUSTER_IDEMPOTENCY_KEY_PREFIX.
  cluster_key_like = "bootstrap-inventory-cluster-%"
  # bootstrap_scan_gate.py's PRIORITIZE_IDEMPOTENCY_KEY: the ranking card the
  # hand-off files.
  prioritize_key = "bootstrap-inventory-prioritize"
  # The hand-off waits on the sweep's worker and on every Cluster Agent card
  # the gate filed, each a full single-cluster audit, so it is given well past
  # run_wait.
  handoff_wait = 1800
  # How long the sweep and its Cluster Agent cards stay settled with no
  # ranking card before the hand-off counts as not made: one gate cron tick
  # (60s), so a gate that compiles and files after the cards settle gets its
  # run, plus that much again for the run itself.
  settle_hold = 120
  # The exit trap's tries at listing the cards it archives, and the wait
  # between them: what failed the apply can fail one listing too.
  list_tries = 3
  list_wait  = 5
  # How long an exec into the agent Deployment waits for a pod when it has
  # none. A pod created in that time is not running yet and fails the exec
  # anyway, so kubectl's default of 60s only delays each failure by a minute.
  pod_wait = 5
  # bootstrap_handoff.py's FIRST_RUN_CLUSTER_CARDS_FILE, and the value step 2
  # writes there: empty when first_run_cluster_cards is null, so no file.
  cap_file  = "${local.home}/.first_run_cluster_cards"
  cards_cap = var.first_run_cluster_cards == null ? "" : tostring(var.first_run_cluster_cards)
  # Present whenever the cap file is one this stack wrote, so a later run can
  # tell its own leftover from an operator's setting.
  cap_mark = "${local.home}/.first_run_cluster_cards.bench"
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
    pod_wait          = local.pod_wait
    home              = local.home
    hermes            = local.hermes
    python            = local.python
    key_like          = local.key_like
    inventory         = local.inventory
    cap_file          = local.cap_file
    cards_cap         = local.cards_cap
    cap_mark          = local.cap_mark
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
      capped=""
      on_exit() {
        status=$?
        # A second signal would end the cleanup part-way.
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$rearmed" ]; then
          echo "Plant failed (exit $status); archiving the bootstrap-inventory cards it left open." >&2
          # The gate files only while this marker is absent, and a sweep filed
          # after this exit would run with nothing to archive it, so a gate
          # step 2 opened is closed again before the cards are listed. One
          # that was open gets no marker: if it has not filed yet, it still
          # files its own sweep; if it filed during this run, its own marker
          # stays, as after the destroy, and its sweep is among the cards
          # archived below. A gate run that read the marker before it went
          # back files once its reconcile ends, so the listing also waits for
          # any gate run to exit, for up to gate_wait once the marker is back:
          # a gate run that read it absent while the restore was failing can
          # take all of that. The restore is retried for as long, and the wait
          # starts over however the restore ended: one that failed on the
          # write rather than the exec leaves a gate that can still file.
          failed=""
          waited=0
          if [ -n "$old_id" ]; then
            until agent sh -c 'test -e ${local.home}/.bootstrap_scan_filed || echo "task_id=$1" > ${local.home}/.bootstrap_scan_filed' sh "$old_id" >&2; do
              if [ "$waited" -ge ${local.gate_wait} ]; then
                failed="$failed, put back the sweep marker"
                break
              fi
              sleep 5
              waited=$((waited + 5))
            done
          fi
          waited=0
          until [ "$(gate_running)" = idle ] || [ "$waited" -ge ${local.gate_wait} ]; do
            sleep 5
            waited=$((waited + 5))
          done
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
          # Terraform skips the destroy after a failed create, so this is the
          # only removal the cap step 2 wrote gets. The sentinel goes only once
          # the cap has, in the same exec, so a cap left behind is still marked
          # as this stack's.
          if [ -n "$capped" ]; then
            agent sh -c 'rm -f "$1" && rm -f "$2"' sh ${local.cap_file} ${local.cap_mark} >&2 \
              || failed="$failed, remove ${local.cap_file} and ${local.cap_mark}"
          fi
          if [ -n "$failed" ]; then
            echo "Cleanup incomplete: could not$${failed#,}. The next run's step 2, capped or not, archives the cards and removes the files left behind, a first-run cluster card cap this stack wrote included." >&2
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
      # Anything but `idle`, a failed exec included, reads as running. Every
      # Running pod is checked because above one replica only the leader runs
      # cron; an evicted pod stays listed, and its exec always fails.
      gate_running() {
        selector="$(kubectl get deployment -n "${var.agent_namespace}" "${var.agent_deployment}" \
          -o go-template='{{range $k, $v := .spec.selector.matchLabels}}{{$k}}={{$v}},{{end}}' || true)"
        pods=""
        if [ -n "$selector" ]; then
          pods="$(kubectl get pods -n "${var.agent_namespace}" -l "$${selector%,}" \
            --field-selector=status.phase=Running -o name || true)"
        fi
        if [ -z "$pods" ]; then
          echo running
          return
        fi
        for pod in $pods; do
          state="$(kubectl exec -i -n "${var.agent_namespace}" "$pod" -c "${var.agent_container}" -- \
            ${local.python} - ${local.gate_script} <<'PY' || true
      import os, sys
      me = os.getpid()
      for pid in filter(str.isdigit, os.listdir("/proc")):
          try:
              with open("/proc/%s/cmdline" % pid, "rb") as fh:
                  argv = fh.read().decode(errors="replace").split("\0")
          except OSError:
              continue
          if int(pid) != me and any(os.path.basename(a) in sys.argv[1:] for a in argv[1:]):
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
        agent sh -c 'sed -n -E "s/(^|.*[[:space:]])task_id[[:space:]]*=[[:space:]]*([^[:space:]]+).*/\\2/p" ${local.home}/.bootstrap_scan_filed 2>/dev/null | head -n 1; true' || true
      }
      # A failed listing or rm fails step 2, where the destroy's copy only
      # reports it: the sandbox's /opt/data outlives its pod, and a raw file
      # left there would be graded by raw-report-has-findings-block before this
      # run's hand-off writes its own. Every step runs and the
      # status covers them all, so step 2 still stops and the trap can name
      # what it left. The listing is checked on its own because a failure
      # inside a `for` word list fails nothing.
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

      # ---- 1. Refuse an install that would reach a person or file nothing --
      # One read that has to answer `clear`, so a failed exec refuses rather
      # than reading as "no marker".
      state="$(agent_py "${local.scan_job}" <<'PY' || true
      import json, os, sys
      home = "${local.home}"
      if os.path.exists(home + "/.user_aligned") or os.path.exists(home + "/.user_aligned_conversation") or os.path.exists(home + "/onboarding_conversation.json"):
          print("aligned")
      elif os.path.exists(home + "/.bootstrap_completed"):
          print("completed")
      else:
          try:
              with open(home + "/cron/jobs.json") as fh:
                  jobs = json.load(fh).get("jobs", [])
          except FileNotFoundError:
              jobs = []
          job = next((j for j in jobs if j.get("id") == sys.argv[1]), None)
          print("nojob" if job is None else "clear" if job.get("enabled", True) else "paused")
      PY
      )"
      case "$state" in
        clear) ;;
        aligned)
          echo "ERROR: ${local.home}/.user_aligned exists (or .user_aligned_conversation) on ${var.host_cluster_name}: a person has connected, and the report a fresh sweep writes would be delivered to their chat. Run this case on an install nobody is chatting with." >&2
          exit 1 ;;
        completed)
          echo "ERROR: onboarding already delivered on ${var.host_cluster_name} (${local.home}/.bootstrap_completed), and the ${local.scan_job} job removes itself after delivery once its audits stage is done. There is no gate left to re-arm." >&2
          exit 1 ;;
        nojob)
          echo "ERROR: the ${local.scan_job} cron job is not in ${local.home}/cron/jobs.json on ${var.host_cluster_name}, so nothing will file a sweep." >&2
          exit 1 ;;
        paused)
          echo "ERROR: the ${local.scan_job} cron job is paused (\"enabled\": false in ${local.home}/cron/jobs.json) on ${var.host_cluster_name}, so nothing will file a sweep. The agent pod's next start re-enables it." >&2
          exit 1 ;;
        *)
          echo "ERROR: could not read the onboarding markers or ${local.home}/cron/jobs.json on ${var.host_cluster_name} (got '$state')." >&2
          exit 1 ;;
      esac

      # ---- 2. Clear the previous sweep -------------------------------------
      # What the trap puts back: the previous sweep's id; `none` when a marker
      # without one, or only an inventory file, kept the gate closed; or
      # nothing when it was open. A failed read stops the apply before
      # anything changes; taken for an open gate, it would have the trap
      # leave this one open.
      old_id="$(agent_py <<'PY'
      import os, re
      home = "${local.home}"
      try:
          with open(home + "/.bootstrap_scan_filed") as fh:
              ids = [m.group(1) for m in (re.search(r"(?:^|\s)task_id\s*=\s*(\S+)", line) for line in fh) if m]
          print(ids[0] if ids and ids[0] else "none")
      except FileNotFoundError:
          if any(os.path.exists(path) for path in "${local.inventory}".split()):
              print("none")
      PY
      )"
      # The first-run cap file: `bench` when an earlier run of this stack left
      # its cap behind (the sentinel is there and holds the cap's value);
      # `orphan none` or `orphan operator` when the sentinel is there without
      # its cap (its own removal failed), alone or beside a cap file of another
      # value, which an operator wrote since; `operator` when a cap file is
      # there without the sentinel; `none` otherwise. The sentinel alone is no
      # proof: an orphaned one beside an operator's later cap would have that
      # cap removed. An empty sentinel matches nothing. A failed read, the exec
      # or a `cat` in the pod, prints no state and stops the apply below, as
      # step 1's does.
      # Known limit: an orphaned sentinel beside an operator's cap of the same
      # value reads as `bench`, and that cap is removed. Every removal takes
      # the cap and the sentinel in one exec, cap first, so an orphan takes the
      # sentinel's `rm` failing inside the pod after the cap's succeeded, then
      # an operator writing exactly this stack's last value before the next
      # run. The cap file cannot carry proof of its
      # writer: the gate reads it as a bare integer, and anything beside the
      # number would read as malformed and fall back to the default.
      cap="${local.cards_cap}"
      cap_read="$(agent sh -c 'if [ -e "$1" ]; then
          mark="$(cat "$1")" || exit 1
          if [ ! -e "$2" ]; then echo orphan none
          else
            file="$(cat "$2")" || exit 1
            if [ -n "$mark" ] && [ "$mark" = "$file" ]; then echo bench; else echo orphan operator; fi
          fi
        elif [ -e "$2" ]; then echo operator; else echo none; fi' \
        sh ${local.cap_mark} ${local.cap_file} || true)"
      # Whatever this run's cap: a leftover cap would bound this sweep, the
      # next case's and the install's real onboarding. The cap goes first, so
      # a removal that fails part-way leaves it marked for the next run.
      case "$cap_read" in
        bench)
          agent sh -c 'rm -f "$1" && rm -f "$2"' sh ${local.cap_file} ${local.cap_mark}
          echo "Removed the first-run cluster card cap an earlier run of this stack left in ${local.cap_file}."
          cap_state=none ;;
        "orphan none"|"orphan operator")
          agent rm -f ${local.cap_mark}
          cap_state="$${cap_read#orphan }"
          if [ "$cap_state" = none ]; then
            echo "Removed ${local.cap_mark}, a sentinel an earlier run of this stack left without its cap."
          else
            echo "Removed ${local.cap_mark}, a sentinel an earlier run of this stack left without its cap; the ${local.cap_file} beside it holds another value, so it is an operator's."
          fi ;;
        operator|none) cap_state="$cap_read" ;;
        *)
          echo "ERROR: could not read ${local.cap_file} or ${local.cap_mark} on ${var.host_cluster_name} (got '$cap_read')." >&2
          exit 1 ;;
      esac
      if [ -n "$cap" ] && [ "$cap_state" != none ]; then
        echo "ERROR: ${local.cap_file} exists on ${var.host_cluster_name} and this stack did not write it (no ${local.cap_mark} holding its value), so it is an operator's first-run cluster card cap. This case would have to overwrite it to grade its own cap. Remove the file if nobody needs it, or run this case on another install." >&2
        exit 1
      fi
      if [ "$cap_state" = operator ]; then
        echo "Note: ${local.cap_file} exists on ${var.host_cluster_name} and this stack did not write it; the sweep this run files is bounded by that cap."
      fi
      rearmed=1
      # The cap goes in before anything the gate acts on changes, so whichever
      # gate run files the new sweep reads it: on an install whose gate was
      # open, that run can come before the markers are removed below. Marked
      # first, in the trap's `capped` and then in the sentinel, which holds the
      # same value, so the trap also removes a write that failed part-way and a
      # run after a failed removal knows the cap for this stack's.
      if [ -n "$cap" ]; then
        capped=1
        agent sh -c 'printf "%s\n" "$1" > "$2"' sh "$cap" ${local.cap_mark}
        agent sh -c 'printf "%s\n" "$1" > "$2"' sh "$cap" ${local.cap_file}
        echo "First-run cluster card cap set to $cap in ${local.cap_file}."
      fi
      # Listed first: a failure inside a `for` word list fails nothing.
      ids="$(open_cards)"
      for id in $ids; do
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

      # ---- 4. Wait for the hand-off to ranking ---------------------------
      # Settled is the sweep and every Cluster Agent card filed at or after it
      # in a status nothing more comes from (done, blocked, triage, failed,
      # cancelled, archived), with at least one such card or a sweep that
      # completed (a blocked sweep can still list the fleet once unblocked).
      # The ranking card is not settled-gated: once the hand-off's own exists,
      # the hand-off has happened. Its own is the one carrying the hand-off
      # module's body; any other keyed card, such as one the sweep's worker
      # filed early, is one the hand-off replaces. Where the module is absent
      # every keyed card counts. A failed read leaves the last state standing.
      handoff_state() {
        agent_py "$sweep" "${local.cluster_key_like}" "${local.prioritize_key}" <<'PY'
      import sqlite3, sys
      sweep, cluster_like, key = sys.argv[1:4]
      sys.path.insert(0, "${local.home}/scripts")
      try:
          from bootstrap_handoff import _prioritize_body
          own = _prioritize_body()
      except Exception:
          own = None
      c = sqlite3.connect("file:${local.home}/kanban.db?mode=ro", uri=True)
      status, since = c.execute("SELECT status, created_at FROM tasks WHERE id = ?", (sweep,)).fetchone()
      started = c.execute("SELECT count(*) FROM task_runs WHERE task_id = ?", (sweep,)).fetchone()[0]
      keyed = sum(1 for (body,) in c.execute(
          "SELECT body FROM tasks WHERE idempotency_key = ? AND created_at >= ? AND status != 'archived'",
          (key, since)) if own is None or (body or "") == own)
      cards = [s for (s,) in c.execute(
          "SELECT status FROM tasks WHERE idempotency_key LIKE ? AND created_at >= ?", (cluster_like, since))]
      ended = ("done", "blocked", "triage", "failed", "cancelled", "archived")
      settled = status in ended and all(s in ended for s in cards) and (bool(cards) or status != "blocked")
      print(started, int(keyed > 0), int(settled))
      PY
      }
      triple='^[0-9]+ [01] [01]$'
      elapsed=0
      read_at=""
      started=0
      keyed=0
      settled_at=""
      read_handoff_state() {
        out="$(handoff_state)" || out=""
        if [[ "$out" =~ $triple ]]; then
          read -r started keyed settled <<<"$out"
          read_at=$elapsed
          if [ "$settled" -eq 0 ]; then
            settled_at=""
          elif [ -z "$settled_at" ]; then
            settled_at=$elapsed
          fi
        fi
      }
      picked=""
      read_handoff_state
      while :; do
        if [ "$keyed" -eq 1 ]; then
          echo "Sweep card $sweep handed off: a card keyed ${local.prioritize_key} is on the board after $${elapsed}s."
          exit 0
        fi
        if [ -n "$settled_at" ] && [ $((elapsed - settled_at)) -ge ${local.settle_hold} ]; then
          echo "Sweep card $sweep and its Cluster Agent cards have been settled for $((elapsed - settled_at))s with no card keyed ${local.prioritize_key}; handing over to the verifier."
          exit 0
        fi
        if [ -z "$picked" ] && [ "$elapsed" -ge ${local.run_wait} ]; then
          if [ -z "$read_at" ]; then
            echo "ERROR: no read of sweep card $sweep's hand-off from the board succeeded in $${elapsed}s, so there is nothing to grade. The read errors are above." >&2
            exit 1
          fi
          if [ "$started" -eq 0 ]; then
            echo "ERROR: no worker picked up sweep card $sweep within $${read_at}s, so there is no hand-off to grade." >&2
            agent ${local.hermes} kanban show "$sweep" >&2 || true
            exit 1
          fi
          picked=1
        fi
        if [ "$elapsed" -ge ${local.handoff_wait} ]; then
          if [ "$read_at" -lt "$elapsed" ]; then
            echo "Board reads after $${read_at}s failed; what follows is from the read at $${read_at}s." >&2
          fi
          echo "No card keyed ${local.prioritize_key} and the sweep's cards not settled after $${elapsed}s; handing over to the verifier."
          exit 0
        fi
        sleep ${local.poll}
        elapsed=$((elapsed + ${local.poll}))
        read_handoff_state
      done
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
      # The sweep marker stays, so the gate does not file again once this
      # case is gone.
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
      # The cap goes only where this stack wrote it. With first_run_cluster_cards
      # null nothing was written, and a file there is an operator's setting on
      # that install, which the gate honours on every first run; removing it
      # would undo a choice this case never made. With it set, step 2 refused
      # an install that had an operator's file, so the file here is this
      # stack's. Left behind, the cap would bound the next sweep, the next
      # case's and the install's real onboarding, so the sentinel goes only once
      # the cap has, in the same exec: the next run's step 2 removes a cap whose sentinel still
      # holds its value, and a sentinel left without its cap.
      if [ -n "${self.triggers.cards_cap}" ]; then
        kubectl exec -n "$ns" "$target" -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
          sh -c 'rm -f "$1" && rm -f "$2"' sh ${self.triggers.cap_file} ${self.triggers.cap_mark} \
          || failed="$failed, remove ${self.triggers.cap_file} and ${self.triggers.cap_mark}"
      fi
      if [ -n "$failed" ]; then
        echo "Cleanup incomplete: could not$${failed#,}. The next run's step 2, capped or not, archives the cards and removes the files left behind, a first-run cluster card cap this stack wrote included." >&2
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
