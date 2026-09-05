# Release Automation Scripts

This directory contains executable scripts supporting the environment pipelines: the
release-candidate (RC) pipeline, the nightly pipeline that promotes a validated candidate to
staging, and the GA release. The provisioning, teardown and E2E scripts are shared between the
first two — which environment they act on comes from the calling workflow's GitHub environment,
not from anything here.

## Release note: `PLATFORM_AGENT_PERMISSION_SET=gke-admin` now fails the deploy

**Action required before the next RC deploy** for any GitHub environment whose
`PLATFORM_AGENT_PERMISSION_SET` variable is set to `gke-admin`. That value has been removed and
`install.sh` now exits non-zero on it, so the deploy hard-fails rather than falling back to a
default.

**It does not fail before doing damage.** `provision_environment.sh` is `uninstall.sh` followed
by `install.sh`, and the refusal fires while `install.sh` is collecting configuration — before
`terraform apply`, but after the teardown has already run. Expect a torn-down environment that
was not rebuilt, not a run that refused to start.

`deploy-environment.yml` forwards `vars.PLATFORM_AGENT_PERMISSION_SET` verbatim to both
`validate_and_log_deploy_summary.sh` and `provision_environment.sh`, so the summary step logs the
doomed value and proceeds. The refusal itself is fail-closed by design — `roles/container.admin`
authorizes the agent through IAM regardless of its Kubernetes RBAC, and its
`container.clusters.impersonate` permission cannot be scoped by IAM — but nothing warns you ahead of
the run.

Fix it by editing the environment variable to `read-only`, or to `custom` with
`PLATFORM_AGENT_CUSTOM_ROLES` naming every role, if you accept that risk explicitly. The reasoning
is on the site's [Security & IAM](../../docs/site/src/content/docs/reference/security-and-iam.md)
page under "Why there is no `gke-admin` set".

## Overview of Scripts

