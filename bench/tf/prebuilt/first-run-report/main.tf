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

# The scenario driver for bench/tasks/first-run-report-on-demand.
#
# It plants a fixed ranked report as INVENTORY.delivered.md on the shell
# sandbox's data volume, where onboarding's delivery renames the real one once
# it has been posted, filed or retired, and where `get_first_run_report` reads
# it back. The report names two workloads nothing else on the install knows
# about; the task asks what onboarding found and grades that the answer came
# through the tool and names both.
#
# WHY THE DELIVERED NAME. The tool's ranked read takes INVENTORY.delivered.md
# before INVENTORY.md, so the plant answers whatever a pending report says, and
# a pending INVENTORY.md is left where the delivery tick looks for it: moving
# it aside would hide a real report from a delivery that is due during the run.
#
# WHAT IT LEAVES ALONE. The agent pod's .bootstrap_* markers are a pool
# install's real onboarding state and are not touched, so the tool's `state`
# on the run is the install's own (filed, on a pool install whose report went
# to a ledger issue); the case grades the report's content, not the state.
#
# BACKUP AND RESTORE. An existing INVENTORY.delivered.md that is not this
# stack's text is a real delivered report: the apply moves it to a backup name
# before planting and the destroy moves it back. A backup an earlier failed
# destroy left is kept as the real one and restored by this destroy. A report
# delivered during the run (renamed over the plant by a real delivery) is not
# overwritten by the restore: the destroy sets it aside at a side name it
# prints, then restores the backup. With no backup, the destroy removes the
# plant only while it still holds this stack's text; anything written there
# since is not this case's.
#
# Isolation class: one file on the sandbox's data volume, restored on destroy;
# no cluster object, no agent-pod marker. Eligible tier: nightly.

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
  # The sandbox's data volume and the delivered name, as first_run_report.py
  # names them (SANDBOX_HOME, DELIVERED_REPORT_NAME).
  report_path = "/opt/data/INVENTORY.delivered.md"
  backup_path = "/opt/data/INVENTORY.delivered.md.bench-first-run-report.bak"
  # Where the destroy sets aside a report that was delivered during the run,
  # when a backup is also waiting to go back.
  displaced_path = "/opt/data/INVENTORY.delivered.md.bench-first-run-report.displaced"
  first       = var.workload_names[0]
  second      = var.workload_names[1]
  # The shape the ranking worker writes (inventory_prioritize_sop.md, Step 6):
  # a heading, the posture with its `Scanned N clusters and M workloads.`
  # sentence (the reader skill builds its headline from it), the criticals as
  # two lines each under a `Critical:` label, the roll-up line and the closing
  # line. The workload names are the words the case grades, so they appear as
  # namespace/name, the report's own form.
  planted_report = <<-EOT
    # GKE Environment Scan

    Scanned 1 cluster and 14 workloads.

    Two findings need attention before anything else; the rest can wait for the scheduled audits.

    1. **Critical: ${var.workload_namespace}/${local.first} runs as root with a writable root
       filesystem and no resource limits, on a node pool with Workload Identity off.** Left alone it
       is the first foothold a compromised container gets; set `runAsNonRoot`, add limits and move it
       to a Workload Identity pool.
    2. **Critical: ${var.workload_namespace}/${local.second} has had three of four replicas OOMKilled
       in the last hour and its PodDisruptionBudget allows zero disruptions.** The next node upgrade
       stalls on it; raise the memory limit and set `maxUnavailable: 1`.

    Also found: 11 more items, 1 of them critical, in the findings queue; the scheduled audits
    re-check them on their schedules.

    The full inventory is available on request.

    Planted by the bench stack prebuilt/first-run-report; its destroy removes it.
  EOT
  # In-sandbox tests on the report path $1, shared by the apply and the
  # destroy so the file the apply plants over is the file the destroy restores.
  # "Ours" compares against $planted, the decoded planted_report.
  report_absent = "[ ! -e \"$1\" ] && [ ! -L \"$1\" ]"
  report_ours   = "[ -f \"$1\" ] && [ ! -L \"$1\" ] && [ \"$(cat \"$1\")\" = \"$planted\" ]"
  backup_exists = "[ -e \"$2\" ] || [ -L \"$2\" ]"
}

