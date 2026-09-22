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
# names the CATALOGUE ROLE that addresses it — not a scenario, unlike
# defects-a.tf: no case reads these yet. Renaming a workload here breaks that
# role's probe and state assertions in fixtures.json, which surfaces as
# fixture drift on the hourly scan rather than as a red case.
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

# Defect (upgrade readiness): a pool that upgrades in place. `max_surge = 0`
# with `max_unavailable = 1` means GKE takes the node away and recreates it in
# the same pool rather than adding a replacement first, so everything on it is
# down for the length of that recreate. On a one-node pool that is the whole
# pool at once.
#
# What this is NOT is a pool being retired, and the distinction matters for
# whoever writes the check. The node comes back, so a single-replica workload
# here suffers a guaranteed outage of minutes per upgrade, not a permanent
# Pending. It is also not universally a misconfiguration: the shipped
# gke-upgrades skill prescribes exactly `maxSurge=0, maxUnavailable=1` for
# reservation-bound pools and as the remedy for a stockout. So the finding a
# check should raise is the JOIN -- this surge setting under a workload that
# cannot absorb the gap -- and its tier is a Risk, not a Blocker. The Blocker
# on this cluster is the disruption budget further down, which stops the drain
# from finishing at all.
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
# Addressed by the readiness-surge-blocked role.
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

    # The workload below selects on this, and the role's `state` assertion
    # reads it back. The probe addresses the pool through GKE's own
    # `cloud.google.com/gke-nodepool` key, because the harness resolves a
    # selector probe to a Terraform-declared pool name; the state path
    # grammar cannot express a key containing dots or a slash, so the
    # nodeSelector assertion needs a plain one.
    labels = {
      "seeded-role" = "no-surge"
    }

    # Tainted for the same reason idle-batch-pool and pinned-inference-pool
    # are: kube-scheduler prefers empty nodes, so a system Deployment landing
    # here would change what the cost audit's idle-nodepool check sees on a
    # pool whose only declared occupant requests 10m. Only the pinned
    # workload tolerates it.
    taint {
      key    = "seeded-role"
      value  = "no-surge"
      effect = "NO_SCHEDULE"
    }

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

# Defect (upgrade readiness): a single-replica workload pinned to the pool
# that upgrades in place. The nodeSelector names the one pool carrying the
# `seeded-role` label, so the pod does not fall back to the default pool while
# its node is being recreated — it is Pending for the whole recreate, and with
# one replica that is a full outage of this workload every time the pool
# upgrades. It is not Pending forever; the node returns. A check that reports
# "nowhere to land" here has overstated it, and an agent that reports minutes
# of downtime per node recreate is right.
#
# One replica, and that is a constraint rather than a preference. At two it
# would be a multi-replica workload with no PodDisruptionBudget, which is
# byte-for-byte the obtainability SOP 3.3 shape that `checkout-gateway`
# plants on slot a, and `obtainability-fleet-exposure-sweep`'s header rests
# on that workload being the fleet's only one: "the sweep has exactly one
# right answer". Its objectives are `report_contains`, so a reply naming both
# still passes — the cost lands on the case's premise and its judge rather
# than as a deterministic red. One replica avoids the question. SOP 3.3
# does not flag
# `replicas <= 1`, and a single pinned pod still has nowhere to land, so the
# fixture keeps its property and adds no finding. `inference-server` in
# defects-a.tf carries a PDB for the same reason.
#
# The hardening below closes the compliance and reliability findings this
# workload would otherwise add to a cluster whose accepted background
# findings are declared in README.md — the same block every planted workload
# in defects-a.tf carries. No topology spread: SOP 3.8 only flags
# multi-replica workloads, and one replica cannot spread.
#
# This is the pair to the fixture above and the reason both exist: a check
# that reports the surge setting alone is reporting a configuration, while a
# check that joins it to what runs there is reporting an outage. The two
# together are what let a case ask whether the agent made that join.
#
# Addressed by the readiness-pinned-workload role.
resource "kubernetes_deployment_v1" "pinned_batch_runner" {
  provider = kubernetes.seeded_b

  metadata {
    name      = "pinned-batch-runner"
    namespace = kubernetes_namespace_v1.seeded_upgrade.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    replicas = 1

    selector {
      match_labels = { app = "pinned-batch-runner" }
    }

    template {
      metadata {
        labels = { app = "pinned-batch-runner" }
      }

      spec {
        node_selector = {
          "seeded-role" = "no-surge"
        }

        toleration {
          key      = "seeded-role"
          operator = "Equal"
          value    = "no-surge"
          effect   = "NoSchedule"
        }

        # Compliance SOP 2.7.
        automount_service_account_token = false

        # Compliance SOP 2.11.
        security_context {
          run_as_non_root = true
          run_as_user     = 65534
          seccomp_profile {
            type = "RuntimeDefault"
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
            # Obtainability SOP 3.2.
            limits = {
              memory = "32Mi"
            }
          }
        }
      }
    }
  }

  depends_on = [google_container_node_pool.no_surge_pool]
}

