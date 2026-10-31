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

# The scenario driver for bench/tasks/upgrades-retrospective-log-volume-gate and
# bench/tasks/upgrades-rollout-family-held-by-canary-gate: seed the upgrade
# retrospective's report store on the shell sandbox with a full report in which
# the seeded fleet's first-upgraded cluster (slot a) carries a gate, so the
# on-demand route and the rollout orchestration SOP have a gate to read without
# an upgrade having happened on the leased fleet.
#
# The report is the collector's own rendering of a synthetic run (render.py
# beside this file, pinned by bench/tests/test_recorded_weeks_stack.py), with the
# leased project, the four slot clusters and the apply time substituted here.
# The two recorded weeks of Cloud Logging ingestion behind the gate were read
# from a development fleet; in the `risk` mode one container's after-week is
# ten times its before-week and past the 1 GiB floor.
#
# The seeded pair is written beside the store's own reports under a fresh stamp,
# and the latest link is pointed at it; the teardown, or the exit trap on a
# failed apply, removes the pair and puts the link back (store.sh).

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
  store_b64 = base64encode(file("${path.module}/store.sh"))
  md_b64    = base64encode(file("${path.module}/report/report-${var.gate_mode}.md.tmpl"))
  json_b64  = base64encode(file("${path.module}/report/report-${var.gate_mode}.json.tmpl"))
  # render.py: GENERATED_AT and TITLE_DATE, the sentinels the apply time replaces.
  generated_sentinel = "2026-01-02T03:04:05Z"
  title_sentinel     = "2026-01-02"
  pod_wait           = 5
}

resource "null_resource" "recorded_weeks" {
  triggers = {
    host_cluster       = var.host_cluster_name
    host_location      = var.host_cluster_location
    host_project       = var.project_id
    namespace          = var.agent_namespace
    sandbox_pod        = var.sandbox_pod
    sandbox_container  = var.sandbox_container
    gate_mode          = var.gate_mode
    seeded_filter      = var.seeded_label_filter
    store_dir          = var.store_dir
    store_b64          = local.store_b64
    md_b64             = local.md_b64
    json_b64           = local.json_b64
    generated_sentinel = local.generated_sentinel
    title_sentinel     = local.title_sentinel
    pod_wait           = local.pod_wait
    stamp              = formatdate("YYYYMMDD'T'hhmmss'Z'", timestamp())
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      work="$(mktemp -d)"
      seeded=""
      sandbox() {
        kubectl exec -i -n "${self.triggers.namespace}" "${self.triggers.sandbox_pod}" -c "${self.triggers.sandbox_container}" \
          --pod-running-timeout=${self.triggers.pod_wait}s -- sh -c "$(cat "$work/store.sh")" store.sh "$@"
      }
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$seeded" ]; then
          echo "Apply failed (exit $status); restoring the report store." >&2
          sandbox restore "${self.triggers.store_dir}" "${self.triggers.stamp}" < /dev/null >&2 || echo "WARNING: could not restore the report store." >&2
        fi
        rm -rf "$work"
      }
      trap on_exit EXIT
      trap 'exit 143' TERM INT
      KUBECONFIG="$work/config"
      export KUBECONFIG
      printf '%s' '${self.triggers.store_b64}' | base64 -d > "$work/store.sh"

      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project." >&2
        exit 1
      fi
      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet

      # The seeded fleet's slots, by the name's last segment (bench/tf/fleet:
      # "<prefix>-a" .. "<prefix>-d").
      gcloud container clusters list --project "$project" --filter "${self.triggers.seeded_filter}" \
        --format "value(name,location)" > "$work/clusters.tsv"
      printf '%s' '${self.triggers.md_b64}' > "$work/report.md.b64"
      printf '%s' '${self.triggers.json_b64}' > "$work/report.json.b64"
      python3 - "$work" "$project" "${self.triggers.generated_sentinel}" "${self.triggers.title_sentinel}" "${self.triggers.stamp}" <<'PY'
      import base64, datetime, sys
      work, project, generated_sentinel, title_sentinel, stamp = sys.argv[1:6]
      slots = {}
      for line in open(f"{work}/clusters.tsv"):
          parts = line.split()
          if len(parts) == 2 and parts[0].rsplit("-", 1)[-1] in ("a", "b", "c", "d"):
              slots[parts[0].rsplit("-", 1)[-1]] = parts
      missing = [s for s in "abcd" if s not in slots]
      if missing:
          sys.exit(f"ERROR: the seeded fleet in {project} has no cluster for slot(s) {', '.join(missing)}: {sorted(slots)}")
      now = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
      generated = now.strftime("%Y-%m-%dT%H:%M:%SZ")
      for ext in ("md", "json"):
          text = base64.b64decode(open(f"{work}/report.{ext}.b64").read()).decode()
          text = text.replace("{{PROJECT}}", project).replace(generated_sentinel, generated).replace(title_sentinel, now.strftime("%Y-%m-%d"))
          for slot, (name, location) in slots.items():
              text = text.replace("{{CLUSTER_" + slot.upper() + "}}", name).replace("{{LOCATION_" + slot.upper() + "}}", location)
          if "{{" in text:
              sys.exit(f"ERROR: a placeholder survived the substitution in the {ext} template")
          open(f"{work}/report.{ext}", "w").write(text)
      PY

      seeded=1
      sandbox write "${self.triggers.store_dir}" "${self.triggers.stamp}" md < "$work/report.md"
      sandbox write "${self.triggers.store_dir}" "${self.triggers.stamp}" json < "$work/report.json"
      sandbox seed "${self.triggers.store_dir}" "${self.triggers.stamp}" < /dev/null
      echo "Seeded ${self.triggers.store_dir}/${self.triggers.stamp}.{md,json} (gate ${self.triggers.gate_mode}) and pointed the latest link at it."
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -uo pipefail
      work="$(mktemp -d)"
      trap 'rm -rf "$work"' EXIT
      KUBECONFIG="$work/config"
      export KUBECONFIG
      printf '%s' '${self.triggers.store_b64}' | base64 -d > "$work/store.sh"
      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet || exit 0
      kubectl exec -i -n "${self.triggers.namespace}" "${self.triggers.sandbox_pod}" -c "${self.triggers.sandbox_container}" \
        --pod-running-timeout=${self.triggers.pod_wait}s -- sh -c "$(cat "$work/store.sh")" store.sh restore "${self.triggers.store_dir}" "${self.triggers.stamp}" < /dev/null \
        || echo "WARNING: could not restore the report store; ${self.triggers.store_dir}/${self.triggers.stamp}.{md,json} may remain." >&2
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
