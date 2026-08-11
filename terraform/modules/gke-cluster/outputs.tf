output "cluster_name" {
  description = "Name of the provisioned GKE Autopilot cluster"
  value       = google_container_cluster.autopilot.name
}

output "cluster_endpoint" {
  description = "Endpoint of the GKE cluster"
  value       = google_container_cluster.autopilot.endpoint
  sensitive   = true
}

output "cluster_location" {
  description = "Region the cluster runs in"
  value       = google_container_cluster.autopilot.location
}

output "cluster_ca_certificate" {
  description = "Base64-encoded public CA certificate of the cluster"
  value       = google_container_cluster.autopilot.master_auth[0].cluster_ca_certificate
  sensitive   = true
}

output "workload_identity_pool" {
  description = "Workload Identity pool of the cluster (PROJECT_ID.svc.id.goog)"
  value       = google_container_cluster.autopilot.workload_identity_config[0].workload_pool
}
