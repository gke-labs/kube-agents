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

# The upgrade-readiness defects, on seeded-b because that is the upgrade
# cluster: it already carries the held-back control plane the readiness
# checks read, and a drain that cannot finish is the same subject. Each block
# names the scenario that asserts on it.
#
# Upgrading a node means draining it, and most upgrade failures are really
# drain failures. What the fleet could already show was a workload that
# resists eviction (obtainability-audit's PDB checks, on seeded-a). What it
# could not show is the two cases where the drain never starts: the pool has
# no room to add a replacement node, and the workload has nowhere else to go.

provider "kubernetes" {
  alias                  = "seeded_b"
  host                   = "https://${google_container_cluster.seeded_b.endpoint}"
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(google_container_cluster.seeded_b.master_auth[0].cluster_ca_certificate)
}

# Defect (upgrade readiness): a pool that cannot surge. `max_surge = 0` with
# `max_unavailable = 1` means GKE upgrades this pool by taking a node away
# rather than adding one first, so every pod on it is evicted with nowhere
# prepared to land. On a one-node pool that is the whole pool at once.
#
# A second pool rather than a setting on the default one, deliberately: the
# default pool's version pinning is what makes `version-laggard` exact (see
# main.tf), and a scenario that reds because this fixture disturbed the pin
# would point at the wrong place.
#
# `version` and `auto_upgrade` follow the default pool for the same reason
# the default pool carries them — a pool left to drift from the pinned master
# produces an undeclared `pool-skew` finding on a fleet whose premise is that
# every finding is known in advance.
#
# Asserted by readiness-surge-blocked.
resource "google_container_node_pool" "no_surge_pool" {
  name       = "no-surge-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_b.name
  node_count = 1
  version    = local.lagging_version

  management {
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 0
    max_unavailable = 1
  }

  node_config {
    machine_type    = "e2-small"
    disk_size_gb    = 20
    resource_labels = local.fleet_labels
    service_account = google_service_account.fleet_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }
}

resource "kubernetes_namespace_v1" "seeded_upgrade" {
  provider = kubernetes.seeded_b

  metadata {
    name   = "seeded-upgrade"
    labels = local.fleet_labels
  }

  depends_on = [google_container_node_pool.seeded_b_default]
}

# Defect (upgrade readiness): a workload pinned to the pool that cannot
# surge. The nodeSelector names the pool itself, through the
# `cloud.google.com/gke-nodepool` label GKE puts on every node, so when that
# pool is drained these pods have nowhere to go — they do not move to the
# default pool, they go Pending and stay there. The fleet's other node-level
# fixture (`idle-nodepool`) addresses its pool the same way, and the harness
# resolves a node selector to a Terraform-declared pool name.
#
# This is the pair to the fixture above and the reason both exist: a check
# that reports the surge setting alone is reporting a configuration, while a
# check that joins it to what runs there is reporting an outage. The two
# together are what let a case ask whether the agent made that join.
#
# Asserted by readiness-pinned-workload.
resource "kubernetes_deployment_v1" "pinned_batch_runner" {
  provider = kubernetes.seeded_b

  metadata {
    name      = "pinned-batch-runner"
    namespace = kubernetes_namespace_v1.seeded_upgrade.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    replicas = 2

    selector {
      match_labels = { app = "pinned-batch-runner" }
    }

    template {
      metadata {
        labels = { app = "pinned-batch-runner" }
      }

      spec {
        node_selector = {
          "cloud.google.com/gke-nodepool" = "no-surge-pool"
        }

        container {
          name    = "pause"
          image   = "registry.k8s.io/pause:3.10"
          command = ["/pause"]

          resources {
            requests = {
              cpu    = "10m"
              memory = "16Mi"
            }
          }
        }
      }
    }
  }

  depends_on = [google_container_node_pool.no_surge_pool]
}

# Defect (upgrade readiness): an admission webhook that fails closed with no
# timeout. `failurePolicy: Fail` and no `timeoutSeconds` means that while the
# webhook's own backend is restarting — which is exactly what happens when
# the node it runs on is drained — every write it matches is rejected for the
# default thirty seconds per call. A drain that evicts the webhook's own pod
# then stalls on its own admission rule.
#
# Two properties of the real finding are planted here and one is not. The
# dangerous variant matches cluster-wide, and planting that on a standing
# shared cluster would reject writes for every scenario that touches
# seeded-b, not only for this fixture — the fleet is read-only for
# evaluations, and a fixture that can break unrelated runs is not worth the
# fidelity. So the namespaceSelector confines it to the seeded-upgrade
# namespace, and the check is expected to flag `failurePolicy: Fail` with no
# `timeoutSeconds`; the scope dimension is left to a unit test with a
# recorded manifest, where nothing can be broken by it.
#
# `clientConfig` names a Service that does not exist, which is what makes the
# fail-closed behaviour real rather than theoretical — and is safe precisely
# because the selector above bounds what it can reject.
#
# Asserted by readiness-failclosed-webhook.
resource "kubernetes_validating_webhook_configuration_v1" "fail_closed_gate" {
  provider = kubernetes.seeded_b

  metadata {
    name   = "seeded-fail-closed-gate"
    labels = local.fleet_labels
  }

  webhook {
    name                      = "gate.seeded.invalid"
    side_effects              = "None"
    admission_review_versions = ["v1"]
    failure_policy            = "Fail"

    client_config {
      service {
        name      = "nonexistent-admission-gate"
        namespace = kubernetes_namespace_v1.seeded_upgrade.metadata[0].name
        path      = "/validate"
      }
    }

    namespace_selector {
      match_labels = {
        "kubernetes.io/metadata.name" = kubernetes_namespace_v1.seeded_upgrade.metadata[0].name
      }
    }

    rule {
      api_groups   = [""]
      api_versions = ["v1"]
      operations   = ["CREATE"]
      resources    = ["configmaps"]
      scope        = "Namespaced"
    }
  }
}
