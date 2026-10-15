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

# The scenario driver for bench/tasks/oobe-late-first-message: an install nobody has spoken to
# runs its first-run audits, and the first chat message arrives only after all four have ended.
#
# 1. late.py `clear` records, then takes away, the home channel (`config.yaml` and `.env`) and the
#    delivery markers (`.user_aligned`, `.bootstrap_completed`), so each audit's summary finds
#    no home channel, as on a fresh install nobody has opened the chat on.
# 2. oobe-first-run-audits/arm.py (shared, not copied) arms the `oobe` stage the way that case
#    does, rewriting .bootstrap_scan_filed, .bootstrap_handoff_filed and .oobe_audits_fired, and
#    the stage starts the four audits one after another.
# 3. The apply waits, up to `chain_wait`, until each audit has a run since the arm that ended.
# 4. It plants a stand-in INVENTORY.md on the shell sandbox, where the delivery stage reads the
#    report, and late.py `open` touches `.user_aligned` as the onboarding plugin does on a first
#    message. The next `oobe` tick delivers the report; the apply waits, up to `delivery_wait`,
#    for its claim.
#
# The `oobe` job delivers to `local` here (late.py refuses one bound to a chat), so the report goes
# to the default profile's cron/output/oobe/ only, where the verifier reads it.
#
# The teardown, and the exit trap on a failed apply, put everything back: the `oobe` stage through
# oobe-first-run-audits/disarm.py, the home channel and the markers byte for byte through late.py
# `restore`, and the sandbox's INVENTORY.delivered.md from the copy taken before the plant. The
# audits are left to finish, as in oobe-first-run-audits.

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
  home       = "/opt/data"
  hermes     = "/opt/hermes/.venv/bin/hermes"
  python     = "/opt/hermes/.venv/bin/python3"
  shared     = "${path.module}/../oobe-first-run-audits"
  arm_b64    = base64encode(file("${local.shared}/arm.py"))
  disarm_b64 = base64encode(file("${local.shared}/disarm.py"))
  own_b64    = base64encode(file("${local.shared}/own_stage.py"))
  late_b64   = base64encode(file("${path.module}/late.py"))
  report_b64 = base64encode(file("${path.module}/report.txt"))
  report     = "INVENTORY.md"
  delivered  = "INVENTORY.delivered.md"
  # The sandbox's INVENTORY.delivered.md from before the plant: the delivery moves the stand-in
  # report over it.
  delivered_copy = ".bench-oobe-late.delivered.md"
  # Written on the sandbox before anything there changes; only a sandbox carrying it is cleaned, so
  # a report a real onboarding left waiting is never removed.
  sandbox_marker = ".bench-oobe-late.armed"
  # As in oobe-first-run-audits: the install's own onboarding must be over before the arm.
  own_wait = 3600
  # Run 1 of #1866 on a five-cluster fleet ran the four audits in 42 minutes; each audit's own
  # limit is 45. own_wait, chain_wait and delivery_wait together are the longest this stack holds
  # the onboarding state lock (hack/ci-eval-pr.sh: STATE_LOCK_HOLD_SECONDS).
  chain_wait = 5400
  # The delivery stage runs first in each `oobe` tick, every minute.
  delivery_wait = 600
  poll          = 30
  pod_wait      = 5
}