# Compliance SOP 2.6 flags any non-system namespace that has workloads and no
# NetworkPolicy. seeded-upgrade has one workload and it uses no network at
# all, so a default-deny closes the finding at zero fixture risk. defects-a.tf
# does the same for the three namespaces on slot a.
resource "kubernetes_network_policy_v1" "seeded_upgrade_default_deny" {
  provider = kubernetes.seeded_b

  metadata {
    name      = "default-deny"
    namespace = kubernetes_namespace_v1.seeded_upgrade.metadata[0].name
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

# Defect (upgrade readiness): an admission webhook that fails closed onto a
# backend that does not exist. The planted property is the unresolvable
# `clientConfig.service` below, NOT `failurePolicy: Fail` on its own — this
# repository's own operator webhook sets Fail
# (k8s-operator/config/webhook/manifests.yaml), as do cert-manager and GKE's
# managed-prometheus, so a check that fires on the policy alone reports every
# healthy cluster. Fail plus a backend that can never answer is the
# combination that turns a drain into a deadlock: while the backend is down,
# every write the rule matches is rejected rather than allowed through, and a
# drain that evicts the backend's own pod stalls on its own admission rule.
#
# `timeout_seconds = 30` is set explicitly, and that is the fixture rather
# than an incidental value. "failurePolicy Fail with no timeoutSeconds" is
# not a plantable state: on admissionregistration.k8s.io/v1 the API server
# defaults an omitted timeout to 10 seconds and the Terraform provider
# defaults its own attribute to 10, so the persisted object every reader sees
# carries a timeout either way and there is no absence for a check to find.
# 30 is the API's maximum, which is the dangerous end of the range and an
# observable property a readiness check can asserted against.
#
# Two properties of the real finding are planted here and one is not. The
# dangerous variant matches cluster-wide, and planting that on a standing
# shared cluster would reject writes for every scenario that touches
# seeded-b, not only for this fixture — the fleet is read-only for
# evaluations, and a fixture that can break unrelated runs is not worth the
# fidelity. So the namespaceSelector confines it to the seeded-upgrade
# namespace, and the check is expected to flag `failurePolicy: Fail` with a
# timeout long enough to stall a drain; the scope dimension is left to a unit test with a
# recorded manifest, where nothing can be broken by it.
#
# `clientConfig` names a Service that does not exist, which is what makes the
# fail-closed behaviour real rather than theoretical — and is safe precisely
# because the selector above bounds what it can reject.
#
# Addressed by the readiness-failclosed-webhook role.
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
    timeout_seconds           = 30

    client_config {
      service {
        name      = "nonexistent-admission-gate"
        namespace = kubernetes_namespace_v1.seeded_upgrade.metadata[0].name
        path      = "/validate"
      }
    }

    # Without this the rule matches every ConfigMap CREATE in the namespace,
    # including the kube-root-ca.crt that kube-controller-manager's
    # root-ca-cert-publisher writes into every namespace. A fail-closed
    # webhook with no backend would reject that permanently, and the symptom
    # — later pods stuck on a missing ConfigMap — never names the webhook.
    # The fixture needs to BE a dangerous webhook, not to reject anything
    # real.
    object_selector {
      match_labels = local.fleet_labels
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

# Defect (upgrade readiness): the budget that makes the drain impossible, not
# merely slow. `maxUnavailable: 0` means no pod this budget matches may ever be
# evicted voluntarily, so `disruptionsAllowed` is 0 forever -- not because the
# workload is unhealthy, but because the spec forbids it. Obtainability SOP 3.4
# calls this "the highest-value finding in the audit and the one most often
# missed": it stops every node drain in the cluster, so node-pool upgrades,
# auto-repair and autoscaler scale-down all stall until a human edits it.
#
# This is the fixture the readiness capability exists for. The other two on this
# cluster are configurations that LOOK dangerous; this one, composed with them,
# is an upgrade that cannot complete:
#
#   no-surge-pool has max_surge = 0, so the upgrade drains its only node
#   rather than adding a replacement first;
#   pinned-batch-runner runs only there, by nodeSelector;
#   this budget refuses its eviction.
#
# A check that reports the surge setting alone, or the budget alone, has not
# said the upgrade will fail. Reporting the chain is what the case grades.
#
# Bounded deliberately: the pool is tainted and only this workload tolerates
# it, so the drain that cannot finish is a drain of one node nothing else uses.
# The fleet already tolerates this shape -- defects-a.tf's inference-server
# budget sits at disruptionsAllowed 0 permanently for a different reason, and
# main.tf records that nothing in the eval path drains that node either.
#
# maxUnavailable 0 rather than minAvailable >= replicas, because SOP 3.4 flags
# it on the spec alone. A minAvailable rule is read against the live replica
# count, so it would stop being a finding the moment the workload scaled.
#
# Addressed by the readiness-drain-blocked role.
resource "kubernetes_pod_disruption_budget_v1" "drain_blocked" {
  provider = kubernetes.seeded_b

  metadata {
    name      = "pinned-batch-runner"
    namespace = kubernetes_namespace_v1.seeded_upgrade.metadata[0].name
    labels    = local.fleet_labels
  }

  spec {
    max_unavailable = "0"

    selector {
      match_labels = { app = "pinned-batch-runner" }
    }
  }
}
