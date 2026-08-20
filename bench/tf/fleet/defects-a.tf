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

# The in-cluster defects, all on seeded-a. Each block names the scenario
# that asserts on it; change a name here and that scenario's exact check
# goes red, which is the intended failure mode -- the names are the test.

data "google_client_config" "default" {}

provider "kubernetes" {
  host                   = "https://${google_container_cluster.seeded_a.endpoint}"
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(google_container_cluster.seeded_a.master_auth[0].cluster_ca_certificate)
}

resource "kubernetes_namespace_v1" "seeded_reliability" {
  metadata {
    name   = "seeded-reliability"
    labels = local.fleet_labels
  }
  depends_on = [google_container_node_pool.seeded_a_default]
}

resource "kubernetes_namespace_v1" "seeded_security" {
  metadata {
    name   = "seeded-security"
    labels = local.fleet_labels
  }
  depends_on = [google_container_node_pool.seeded_a_default]
}

resource "kubernetes_namespace_v1" "seeded_debug" {
  metadata {
    name   = "seeded-debug"
    labels = local.fleet_labels
  }
  depends_on = [google_container_node_pool.seeded_a_default]
}

resource "kubernetes_namespace_v1" "seeded_capacity" {
  metadata {
    name   = "seeded-capacity"
    labels = local.fleet_labels
  }
  depends_on = [google_container_node_pool.pinned_inference_pool]
}

# Defect (reliability): single replica, no PodDisruptionBudget. A node drain
# takes it to zero. Asserted by obtainability-planted-pdb.
resource "kubernetes_deployment_v1" "checkout_gateway" {
  metadata {
    name      = "checkout-gateway"
    namespace = kubernetes_namespace_v1.seeded_reliability.metadata[0].name
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "checkout-gateway" }
    }
    template {
      metadata {
        labels = { app = "checkout-gateway" }
      }
      spec {
        container {
          name  = "gateway"
          image = "registry.k8s.io/pause:3.9"
          resources {
            requests = { cpu = "10m", memory = "16Mi" }
            limits   = { memory = "32Mi" }
          }
        }
      }
    }
  }
}

# Defect (security): the classic over-grant -- cluster-admin bound to the
# namespace's default ServiceAccount. Asserted by compliance-rbac-overgrant.
# A RoleBinding to a ClusterRole grants the role's permissions within this
# namespace only, but binding cluster-admin to default is exactly the shape
# the audit exists to flag.
resource "kubernetes_role_binding_v1" "debug_binding" {
  metadata {
    name      = "debug-binding"
    namespace = kubernetes_namespace_v1.seeded_security.metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "cluster-admin"
  }
  subject {
    kind      = "ServiceAccount"
    name      = "default"
    namespace = kubernetes_namespace_v1.seeded_security.metadata[0].name
  }
}

# Defect (debugging, remediation): a deterministic OOM crashloop. tail on
# /dev/zero buffers without bound, so every start hits the 64Mi limit and
# dies OOMKilled -- the noun the RCA must contain. Asserted by
# cluster-agent-crashloop-debug and used as the issue fixture by the
# remediation scenario.
resource "kubernetes_deployment_v1" "payments_api" {
  metadata {
    name      = "payments-api"
    namespace = kubernetes_namespace_v1.seeded_debug.metadata[0].name
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "payments-api" }
    }
    template {
      metadata {
        labels = { app = "payments-api" }
      }
      spec {
        container {
          name    = "api"
          image   = "busybox:1.36"
          command = ["sh", "-c", "tail /dev/zero"]
          resources {
            requests = { cpu = "10m", memory = "32Mi" }
            limits   = { memory = "64Mi" }
          }
        }
      }
    }
  }

  # The deployment never becomes Ready -- that is the defect. Without this,
  # every apply of the stack blocks on a rollout that cannot finish and the
  # scheduled reconcile reads as a provisioning failure.
  wait_for_rollout = false
}

# Defect (capacity): the workload half of pinned-inference-pool, and the
# live signal the audit reads. The container burns a full core against a
# 400m request (capped at 500m), so CPU utilization sits at ~125% of request
# against the HPA's 60% target and the HPA pins at max_replicas wanting
# pods the pool can never place: an e2-small allocates ~940m CPU, system
# daemonsets take ~250m, so one 400m replica fits and the second does not --
# with the autoscaler pinned at one node, replicas 2..10 sit Pending. That
# standing Pending backlog is the shortfall the audit must quantify.
resource "kubernetes_deployment_v1" "inference_server" {
  metadata {
    name      = "inference-server"
    namespace = kubernetes_namespace_v1.seeded_capacity.metadata[0].name
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "inference-server" }
    }
    template {
      metadata {
        labels = { app = "inference-server" }
      }
      spec {
        node_selector = {
          "seeded-role" = "pinned-inference"
        }
        toleration {
          key      = "seeded-role"
          operator = "Equal"
          value    = "pinned-inference"
          effect   = "NoSchedule"
        }
        container {
          name    = "server"
          image   = "busybox:1.36"
          command = ["sh", "-c", "while true; do :; done"]
          resources {
            requests = { cpu = "400m", memory = "64Mi" }
            limits   = { cpu = "500m", memory = "128Mi" }
          }
        }
      }
    }
  }

  # The HPA owns replicas from the moment it syncs, and most of what it asks
  # for can never become Ready -- both halves are the defect. Ignoring the
  # drift keeps the reconcile from resetting the HPA's count, and skipping
  # the rollout wait keeps an apply from blocking on Pending pods forever.
  wait_for_rollout = false
  lifecycle {
    ignore_changes = [spec[0].replicas]
  }
}

resource "kubernetes_horizontal_pod_autoscaler_v2" "inference_server" {
  metadata {
    name      = "inference-server"
    namespace = kubernetes_namespace_v1.seeded_capacity.metadata[0].name
  }
  spec {
    min_replicas = 1
    max_replicas = 10
    scale_target_ref {
      api_version = "apps/v1"
      kind        = "Deployment"
      name        = kubernetes_deployment_v1.inference_server.metadata[0].name
    }
    metric {
      type = "Resource"
      resource {
        name = "cpu"
        target {
          type                = "Utilization"
          average_utilization = 60
        }
      }
    }
  }
}