- `common.sh`: Centralized registry/repository helpers (`DEFAULT_REGISTRY_PREFIX`, `DEFAULT_RELEASE_REPO`, `REQUIRED_RELEASE_IMAGES`), commit discovery (`find_latest_built_commit`), validation check (`is_rc_candidate_commit_already_validated`, anchored to the `rc_*_validated` family so no other tag family can answer for it), the release commit-range read and the Conventional Commits breaking-change test (`release_read_commit_range`, `commit_messages_have_breaking_change` — both shared by the version calculator and the scheduled-release gate, so a bump and a halt cannot end up scoped to different commits or disagreeing about what "breaking" means; the range read also keeps git's stderr out of the captured subjects, since an ambiguous-refname warning on a successful, empty range would otherwise read as "there are commits to ship"), the staging promotion tag transform and the shape-anchored lookups the GA gate reads (`staging_tag_for_rc`, `get_existing_staging_tag`, `STAGING_TAG_SHAPE_REGEX`, `get_latest_staging_tag`, `staging_promotion_tags_at_commit` — the last three match `staging_<ts>_<sha>` rather than the `staging_` prefix `staging-redeploy-*.yml` triggers on, so a hand-pushed trigger tag cannot read back to the release path as evidence the nightly matrix passed), container image promotion (`promote_release_images`), automated bot tagging (`ensure_git_tag`), and `release_fetch_tags` — the CI-only tag sync every script that answers a question from the tag graph runs first, since a shallow or tagless checkout otherwise resolves "no such tag" rather than failing. It also holds `release_resolve_target`, which resolves the cluster the kubectl-facing scripts act on. **In CI that resolution has no defaults**: `GKE_CLUSTER_NAME`, `GCP_REGION`, `GCP_PROJECT_ID` and `AGENT_NAMESPACE` must all be set — they come from the job's `env:` block, which reads them from the workflow's GitHub environment — and the script exits non-zero naming whichever is missing. A release script guessing which project it targets is the failure this prevents, since the old default pointed a teardown-and-reinstall at `kube-agents-rc` whatever the caller meant. Outside CI (`CI` unset or falsy) the developer defaults still apply, so running these by hand after `install.sh` needs no extra exports.
- `resolve_rc_tag.sh`: Validates candidate commit SHAs, resolves input tags/commit inputs, discovers the latest built commit on `main` during scheduled runs, checks for an existing `rc_*_validated` tag to skip redundant runs, and sets workflow step outputs.
- `verify_candidate_images.sh`: Verifies that prebuilt container images (`k8s-operator`, `platform-agent`, `credential-proxy`, `agent-sandbox`, `replay-proxy`, `pubsub-platform`, `gke-stockout-investigator`) exist in GHCR/registry for the target candidate SHA.
- `tag_commit.sh`: The one place a Git tag is created and pushed. Prints a banner naming what is being tagged, then calls `ensure_git_tag`, which no-ops when the tag already points at the same commit and fails when it points at a different one. Every tagger below is a wrapper over it, keeping only what is genuinely its own; a second copy of this body is how the rungs of the release ladder drift apart.
- `create_release_tag.sh`: Creates and pushes candidate release tags (`rc_YYMMDDHHMM_<short_sha>`, derived from commit timestamp) safely and idempotently. When executed locally outside CI, runs in dry-run mode (creates tag locally and skips remote push).
- `resolve_promotion_candidate.sh`: Picks the candidate the nightly pipeline tests and decides whether passing it promotes anything. Emits `commit_sha`, `rc_tag`, `staging_tag`, `skip_reason`, `skip_pipeline` (the run does nothing at all — either no validated candidate exists, or the newest one is refused because its tree predates the shared-pipeline restructure; see "Workflow Mapping" below) and `skip_promotion` (the candidate already carries a `staging_*` tag, so the matrix still runs and nothing is pushed). Selection and the validated check come from `common.sh`, so the promotion gate and the RC gate answer the same question. Every skip is exit 0; the exits that are not are a tag that does not resolve, and a hand-passed tag the RC pipeline never validated or whose tree predates the shared-pipeline restructure.
- `record_nightly_candidate_summary.sh`: Renders step 1 of the nightly pipeline into the job summary — which candidate the run picked, and whether a green matrix will move staging. Keeps the two skips distinct: `SKIP_PIPELINE` means no run at all and carries `SKIP_REASON` to say which of its two causes applied, `SKIP_PROMOTION` means the matrix runs against a commit that already carries a staging tag and a pass pushes nothing.
- `dispatch_rc_pipeline.sh`: Starts `rc-release-pipeline.yml` for a candidate `rc-scheduler.yml` resolved. Since the scheduler is the only thing that starts the pipeline, a failure here means no candidate is being tested at all, so it raises an `::error` annotation saying so before exiting non-zero, rather than leaving a bare exit code for the reader to interpret. The default `GITHUB_TOKEN` is enough: GitHub's recursion suppression exempts `workflow_dispatch`, which is why the staging tag push needs a PAT and this does not.
- `record_rc_scheduler_skip.sh`: Records a quiet three-hourly tick. Because such a tick deliberately leaves no pipeline run behind, this summary is its only trace, and it says outright that a green scheduler reports nothing about the last pipeline run's result.
- `run_optional_e2e_suites.sh`: Runs `e2e-run.yml`'s `optional_suites` list one suite at a time. Every suite runs regardless of what the ones before it did — a failure that short-circuited the loop would silently drop the coverage behind it — and the script exits non-zero if any failed, which the `continue-on-error` step turns into a red-but-tolerated result with the failing suites named in the job summary. The list is comma-separated because `workflow_call` has no list input type.
- `tag_staging_promotion.sh`: Pushes the `staging_<ts>_<sha>` tag that `staging-redeploy-*.yml` deploy on. It derives the tag from the candidate rather than trusting one passed in, and refuses anything outside the `staging_` namespace — the tag is a live deploy trigger. It must be pushed with `RELEASE_BOT_TOKEN`; a tag pushed with the default `GITHUB_TOKEN` triggers no workflow, so the promotion would go green having deployed nothing.
- `peel_tag_commit.sh`: Resolves the ref a push event fired on to the commit it points at and writes it to `GITHUB_OUTPUT` as `commit_sha`. `staging-redeploy-*.yml` run it because `ensure_git_tag` creates annotated tags: a push event's `github.sha` is the new value of the ref, which for an annotated tag is the tag object's SHA, and passing that to `helm upgrade --set …image.tag` names a GHCR image that was never published — a `--wait` deploy then times out on `ImagePullBackOff` and strands the shared release in `pending-upgrade`. Peeling a lightweight tag or a branch head returns the same SHA, so a caller need not know which it got.
- `validate_and_log_deploy_summary.sh`: Validates required environment variables and secrets, then logs a formatted deployment matrix and GCP cluster target overview for auditing before provisioning.
- `teardown_common.sh`: Sourced by the two scripts below, which both call `uninstall.sh` and read the same three outcomes out of its exit code (`./uninstall.sh --help` lists them). Holds the invocation, the `TEARDOWN_STRICT` parsing, and the job-summary rendering; each caller decides for itself what a failure means. The variable is read under both names — `TEARDOWN_STRICT` first, then the legacy `RC_TEARDOWN_STRICT` — because reading only the new one would have left the parser on an unset variable until the settings caught up, and unset is "off" with no error. Both `rc` and `nightly` now define `TEARDOWN_STRICT`, so `deploy-environment.yml` forwards that name alone; the fallback stays for anyone running these scripts by hand against an environment nobody has migrated, and is dropped when the old variable is deleted from both settings pages.
- `provision_environment.sh`: Tears the environment down with `uninstall.sh`, then reinstalls it at the candidate commit with `install.sh`. Which environment is entirely `GCP_PROJECT_ID` / `GCP_REGION` / `GKE_CLUSTER_NAME` and the rest of the install inputs, which `deploy-environment.yml` reads from the GitHub environment named by its `github_environment` input — `rc` for the RC pipeline, `nightly` for the nightly one. A failed teardown raises an `::error` annotation and a job-summary entry carrying the teardown output, and provisions anyway unless `TEARDOWN_STRICT` is truthy — the choice between validating a candidate against stale state and letting a teardown problem block every release. It also forwards the GitOps repository and, with it, the GitHub token minter, and stages an optional `GH_APP_PRIVATE_KEY` to a private temporary file because `install.sh` takes a path; see "Enabling the GitHub token minter on the RC" below for what to set.
- `render_install_env.sh`: The one mapping from a GitHub environment's `vars.*`/`secrets.*` to the installer's `install.env`. A runner is ephemeral and has no hand-authored one, so every job that drives the installer renders one and points `KUBE_AGENTS_INSTALL_ENV` at it. `--strict` additionally requires every setting whose absence would _remove_ something from an install that already exists — the gVisor node pool, Hindsight, the backup plan, the Chat topic — and names all the missing ones in one annotation rather than one per run. That distinction is the whole difference between the two families of environment: `rc` and `nightly` are rebuilt every run, so an omitted setting costs a feature; `autopush` and `staging` have been up for weeks, so the same omission is a `terraform apply` that plans a destroy. A key with no value is omitted from the file rather than written empty, because `KEY=` beats `install.defaults.env` and means "explicitly nothing".
- `reconcile_environment.sh`: Applies `terraform/examples/full-install` to a long-lived environment, or reports what applying it would change. Renders the configuration strictly, waits out any `*-redeploy-*` run already in flight (both drive `helm upgrade` on the release `helm_release.kube_agents` owns), takes the live-test lease for an apply, and then runs `upgrade.sh --plan` or `upgrade.sh --upgrade-mode=full`. `LEASE_POLICY` picks what a held lease means: `defer` for the scheduled path, `fail` for a manual one. A plan takes no lease and holds no lock, because it changes nothing. Called by `reconcile-environment.yml`.
- `report_drift.py`: Turns a non-empty plan into one tracked issue per environment, labelled `infra-drift`, edited in place while the drift lasts and closed by the first clean plan. Found again by a marker in the body rather than by title, so retitling one does not orphan it. A plan that failed to run leaves whatever is open exactly as it is: a failure is evidence of nothing either way. Called by `drift-detect.yml`.
- `teardown_environment.sh`: Destroys the environment after a run that passed end to end, so the cluster exists only for the length of a run rather than idling between them. A failure here is always fatal and `TEARDOWN_STRICT` does not apply: nothing runs afterwards, so the alternative to a red job is a GKE cluster billing under a green pipeline. It runs only when every earlier step succeeded, which is what leaves a failed run's environment standing to be examined live — until the next scheduled run reclaims it on the RC, three hours at most, and indefinitely on the nightly, which has no schedule to reclaim anything.
- `wait_for_gke_readiness.sh`: Connects `kubectl` to the target cluster, configures Artifact Registry credentials, optionally verifies the gateway is running the candidate commit's image — delegated to `scripts/confirm_agent_image.sh`, which the agent redeploy workflow runs for the same purpose — and waits for `litellm` and `platform-agent-gateway` to report ready.
- `tag_validated_release.sh`: Attaches the `rc_*_validated` marker to a candidate commit upon 100% test pass, by appending `_validated` to its `rc_*` tag.
- `resolve_scheduled_release.sh`: Decides whether an unattended run of `release-publish.yml` should publish. Three conditions — a candidate carries a shape-valid `staging_<ts>_<sha>` tag, commits exist between the newest GA tag and that candidate, and nothing in the range is a breaking change — emitted as `should_release`, `release_commit`, `gate_tag` and `skip_reason`. The first two failing are skips with exit 0; a breaking change is not a skip, because it recurs until somebody publishes by hand, so it raises an `::error` and exits non-zero. A repository with no GA tag yet skips both remaining conditions rather than evaluating them against all of history, matching what `calculate_next_version.sh` does in that state — checking would halt on some long-shipped `feat!:` with no range left to shrink, permanently. There is no weekday or elapsed-time check in it — the cron is the cadence — and no "already released?" condition either, because a GA tag on the gated commit empties the range and the second condition covers it. It takes the candidate lookup (`get_latest_staging_tag`), the commit-range read (`release_read_commit_range`) and the breaking-change definition (`commit_messages_have_breaking_change`) from `common.sh` rather than re-implementing any of them: the first keeps it agreeing with `verify_release_eligibility.sh` about which candidate has been promoted, and the other two keep it agreeing with `calculate_next_version.sh` about which commits are in the range and which of them count as breaking. See "The weekly GA release" below for what the gate is actually buying over the publishing path's own behaviour.
- `decide_release_gate.sh`: Chooses which way into `release-publish.yml` a run is taking and emits the verdict the publish job is gated on. A `schedule` event always evaluates; a dispatch reads the workflow's `schedule_gate` input — `bypass` (the default, and what every dispatch did before the gate existed, emergency path included), `dry-run` (run the resolver, report the verdict, publish nothing) and `evaluate` (act on it, exactly as a cron tick would). An unrecognised mode exits non-zero rather than falling back to publishing, and so does `dry-run` or `evaluate` dispatched alongside a `target_commit`: those two modes let the resolver pick the commit from the tag graph, while the publish job's `TARGET_COMMIT` prefers the input, so honouring both would release a commit whose range the breaking-change halt never scanned. Naming a commit is `bypass`, which is the default.
- `calculate_next_version.sh`: Automatically calculates the next SemVer 2.0 version from Conventional Commits since the latest numeric GA release tag.
- `verify_release_eligibility.sh`: Release gatekeeper that verifies commit eligibility, checks for a shape-valid staging promotion tag (`staging_<ts>_<sha>`, meaning the full nightly matrix passed on the commit), performs tag collision detection, and verifies all 6 required container images exist in registry. It does not also require `rc_*_validated`: a staging tag is only ever derived from a candidate that carries one, so checking both would leave two gates to keep in step. `skip_staging_validation` with an audit reason is the emergency bypass.
- `tag_ga_release.sh`: Creates and pushes official GA SemVer Git tags (`X.Y.Z`) on a detached HEAD commit stamped with the release version in installer scripts (`install.sh`, `uninstall.sh`, `upgrade.sh`), Helm charts (`charts/kube-agents/Chart.yaml`), and Terraform defaults (`terraform/examples/full-install/variables.tf`, `terraform.tfvars.example`). Note: candidate commits must carry the `^BAKED_RELEASE_VERSION=` placeholder line in root installer scripts, `version`/`appVersion` fields in Helm charts, and `image_tag` defaults in Terraform examples.
- `promote_release_images.sh`: Promotes verified container images from candidate commit SHA to GA release tag in GHCR without rebuilding.
- `sign_release_images.sh`: Signs promoted GA release container images in GHCR using Keyless Cosign OIDC.
- `publish_helm_chart.sh`: Packages, publishes, and signs the official kube-agents Helm chart to GHCR as an OCI artifact. Extracts the chart tree directly from the release commit SHA via `extract_commit_tree`.
- `generate_release_sbom.sh`: Generates Software Bill of Materials (SBOM) in SPDX 2.3 JSON (`.spdx.json`) and CycloneDX 1.5 JSON (`.cdx.json`) formats using Syft for the staged filesystem bundle and each of the four release container images (`k8s-operator`, `platform-agent`, `credential-proxy`, `replay-proxy`). Staged in an isolated temporary directory and moved atomically into `DIST_DIR`. The `syft` CLI is mandatory in CI (exits 1 if missing or if image SBOM generation fails) and optional locally (warns and skips).
- `package_release_bundle.sh`: Assembles self-contained offline distribution archives (`kube-agents-<version>.tar.gz` and `.zip`) for air-gapped environments. Extracts tracked files directly from the resolved release commit via `extract_commit_tree` to ensure dirty or untracked files are never packaged. Stamps `BAKED_RELEASE_VERSION` into root installer scripts, Helm `Chart.yaml`, and Terraform example defaults, writes the `.release-bundle` provenance marker, packages Helm charts, invokes `generate_release_sbom.sh`, sanitizes sensitive files (tokens, credentials, keys, real tfvars while preserving `terraform.tfvars.example`), computes SHA256 checksums into `checksums.txt`, and promotes verified assets atomically into `DIST_DIR`.
- `sign_release_artifacts.sh`: Signs `checksums.txt` in `DIST_DIR` using Keyless Cosign OIDC, producing `checksums.txt.bundle` to provide verifiable cryptographic supply-chain provenance for all offline distribution archives and SBOMs. The `cosign` CLI is mandatory in CI (exits 1 if missing or signing fails) and skipped with a dry-run warning locally.
- `publish_github_release.sh`: Publishes official GitHub Releases with auto-generated release notes from Conventional Commits, discovers and attaches all distribution artifacts (`.tar.gz`, `.zip`, `.tgz`, `*.spdx.json`, `*.cdx.json`, `checksums.txt`, `checksums.txt.bundle`) from `DIST_DIR`, and handles idempotent re-runs via `gh release upload --clobber`.

## Pipeline Cadence & Execution Flow

The end-to-end pipeline (`.github/workflows/rc-release-pipeline.yml`) is dispatched by a scheduler and can also be triggered manually:

- **Scheduled Cadence (`rc-scheduler.yml`, every 3 hours `17 */3 * * *`, best-effort)**:
  - Automatically scans recent commits on `main` (`FETCH_HEAD`) for published container images in GHCR, using the same `resolve_rc_tag.sh` the pipeline runs.
  - **Redundant Run Skipping**: If the latest candidate commit already carries an `rc_*_validated` tag or was previously attempted, the scheduler dispatches nothing and records why in its job summary. The pipeline gets no run at all, which is the point — a skipped pipeline run concluded `success` and painted over the last run that failed.
  - Dispatches with the default `GITHUB_TOKEN` and `actions: write` on the job. `workflow_dispatch` and `repository_dispatch` are the two events GitHub exempts from the rule that suppresses runs triggered by that token, so no PAT is needed here — unlike a tag push, where the suppression is real and `RELEASE_BOT_TOKEN` is required.
  - _Note_: Scheduled runs are scheduled at minute `17` to avoid GitHub Actions peak top-of-the-hour queue congestion; actual start times are best-effort based on GitHub scheduler availability.
- **Manual Trigger (`workflow_dispatch`)**:
  - Requires an explicit `commit_sha` input to rigorously test a specific target commit.

## What Happens to the RC Cluster

The pipeline builds a full GKE cluster per candidate and destroys it twice over: step 2 removes whatever was there before it installs, and step 5 removes what the run itself built. A run that passes therefore leaves nothing behind and nothing billing.

A run that fails anywhere does leave its environment standing, deliberately — step 5 hangs off the success of every earlier job, and the E2E failures worth diagnosing are the ones that only reproduce on the cluster that produced them. Two consequences to know about:

- Nothing else removes that environment. The next run's step 2 does, which on the schedule is up to three hours later, so an investigation that needs longer than that wants the schedule paused rather than a race against it.
- Step 2 is the only thing standing between a surviving environment and a candidate validated against stale state, which is what `TEARDOWN_STRICT` decides. Truthy stops the run instead of installing on top; the same failure in step 5 is fatal regardless, because no later step compensates for it. Set it on each environment the pipeline runs against, where `GCP_PROJECT_ID` and every other value these jobs read already live — the repository level holds none of them, and `vars` resolving environment over repository makes a stray repository-level copy easy to set and then not find again.

## Enabling the GitHub token minter on the RC

`test_github_token_minting_and_connectivity` mints a real GitHub App token inside the agent pod and reads a repository back through it. It fails on an install where the minter was never provisioned: the chart renders the `github-token-minter` Deployment only under `githubMinter.enabled`, so the credential sidecar's refresh reaches no broker and answers `HTTP 502`, with the reason logged inside the sidecar where CI never sees it.

The repository it probes comes from the same two variables that scope the minter, so the two cannot drift: `deploy-environment.yml` gives them to the installer and `e2e-run.yml` gives them to the suite. The GitHub App has to be installed on that repository — a token minted for one repository does not authenticate against another.

Three settings on the `rc` GitHub environment turn it on, and all three must be present before the minter is provisioned at all ([`installer_common.sh`](../../scripts/installer/installer_common.sh)). All three empty is a supported configuration — an install without a minter, which is the default everywhere outside the RC. Some set and some empty is not: `provision_environment.sh` refuses, before the teardown, rather than reprovisioning an RC whose token-minting test would fail with an HTTP 502 forty minutes later.

`GH_APP_ID` is a _secret_, and that takes one thing the two variables do not. A called workflow receives only the secrets its caller passes, so reaching the `rc` environment's copy needs both halves: `rc-release-pipeline.yml` calling this workflow with `secrets: inherit`, and the `deploy-environment` job declaring an `environment:` — which it renders from its `github_environment` input, so the RC caller has to pass `rc`. An explicit `secrets:` mapping in the caller cannot substitute — a `uses:` job has no environment, so it resolves the names against nothing and forwards empty strings, which is indistinguishable from never having configured the minter. `tests/test_minter_secret_wiring.py` pins both halves.

Set all three on the environment rather than the repository. A repository-level copy is not invisible — `vars` resolve environment over repository, and `secrets: inherit` carries the caller's repository secrets too — which is the problem: the wrong scope quietly works, so a stray copy is easy to set and then never find again.

| Setting                | Value                  | Notes                                                                                                 |
| ---------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------- |
| Variable `GITOPS_ORG`  | `gke-agentic`          | Repository owner.                                                                                     |
| Variable `GITOPS_REPO` | `kube-agents-rc-infra` | Bare name, not `owner/repo`. Terraform's `github_repo` is composed as `${GITOPS_ORG}/${GITOPS_REPO}`. |
| Secret `GH_APP_ID`     | the App ID             | Same App that is installed on the repository above.                                                   |

`GITOPS_ORG` and `GITOPS_REPO` are deliberately separate from `GH_ORG` and `GH_REPO`, which every other workflow does use for this. On the `rc` environment that pair names the _release_ repository (`gke-labs/kube-agents`) and is what `common.sh`'s `get_target_repo` resolves for tag and release operations; pointing the minter at it would scope a live App token to this repository.

The App's private key is separate, because it is signing material rather than configuration and never enters Terraform state. Import it into the minter's KMS key once, by hand. [`terraform/modules/github-minter/README.md`](../../terraform/modules/github-minter/README.md) is canonical for that import and carries the Minty CLI route inline; it hands the `gcloud`/`openssl` path, for a host whose Go toolchain cannot build it, to `k8s-operator/config/integrations/github/README.md`. For the RC, the parameters it asks for are project `kube-agents-rc` and location `us-central1` (the KMS location is `GCP_REGION` with any zone suffix stripped, so it moves if the region does), with the default `github-token-minter-keyring` and `github-token-minter-key` names.

Do not hand-create the key from the `gcloud kms keys create` in that module's Terraform without `--skip-initial-version-creation`: KMS rejects an import-only key that does not skip it, which is why `skip_initial_version_creation = true` is set on the resource and why `install.sh`'s own pre-create passes the flag.

That import is a one-off. The key ring survives the teardown/reinstall cycle — `terraform destroy` cannot delete a Cloud KMS key ring, so `lifecycle.sh adopt-kms` re-adopts it on every apply — and both the enable decision and `install.sh`'s own import step short-circuit on an existing enabled version. Confirm with:

```bash
gcloud kms keys versions list --key=github-token-minter-key \
  --keyring=github-token-minter-keyring --location=us-central1 \
  --project=kube-agents-rc --filter=state=ENABLED
```

Setting an optional `GH_APP_PRIVATE_KEY` secret to the `.pem` contents is the alternative: `provision_environment.sh` writes it to a private temporary file and hands `install.sh` the path, which imports it on the first install that finds no enabled version. It exists to bootstrap an environment without a manual step, and costs an App private key living in GitHub Actions — which is why the manual import is the better of the two.

## The nightly environment

`nightly-pipeline.yml` resolves the newest `rc_*_validated` candidate, builds a whole environment
at that commit, and runs the `nightly` matrix on it. If the matrix passes it reconciles `staging`
against the same composition, tags the commit `staging_<ts>_<sha>`, reconciles `autopush`, and
destroys the nightly environment. The staging tag is the deploy trigger: pushing it starts
`staging-redeploy-{agent,controller,integrations}.yml` — which is why staging's reconcile goes
before it and autopush's, which no tag starts, goes after.

The two reconciles are #1117, and their placement is the substance of it. They run only after the
matrix, because applying a composition nobody has built from scratch to an environment people
live-test against is worse than leaving it stale; and staging's runs _before_ the tag is pushed,
because that tag starts three `helm upgrade`s on the release `helm_release.kube_agents` owns.
autopush's passes no image tag at all — it tracks main's tip, so pinning this pipeline's older
candidate would roll its images backwards.
[`environment-reconcile.md`](../../docs/site/src/content/docs/deploy/environment-reconcile.md) is
the canonical page for that whole path.

It reuses the RC pipeline's machinery unchanged. `deploy-environment.yml`, `teardown-environment.yml`,
`e2e-run.yml` and `reconcile-environment.yml` each take a `github_environment` input that renders
into the job's `environment:` key and into its concurrency group, so `rc` yields `rc-environment`
and `nightly` yields `nightly-environment` and the two pipelines never contend for a cluster. The
input is required and has no default, because a nightly caller that omitted it would tear down and
rebuild the RC.

It ships without a `schedule:`. A cron and a `workflow_dispatch` button only exist once the file is
on the default branch, so the first real run is necessarily after merge; a dispatch-only workflow
cannot affect anything running today. Add `cron: "0 2 * * *"` as its own change once the pipeline
has run by hand.

### What has to exist before it can run

None of this is in the repository, and the pipeline fails at `google-github-actions/auth` without
it — a job bound to an environment that does not exist resolves every `vars.*` to empty. The
project and the environment now exist and the variables are complete; the secrets are not, and
item 3 says which. The list stays whole because it is what a second environment would have to
reproduce, and because a value deleted from the web form fails the same way as one never created.

1. **A GCP project of its own.** `kube-agents-nightly`, not `kube-agents-rc`: sharing the project
   would put the two pipelines back on one cluster, which is the collision this exists to remove.
2. **Workload Identity Federation and the deploy service account**, created with
   [`setup-gcp-github-wif.sh --admin`](../../scripts/dev/setup-gcp-github-wif.sh)
   against that project. It creates the pool, the provider with its `assertion.repository`
   attribute condition, the service account and the full autonomous-E2E role set. Do not hand-roll
   the equivalent `gcloud` calls.
3. **A `nightly` GitHub environment** holding the same variables as `rc` rather than a trimmed
   subset — a missing one surfaces as an install failure deep in Terraform, not as a clear error.
   The ones the pipeline reads directly are `GCP_PROJECT_ID`, `GCP_REGION`, `GKE_CLUSTER_NAME`,
   `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `AGENT_NAMESPACE`, `GH_ORG`, `GH_REPO`,
   `GITOPS_ORG`, `GITOPS_REPO`, `CHAT_TOPIC_NAME`, `TEARDOWN_STRICT`, `ENABLE_PUBSUB_PLATFORM` and
   `ENABLE_STOCKOUT_INVESTIGATOR`. The rest are install inputs. Both environments also still carry
   the legacy `RC_TEARDOWN_STRICT`; nothing forwards it any more, so it can be deleted along with `teardown_common.sh`'s fallback.

   `REGISTRY_PREFIX` is read too but is **optional and set on no environment**, so do not go
   looking for it: the repository scope holds no variables at all, the workflows forward an empty
   string, and `get_registry_prefix` falls back to `DEFAULT_REGISTRY_PREFIX`. Set it only to point
   an environment at a registry other than `ghcr.io/gke-labs/kube-agents`. If you do, note that
   `registry_image_exists` — the probe `check_commit_images_exist` runs per image — has a docker-free
   fallback only for `ghcr.io`. Every release-path caller runs on a GitHub Actions runner with docker
   and so takes the `docker manifest inspect` branch regardless; a caller without docker pointed at
   another registry gets a warning and a "missing image" answer.

   Two of those earn a line of their own because getting them wrong is silent rather than loud.
   `GITOPS_ORG`/`GITOPS_REPO` name the repository the token minter is scoped to and the suite
   probes — not this repository, which is what `GH_ORG`/`GH_REPO` name. And `CHAT_TOPIC_NAME` has
   no fallback in `e2e_config.yaml`: `e2e-run.yml` exports it unconditionally, and an Actions
   `env:` key is defined even when its expression is empty, so an unset variable reaches the suite
   as an empty string rather than letting a configured default apply.

   The secrets are separate from the variables, and `nightly` needs `GH_APP_ID` and
   `GEMINI_API_KEY` on top of them. `GH_APP_ID` is not optional and its absence is not a skipped
   feature — `provision_environment.sh` treats `GITOPS_ORG`/`GITOPS_REPO`/`GITHUB_APP_ID` as
   all-or-nothing and hard-exits before teardown when two of three are set.

   The four `E2E_CHAT_*` secrets exist at repository scope and cascade, so an environment that
   overrides **none** of them still resolves all four. Override them as a set or not at all. Three
   of the four are a client id, a client secret and the refresh token issued against that pair: a
   refresh token does not exchange against a different client, so an environment that overrides the
   credentials and inherits the repository-scope refresh token fails the Chat token exchange — and
   on `nightly` that is a blocking-suite failure reported as a Chat regression in the candidate.

### Integrations the nightly matrix needs and the RC does not

`nightly` is a superset of `rc`, so the nightly environment needs everything the RC
environment needs and three things more. Setting them up is environment configuration rather than
repository code, and none of it happens automatically.

| Integration               | RC                                                          | Nightly                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **GitHub token minter**   | Configured                                                  | Required. `nightly` runs `test_agent_fleet_audit.py`, which contains `test_github_token_minting_and_connectivity`, and here it is a **blocking** suite where on the RC it sits behind `continue-on-error`. Needs `GITOPS_ORG`, `GITOPS_REPO` and the `GH_APP_ID` secret on the `nightly` environment, the App installed on that repository, and its private key imported into the nightly project's KMS key — the whole of "Enabling the GitHub token minter on the RC" above, against the new project. |
| **Google Chat**           | Configured                                                  | Required, same shape: `GOOGLE_CHAT_ENABLED`, `GOOGLE_CHAT_MODE`, `CHAT_TOPIC_NAME` and the four `E2E_CHAT_*` secrets. `gchat_agent_test.py` is in the matrix.                                                                                                                                                                                                                                                                                                                                           |
| **Pub/Sub alert ingress** | `ENABLE_PUBSUB_PLATFORM` and `ENABLE_STOCKOUT_INVESTIGATOR` | Required. Set both `ENABLE_PUBSUB_PLATFORM` and `ENABLE_STOCKOUT_INVESTIGATOR` to `true` on the environment (`rc` and `nightly`). Provisioned via Terraform and Helm during environment deployment (`deploy-environment.yml`), and `test_stockout_investigation.py` runs **all** scenarios here against the RC's one.                                                                                                                                                                                   |
| **Model provider**        | `GEMINI_API_KEY` on `rc`                                    | Required. Its own key, so a nightly run cannot exhaust the RC's quota.                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| **Operator plugin suite** | Not run                                                     | New. `operator/agentplugins_e2e_test.py` builds and pushes a plugin image, so the nightly project needs an Artifact Registry repository and the deploy service account needs write access to it. `setup-gcp-github-wif.sh --admin` grants the roles; the repository itself is created by `install.sh`.                                                                                                                                                                                                  |

Two things this list deliberately does not cover, because they are not part of the nightly
pipeline: the `staging` environment, which is a deploy target that nothing tests, and the GA
release path, which runs from `release-publish.yml` against the release repository.

## The weekly GA release

`release-publish.yml` has a gate job, `evaluate-schedule`, that answers the question a human used
to answer by choosing when to click "Run workflow". It runs before anything is published and the
publishing job is conditioned on its verdict, so the decision is one `if:` rather than one per
publishing step.

The gate reads the newest `staging_<ts>_<sha>` tag, and that tag is the only evidence a GA release
needs. It means the full nightly matrix passed on the commit, where an `rc_*_validated` tag means
only the narrow three-hourly suite did. `verify_release_eligibility.sh` reads the same family, so
the gate and the publishing path agree about which commits are releasable.

An `rc_*_validated` tag is not checked alongside it, because it is implied: a staging tag is only
ever created by `tag_staging_promotion.sh`, which refuses a commit that does not already carry one.
Requiring both would re-check a property the first guarantees and leave two gates to keep in step.

What replaces it as the defence against a fabricated tag is the **shape**. `staging-redeploy-*.yml`
triggers on the bare `staging_` prefix, so a hand-typed `staging_hotfix` is a supported way to
redeploy staging — and must not read back as "the matrix passed here". `STAGING_TAG_SHAPE_REGEX` in
`common.sh` is what the release path matches: the timestamp and short SHA in the positions
`staging_tag_for_rc` puts them, which is not a thing you compose by accident.

**Why the gate is a script rather than a `schedule:` block.** Less of the answer than you might
expect is "the quiet week would go red". It would not: on an ordinary week with nothing merged
since the last release, `calculate_next_version.sh` exits 0 with `has_changes=false`, then
`verify_release_eligibility.sh` recognises the GA tag as the stamped single-parent child of the
gated candidate, finds the GitHub Release, and takes its idempotent-skip branch. `skip_release=true`
gates every publishing step and the job finishes green today, with none of this.

Three narrower things are left, and together they are the gate:

- **The halt.** Nothing in the publishing path stops for a breaking change, and an unattended run
  is exactly where one should not go out unwatched. It will not clear itself either — every
  following run takes the same branch and GA releases stop — so it fails the job rather than
  skipping. Publish that one by hand.
- **Two shapes that do exit 1 with nothing to ship.** No staging tag anywhere in history trips
  `verify_release_eligibility.sh`; and a GA tag sitting on a commit that is not the gated
  candidate's stamped child — what an emergency release leaves behind — trips its "tag already
  exists on a different commit" collision. Condition 2 covers the second, which is the one that
  recurs.
- **Deciding in one place.** The idempotent-skip branch reaches its answer through a
  `gh release view` network call and a commit-shape heuristic several scripts deep. The gate reads
  the tag graph and says so in the job summary.

Be clear about how little the first of those saves on an ordinary week: the publish job's checkout,
a version calculation and that `gh release view` call. Not the registry inspections — both the
idempotent skip and the emergency-leftover collision return well before
`check_commit_images_exist` — and not a checkout on balance either, since the gate job pays its own
`fetch-depth: 0` on every run. Condition 2 earns its place on the emergency-leftover shape, where it
turns a red run into a green one, rather than on the quiet week, where it turns one green outcome
into a more legible green outcome. Red is left to mean the machinery is broken.

### Read this before you dispatch a release

**Until the nightly pipeline has pushed its first `staging_<ts>_<sha>` tag, no GA release can be
published at all** — not by cron, and not by hand. `bypass` short-circuits the gate job, so the
dispatch still starts; two steps later `verify_release_eligibility.sh` finds no staging tag on the
candidate and exits 1:

```
❌ BLOCKED: Commit <sha> has NOT been promoted to staging!
   No tag matching 'staging_<ts>_<sha>' points to this commit.
```

There are no staging tags in this repository yet, so that is the state on the day the retarget
lands, for the ordinary manual release as much as for a scheduled one. It is the gate working as
designed rather than a bug, but it is a release outage until step 1 below is done. The way through
is step 1; `skip_staging_validation` with an audit reason also passes, and is the emergency
override for hotfixes rather than a way to cut an ordinary release.

### Turning it on

**It ships without a `schedule:`, and the reason is one rung down the ladder.** The gate reads the
staging tag; `nightly-pipeline.yml` is the only thing that pushes one, and it is dispatch-only too.
A weekly cron over a tag family nothing produces on a schedule would skip green every Thursday and
demonstrate nothing about the gate. So, in order:

1. **Get `nightly-pipeline.yml` green.** Dispatch it against a validated candidate and let it push
   a real `staging_<ts>_<sha>` tag. Everything below is unreachable until one exists, and so is a
   GA release. Its own cron goes on once that is boring.
2. `workflow_dispatch` on `release-publish.yml` with `schedule_gate: dry-run` — the resolver runs
   against the real tag graph and reports what a cron tick would decide. Nothing is published. Note
   that a dry run still goes **red** if the verdict is a halt: it reports what the cron would do,
   and going red is part of that.
3. Same again with `evaluate` — the verdict is honoured, so a `should_release=true` publishes a
   real GA release. This is a cron tick in every respect except what started it.

   **Expect steps 2 and 3 to halt red the first time, and to keep halting until one release goes
   out by hand.** `feat(install)!: sandbox the agent under gVisor by default` (#865) is already on
   `main` and inside the range any first staging tag will produce, so condition 3 fires: step 2
   reports the halt and step 3 publishes nothing. That is the gate doing its job, not a fault in
   it. The way out is the one the `skip_reason` names — dispatch `release-publish.yml` with
   `schedule_gate: bypass` and publish that release yourself. The next GA tag empties the range,
   and steps 2 and 3 then behave as written.

4. Add `schedule: - cron: "17 5 * * 4"` to the workflow. Thursday leaves a working day to react to
   a bad release, which Friday does not. 05:17 UTC is meant to sit after the nightly pipeline has
   finished, but that is an estimate rather than a measured margin — its proposed 02:00 start gives
   a little over three hours for a run that budgets 60 minutes on the deploy alone. Being wrong
   about it costs latency and never correctness, because the gate is a poll: a candidate promoted
   later is simply picked up the following week. Pick a later slot if the two turn out to overlap.

Two things to know about a weekly cadence, neither of them a reason to change it. A Thursday that
produces nothing costs a full week, because there is no rate limiter inside the resolver to buy the
week back — the cron is the cadence, which is what keeps wall-clock arithmetic out of the decision
entirely. Against the staging gate that is a real risk rather than a rarity: a release needs a
staging tag newer than the last GA tag, which needs a fresh validated candidate _and_ a green
matrix on the same night, so the interval will sometimes be a fortnight. And a green skip and a
green pass are both `success` to
GitHub, so "the release workflow is green" does not distinguish a week that shipped from a week
that had nothing to ship. Reading the job summary is how you tell, until scheduled work here grows
an out-of-band signal.

## Workflow Mapping

These modular scripts back the corresponding child workflows in `.github/workflows/`:

| GitHub Workflow             | Release Step                                | Executed Scripts                                                                                                                                                                                                                                         |
| --------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rc-create-tag.yml`         | Step 1 - Create Candidate Tag               | `resolve_rc_tag.sh`, `verify_candidate_images.sh`, `create_release_tag.sh`                                                                                                                                                                               |
| `deploy-environment.yml`    | Step 2 - Deploy Environment                 | `resolve_rc_tag.sh`, `render_install_env.sh` (lease check only), `validate_and_log_deploy_summary.sh`, `provision_environment.sh`                                                                                                                        |
| `e2e-run.yml`               | Step 3 - GKE Readiness & E2E Validation     | `install_e2e_deps.sh`, `install_pubsub_platform.sh`, `wait_for_gke_readiness.sh`, `execute_e2e_tests.sh`, `run_optional_e2e_suites.sh`                                                                                                                   |
| `rc-tag-validated.yml`      | Step 4 - Validate Candidate Commit          | `resolve_rc_tag.sh`, `tag_validated_release.sh`                                                                                                                                                                                                          |
| `teardown-environment.yml`  | Step 5 - Tear Down Environment              | `resolve_rc_tag.sh`, `teardown_environment.sh`                                                                                                                                                                                                           |
| `nightly-pipeline.yml`      | Nightly promotion to staging                | `resolve_promotion_candidate.sh`, `verify_candidate_images.sh`, `record_nightly_candidate_summary.sh`, `tag_staging_promotion.sh`, plus the shared workflows listed elsewhere in this table                                                              |
| `rc-scheduler.yml`          | Three-hourly RC trigger                     | `resolve_rc_tag.sh`, `record_rc_scheduler_skip.sh`, `dispatch_rc_pipeline.sh`                                                                                                                                                                            |
| `staging-redeploy-*.yml`    | Staging deploy on a promotion tag           | `peel_tag_commit.sh`                                                                                                                                                                                                                                     |
| `reconcile-environment.yml` | Reconcile a long-lived environment in place | `render_install_env.sh`, `reconcile_environment.sh`                                                                                                                                                                                                      |
| `drift-detect.yml`          | Daily drift report on `autopush`/`staging`  | `render_install_env.sh`, `reconcile_environment.sh`, `report_drift.py`                                                                                                                                                                                   |
| `release-publish.yml`       | GA Release Orchestration                    | `decide_release_gate.sh`, `resolve_scheduled_release.sh`, `calculate_next_version.sh`, `verify_release_eligibility.sh`, `tag_ga_release.sh`, `promote_release_images.sh`, `sign_release_images.sh`, `publish_helm_chart.sh`, `publish_github_release.sh` |

`deploy-environment.yml`, `teardown-environment.yml` and `e2e-run.yml` are the rows
where the workflow and the script come from different commits. Each checks the
candidate out over the workspace before running its script, so the script is the
candidate's copy while the workflow is the caller's — which means a rename lands in
the workflow before it exists in any tree the workflow runs against.

For the first two the mismatch is loud, so a fallback covers it.
`provision_environment.sh` and `teardown_environment.sh` were renamed from
`provision_rc_environment.sh` and `teardown_rc_environment.sh`, and every
`rc_*_validated` tag up to `rc_2608310656_cf038a2_validated` predates that, so both
steps fall back to the old name when the new one is absent. `get_latest_validated_rc_tag`
has no recency window, so those candidates keep being resolved until the RC pipeline
validates a post-rename commit.

`e2e-run.yml` cannot be papered over the same way, because
its two mismatches are silent rather than loud: it names the suite in `E2E_SUITE`,
which a pre-rename runner ignores in favour of its own default, and it calls
`run_optional_e2e_suites.sh`, which does not exist in those trees at all under a
`continue-on-error` step. Either way the run reports a green matrix having tested
something else. So the nightly refuses those candidates outright rather than
running against them — `candidate_supports_shared_pipeline` checks both markers and
`resolve_promotion_candidate.sh` turns a negative into `skip_pipeline`, so no
cluster is built and nothing is tagged. `tag_staging_promotion.sh` keeps its own
check on the redeploy trigger, since it is reachable by hand.

Those last two outlive the transition, unlike the script-name fallbacks above.
`nightly-pipeline.yml`'s `rc_tag` input offers any validated candidate, and the
tag graph keeps every candidate it ever validated, so naming an old one by hand
stays possible long after the default path stops resolving one. So: delete the
two fallbacks once no `rc_*_validated` tag predates the rename, and keep the two
checks.

While any candidate is refused this way, a nightly dispatch reports that it did
nothing rather than exercising the pipeline — including the by-hand run the
"The nightly environment" section above asks for. Run the RC pipeline first and
let it validate a post-restructure commit.
