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

# The scenario driver for bench/tasks/node-nf-conntrack-table-saturation.
#
# It plants ONE node whose connection tracking table was full: a one-node
# zonal Standard cluster of its own on the project's default network, and one
# privileged host-network pod on that node. The pod lowers the node's
# net.netfilter.nf_conntrack_max to just above the table's use, opens new
# connections, and puts the limit back. While the limit is low the kernel
# drops the connections that do not fit and writes
# "nf_conntrack: table full, dropping packet" to the node's serial console.
# GKE copies that console to Cloud Logging, which is where the GCE compute
# SOP's check 2.6 (node-conntrack-saturation) reads the line. The apply
# returns only when the line is in Cloud Logging (wait_for_kernel_line.sh),
# so the audit that the case runs next has it to read, and an apply over a
# plant that never took fails instead of handing the case a red that is not
# the product's.
#
# A cluster of its own rather than a pod on the host or the seeded fleet: the
# pod is privileged and changes a kernel limit of its node, and for the
# seconds the limit is low that node drops new connections of every workload
# on it. On a cluster of its own nothing else runs there. On a new node the
# burst also fails the image pulls of system pods that are under way; the
# kubelet pulls again, and in a live test every pod was Running three minutes
# later (GKE 1.36.4, 2026-11-26).
#
# The fault is planted once and not held. The check reads the last 24 hours
# of the log, so one burst of the kernel's line is what a run needs, and the
# node is healthy again seconds later. The lines of a run stay in the
# project's log after the cluster is gone. A later compute audit of the same
# project does not read them: the check asks the log only for the node pools
# that it lists as running. So a later run of this case cannot pass on an
# earlier run's lines, and no finding of this case stays behind for another
# case's audit.
#
# The default network, as prebuilt/networking-pod-cidr-slash24-exhaustion:
# the lane's runner holds no Compute network role.
#
# The node service account holds no project role, and a fixture edits no
# project IAM (bench/README.md, Validated Capabilities fixture rules 2 and
# 3). The line reached Cloud Logging from a node with such an account in a
# live test (2026-11-26): the node's account is not what writes that log.
#
# Names are derived from the run's cluster_name so two runs never collide on
# a leftover.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

locals {
  region = regex("^(.*)-[a-z]$", var.location)[0]

  # 24 characters: inside GKE's 40-character cluster name limit whatever the
  # runner's cluster_name is, and distinct from it.
  name = "${var.cluster_name_prefix}-${substr(md5(var.cluster_name), 0, 8)}"

  # A service account id is 6 to 30 characters; the plant name fits.
  node_account_id = local.name

  # The created-by-stack label of the planted namespace.
  stack = "node-nf-conntrack-table-saturation"

  # GCP label values accept lowercase letters, digits, '-' and '_' only, so
  # the Prow ids go in verbatim; quoted because a Prow build id is an integer.
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }

  # The Kubernetes label on the planted node; the case's safeguard selects on
  # it and the pod below is scheduled by it. Repeated in the task file: change
  # one and change both.
  node_label_key   = "bench-plant"
  node_label_value = "conntrack-full"

  plant_namespace = "bench-conntrack"
  plant_pod       = "conntrack-fill"

  # The limit goes this many entries above the table's use, then the pod opens
  # this many connections. A fresh GKE node used 231 to 534 entries in the
  # live tests, so 400 connections do not fit in 20 free entries.
  fill_margin = 20
  fill_flows  = 400
  # Seconds the limit stays low after the connections are opened.
  fill_hold_seconds = 10
  # A burst that wrote no line (the limit did not bind) is tried again.
  fill_attempts = 3

  # The shell text holds no "$" followed by "{": Terraform would read it as
  # an interpolation.
  manifest = <<-MANIFEST
    apiVersion: v1
    kind: Pod
    metadata:
      name: ${local.plant_pod}
      labels:
        managed-by: kube-agents-bench
    spec:
      hostNetwork: true
      restartPolicy: Never
      terminationGracePeriodSeconds: 5
      nodeSelector:
        ${local.node_label_key}: ${local.node_label_value}
      containers:
        - name: fill
          image: busybox:1.36
          securityContext:
            privileged: true
          command:
            - sh
            - -c
            - |
              M=/proc/sys/net/netfilter/nf_conntrack_max
              C=/proc/sys/net/netfilter/nf_conntrack_count
              SAVED=$(cat $M)
              trap 'echo $SAVED > $M' EXIT TERM
              before=$(dmesg | grep -c 'nf_conntrack: table full')
              after=$before
              attempt=0
              while [ $attempt -lt ${local.fill_attempts} ] && [ $after -le $before ]; do
                attempt=$((attempt + 1))
                echo $(( $(cat $C) + ${local.fill_margin} )) > $M
                i=0
                while [ $i -lt ${local.fill_flows} ]; do
                  nc -w 1 "$KUBERNETES_SERVICE_HOST" 443 </dev/null >/dev/null 2>&1 &
                  i=$((i + 1))
                done
                wait
                sleep ${local.fill_hold_seconds}
                echo $SAVED > $M
                after=$(dmesg | grep -c 'nf_conntrack: table full')
                echo "attempt $attempt: limit back at $(cat $M); kernel lines before=$before after=$after"
              done
              if [ $after -gt $before ]; then echo PLANTED; else echo NOT-PLANTED; fi
              while true; do sleep 3600; done
  MANIFEST
}

