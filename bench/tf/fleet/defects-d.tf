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
# multi-zonal shape main.tf gives it. Each block names the CATALOGUE ROLE
# that addresses it -- not a scenario, unlike defects-a.tf: no case reads
# these yet. Renaming a workload here breaks that role's probe and `state`
# assertions in fixtures.json, which surfaces as fixture drift on the hourly
# scan rather than as a red case.
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

# Compliance SOP 2.6 flags a non-system namespace that has workloads and no
# NetworkPolicy. These pods use no network at all, so a default-deny closes
# the finding at zero fixture risk, as defects-a.tf does on slot a.
resource "kubernetes_network_policy_v1" "seeded_topology_default_deny" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "default-deny"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

# The two small fixtures schedule ahead of the capacity one. Without this the
# three compete for the same two e2-smalls in whatever order a node rebuild
# happens to produce: capacity-starved-worker pods landing first leave too
# little for zone-pinned-api, whose node affinity gives it nowhere else to
# go, and the scheduling and volume fixtures then read as drifted for a reason
# that is not their own. Slot a solves the same problem with a taint on
# pinned-inference-pool; here the workloads must share a pool, because the
# capacity fixture has to be starved by a real node, so priority is the lever
# rather than isolation.
#
# Far below the system-critical floor on purpose: these must never preempt
# anything GKE runs.
resource "kubernetes_priority_class_v1" "seeded_topology_fixture" {
  provider = kubernetes.seeded_d

  metadata {
    name = "seeded-topology-fixture"
  }

  value       = 100
  description = "Seeded-fleet zonal-skew fixtures that must schedule before the capacity fixture."
}

# Defect (skew cause 1: the scheduler was allowed to give up). Two replicas
# with a topology spread constraint whose `whenUnsatisfiable` is
# ScheduleAnyway, plus a node affinity that only the first zone satisfies. The
# constraint reads as if it spreads; ScheduleAnyway means the scheduler treats
# it as a preference and places both pods in one zone anyway, which is exactly
# the misconfiguration operators mistake for protection. A check that reports
# "skew" here without naming ScheduleAnyway has not done the job.
#
# Two replicas rather than four, and that is a constraint. At three or more a
# Deployment with no HorizontalPodAutoscaler trips obtainability SOP 3.5, and
# an HPA would move the replica count out from under a fixture whose whole
# subject is where the replicas sit. Two is the smallest count on which "all
# of them are in one zone" is a statement about a distribution.
#
# Addressed by the zonal-skew-scheduling role.
resource "kubernetes_deployment_v1" "zone_pinned_api" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "zone-pinned-api"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    replicas = 2

    selector {
      match_labels = { app = "zone-pinned-api" }
    }

    template {
      metadata {
        labels = { app = "zone-pinned-api" }
      }

      spec {
        priority_class_name = kubernetes_priority_class_v1.seeded_topology_fixture.metadata[0].name

        # Compliance SOP 2.7 and 2.11, obtainability SOP 3.2: the closures
        # every planted workload in defects-a.tf carries, so these fixtures
        # add no finding beyond the ones README.md declares.
        automount_service_account_token = false

        security_context {
          run_as_non_root = true
          run_as_user     = 65534
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

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
            limits = {
              memory = "32Mi"
            }
          }
        }
      }
    }
  }

  # These pods are meant to sit two-in-one-zone, which is a satisfied rollout,
  # but the priority class below can leave them briefly Pending behind a
  # rebuild. Not waiting keeps an apply from turning a scheduling delay into a
  # provisioning failure, the way defects-a.tf does for its own fixtures.
  wait_for_rollout = false

  depends_on = [google_container_node_pool.seeded_d_default]
}

# Reliability SOP 3.3 background closure: zone-pinned-api runs two replicas
# with no PodDisruptionBudget, which is the planted checkout-gateway defect on
# slot a -- and obtainability-fleet-exposure-sweep requires that workload to be
# the fleet's only right answer. maxUnavailable 1 is the SOP's structurally
# safe shape and blocks no drain. defects-a.tf gives inference-server the same
# budget for the same reason.
resource "kubernetes_pod_disruption_budget_v1" "zone_pinned_api" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "zone-pinned-api"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
  }

  spec {
    max_unavailable = "1"
    selector {
      match_labels = { app = "zone-pinned-api" }
    }
  }
}

