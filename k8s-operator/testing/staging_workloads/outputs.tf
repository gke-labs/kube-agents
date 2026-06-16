output "autopilot_clusters_list" {
  value = join("\n", [
    for name, cluster in google_container_cluster.primary : "${cluster.name}|${cluster.location}|${var.autopilot_clusters[name].traffic_shape}|${var.autopilot_clusters[name].enable_ray}"
  ])
  description = "Newline-separated list of Autopilot clusters (format: name|location|traffic_shape|enable_ray)"
}

output "standard_clusters_list" {
  value = join("\n", [
    for name, cluster in google_container_cluster.standard : "${cluster.name}|${cluster.location}|${var.standard_clusters[name].traffic_shape}"
  ])
  description = "Newline-separated list of GKE Standard clusters (format: name|location|traffic_shape)"
}

output "project_id" {
  value       = var.project_id
  description = "The GCP Project ID where the clusters were created"
}
