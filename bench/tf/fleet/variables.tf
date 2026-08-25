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
  description = "IAM members that may mint an access token as the seeded-fleet reader service account, in `serviceAccount:...`/`user:...`/`group:...` form. This is the eval project's Prow runner identity: hack/fleet-kubeconfigs.sh calls `gcloud auth print-access-token --impersonate-service-account` as that identity, so without an entry here the runner cannot assume the read-only account and falls back to its own cluster-admin credential (loudly). Empty by default so a project can adopt the stack before its runner identity is known."
  type        = list(string)
  default     = []

  # A bare email here applies cleanly and grants nothing: the IAM API treats an
  # unprefixed member as invalid, and the mistake would only surface as the
  # runner falling back to its own credential on the fleet it is grading.
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
