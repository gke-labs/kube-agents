output "backup_plan_name" {
  description = "Name of the created BackupPlan"
  value       = google_gke_backup_backup_plan.this.name
}

output "backup_plan_id" {
  description = "Fully qualified resource id of the created BackupPlan"
  value       = google_gke_backup_backup_plan.this.id
}