# The zonal class the volume fixture binds to. GKE's standard-rwo is zonal
# as well, but it lets the scheduler pick the zone per claim, and a second
# replica could then land in the other zone and spread the StatefulSet on
# its own. allowedTopologies pins every claim of this class to var.zone, so
# the skew is a property of the storage and not of where two disks happened
# to be provisioned.
resource "kubernetes_storage_class_v1" "seeded_zonal_pd" {
  provider = kubernetes.seeded_d

  metadata {
    name   = "seeded-zonal-pd"
    labels = local.fleet_labels
  }

  storage_provisioner = "pd.csi.storage.gke.io"
  reclaim_policy      = "Delete"
  volume_binding_mode = "WaitForFirstConsumer"
  parameters = {
    type = "pd-balanced"
  }

  allowed_topologies {
    match_label_expressions {
      key    = "topology.gke.io/zone"
      values = [var.zone]
    }
  }
}

# Reliability SOP 3.3 background closure for the two-replica StatefulSet,
# the same budget zone-pinned-api carries and for the same reason.
resource "kubernetes_pod_disruption_budget_v1" "zone_bound_store" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "zone-bound-store"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
  }

  spec {
    max_unavailable = "1"
    selector {
      match_labels = { app = "zone-bound-store" }
    }
  }
}

