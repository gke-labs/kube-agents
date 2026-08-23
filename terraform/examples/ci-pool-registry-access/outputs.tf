output "granted_member" {
  description = "The IAM member this apply added. Check it against the live policy afterwards; it must appear under roles/artifactregistry.reader ALONGSIDE the other pool projects' members, not instead of them — see verify_command."
  value       = google_artifact_registry_repository_iam_member.warm_cache_reader.member
}

output "cache_image" {
  description = "The image the grant exists to make pullable — the CACHE_IMAGE default in hack/ci-deploy.sh, pulled by the warm-cache step of deploy/docker/cloudbuild-ci.yaml. A Cloud Build in the pool project that can docker pull this is the real end-to-end check."
  value       = "${var.cache_location}-docker.pkg.dev/${var.cache_project_id}/${var.cache_repository}/platform-agent:latest"
}

output "verify_command" {
  description = "Read-only confirmation that the apply added a member rather than replacing the set. Every previously onboarded pool project must still be listed."
  value       = "gcloud artifacts repositories get-iam-policy ${var.cache_repository} --location=${var.cache_location} --project=${var.cache_project_id}"
}
