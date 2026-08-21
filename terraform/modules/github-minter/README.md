# GitHub Token Minter Identity & KMS Module

Reusable Terraform module for provisioning the GitHub token minter's Google Service Account (GSA), its Workload Identity binding, and the KMS asymmetric signing key it signs GitHub App JWTs with.

The KMS key is created **import-only and empty** (`skip_initial_version_creation = true`): importing the GitHub App private key PEM into it is a separate one-shot step — the PEM must never enter Terraform state — using the Minty CLI for the cryptographic wrapping:

```bash
# Clone-and-run: `go run github.com/abcxyz/github-token-minter/cmd/minty@v2.7.1`
# does not resolve — upstream's go.mod lacks the /v2 suffix its v2 tags require.
git clone --depth 1 --branch v2.7.1 https://github.com/abcxyz/github-token-minter.git /tmp/minty
cd /tmp/minty && go run ./cmd/minty tools import-pk \
  -project-id=<project> -location=<region> \
  -key-ring=github-token-minter-keyring -key=github-token-minter-key \
  -private-key=@/path/to/app-private-key.pem
```

`install.sh` runs this import for you when it collects a PEM path. The minter's Kubernetes half (Deployment, Service, NetworkPolicy, KSA, minty rule ConfigMap) is the chart's `githubMinter.*` values; the minter pod fails its readiness probe until the key version imported here is ENABLED.

> **KMS resources cannot be deleted.** Cloud KMS key rings and keys are never actually
> destroyed — `terraform destroy` only removes them from state, and a subsequent apply
> with the same names fails with a 409. Recover by importing the existing resources
> back into state
> (`terraform import module.<name>.google_kms_key_ring.minter ...`) or by choosing new
> `kms_keyring_name`/`kms_key_name` values.

## Relationship to the install

This is the module the full-install composition (and therefore `install.sh`, when the
GitHub integration is configured) uses for the minter's GCP half; the chart's
`githubMinter.*` values render the Kubernetes half, and the PEM import above completes
the pair. The canonical identifiers (GSA `kubeagents-github-minter-gsa`, KSA
`kubeagents-github-minter`, namespace `kubeagents-system`) also appear in
`k8s-operator/scripts/common.sh` for the dev tooling, and the module's defaults mirror
them.

## Usage

```hcl
module "github_minter" {
  source     = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/github-minter?ref=1.2.0"
  project_id = "my-gcp-project"
  location   = "us-central1"
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