provider "google" {
  project = var.project_id
  region  = local.region
}

# A dedicated node account with no project role, destroyed with the stack. A
# service account takes no labels, so its description carries the plant
# prefix fixture rule 8 gives such a kind (bench/README.md).
resource "google_service_account" "nodes" {
  account_id   = local.node_account_id
  display_name = "GKE node service account for bench plant ${local.name}"
  description  = "kube-agents-bench plant (node account of ${local.name}); safe to delete when no cluster names it"
}

# The default pool is the planted pool, created with the cluster. One node of
# the shape the live tests ran on.
#
# No authorized-networks list: the harness reaches the planted cluster from a
# runner or a laptop whose address is not known when the stack is written,
# and the cluster lives for one repetition. The control plane still asks for
# credentials.
#trivy:ignore:AVD-GCP-0061
resource "google_container_cluster" "plant" {
  name     = local.name
  location = var.location

  networking_mode = "VPC_NATIVE"
  # An empty block: GKE picks and creates both ranges on the default subnet.
  ip_allocation_policy {}

  initial_node_count  = 1
  deletion_protection = false
  resource_labels     = local.ci_labels

  node_config {
    machine_type    = "e2-standard-2"
    disk_size_gb    = 20
    resource_labels = local.ci_labels
    labels          = { (local.node_label_key) = local.node_label_value }
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # GKE adds serial-port-logging-enable = "true" itself; the check reads it.
    metadata = {
      disable-legacy-endpoints = "true"
    }
  }

  # Workload Identity on with GKE_METADATA above, as on the fleet: a cluster
  # standing in the leased project during a run is one the other audits can
  # sweep, and a metadata escalation is not what this case plants.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }
}

module "plant" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = google_container_cluster.plant.name
  host_cluster_location = google_container_cluster.plant.location
  project_id            = var.project_id
  namespace             = local.plant_namespace
  stack                 = local.stack
  labels                = local.ci_labels
  manifest              = local.manifest
}

resource "terraform_data" "kernel_line_in_log" {
  depends_on       = [module.plant]
  triggers_replace = [sha256(local.manifest), google_container_cluster.plant.id]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash '${path.module}/wait_for_kernel_line.sh'"
    environment = {
      WAIT_PROJECT         = var.project_id
      WAIT_CLUSTER         = google_container_cluster.plant.name
      WAIT_TIMEOUT_SECONDS = var.kernel_line_timeout_seconds
    }
  }
}

# devops-bench reads these outputs after up() and fetches credentials for
# cluster_name, so the run's kubeconfig is the planted cluster and the case's
# resource_property safeguard reads its node.
output "cluster_name" {
  value = google_container_cluster.plant.name
}

output "cluster_location" {
  value = google_container_cluster.plant.location
}
