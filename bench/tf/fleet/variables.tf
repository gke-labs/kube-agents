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

variable "exclusion_window_hours" {
  description = "Length in hours of seeded-b's NO_MINOR_UPGRADES maintenance exclusion, re-stamped from now on every apply. The GKE API rejects an endTime past the held minor's end of life (observed live: 'endTime needs to be before minor version 1.34 end of life: (2027-1-25)'), so now + this window must stay inside the EOL -- which no fixed window can do forever. When a reconcile starts failing with that 400, that IS the EOL approaching: shorten this variable to fit, or accept the self-heal and re-lag seeded-b by replacement at EOL (see README). 90 days balances a long protective window against how soon the 400s begin."
  type        = number
  default     = 2160 # 90 days
}