resource "null_resource" "oobe_late" {
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
    python            = local.python
    hermes            = local.hermes
    disarm_b64        = local.disarm_b64
    late_b64          = local.late_b64
    report            = local.report
    delivered         = local.delivered
    delivered_copy    = local.delivered_copy
    sandbox_marker    = local.sandbox_marker
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      kubeconfig_dir="$(mktemp -d)"
      arming=""
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$arming" ]; then
          echo "Apply failed (exit $status); putting the install back." >&2
          teardown >&2 || echo "Cleanup incomplete. The next run puts the install back before it arms." >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT
      trap 'exit 143' TERM INT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${var.project_id}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project." >&2
        exit 1
      fi
      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet

      agent_py() {
        kubectl exec -i -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
          -c "${var.agent_container}" --pod-running-timeout=${local.pod_wait}s -- ${local.python} - "$@"
      }
      late() {
        printf '%s' '${local.late_b64}' | base64 -d | agent_py "${local.home}" "$@"
      }
      sandbox_pods="$(kubectl get pods -n "${var.agent_namespace}" -l "${var.sandbox_selector}" -o name)"
      set -- $sandbox_pods
      if [ "$#" -ne 1 ]; then
        echo "ERROR: expected one shell sandbox pod matching ${var.sandbox_selector} in ${var.agent_namespace}, found $#." >&2
        exit 1
      fi
      sandbox_pod="$1"
      sandbox() {
        kubectl exec -i -n "${var.agent_namespace}" "$sandbox_pod" -c "${var.sandbox_container}" -- sh -c "$@"
      }
      teardown() {
        printf '%s' '${local.disarm_b64}' | base64 -d | agent_py "${local.home}" "${local.hermes}"
        late restore
        sandbox 'cd "$1" && [ -e "$5" ] || exit 0; rm -f "$2" "$3" && if [ -f "$4" ]; then mv -f "$4" "$3"; fi && rm -f "$5"' sh \
          "${local.home}" "${local.report}" "${local.delivered}" "${local.delivered_copy}" "${local.sandbox_marker}"
      }

      # ---- 1. Finish an earlier run's teardown ----------------------------
      teardown

      # ---- 2. Wait for the install's own first-run stage ------------------
      elapsed=0
      until own="$(printf '%s' '${local.own_b64}' | base64 -d | agent_py "${local.home}")" && [ "$own" = clear ]; do
        if [ "$elapsed" -ge ${local.own_wait} ]; then
          echo "ERROR: the install's own first-run stage on ${var.host_cluster_name} is still $${own:-unreadable} after $${elapsed}s." >&2
          exit 1
        fi
        sleep ${local.poll}
        elapsed=$((elapsed + ${local.poll}))
      done
      if sandbox 'test -e "$1/$2"' sh "${local.home}" "${local.report}"; then
        echo "ERROR: $sandbox_pod already holds ${local.report}: an onboarding report is waiting for delivery, and this case would deliver it." >&2
        exit 1
      fi

      # ---- 3. Clear the home channel, then arm the stage ------------------
      arming=1
      sandbox 'cd "$1" && touch "$4" && if [ -f "$2" ]; then cp -p "$2" "$3"; fi' sh \
        "${local.home}" "${local.delivered}" "${local.delivered_copy}" "${local.sandbox_marker}"
      late clear
      printf '%s' '${local.arm_b64}' | base64 -d | agent_py "${local.home}" "${local.hermes}" "$(date -u +%Y%m%d%H%M%S)"

      # ---- 4. Wait for every audit to end ----------------------------------
      elapsed=0
      until done_="$(late finished)" && [ "$done_" = finished ]; do
        if [ "$elapsed" -ge ${local.chain_wait} ]; then
          echo "ERROR: the four first-run audits have not all ended $${elapsed}s after the arm; the first message would not come after them." >&2
          exit 1
        fi
        sleep ${local.poll}
        elapsed=$((elapsed + ${local.poll}))
      done

      # ---- 5. The first message --------------------------------------------
      printf '%s' '${local.report_b64}' | sandbox \
        'base64 -d > "$1/$2.tmp" && chown "$(stat -c %u:%g "$1")" "$1/$2.tmp" && mv -f "$1/$2.tmp" "$1/$2"' sh \
        "${local.home}" "${local.report}"
      late open

      # ---- 6. Wait for the delivery's claim --------------------------------
      elapsed=0
      until agent_py "${local.home}" <<<'import os, sys; sys.exit(0 if os.path.exists(os.path.join(sys.argv[1], ".bootstrap_completed")) else 1)'; do
        if [ "$elapsed" -ge ${local.delivery_wait} ]; then
          echo "The delivery has not claimed the report $${elapsed}s after the first message; leaving it to the verifier." >&2
          break
        fi
        sleep ${local.poll}
        elapsed=$((elapsed + ${local.poll}))
      done
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -uo pipefail
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet \
        || echo "WARNING: could not fetch credentials for ${self.triggers.host_cluster}; the steps below will likely fail too." >&2

      agent_py() {
        kubectl exec -i -n "${self.triggers.namespace}" "deployment/${self.triggers.deployment}" \
          -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- ${self.triggers.python} - "$@"
      }
      printf '%s' '${self.triggers.disarm_b64}' | base64 -d | agent_py "${self.triggers.home}" "${self.triggers.hermes}" \
        || echo "WARNING: could not disarm the oobe stage; the next run of this case does it first." >&2
      printf '%s' '${self.triggers.late_b64}' | base64 -d | agent_py "${self.triggers.home}" restore \
        || echo "WARNING: could not restore the home channel; the next run of this case does it first." >&2
      for pod in $(kubectl get pods -n "${self.triggers.namespace}" -l "${self.triggers.sandbox_selector}" -o name); do
        kubectl exec -i -n "${self.triggers.namespace}" "$pod" -c "${self.triggers.sandbox_container}" -- \
          sh -c 'cd "$1" && [ -e "$5" ] || exit 0; rm -f "$2" "$3" && if [ -f "$4" ]; then mv -f "$4" "$3"; fi && rm -f "$5"' sh \
          "${self.triggers.home}" "${self.triggers.report}" "${self.triggers.delivered}" "${self.triggers.delivered_copy}" \
          "${self.triggers.sandbox_marker}" \
          || echo "WARNING: could not put back ${self.triggers.delivered} on $pod." >&2
      done
    EOT
  }
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
