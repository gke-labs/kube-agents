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

variable "project_id" {
  description = "GCP project the seeded fleet lives in."
  type        = string
  default     = "kube-agents-evals"
}

variable "zone" {
  description = "Zone for all three clusters. Zonal on purpose: the fleet exists to be looked at, not to be available, and a regional control plane triples nothing but the bill."
  type        = string
  default     = "us-central1-a"
}

variable "cluster_prefix" {
  description = "Name prefix for the three clusters (seeded-a, seeded-b, seeded-c)."
  type        = string
  default     = "seeded"
}

variable "fleet_reader_token_creators" {
  description = "IAM members that may mint an access token as the seeded-fleet reader service account, in `serviceAccount:...`/`user:...`/`group:...` form. Three callers need it. Two are the identities that run hack/ci-eval-pr.sh in a leased pool project, the presubmit's Prow runner and the nightly periodic's recorder: hack/fleet-kubeconfigs.sh calls `gcloud auth print-access-token --impersonate-service-account` as whichever is running, so without its entry here that runner cannot assume the read-only account, hack/fleet-kubeconfigs.sh writes nothing and exits 3, and the presubmit stops at its fleet step. The third is the CI health bot (eval-dashboard-publisher@kube-agents-prow), whose hourly fixture-state scan (.github/workflows/ci-health.yml, docs/ci-health.md) runs every gcloud and kubectl read as the reader and holds no grant of its own on the project, so a project without its entry scans as 'not checked'. Defaults to all three -- the same three accounts in every pool project, kept equal to FLEET_READER_TOKEN_CREATORS in scripts/verify_ci_pool_project.py by its tests. Override it when applying this stack outside the CI pool."
  type        = list(string)

  # Defaulted here rather than passed with -var, because the resource is keyed
  # by member: an apply that does not carry the value plans the binding for
  # destruction, and the next presubmit to lease the project stops at its fleet step.
  # Observed as `1 to destroy` on kube-agents-evals-16 (gke-labs/kube-agents#1051).
  default = [
    "serviceAccount:prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com",
    "serviceAccount:eval-baseline-recorder@kube-agents-prow.iam.gserviceaccount.com",
    "serviceAccount:eval-dashboard-publisher@kube-agents-prow.iam.gserviceaccount.com",
  ]

  # A bare email here applies cleanly and grants nothing: the IAM API treats an
  # unprefixed member as invalid, and the mistake would only surface as the
  # runner refusing to read the fleet (exit 3) on the next lease.
  validation {
    condition = alltrue([
      for m in var.fleet_reader_token_creators :
      can(regex("^(serviceAccount|user|group|domain|principal|principalSet):", m))
    ])
    error_message = "Each member must carry an IAM type prefix, e.g. serviceAccount:prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com."
  }
}

variable "exclusion_window_hours" {
  description = "Length in hours of seeded-b's NO_MINOR_UPGRADES maintenance exclusion, re-stamped from now on every apply. The GKE API rejects an endTime past the held minor's end of life (observed live: 'endTime needs to be before minor version 1.34 end of life: (2027-1-25)'), so now + this window must stay inside the EOL -- which no fixed window can do forever. When a reconcile starts failing with that 400, that IS the EOL approaching: shorten this variable to fit, or accept the self-heal and re-lag seeded-b by replacement at EOL (see README). 90 days balances a long protective window against how soon the 400s begin."
  type        = number
  default     = 2160 # 90 days
}
