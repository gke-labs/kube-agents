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

# Config Connector on seeded-a (#4218), the cluster in the audited fleet that
# the Config Connector eval cases plant their objects on. Three parts, all in
# one apply:
#
#  - the add-on, on google_container_cluster.seeded_a in main.tf;
#  - the ConfigConnector object in namespaced mode, below;
#  - a Google service account with no project role, for the
#    ConfigConnectorContext an eval stack creates in its own namespace.
#
# Namespaced mode, not cluster mode: in cluster mode one controller acts for
# every namespace as one Google service account, so an object in any namespace
# could act on the project. In namespaced mode no controller runs until a
# namespace holds a ConfigConnectorContext, and the controller for that
# namespace acts only as the account the context names. The account below
# holds no role, so an object planted there can call no Google API that needs
# one. The eval objects stop before any call to Google (a missing dependency),
# so they need none.
#
# Not a defect: nothing here is a finding a case grades. The cases that need no
# Config Connector anywhere in the fleet exclude slot a from the install's
# scope (`scope_exclude` in their task.yaml).

locals {
  # The Google service account an eval stack's ConfigConnectorContext names.
  kcc_eval_context_account = "kcc-eval-context"
  # The namespace the Config Connector eval stack plants in, and so the
  # Kubernetes service account of its namespaced controller, which the add-on
  # names cnrm-controller-manager-<namespace> in cnrm-system. Change both
  # together with that stack's namespace.
  kcc_eval_namespace = "eval-kcc-stalled"
  kcc_controller_ksa = "cnrm-system/cnrm-controller-manager-${local.kcc_eval_namespace}"
  # The add-on reads exactly one ConfigConnector object, by this name.
  config_connector_name = "configconnector.core.cnrm.cloud.google.com"
  config_connector_mode = "namespaced"
}

resource "google_service_account" "kcc_eval_context" {
  account_id   = local.kcc_eval_context_account
  display_name = "Config Connector eval context on seeded-a, with no project role"
}

# The namespaced controller acts as the account through Workload Identity. The
# binding is on the account alone; no project role goes with it.
resource "google_service_account_iam_member" "kcc_eval_context_workload_identity" {
  service_account_id = google_service_account.kcc_eval_context.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${local.kcc_controller_ksa}]"
}

# The ConfigConnector object. Written once, on create, and again when
# config_connector.py or the object's spec changes. A change to either plans a
# replace of this resource, which holds no state of its own; that pull request
# adds its address to reconcile-allow.json. The script waits for the add-on to
# install the CRD, so this needs no second apply. An out-of-band edit or delete
# of the object is drift the fixture-state scan reports (the
# config-connector-namespaced role in fixtures.json); nothing re-plans this
# resource, so the repair is to replace it by hand (README.md, hand recoveries).
resource "terraform_data" "config_connector" {
  triggers_replace = [
    filesha256("${path.module}/config_connector.py"),
    local.config_connector_name,
    local.config_connector_mode,
  ]

  provisioner "local-exec" {
    interpreter = ["python3"]
    command     = "${path.module}/config_connector.py"
    environment = {
      K8S_HOST                = "https://${google_container_cluster.seeded_a.endpoint}"
      K8S_CA_BASE64           = google_container_cluster.seeded_a.master_auth[0].cluster_ca_certificate
      CONFIG_CONNECTOR_NAME   = local.config_connector_name
      CONFIG_CONNECTOR_MODE   = local.config_connector_mode
      CONFIG_CONNECTOR_LABELS = jsonencode(local.fleet_labels)
    }
  }

  # The add-on must be on before the CRD can exist, and the default pool must
  # be there for the operator's pods.
  depends_on = [google_container_cluster.seeded_a, google_container_node_pool.seeded_a_default]
}
