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

# hack/sized_fleet.py reads these with `tofu output -json`.
output "clusters" {
  description = "Each cluster's name, zone and node count."
  value = {
    for name, cluster in local.fleet : name => {
      zone  = var.zone
      nodes = cluster.nodes
    }
  }
}

output "size" {
  description = "The fleet's size and its totals."
  value = {
    size     = var.size
    clusters = local.clusters
    nodes    = local.nodes
  }
}
