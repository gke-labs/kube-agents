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

# The ComputeClass fixtures, all on seeded-c. Each block names the CATALOGUE
# ROLE that addresses it, as defects-d.tf does; renaming an object here breaks
# that role's probe and `state` assertions in fixtures.json.
#
# Why seeded-c. The obtainability audit's 3.24 (`untargeted-compute-class-
# workload`) files nothing while the cluster has an untainted node pool with
# no compute-class label ("general capacity is available"), and the stockout
# checks the certification suite waits for reuse that predicate. seeded-a's
# default-pool must stay untainted and unlabelled for the fixtures it hosts,
# so the family lives on the one seeded cluster whose only pool can carry the
# class label: seeded-c, which main.tf labels for `general-ccc` and which
# carried no workloads before this file.
#
# Two nightly cases use seeded-c as a clean control, and they bound what this
# file may plant: cluster-agent-fleet-health-triage expects no failing
# workload here, so every pod below runs (10m, pause) rather than sitting
# Pending; upgrades-network-policy-enforcement-stated and
# upgrades-readiness-legacy-dataplane expect "namespaces with policies: none"
# here, so the namespace carries NO NetworkPolicy, unlike every other fixture
# namespace (compliance 2.6 on it is a declared background finding, README).
#
# The pods run on the class-labelled node: the checks read the manifests, the
# ComputeClass and the pool configuration, never the pod phase. Requests are
# the smallest the fleet uses: seeded-c's one e2-small carries GKE's system
# pods at about 94 percent of its allocatable memory (read 11-10 on a dev
# fleet), so five pods at 16Mi would strand one of them Pending.

provider "kubernetes" {
  alias                  = "seeded_c"
  host                   = "https://${google_container_cluster.seeded_c.endpoint}"
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(google_container_cluster.seeded_c.master_auth[0].cluster_ca_certificate)
}

# Its own name, not slot a's `seeded-capacity`: three nightly prompts send the
# agent to "inference-server in the seeded-capacity namespace", and a second
# namespace of that name on another cluster is where a search can stop.
resource "kubernetes_namespace_v1" "seeded_compute_class" {
  provider = kubernetes.seeded_c

  metadata {
    name   = "seeded-compute-class"
    labels = local.fleet_labels
  }

  depends_on = [google_container_node_pool.seeded_c_default]
}

# The class. One `nodepools` rule naming the labelled default-pool, then two
# machine-family rules nothing backs, auto-creation off, not the default class:
#   - the nodepools rule plus the label on default-pool is the shape the
#     pre-filter cases read (untargeted-compute-class-workload,
#     compute-class-affinity-workload, compute-class-toleration-workload);
#   - the two unbacked family rules with auto-creation off are the manual-pools
#     shape (manual-pools-compute-class): GKE can create no pool for a later
#     rule, so the fallback the class promises does not exist;
#   - the label without the matching taint is the untainted-pool shape
#     (untainted-compute-class-pool).
# Auto-creation stays off on a standing fixture: with it on, GKE (1.33.3 and
# later, without cluster-level auto-provisioning) would create node pools in
# every pool project on its own. No other field: every field GKE defaults
# stays out of `manifest`, so the daily plan reads converged.
#
# kubernetes_manifest reads the ComputeClass CRD at plan time. GKE installs it
# with every Standard cluster at the versions the fleet runs, so a project's
# first apply targets seeded_c alongside seeded_a (README, "State and
# reconcile"; scripts/provision_ci_pool_project.sh).
resource "kubernetes_manifest" "general_ccc" {
  provider = kubernetes.seeded_c

  manifest = {
    apiVersion = "cloud.google.com/v1"
    kind       = "ComputeClass"
    metadata = {
      name   = "general-ccc"
      labels = local.fleet_labels
    }
    spec = {
      priorities = [
        { nodepools = ["default-pool"] },
        { machineFamily = "n4" },
        { machineFamily = "c3" },
      ]
      nodePoolAutoCreation = { enabled = false }
    }
  }

  depends_on = [google_container_node_pool.seeded_c_default]
}