# Defect (skew cause 2: a volume pinned it). A StatefulSet whose PVC binds a
# zonal PersistentDisk. The pod cannot move zones without leaving its data,
# so the skew is a storage fact rather than a scheduling one, and the
# remediation an agent should propose is different in kind: the scheduling
# case is a manifest fix, this one is a data migration.
#
# Two replicas, because one pod has no distribution to skew: the anomaly
# check that owns this fixture scopes itself to replicas > 1. The class
# below names only var.zone in allowedTopologies, so both claims bind there
# and both pods sit in one zone for a reason no manifest edit can move.
# Two 1Gi disks are the price. zone-pinned-api's maxUnavailable 1 budget is
# carried here too, for the same reliability-SOP background reason.
#
# Addressed by the zonal-skew-volume role.
resource "kubernetes_stateful_set_v1" "zone_bound_store" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "zone-bound-store"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    service_name = "zone-bound-store"
    replicas     = 2

    selector {
      match_labels = { app = "zone-bound-store" }
    }

    template {
      metadata {
        labels = { app = "zone-bound-store" }
      }

      spec {
        # Compliance SOP 2.7 and 2.11, obtainability SOP 3.2: the closures
        # every planted workload in defects-a.tf carries, so these fixtures
        # add no finding beyond the ones README.md declares.
        automount_service_account_token = false

        security_context {
          run_as_non_root = true
          run_as_user     = 65534
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

        # Obtainability SOP 3.8: a soft spread on hostname, which the zonal
        # class below overrides in practice and which keeps the two-replica
        # set off the no-spread list without touching the zone pin that is
        # the fixture.
        topology_spread_constraint {
          max_skew           = 1
          topology_key       = "kubernetes.io/hostname"
          when_unsatisfiable = "ScheduleAnyway"
          label_selector {
            match_labels = { app = "zone-bound-store" }
          }
        }

        priority_class_name = kubernetes_priority_class_v1.seeded_topology_fixture.metadata[0].name

        container {
          name    = "pause"
          image   = "registry.k8s.io/pause:3.10"
          command = ["/pause"]

          resources {
            requests = {
              cpu    = "10m"
              memory = "16Mi"
            }
            limits = {
              memory = "32Mi"
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
        storage_class_name = kubernetes_storage_class_v1.seeded_zonal_pd.metadata[0].name

        resources {
          requests = {
            storage = "1Gi"
          }
        }
      }
    }
  }

  wait_for_rollout = false

  depends_on = [google_container_node_pool.seeded_d_default]
}

# The capacity skew has to be planted, not left to chance. How much CPU each
# e2-small has left depends on where GKE's own Deployments land, and if the
# two nodes end up within a replica of each other the soft spread below puts
# one capacity-starved-worker in each zone, two stay Pending, and the state
# assertions pass on a cluster with no skew in it. var.zone's node is the
# crowded one by construction: the other two fixtures and their claims are
# pinned there. This sponge keeps it that way whatever a node rebuild does
# to the system set: six 100m replicas pinned to var.zone, scheduled behind
# the two fixtures and ahead of the capacity worker (priority 90, far below
# anything GKE runs), pinned to the first zone's pool,
# absorb whatever CPU that node has free in 100m steps and leave less than a
# 120m replica behind; the replicas that do not fit stay Pending, which is
# their job. Every capacity-starved-worker that runs therefore runs in the
# second zone, and the surplus is Pending for want of CPU, not for a zone
# pin an agent could blame. Memory is kept tiny so CPU stays the axis that
# binds. Its priority sits below the two fixtures that share the node (90 to
# their 100), so they take their 40m first and the sponge can neither starve
# nor preempt them, and above the capacity worker (default 0), which is the
# ordering that matters. Its budget allows every disruption: the sponge must
# not read as an unprotected multi-replica workload (the sweep's one right
# answer is checkout-gateway on slot a) and must never hold a drain. Measured
# on the first apply: 47m free on var.zone's node, so all six sponge replicas
# sat Pending and two workers ran in the second zone.
resource "kubernetes_priority_class_v1" "seeded_topology_sponge" {
  provider = kubernetes.seeded_d

  metadata {
    name = "seeded-topology-sponge"
  }

  value       = 90
  description = "Seeded-fleet sponge that takes the first zone's CPU headroom after the two pinned fixtures and before the capacity fixture schedule."
}

resource "kubernetes_deployment_v1" "first_zone_sponge" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "first-zone-sponge"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    replicas = 6

    selector {
      match_labels = { app = "first-zone-sponge" }
    }

    template {
      metadata {
        labels = { app = "first-zone-sponge" }
      }

      spec {
        priority_class_name = kubernetes_priority_class_v1.seeded_topology_sponge.metadata[0].name

        # Compliance SOP 2.7 and 2.11, obtainability SOP 3.2 closures.
        automount_service_account_token = false

        security_context {
          run_as_non_root = true
          run_as_user     = 65534
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

        # Pinned by pool rather than by zone: main.tf gives the first zone
        # its own pool, and obtainability SOP 3.7 reads a zone pin on a
        # Deployment as rigid scheduling, which is zone-pinned-api's planted
        # finding and must stay the only one.
        node_selector = {
          "cloud.google.com/gke-nodepool" = google_container_node_pool.seeded_d_default.name
        }

        # Obtainability SOP 3.8: a soft spread, which changes nothing on a
        # one-node pool and keeps this off the no-spread list.
        topology_spread_constraint {
          max_skew           = 1
          topology_key       = "kubernetes.io/hostname"
          when_unsatisfiable = "ScheduleAnyway"
          label_selector {
            match_labels = { app = "first-zone-sponge" }
          }
        }

        container {
          name    = "pause"
          image   = "registry.k8s.io/pause:3.10"
          command = ["/pause"]
          resources {
            requests = {
              cpu    = "100m"
              memory = "8Mi"
            }
            limits = {
              memory = "16Mi"
            }
          }
        }
      }
    }
  }

  # Replicas that find no room stay Pending by design, as the capacity
  # fixture's own surplus does.
  wait_for_rollout = false
  depends_on       = [google_container_node_pool.seeded_d_default]
}

# Obtainability SOP 3.3 closure for the sponge, permissive on purpose: a
# budget that allows every disruption keeps it off the no-pdb list without
# ever holding a drain, which a readiness check would otherwise read as the
# drain blocker seeded-b plants.
resource "kubernetes_pod_disruption_budget_v1" "first_zone_sponge" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "first-zone-sponge"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
  }

  spec {
    max_unavailable = "100%"
    selector {
      match_labels = { app = "first-zone-sponge" }
    }
  }
}

