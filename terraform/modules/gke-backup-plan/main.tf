locals {
  backup_plan_name = coalesce(var.name, "${var.cluster_name}-backup-plan")
}

# Mirrors the BackupPlan k8s-operator/scripts/provision_12_gke_backup_plan.sh
# creates: the same default name, schedule, retention, namespace scope, and
# include-secrets/include-volume-data choices. Pick one path per cluster —
# both create the same resource, and the script's check-then-create would
# adopt a Terraform-managed plan without Terraform knowing.
resource "google_gke_backup_backup_plan" "this" {
  name     = local.backup_plan_name
  project  = var.project_id
  location = var.location
  cluster  = "projects/${var.project_id}/locations/${var.location}/clusters/${var.cluster_name}"

  backup_config {
    include_secrets     = var.include_secrets
    include_volume_data = var.include_volume_data

    selected_namespaces {
      namespaces = var.selected_namespaces
    }

    # Set this once, ideally at creation. Changing it later is an update
    # in-place, not a replacement (verified against hashicorp/google v7.44:
    # adding a key to a live plan shows "will be updated in-place"), but the
    # new key governs only backups taken after the change — each existing
    # backup keeps the key it was encrypted with. provision_12 warns and
    # preserves the existing key rather than swapping it for the same reason.
    dynamic "encryption_key" {
      for_each = var.encryption_key == "" ? [] : [var.encryption_key]
      content {
        gcp_kms_encryption_key = encryption_key.value
      }
    }
  }

  backup_schedule {
    cron_schedule = var.cron_schedule
    paused        = var.paused
  }

  retention_policy {
    backup_retain_days = var.backup_retain_days
  }
}
