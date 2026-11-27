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
# no compute-class label ("general capacity is available"). seeded-a's
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
# the smallest the fleet uses, and the pool is an e2-standard-2: main.tf's
# seeded_c_default says why an e2-small could not hold these five pods and
# why an e2-medium did not keep them through a node replacement.

provider "kubernetes" {
  alias                  = "seeded_c"
  host                   = "https://${google_container_cluster.seeded_c.endpoint}"
  cluster_ca_certificate = base64decode(google_container_cluster.seeded_c.master_auth[0].cluster_ca_certificate)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "gke-gcloud-auth-plugin"
  }
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

# The class `general-ccc`. One `nodepools` rule naming the labelled
# default-pool, then two machine-family rules nothing backs, auto-creation
# off, not the default class:
#   - the nodepools rule plus the label on default-pool is the shape the
#     obtainability audit's 3.24 reads (untargeted-compute-class-workload); the
#     roles compute-class-affinity-workload and
#     compute-class-toleration-workload stand on it as controls no case reads;
#   - the two unbacked family rules with auto-creation off are the manual-pools
#     shape (manual-pools-compute-class): GKE can create no pool for a later
#     rule, so the fallback the class promises does not exist;
#   - the label without the matching taint is the untainted-pool shape
#     (untainted-compute-class-pool).
# Auto-creation stays off on this class: workloads select it, so with it on,
# GKE (1.33.3 and later, without cluster-level auto-provisioning) would
# create node pools for them in every pool project on its own. No other
# field: every field GKE defaults stays out of `manifest`, so the daily plan
# reads converged.
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

# Defect (stockout, a class that names a reservation that is not there): a
# second ComputeClass, `reserved-ccc`. Its first rule asks for one named
# reservation (`affinity: Specific`), `seeded-absent-reservation`: the name
# that slot a's missing-reservation-pool uses, and that no reservation of the
# project has. Its second rule is a machine type that GKE can create, of
# another family and another size than the first (`n2-standard-2`, then
# `e2-standard-4`). The two rules differ in two dimensions, so the class has a
# fallback and the stockout audit's 3.1 (`ccc-missing-fallbacks`) does not
# file it. GKE accepts the class and marks it misconfigured:
# `CrdMisconfigured` is True with the reason `ReservationNotFound`, and
# `Health` is False (read on GKE 1.36, on a dev fleet). The stockout audit's
# 3.12(b) files that status as `dangling-compute-class`.
#
# Auto-creation is ON here, unlike `general-ccc`. With it off, GKE reports
# another reason (`NapDisabledAndNoMatchingNodegroups`), says nothing about
# the reservation, and the audit files a second finding (3.13). GKE makes a
# node pool for a class only for a pod that asks for the class: by its name
# (a nodeSelector or an affinity) or through a default class label on its
# namespace. No workload asks for this one, in this stack or in a case, and a
# test of the stack holds that (tests/test_fleet_fixture_state.py), so no pool
# appears; the role asserts that no node carries the class label, so a pool
# that appears reads as drift. `whenUnsatisfiable` is written, so a pod that
# asks for the class gets no node outside its two rules.
#
# The class has one dedication taint (`dedicated=reserved-ccc:NoSchedule` in
# `nodePoolConfig.taints`), for two reasons. The obtainability audit's 3.24
# counts the untainted general-purpose classes of a cluster and names a
# target class in its finding on app-worker-a only when there is exactly one;
# with the taint this class is not counted, so that finding still names
# `general-ccc`. And a pod needs a toleration for the taint before GKE makes
# a node of the class for it: the contract of a taint in `nodePoolConfig`.
# No run made a pod to prove it, so the test that nothing in the stack asks
# for the class stays the first barrier against a paid node pool.
#
# No `project` under `specific`: the reservation would be the project's own.
# No other field: every field GKE defaults stays out of `manifest`, so the
# daily plan reads converged.
# Catalogue role: missing-reservation-compute-class.
resource "kubernetes_manifest" "reserved_ccc" {
  provider = kubernetes.seeded_c

  manifest = {
    apiVersion = "cloud.google.com/v1"
    kind       = "ComputeClass"
    metadata = {
      name   = "reserved-ccc"
      labels = local.fleet_labels
    }
    spec = {
      priorities = [
        {
          machineType = "n2-standard-2"
          reservations = {
            affinity = "Specific"
            specific = [{ name = "seeded-absent-reservation" }]
          }
        },
        { machineType = "e2-standard-4" },
      ]
      nodePoolAutoCreation = { enabled = true }
      nodePoolConfig       = { taints = [{ key = "dedicated", value = "reserved-ccc", effect = "NoSchedule" }] }
      whenUnsatisfiable    = "DoNotScaleUp"
    }
  }

  depends_on = [google_container_node_pool.seeded_c_default]
}

# Defect (obtainability 3.24): three replicas with no
# `cloud.google.com/compute-class` nodeSelector, no compute-class affinity, in a
# namespace with no default class, on a cluster whose only untainted pool
# carries the class label. The Cluster Autoscaler's pre-filter (GKE 1.35.6 and
# later) leaves such pods out of the scale-up simulation for
# ComputeClass-backed pools; the audit must name the workload from the
# manifest alone.
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

# Control: the class named only in a required nodeAffinity, never in
# nodeSelector. A required nodeAffinity on the class key also requests the
# class: the autoscaler grew the pool for such a pod on GKE 1.35.8 and 1.36.4
# (gke-compute-classes, references/compute-class-debug.md, Symptom 13). No
# check may flag this workload; it stands beside the untargeted app-worker-a.
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

# Not a defect: targeted through the nodeSelector, with a hand-written
# toleration for the class taint that names the key, the value and the effect
# and no operator. GKE adds its own toleration with operator Equal to a Pod
# that selects a class, with node pool auto-creation on or off, and a live
# test ran a Pod of this shape on a tainted class pool, one made by hand and
# one that GKE created (Standard 1.35.8 and Autopilot 1.36.4; not tested on an
# older version). Kubernetes also reads an omitted operator as Equal ("The
# default value for operator is Equal", kubernetes.io, Taints and
# Tolerations). So no check may flag it. The pool it targets carries no taint
# (the untainted-pool defect above), so the pod runs. The pod is also what
# keeps default-pool from reading as idle.
#
# A kubernetes_manifest, not kubernetes_deployment_v1: the typed resource
# writes `operator: Equal` into a toleration that names none (the provider's
# default), and the API server stores a toleration without the field as
# written, so only a raw manifest plants the shape. The role's state asserts
# the field's absence.
# Addressed by the compute-class-toleration-workload role, and by the
# no-ephemeral-limit-workload role, which a case reads.
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