# Defect (skew cause 3: capacity, not configuration). A Deployment whose
# replicas cannot all be placed, because together they request more CPU than
# the two e2-smalls have left. Some pods run, the rest stay Pending with an
# insufficient-cpu event -- the shape a real stockout produces, without
# needing a real stockout.
#
# This is the case an agent most often gets wrong: the distribution looks
# identical to the scheduling defect above, and only the Pending pods' events
# say the cause is capacity. A check that reports a misconfiguration here is
# wrong in a way that sends someone to edit a manifest that is correct.
#
# Addressed by the zonal-skew-capacity role.
resource "kubernetes_deployment_v1" "capacity_starved_worker" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "capacity-starved-worker"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    replicas = 4

    selector {
      match_labels = { app = "capacity-starved-worker" }
    }

    template {
      metadata {
        labels = { app = "capacity-starved-worker" }
      }

      spec {
        # Compliance SOP 2.7 and 2.11, obtainability SOP 3.2 closures.
        automount_service_account_token = false

        security_context {
          run_as_non_root = true
          run_as_user     = 65534
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

        # Obtainability SOP 3.8: a soft spread, which cannot block scheduling
        # and so cannot interfere with the capacity the fixture is about.
        topology_spread_constraint {
          max_skew           = 1
          topology_key       = "topology.kubernetes.io/zone"
          when_unsatisfiable = "ScheduleAnyway"

          label_selector {
            match_labels = { app = "capacity-starved-worker" }
          }
        }

        container {
          name    = "pause"
          image   = "registry.k8s.io/pause:3.10"
          command = ["/pause"]

          resources {
            requests = {
              # Sized against the two unequal nodes main.tf gives seeded-d.
              # The first zone's e2-small never has 500m free: the sponge
              # keeps it under 100m whatever the system set does. The second
              # zone's e2-standard-2 (1930m allocatable) keeps at least about
              # 800m free with the whole system set on it and at most about
              # 1600m with none of it, so one to three workers run there and
              # four never fit (2000m). Both halves of the role's state hold
              # on every placement GKE can produce: at least one Running,
              # at least one Pending, all of the running ones in one zone.
              cpu    = "500m"
              memory = "64Mi"
            }
            limits = {
              memory = "128Mi"
            }
          }
        }
      }
    }
  }

  # The surplus replicas never schedule -- that IS the defect. Without this the
  # apply blocks on a rollout that cannot finish and the scheduled reconcile
  # reads as a provisioning failure, exactly as defects-a.tf records for
  # inference-server.
  wait_for_rollout = false

  depends_on = [google_container_node_pool.seeded_d_default]
}

# Reliability SOP 3.3 background closure, as for zone-pinned-api above, but
# spelled minAvailable. maxUnavailable: 1 counts against the four replicas the
# Deployment asks for, not the ones that fit, so it would hold
# disruptionsAllowed at 0 for as long as the fixture holds: every GKE drain of
# the node carrying the running replicas would wait out the ~1h PDB
# force-drain timeout, the side effect defects-a.tf records for
# inference-server, and seeded-d would carry a budget whose status blocks a
# drain, which the readiness case says only seeded-b does. minAvailable: 1 is
# below the four-replica floor, which obtainability SOP 3.4 does not flag, and
# leaves one eviction at the measured split; it reads 0 only if a single
# replica finds room, the low end of the band above.
#
# capacity-starved-worker keeps four replicas because the fixture is that
# they do not all fit, which also means obtainability SOP 3.5 no-hpa applies
# to it -- an HPA would move the replica count the fixture depends on, so
# that row is declared in README.md rather than closed here.
resource "kubernetes_pod_disruption_budget_v1" "capacity_starved_worker" {
  provider = kubernetes.seeded_d

  metadata {
    name      = "capacity-starved-worker"
    namespace = kubernetes_namespace_v1.seeded_topology.metadata[0].name
  }

  spec {
    min_available = "1"
    selector {
      match_labels = { app = "capacity-starved-worker" }
    }
  }
}