# Defect (stockout, obtainability 3.24): three replicas with no
# `cloud.google.com/compute-class` nodeSelector, no compute-class affinity, in a
# namespace with no default class, on a cluster whose only untainted pool
# carries the class label. The Cluster Autoscaler's pre-filter leaves such pods
# out of the scale-up simulation for ComputeClass-backed pools; the audit must
# name the workload from the manifest alone.
# Addressed by the untargeted-compute-class-workload role.
resource "kubernetes_deployment_v1" "app_worker_a" {
  provider = kubernetes.seeded_c

  metadata {
    name      = "app-worker-a"
    namespace = kubernetes_namespace_v1.seeded_compute_class.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    replicas = 3

    selector {
      match_labels = { app = "app-worker-a" }
    }

    template {
      metadata {
        labels = { app = "app-worker-a" }
      }

      spec {
        # Compliance SOP 2.7 and 2.11: the closures every planted workload in
        # defects-a.tf carries, so this fixture adds no finding beyond the ones
        # README.md declares.
        automount_service_account_token = false

        security_context {
          run_as_non_root = true
          run_as_user     = 65534
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

        # Reliability SOP 3.8: a multi-replica workload with no spreading
        # mechanism is a finding (read live: `no-spread` on this Deployment
        # without it). Soft, as every other planted workload's: ScheduleAnyway
        # never blocks scheduling on a one-node pool.
        topology_spread_constraint {
          max_skew           = 1
          topology_key       = "kubernetes.io/hostname"
          when_unsatisfiable = "ScheduleAnyway"
          label_selector {
            match_labels = { app = "app-worker-a" }
          }
        }

        container {
          name    = "worker"
          image   = "registry.k8s.io/pause:3.10"
          command = ["/pause"]

          security_context {
            allow_privilege_escalation = false
            # Set at the container too: the provider writes this block with
            # runAsNonRoot false when it is unset (defects-a.tf, token-reader).
            run_as_non_root = true
            capabilities {
              drop = ["ALL"]
            }
          }

          resources {
            requests = {
              cpu    = "10m"
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

  wait_for_rollout = false

  depends_on = [kubernetes_manifest.general_ccc]
}

# Reliability SOP 3.3 background closure: three replicas with no budget would
# be a second no-pdb finding beside the planted checkout-gateway on slot a.
# maxUnavailable 100% blocks no drain (the first-zone-sponge shape on slot d).
resource "kubernetes_pod_disruption_budget_v1" "app_worker_a" {
  provider = kubernetes.seeded_c

  metadata {
    name      = "app-worker-a"
    namespace = kubernetes_namespace_v1.seeded_compute_class.metadata[0].name
  }

  spec {
    max_unavailable = "100%"
    selector {
      match_labels = { app = "app-worker-a" }
    }
  }
}

# Defect (stockout): the class named only in nodeAffinity, never in
# nodeSelector. The pre-filter reads `nodeSelector` alone, so a workload
# targeted this way is dropped from the scale-up simulation while reading as
# correctly targeted.
# Addressed by the compute-class-affinity-workload role.
resource "kubernetes_deployment_v1" "app_worker_b" {
  provider = kubernetes.seeded_c

  metadata {
    name      = "app-worker-b"
    namespace = kubernetes_namespace_v1.seeded_compute_class.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    replicas = 1

    selector {
      match_labels = { app = "app-worker-b" }
    }

    template {
      metadata {
        labels = { app = "app-worker-b" }
      }

      spec {
        automount_service_account_token = false

        security_context {
          run_as_non_root = true
          run_as_user     = 65534
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

        affinity {
          node_affinity {
            required_during_scheduling_ignored_during_execution {
              node_selector_term {
                match_expressions {
                  key      = "cloud.google.com/compute-class"
                  operator = "In"
                  values   = ["general-ccc"]
                }
              }
            }
          }
        }

        container {
          name    = "worker"
          image   = "registry.k8s.io/pause:3.10"
          command = ["/pause"]

          security_context {
            allow_privilege_escalation = false
            run_as_non_root            = true
            capabilities {
              drop = ["ALL"]
            }
          }

          resources {
            requests = {
              cpu    = "10m"
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

  wait_for_rollout = false

  depends_on = [kubernetes_manifest.general_ccc]
}

# Defect (stockout): targeted through the nodeSelector, with a hand-written
# toleration for the class taint that names the key, the value and the effect
# and no operator. The pool it targets carries no taint (the untainted-pool
# defect above), so the toleration has no runtime effect here and the pod
# runs; the check this plants for reads the toleration's shape against the
# taint GKE puts on class nodes, not a scheduling failure. The pod is also
# what keeps default-pool from reading as idle.
#
# A kubernetes_manifest, not kubernetes_deployment_v1: the typed resource
# writes `operator: Equal` into a toleration that names none (the provider's
# default), and the API server stores a toleration without the field as
# written, so only a raw manifest plants the shape. The role's state asserts
# the field's absence.
# Addressed by the compute-class-toleration-workload role.
resource "kubernetes_manifest" "batch_worker_a" {
  provider = kubernetes.seeded_c

  manifest = {
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata = {
      name      = "batch-worker-a"
      namespace = kubernetes_namespace_v1.seeded_compute_class.metadata[0].name
      labels    = local.fleet_labels
    }
    spec = {
      replicas = 1
      selector = { matchLabels = { app = "batch-worker-a" } }
      template = {
        metadata = { labels = { app = "batch-worker-a" } }
        spec = {
          automountServiceAccountToken = false
          securityContext = {
            runAsNonRoot   = true
            runAsUser      = 65534
            seccompProfile = { type = "RuntimeDefault" }
          }
          nodeSelector = { "cloud.google.com/compute-class" = "general-ccc" }
          tolerations = [
            {
              key    = "cloud.google.com/compute-class"
              value  = "general-ccc"
              effect = "NoSchedule"
            },
          ]
          containers = [
            {
              name    = "worker"
              image   = "registry.k8s.io/pause:3.10"
              command = ["/pause"]
              securityContext = {
                allowPrivilegeEscalation = false
                runAsNonRoot             = true
                capabilities             = { drop = ["ALL"] }
              }
              resources = {
                requests = { cpu = "10m", memory = "8Mi" }
                limits   = { memory = "16Mi" }
              }
            },
          ]
        }
      }
    }
  }

  depends_on = [kubernetes_manifest.general_ccc]
}
