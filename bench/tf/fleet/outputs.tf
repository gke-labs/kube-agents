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

output "cluster_names" {
  description = "The three seeded clusters. seeded-b is the version laggard; seeded-c is the consistency outlier."
  value = [
    google_container_cluster.seeded_a.name,
    google_container_cluster.seeded_b.name,
    google_container_cluster.seeded_c.name,
  ]
}

output "fleet_reader_service_account" {
  description = "Read-only service account the evaluation checks read the fleet as. Export it as FLEET_READONLY_SA in the Prow job so hack/fleet-kubeconfigs.sh mints its token instead of using the runner's own credential."
  value       = google_service_account.fleet_reader.email
}

output "lagging_version" {
  description = "The version seeded-b is pinned to this cycle (REGULAR default minus one minor, freshest patch)."
  value       = local.lagging_version
}
