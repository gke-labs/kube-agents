output "cluster_name" {
  description = "Name of the GKE cluster (created or pre-existing)"
  value       = local.cluster_name
}

output "cluster_endpoint" {
  description = "Endpoint of the GKE cluster"
  value = one(concat(
    google_container_cluster.autopilot[*].endpoint,
    google_container_cluster.standard[*].endpoint,
    data.google_container_cluster.existing[*].endpoint,
  ))
  sensitive = true
}

output "cluster_location" {
  description = "Location the cluster runs in"
  value = one(concat(
    google_container_cluster.autopilot[*].location,
    google_container_cluster.standard[*].location,
    data.google_container_cluster.existing[*].location,
  ))
}

output "cluster_ca_certificate" {
  description = "Base64-encoded public CA certificate of the cluster"
  value = one(concat(
    google_container_cluster.autopilot[*].master_auth[0].cluster_ca_certificate,
    google_container_cluster.standard[*].master_auth[0].cluster_ca_certificate,
    data.google_container_cluster.existing[*].master_auth[0].cluster_ca_certificate,
  ))
  sensitive = true
}

output "workload_identity_pool" {
  description = "Workload Identity pool of the cluster (PROJECT_ID.svc.id.goog). Empty for a pre-existing cluster without Workload Identity — which kube-agents requires, so treat empty as a pre-flight failure rather than a value to work around."
  value = try(one(concat(
    google_container_cluster.autopilot[*].workload_identity_config[0].workload_pool,
    google_container_cluster.standard[*].workload_identity_config[0].workload_pool,
    data.google_container_cluster.existing[*].workload_identity_config[0].workload_pool,
  )), "")
}
