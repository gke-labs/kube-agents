variable "project_id" {
  description = "GCP project ID of the evaluation-pool project to provision the minter in (e.g. kube-agents-evals). One apply, and one state, per pool project."
  type        = string
}

variable "location" {
  description = "GCP region the KMS key ring is created in. Must match REGION in hack/ci-env.sh, because the chart derives KMS_KEY_NAME from platformAgent.harness.location and hack/ci-deploy.sh sets that from the same variable."
  type        = string
  default     = "us-central1"
}

variable "namespace" {
  description = "Kubernetes namespace the minter runs in — the Workload Identity binding names it. Must match TARGET_NAMESPACE in hack/ci-env.sh."
  type        = string
  default     = "kubeagents-system"
}

variable "gitops_repo" {
  description = "The project's private GitOps repository as owner/repo. No GCP resource depends on it; it is recorded here so the outputs can print the exact manual steps and the exact helm values for this project, and so a plan states which repository this minter is for."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.gitops_repo))
    error_message = "gitops_repo must be in owner/repo form — the same shorthand hack/ci-deploy.sh resolves and the minty rule splits."
  }
}