resource "null_resource" "planted_report" {
  triggers = {
    host_cluster      = var.host_cluster_name
    host_location     = var.host_cluster_location
    host_project      = var.project_id
    namespace         = var.agent_namespace
    sandbox_selector  = var.sandbox_selector
    sandbox_container = var.sandbox_container
    sandbox_user      = var.sandbox_user
    report_path       = local.report_path
    backup_path       = local.backup_path
    displaced_path    = local.displaced_path
    report_b64        = base64encode(local.planted_report)
    report_absent     = local.report_absent
    report_ours       = local.report_ours
    backup_exists     = local.backup_exists
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      REPORT_PATH = local.report_path
      BACKUP_PATH = local.backup_path
      REPORT_B64  = base64encode(local.planted_report)
    }
    command = <<-EOT
      set -euo pipefail

      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
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

      sandbox_pods="$(kubectl get pods -n "${var.agent_namespace}" -l "${var.sandbox_selector}" -o name)"
      set -- $sandbox_pods
      if [ "$#" -ne 1 ]; then
        echo "ERROR: expected one shell sandbox pod matching ${var.sandbox_selector} in ${var.agent_namespace}, found $#. The tool reads the report there." >&2
        exit 1
      fi
      sandbox_pod="$1"
      in_sandbox=(kubectl exec -i -n "${var.agent_namespace}" "$sandbox_pod" -c "${var.sandbox_container}" -- \
        setpriv --reuid="${var.sandbox_user}" --regid="${var.sandbox_user}" --init-groups --)

      # One remote step, so nothing lands between the check and the write. A
      # file holding this stack's own text is one an earlier run left and is
      # kept. Any other file at the path is a real delivered report: it goes to
      # the backup name, unless a backup is already there (an earlier run's,
      # the real one; the current file is then a stale plant and is replaced).
      # The plant is written aside and renamed, so a reader never sees a part.
      result="$(printf '%s' "$REPORT_B64" | "$${in_sandbox[@]}" sh -c \
        'b64="$(cat)"; planted="$(printf "%s" "$b64" | base64 -d)"; if ${local.report_ours}; then echo ours; else if ! { ${local.report_absent}; } && ! { ${local.backup_exists}; }; then mv -f -- "$1" "$2" || { echo backup-failed; exit 0; }; fi; tmp="$1.$$.tmp"; if printf "%s" "$b64" | base64 -d > "$tmp" && mv -f -- "$tmp" "$1"; then echo planted; else rm -f -- "$tmp"; echo write-failed; fi; fi' \
        sh "$REPORT_PATH" "$BACKUP_PATH")"
      case "$result" in
        planted) echo "Planted $REPORT_PATH on $sandbox_pod." ;;
        ours) echo "$REPORT_PATH on $sandbox_pod already holds this stack's text; kept, and the destroy restores what was there." ;;
        *)
          echo "ERROR: could not plant $REPORT_PATH on $sandbox_pod (read: $${result:-nothing})." >&2
          exit 1
          ;;
      esac
    EOT
  }

  # No on_failure = continue: a plant left in place would answer the next
  # person's real question with this stack's report.
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    environment = {
      REPORT_PATH    = self.triggers.report_path
      BACKUP_PATH    = self.triggers.backup_path
      DISPLACED_PATH = self.triggers.displaced_path
      REPORT_B64     = self.triggers.report_b64
    }
    command = <<-EOT
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

      sandbox_pods="$(kubectl get pods -n "${self.triggers.namespace}" -l "${self.triggers.sandbox_selector}" -o name)"
      if [ -z "$sandbox_pods" ]; then
        echo "ERROR: no shell sandbox pod matches ${self.triggers.sandbox_selector} in ${self.triggers.namespace}, so $REPORT_PATH could not be restored. The next run of the case restores it." >&2
        exit 1
      fi
      for pod in $sandbox_pods; do
        # The backup is the real report and goes back. A file at the path that
        # is neither the plant nor absent is a report delivered during the run:
        # it is set aside at the displaced name first, never overwritten.
        # Without a backup, the plant is removed only while it still holds what
        # the apply wrote; anything written there since is not this case's.
        result="$(printf '%s' "$REPORT_B64" | kubectl exec -i -n "${self.triggers.namespace}" "$pod" -c "${self.triggers.sandbox_container}" -- \
          setpriv --reuid="${self.triggers.sandbox_user}" --regid="${self.triggers.sandbox_user}" --init-groups -- sh -c \
          'planted="$(base64 -d)"; if ${self.triggers.backup_exists}; then displaced=""; if ! { ${self.triggers.report_absent}; } && ! { ${self.triggers.report_ours}; }; then mv -f -- "$1" "$3" || { echo displace-failed; exit 0; }; displaced=1; fi; mv -f -- "$2" "$1" && if [ -n "$displaced" ]; then echo restored-displaced; else echo restored; fi; elif ${self.triggers.report_absent}; then echo absent; elif ${self.triggers.report_ours}; then rm -f -- "$1" && echo removed; else echo kept; fi' \
          sh "$REPORT_PATH" "$BACKUP_PATH" "$DISPLACED_PATH")"
        case "$result" in
          restored-displaced) echo "$REPORT_PATH on $pod: restored; a report delivered during the run was set aside at $DISPLACED_PATH." ;;
          restored | absent | removed | kept) echo "$REPORT_PATH on $pod: $result." ;;
          *)
            echo "ERROR: could not restore $REPORT_PATH on $pod (read: $${result:-nothing})." >&2
            exit 1
            ;;
        esac
      done
    EOT
  }
}

# Passed straight through: devops-bench reads these after apply and points the
# ambient kubeconfig at the host cluster.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
