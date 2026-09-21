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

# The zonal-skew defects, all on seeded-d because the skew needs the
# multi-zonal shape main.tf gives it. Each block names the scenario that
# asserts on it; change a name here and that scenario's exact check goes red,
# which is the intended failure mode.
#
# Three separate workloads on purpose. Noticing that pods are not spread is
# the easy half; the anomaly checks have to say WHY, and a single skewed
# workload cannot distinguish "the scheduler was told to prefer one zone"
# from "a volume pinned it there". One cause per workload is what makes the
# attribution assertable rather than a guess that happened to be right.

provider "kubernetes" {
  alias                  = "seeded_d"
  host                   = "https://${google_container_cluster.seeded_d.endpoint}"
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(google_container_cluster.seeded_d.master_auth[0].cluster_ca_certificate)
}

resource "kubernetes_namespace_v1" "seeded_topology" {
  provider = kubernetes.seeded_d

  metadata {
    name   = "seeded-topology"
    labels = local.fleet_labels
  }

  depends_on = [google_container_node_pool.seeded_d_default]
}

# Defect (skew cause 1: the scheduler was allowed to give up). Four replicas
# with a topology spread constraint whose `whenUnsatisfiable` is
# ScheduleAnyway, plus a node affinity that only the first zone satisfies. The
# constraint reads as if it spreads; ScheduleAnyway means the scheduler treats
# it as a preference and places every pod in one zone anyway, which is exactly
# the misconfiguration operators mistake for protection. A check that reports
# "skew" here without naming ScheduleAnyway has not done the job.
#
# Asserted by anomaly-zonal-skew-scheduling.
resource "kubernetes_deployment_v1" "zone_pinned_api" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "zone-pinned-api"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    replicas = 4

    selector {
      match_labels = { app = "zone-pinned-api" }
    }

    template {
      metadata {
        labels = { app = "zone-pinned-api" }
      }

      spec {
        # The constraint that looks like protection and is not.
        topology_spread_constraint {
          max_skew           = 1
          topology_key       = "topology.kubernetes.io/zone"
          when_unsatisfiable = "ScheduleAnyway"

          label_selector {
            match_labels = { app = "zone-pinned-api" }
          }
        }

        # And the reason it never spreads: only var.zone matches.
        affinity {
          node_affinity {
            required_during_scheduling_ignored_during_execution {
              node_selector_term {
                match_expressions {
                  key      = "topology.kubernetes.io/zone"
                  operator = "In"
                  values   = [var.zone]
                }
              }
            }
          }
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

  depends_on = [google_container_node_pool.seeded_d_default]
}

# Defect (skew cause 2: a volume pinned it). A StatefulSet whose PVC binds a
# zonal PersistentDisk. The pod cannot move zones without leaving its data,
# so the skew is a storage fact rather than a scheduling one, and the
# remediation an agent should propose is different in kind: the scheduling
# case is a manifest fix, this one is a data migration.
#
# One replica, deliberately: the fixture is the immovability, not the count,
# and a second replica would double the disk.
#
# Asserted by anomaly-zonal-skew-volume.
resource "kubernetes_stateful_set_v1" "zone_bound_store" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "zone-bound-store"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    service_name = "zone-bound-store"
    replicas     = 1

    selector {
      match_labels = { app = "zone-bound-store" }
    }

    template {
      metadata {
        labels = { app = "zone-bound-store" }
      }

      spec {
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

          volume_mount {
            name       = "data"
            mount_path = "/data"
          }
        }
      }
    }

    volume_claim_template {
      metadata {
        name = "data"
      }

      spec {
        access_modes = ["ReadWriteOnce"]
        # standard-rwo is zonal: the disk lands in whichever zone the first
        # pod scheduled into, and from then on the pod is bound to that zone.
        storage_class_name = "standard-rwo"

        resources {
          requests = {
            storage = "1Gi"
          }
        }
      }
    }
  }

  depends_on = [google_container_node_pool.seeded_d_default]
}

# Defect (skew cause 3: capacity, not configuration). A Deployment whose
# replicas cannot all be placed, because each requests more CPU than the
# remaining allocatable in the second zone's single e2-small. Some pods run,
# the rest stay Pending with an insufficient-cpu event -- the shape a real
# stockout produces, without needing a real stockout.
#
# This is the case an agent most often gets wrong: the distribution looks
# identical to the scheduling defect above, and only the Pending pods' events
# say the cause is capacity. A check that reports a misconfiguration here is
# wrong in a way that sends someone to edit a manifest that is correct.
#
# Asserted by anomaly-zonal-skew-capacity.
resource "kubernetes_deployment_v1" "capacity_starved_worker" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "capacity-starved-worker"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    replicas = 3

    selector {
      match_labels = { app = "capacity-starved-worker" }
    }

    template {
      metadata {
        labels = { app = "capacity-starved-worker" }
      }

      spec {
        container {
          name    = "pause"
          image   = "registry.k8s.io/pause:3.10"
          command = ["/pause"]

          resources {
            requests = {
              # An e2-small allocates ~940m, most of it already claimed by the
              # system set. At 400m each, one or two of these fit across the
              # pair and the rest stay Pending -- enough to be uneven, not so
              # much that nothing schedules at all and the fixture reads as
              # broken rather than skewed.
              cpu    = "400m"
              memory = "64Mi"
            }
          }
        }
      }
    }
  }

  depends_on = [google_container_node_pool.seeded_d_default]
}
