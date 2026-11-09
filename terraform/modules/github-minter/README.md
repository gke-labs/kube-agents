# GitHub Token Minter Identity & KMS Module

Reusable Terraform module for provisioning the GitHub token minter's Google Service Account (GSA), its Workload Identity binding, and the KMS asymmetric signing key it signs GitHub App JWTs with.

The KMS key is created **import-only and empty** (`skip_initial_version_creation = true`): importing the GitHub App private key PEM into it is a separate one-shot step (performed either automatically by `install.sh` or upfront Ahead-Of-Time via the Minty CLI) — the PEM must never enter Terraform state.

Follow the official [upstream setup guide](https://github.com/abcxyz/github-token-minter#readme) for the authoritative procedure on importing the GitHub App private key into Google Cloud KMS.

The minter's Kubernetes half (Deployment, Service, NetworkPolicy, KSA, minty rule ConfigMap) is rendered by the chart's `githubMinter.*` values; the minter pod fails its readiness probe until the key version imported into Cloud KMS is `ENABLED`.

## Moving the install to another region

The keyring follows `var.location`, which the full-install composition passes straight from the cluster's location — so changing `location` on an install that has the minter enabled creates a **new, empty** keyring in the new region. Nothing is copied across: the key is `import_only` and KMS never releases private key material, so the version in the old keyring stays where it is.

When changing regions, the GitHub App private key must be re-imported into the new region's KMS key (either automatically via `install.sh --github-pem-path` or Ahead-Of-Time following the [upstream guide](https://github.com/abcxyz/github-token-minter#readme)).

> **KMS resources cannot be deleted.** Cloud KMS key rings and keys are never actually
> deleted, and a raw `terraform destroy` does not leave the key usable: destroying the
> crypto key schedules every version of it for destruction, so the imported GitHub App
> key can no longer sign. Run `terraform state rm` on the key ring and key before a raw
> destroy. A later apply with the same names fails with a 409 until you import the
> existing resources back into state
> (`terraform import module.<name>.google_kms_key_ring.minter ...`) or choose new
> `kms_keyring_name`/`kms_key_name` values. The full-install composition's `lifecycle.sh`
> does both for you; its README's [Teardown and re-apply](../../examples/full-install/README.md#teardown-and-re-apply) section says how, including how to restore
> versions a raw destroy scheduled.

## Relationship to the install

This is the module the full-install composition (and therefore `install.sh`, when the
GitHub integration is configured) uses for the minter's GCP half; the chart's
`githubMinter.*` values render the Kubernetes half, and the AOT KMS key import completes
the pair. The canonical identifiers also live with the installer, and the module's
defaults mirror them: the GSA `kubeagents-github-minter-gsa` and the namespace
`kubeagents-system` as defaults in `install.defaults.env` (an install overrides them
through `install.env`), the KSA `kubeagents-github-minter` as a constant in
`scripts/installer/common.sh` for the dev tooling. `install.sh` gives a new install its
own GSA, key ring and key names, derived from the cluster, and keeps the names an
existing install's state holds (#3504); these defaults are what an existing install was
created with. `owner_labels` and `owner_description` record the owning install on the
crypto key and the GSA, for the full-install composition's adoption check.

## Usage

```hcl
module "github_minter" {
  source     = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/github-minter?ref=1.2.0"
  project_id = "my-gcp-project"
  location   = "us-central1"
}
```

See the [Release versioning guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
