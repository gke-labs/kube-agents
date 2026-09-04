# The self-improvement loop's Google identity.
#
# One account, and it reads telemetry and nothing else. The loop's GitHub
# identity is not here at all: it is a personal access token held by a robot
# account, created out of band and mounted from a Kubernetes Secret, so there is
# no GCP resource behind it. Sec. 6 of docs/designs/self-improvement.md says what
# that credential trades away against a GitHub App, and why the loop takes the
# trade.
#
# Kept out of kube-agents-iam deliberately. That module grants the Platform
# Agent what it needs to manage the fleet -- up to container.admin, under the
# full-install composition's gke-admin permission set -- and the loop must not
# inherit any of it: an agent that can modify the cluster it is
# investigating cannot honestly report on it. A separate module also means an
# install can destroy this one alone and leave the product running.

resource "google_service_account" "investigator" {
  project      = var.project_id
  account_id   = var.service_account_id
  display_name = "Kube-Agents Self-Improvement Investigator"
  description  = "Read-only telemetry access for the self-improvement CronJob. Holds no GKE roles by design."
}

resource "google_service_account_iam_member" "investigator_workload_identity" {
  service_account_id = google_service_account.investigator.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.ksa_name}]"
}

# The complete grant. Three viewer roles, matching the three things
# selfimprove_evidence.py can query, and nothing else.
#
# Notably absent: container.viewer. Kubernetes reads go through the pod's
# Kubernetes service account, which the chart binds to `view` in one namespace
# -- so the blast radius of the cluster half is a namespace rather than a
# project, and it is enforced by RBAC rather than by IAM. Adding
# container.viewer here would silently widen that to every cluster in the
# project.
resource "google_project_iam_member" "investigator" {
  for_each = toset([
    "roles/logging.viewer",
    "roles/cloudtrace.viewer",
    "roles/monitoring.viewer",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.investigator.email}"
}
