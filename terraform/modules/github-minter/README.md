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

The clone-and-run needs a Go toolchain that can build and execute a binary, which is not a given on a locked-down workstation. `gcloud` and `openssl` do the same wrapping in four commands — [Importing Without the Minty CLI](../../../k8s-operator/config/integrations/github/README.md#importing-without-the-minty-cli) is canonical for that path, including the wait for the import job to reach `ACTIVE` and the `CLOUDSDK_PYTHON_SITEPACKAGES=1` that the wrapping step needs. It is parameterised on `${KMS_LOCATION}`, which is the same value as `<region>` in the clone-and-run above: this module's `location` with any zone suffix stripped, so `us-central1-a` becomes `us-central1`.

## Moving the install to another region re-imports the App key

The keyring follows `var.location`, which the full-install composition passes straight from the cluster's location — so changing `location` on an install that has the minter enabled creates a **new, empty** keyring in the new region. Nothing is copied across: the key is `import_only` and KMS never releases private key material, so the version in the old keyring stays where it is.

What that costs depends on whether you still have the PEM GitHub issued. KMS is not the source of truth for the App key; that file is. `import_github_pem` derives the KMS location from the region, and imports `GITHUB_PEM_PATH` whenever the key in that location has no ENABLED version — which is exactly the state a region move produces — so re-running `install.sh` against the new region with the same PEM completes the move. `install.sh` persists `GITHUB_PEM_PATH` into `vars.sh`, and the gcloud path above takes the location as an input, so either route works.

Only if the PEM is gone does the move cost a credential: GitHub does not re-issue a private key it has already handed out, and the KMS copy cannot be exported, so there is nothing left to import and you have to generate a fresh one on the App and import that. Either way the old keyring and key stay in the project forever, per the warning below.

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
