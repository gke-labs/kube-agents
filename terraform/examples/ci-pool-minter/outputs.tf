output "minter_service_account_email" {
  description = "Email of the minter's GSA. The chart's githubMinter.gsaName default derives this same address, so nothing needs to be passed back to helm."
  value       = module.github_minter.service_account_email
}

output "allowed_service_account_email" {
  description = "The platform agent's GSA — the single identity the minty rule accepts a platform-agent-scope request from. The chart derives it from platformAgent.harness.projectId, so this is here to be checked against the rendered rule, not to be passed in."
  value       = "kubeagents-platform-gsa@${var.project_id}.iam.gserviceaccount.com"
}

output "kms_key_version_name" {
  description = "Fully-qualified name of the signing key version the chart points KMS_KEY_NAME at. Version 1 does not exist until the GitHub App PEM has been imported."
  value       = "projects/${var.project_id}/locations/${var.location}/keyRings/${module.github_minter.kms_keyring}/cryptoKeys/${module.github_minter.kms_key}/cryptoKeyVersions/1"
}

# The two steps Terraform cannot take, spelled out with this project's values
# already substituted. The PEM must never enter Terraform state, and a GitHub
# App installation is not a GCP resource.
output "manual_steps" {
  description = "The human-only half of the setup for this project."
  value       = <<-EOT
    1. Confirm the kube-agents-evals-token-minter App (ID 4675512) is
       installed on ${var.gitops_repo} with contents:write,
       pull_requests:write and issues:write. It already is for all three pool
       projects; onboarding a FOURTH project means adding its repository to
       that installation, and that edit is the security review. This — not
       the minty rule, and not hack/ci-deploy.sh — is what bounds where a run
       can write, because a token the App mints can only ever reach
       repositories the App is installed on.

         gh api /orgs/gke-agentic/installations \
           --jq '.installations[] | select(.app_id==4675512) |
                 {repository_selection, permissions}'

       repository_selection must read "selected". If it ever reads "all", a
       presubmit can mint for every repository in the org and the boundary is
       gone.

    2. Import the App's private key into the signing key (one-shot; the PEM
       must not enter Terraform state):

         git clone --depth 1 --branch v2.7.1 \
           https://github.com/abcxyz/github-token-minter.git /tmp/minty
         cd /tmp/minty && go run ./cmd/minty tools import-pk \
           -project-id=${var.project_id} -location=${var.location} \
           -key-ring=${module.github_minter.kms_keyring} \
           -key=${module.github_minter.kms_key} \
           -private-key=@/path/to/app-private-key.pem

    3. Set EVAL_GITHUB_APP_ID=4675512 in the Prow job environment.
       hack/ci-deploy.sh keeps githubMinter.enabled=false until it is set,
       because the minter Deployment is part of the release helm --wait gates
       on: enabling it before step 2 fails every presubmit. The value is the
       same for every pool project — one App serves the pool; what differs
       per project is the KMS key its PEM was imported into.
  EOT
}
