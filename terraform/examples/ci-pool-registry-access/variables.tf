variable "project_id" {
  description = "GCP project ID of the evaluation-pool project being granted read access to the warm build cache (e.g. kube-agents-evals-3). One apply, and one state, per pool project. The project NUMBER the grant is actually keyed on is read out of this at plan time, so onboarding a further pool project is a tfvars value rather than an edit to the HCL."
  type        = string
}

variable "cache_project_id" {
  description = "Project that owns the registry the warm :latest cache image is published into. hack/ci-deploy.sh bakes this project into its CACHE_IMAGE default, so changing it here only makes sense as half of a change to that script."
  type        = string
  default     = "kube-agents-prow"
}

variable "cache_location" {
  description = "Artifact Registry location of the cache repository. The multi-region `us`, matching the us-docker.pkg.dev host in CACHE_IMAGE — deliberately not the us-central1 that each pool project's own PR-image repository uses, which is a different repository in a different project."
  type        = string
  default     = "us"
}

variable "cache_repository" {
  description = "Artifact Registry repository the canonical postsubmit image is published into by .github/workflows/docker-publish-gcp.yml, and the one the presubmit build pulls from."
  type        = string
  default     = "kube-agents"
}
