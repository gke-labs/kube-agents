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
  description = "The seeded clusters. seeded-b is the version laggard; seeded-c is the consistency outlier; seeded-d is multi-zonal and carries the zonal-skew fixtures."
  value = [
    google_container_cluster.seeded_a.name,
    google_container_cluster.seeded_b.name,
    google_container_cluster.seeded_c.name,
    google_container_cluster.seeded_d.name,
  ]
}

output "fleet_reader_service_account" {
  description = "Read-only service account the evaluation checks read the fleet as. hack/ci-eval-pr.sh defaults FLEET_READONLY_SA to it from the leased project; export it yourself for a fleet outside the pool. hack/fleet-kubeconfigs.sh mints its token as this account and writes nothing without one."
  value       = google_service_account.fleet_reader.email
}

output "lagging_version" {
  description = "The version seeded-b is pinned to this cycle: the newest patch REGULAR offers one minor behind its default, or the hold when seeded-b runs a patch REGULAR does not carry (seeded_b_pin.py)."
  value       = local.lagging_version
}

output "seeded_b_pin" {
  description = "How seeded_b_pin.py arrived at the pin: REGULAR's default, the lag target, the running master version and whether it is held."
  value       = data.external.seeded_b_pin.result
}
