# The nightly evals' agent on the Gemini Developer API

> **STATUS — built; all four steps done as of 2026-11-06.** Approved in conversation on
> 2026-11-04. The repository change ships the key script, the provisioning step, the verifier
> check, the deploy's `gemini` path and the documentation. The registered projects carry the key
> (repaired 2026-11-05), the five nightlies export `MODEL_PROVIDER=gemini` from the night of
> 2026-11-06, and the verifier findings block and run in the hourly pool-state scan. The flip on
> 2026-11-06 moved the three APIs into `REQUIRED_APIS` and section 1's enable block and made the
> findings blocking; sections 1, 4 and 8 keep the original plan as history. On 2026-11-06
> `post-kube-agents-eval-rc` (GoogleCloudPlatform/oss-test-infra#2774) and the two Argon smoke
> presubmits `pull-kube-agents-smoke-test` and `-today` (GoogleCloudPlatform/oss-test-infra#2775)
> moved the same way, so "Presubmits stay on Vertex" below is history too: only the Claude and
> Flash smoke jobs and the forge smoke variants stay on Vertex AI. One item stays open: the
> version key does not carry the serving provider (#4932). The one precondition that could
> have made the design moot was probed on 2026-11-04 (see "What the probe showed").

## Intent

The nightly eval periodics grade `main` against the catalog every night. Their agent's model
calls go through LiteLLM to Vertex AI, and the Vertex project is at Tier 0, so the nightly is
rate-limited on the very thing it measures. The team's Gemini Developer API access is at Tier 3,
and the tier comes from the billing account every eval pool project shares, so an API key minted
in any pool project carries Tier 3 limits: on `gemini-4-argon`, 10,000 requests and 20,000,000
input tokens per minute, per project.

This change moves the nightly agent's model calls to the Gemini Developer API, with one API key
per pool project, so that each leased project runs in its own quota window. The judge stays where
it is. Presubmits stay on Vertex.

## Why one key per project, and why this is a reversal

The evals ran on the Developer API before. On 2026-09-02 every smoke run went red on 429s: the
single shared key's fixed Tier 3 quota was 8,000,000 input tokens per minute for
`gemini-3.1-pro`, and five concurrent builds drawing ~94k-token turns shared one minute window
(#1097; the diagnosis is on #1184). The move to Vertex was the fix, because Vertex's dynamic
shared quota had no fixed wall. Vertex is now the wall.

The nightly was three jobs that started together at 00:00 UTC, `ci-kube-agents-eval-nightly`
(`main-a`), `-main-b` and `-writers`, each at task parallelism 6, so 18 concurrent agent turns at
the peak, close to September's shape; since the main part runs as five shards and the writers part
as three thirds it is eight jobs, each on its own project's key: five main shards at parallelism 6
and three writers thirds whose units run one at a time, so 33 turns at the peak across eight keys and at most 6 on any one. One shared key at 20M TPM is about 2.5 times the ceiling
that failed; September's load works out near 400k input tokens per minute per concurrent stream,
which put 18 streams near 7M. That is headroom, not isolation. A key per project is isolation:
each nightly leases a different project, so each gets its own 20M window and the eight never
share a minute. The `-gitlab` nightly at 02:00 and `-gitlab-next` at 04:00 run alone in their
windows and move on the same mechanism.

## What the probe showed

On 2026-11-04, against `kube-agents-evals-40` while it was not leased: enabling
`apikeys.googleapis.com` and `generativelanguage.googleapis.com` worked; the project's effective
org policies carry no API-key-creation restriction and `gcp.restrictServiceUsage` is `allowAll`.
Creating the key was refused until the operator held `roles/serviceusage.apiKeysAdmin`: the pool
projects' only human owners are the pool owners, and the folder roles the operator holds
(`folderAdmin`, `folderEditor` on folder `564593885529`, which holds the whole pool) do not carry
it. With `apiKeysAdmin` and `secretmanager.admin` granted at that folder, a key restricted to
`generativelanguage.googleapis.com` was created, `models/gemini-4-argon` answered `models.get`
(input limit 1,048,576 tokens) and `generateContent` with a 200, and the project's quota table
lists the paid-tier buckets for `gemini-4-argon`: tier 3 at 20,000,000 input tokens and 10,000 requests per minute,
tier 2 at 8,000,000, with the free-tier buckets absent, as a billed project's are. The quota
listing names every tier's limit, not which tier the project is in; that is read once per pool
project in AI Studio's rate-limit page, and the first night's 429 count is the proof that matters.
The probe key was deleted afterwards; the APIs stay enabled.

**Operator precondition this adds:** whoever runs the key step, for a new project or the repair,
holds `roles/serviceusage.apiKeysAdmin` and `roles/secretmanager.admin` on the project, which the
folder-level grants above give for every pool project. The repair section names the pair.

## Goals

- The nightly periodics' agent model calls reach the Gemini Developer API through LiteLLM,
  authenticated by an API key that belongs to the leased pool project.
- Every pool project, the 40 registered ones and every new one, carries such a key, and the pool
  verifier fails a project without one.
- The switch is reversible by one job-env edit per periodic.
- The first night after the switch yields the numbers that justify it: peak input tokens per
  minute per project, 429 count, and prefix-cache hit rate, beside the last Vertex night's.

## Non-goals

- The judge. It keeps the shared key from the job env (`JUDGE_API_KEY` in `hack/ci-eval-pr.sh`)
  and is pinned independently of the agent; nothing here touches its serving path.
- The presubmits. They stayed on Vertex for this design; they had the same Tier 0 problem and
  were the follow-on the per-project keys made possible. The two Argon smoke presubmits moved on
  2026-11-06 (see the status above).
- The model. `gemini-4-argon` stays; `MODEL_DEFAULT_NAME` and `AGENT_MODEL_OVERRIDE` do not
  change on the jobs that export them. `ci-kube-agents-eval-nightly-gitlab-next` exports
  neither and so runs the deploy's default, `gemini-3.1-pro-preview`; the job-env change gives it
  the same two exports as its siblings, so every nightly agrees on the model.
- The evidence-store version key. It names the agent model, the judge, and the harness, not the
  serving path, and none of those change. Nights after the switch pool with nights before it in
  the same window (the call, and why, is under Decisions).
- Key rotation on a schedule. API keys do not expire; rotation is a re-run of the per-project key
  step and is documented, not automated.
- Removing the LiteLLM Vertex identity from pool projects. Presubmits still use it.

## Anti-goals

- A silent fallback to the shared key when a project's own key is missing. The fallback would
  hide a provisioning gap and quietly recreate the shared-minute failure. A missing key fails the
  deploy with a line naming the project and the repair.
- The key appearing in a log, an artifact, a Terraform state the pool does not already protect,
  or a `set -x` trace. The job's existing `GEMINI_API_KEY` handling is the bar.
- Re-running `scripts/provision_ci_pool_project.sh` on a registered project. Section 8 of
  `docs/ci-pool-projects.md` forbids it (the composition re-apply regenerates `api_server_key`
  and overwrites the leased install's values). Registered projects get a one-off, idempotent
  repair, as every other post-registration prerequisite has.

## Constraints

- `docs/ci-pool-projects.md` is the onboarding contract: every prerequisite has a section, a
  provisioning step for a new project, a verifier finding, and a hand repair for a registered one.
  This change adds one such prerequisite and follows that shape.
- `hack/ci-deploy.sh` already honours `MODEL_PROVIDER` from the job env ("a rollout problem is a
  job-env flip rather than a revert"), and the chart's `gemini` provider path reads
  `GEMINI_API_KEY` from the credentials the deploy sets (`charts/kube-agents/templates/litellm.yaml`).
  The Vertex-only pieces degrade safely on `gemini`: the session-affinity hook turns itself off,
  the Workload Identity annotation on the LiteLLM KSA is inert, the cache-strip and thinking-replay
  hooks treat `gemini` and `vertex_ai` alike. No chart change is needed.
- The pool's two runner identities are `prowjob-default-sa@kube-agents-prow` (presubmits) and
  `eval-baseline-recorder@kube-agents-prow` (nightlies). Both are granted on every pool project.
- The nightlies already mount the shared key (`kube-agents-gemini-api-key`) for the judge; the
  agent's key must come from the leased project, not from that mount.
- Secret Manager secrets in the pool projects use the defaults (automatic replication, Google
  managed encryption): the org has no CMEK or regional-replication requirement on them.
- The repository rules: no magic constants, no Co-Authored-By, PR text in Simplified Technical
  English, the pre-PR adversarial and docs-drift passes, live validation.

## The design

### 1. The key, per pool project

A Google Cloud API key in the pool project, restricted to one API target,
`generativelanguage.googleapis.com`, with a fixed display name. No referrer or IP restriction:
the LiteLLM pod's egress address is not stable across the host cluster's node pool. Created with
`gcloud services api-keys create` (the key string is read back with
`gcloud services api-keys get-key-string`), not through the full-install Terraform composition:
the composition is the install's engine and treats the Gemini key as an input a user supplies,
and the pool's other post-install prerequisites (the sweeper role, the minter grants, the GitOps
seed files) are bash in the provisioning script for the same reason.

The project needs three APIs it does not enable today: `apikeys.googleapis.com`,
`secretmanager.googleapis.com` and `generativelanguage.googleapis.com`. The key script enables
them. The plan kept them out of section 1's enable block and out of `REQUIRED_APIS` until the
flip to blocking, and until then the key check reported a project without them as missing the key.

### 2. Where the key lives

A Secret Manager secret in the same pool project, one fixed name, one current version, with
`roles/secretmanager.secretAccessor` granted on the secret (not the project) to both runner
identities, so a presubmit can take the same path later without another repair. Storing the key
in its own project keeps a pool project self-contained and lets the verifier judge a project by
reading the project. The alternative, one central secrets project keyed by pool project id (the
GitLab secrets' pattern, `GITLAB_SECRETS_PROJECT`), was considered and not taken: it adds a
cross-project dependency to every lease and splits the repair across two projects.

### 3. One script does both the new project and the repair

`scripts/provision_ci_pool_gemini_key.sh <project-id>`: idempotent; enables the three APIs,
creates the key if none with the display name exists, restricts an existing key to the one API
target, reads its string, creates the secret if
absent, adds a version only when the current version's value differs, grants the two accessors.
It prints nothing secret. `scripts/provision_ci_pool_project.sh` calls it for a new project; for
the 40 registered projects the operator runs it once per project (a loop over the Boskos roster)
as the hand repair, which `docs/ci-pool-projects.md` records in the new section. Rotation is the
same script after deleting the key.

### 4. The verifier

A new check in `scripts/verify_ci_pool_project.py`: the key exists with exactly the one API
target and no client restriction, the secret
exists with a current version, and both runner identities hold the accessor on it. It reads
metadata and IAM only; it never reads the key string. `scripts/test_verify_ci_pool_project.py`
pins it with fake gcloud answers for each finding and for refused reads, and holds its names
equal to the key script's constants.
Whether `generativelanguage.googleapis.com` is enabled is the project check's read: the three
APIs the key needs are in `REQUIRED_APIS` and the provisioning script's enable block.

The verifier's classification is two-valued: a finding either passes leases (`LEASE_SILENT_FINDINGS`)
or the health bot reads it as one that reds the run that leases the project. Between the
repository merge and the end of the 40 repairs, a blocking finding would mark every project as
pool drift and blame the pool for failures that are this change's. So the finding landed
lease-silent, in the verifier's default set from day one, and a second change flipped it to
blocking once the repairs were done and verified. It was kept out of the hourly pool-state scan
until that flip.

The scan runs as `eval-dashboard-publisher`, which already holds `roles/iam.securityReviewer`
on every pool project. That role carries the three reads the check makes:
`apikeys.keys.list`, `secretmanager.versions.list` and
`secretmanager.secrets.getIamPolicy`. So this change grants the bot no new role, and the
verifier maps the `secrets` surface to `roles/iam.securityReviewer`. The design first named
`roles/serviceusage.apiKeysViewer` for the key listing. That role was rejected: it carries
`apikeys.keys.getKeyString`, so the bot could read every key string.

### 5. The deploy

In `hack/ci-deploy.sh`, under `MODEL_PROVIDER=gemini`: read the leased project's secret
(`gcloud secrets versions access latest`, the form the GitLab path already uses) into the value
passed as `platformAgent.credentials.data.GEMINI_API_KEY`, in place of the job env's
`GEMINI_API_KEY`. The job env's key stays required and untouched, because the judge reads it in
`hack/ci-eval-pr.sh`. A secret that cannot be read fails the deploy before the Helm install, with
a line that names the project, the secret, and `scripts/provision_ci_pool_gemini_key.sh`. Under
`MODEL_PROVIDER=vertex_ai` nothing changes. The secret name is a named constant with an env
override, as the other `EVAL_*` knobs are.

The LiteLLM retry pair the deploy sets (`EVAL_LITELLM_NUM_RETRIES=2`,
`EVAL_LITELLM_RETRY_AFTER_SECONDS=20`) was tuned for Vertex 429s that carry no `Retry-After`. The
Developer API's 429 carries a retry delay, which LiteLLM honours when present, so the pair stays
as the floor and is not re-tuned until the first night's 429 count says otherwise.

### 6. The job env

In `oss-test-infra`, the eval nightlies export `MODEL_PROVIDER=gemini`:
`ci-kube-agents-eval-nightly`, `-main-b` to `-main-e`, `-writers`, `-writers-b`, `-writers-c`,
`-gitlab`, `-gitlab-next`. Nothing else in the job changes. Rollback is the same lines back to `vertex_ai`; the per-project keys and
secrets stay, harmless and ready. The `oss-test-infra` change merges after the repository change
that reads the secret, and after the 40 repairs: a nightly that leases an unrepaired project with
the export already set fails at the deploy by design.

### 7. Attribution and measurement

The serving provider is not in the version key, so the night of the switch has to be findable
another way. The deploy already prints the provider and model it installs; the roster
(`docs/eval-gate-roster.md`) gets a dated line naming the night the nightlies moved, as its other
dated notes do, and the eval dashboard's digest for that night is where a step in cost or latency
would show.

The first night's numbers come from three places, none of which needs new collection code. The
429 count comes from the LiteLLM pod logs `hack/ci-eval-pr.sh` already collects into the
artifacts (`collect_litellm_log`): those carry the 429 bodies, but not per-request token usage,
and the trace logger that would is not enabled on eval installs. Peak input tokens per minute per
project, and the share of cached input tokens, come from the project's own usage: Cloud
Monitoring's quota usage for the Generative Language API, and AI Studio's usage page for the
project, both of which report per minute. The expected result is zero 429s and a peak minute well
under 20M for every leased project. If the peak minute is within a factor of two of the wall,
parallelism comes down before anything else is tried. The three numbers go on the dashboard digest; the
issue that tracks the move names the night and its build id.

### 8. Documentation

- `docs/ci-pool-projects.md`: section 1 named the three APIs, outside its enable block, until the flip; section 3 gains the key as a
  prerequisite beside the LiteLLM Vertex identity, with the repair; the sentence saying eval
  installs route model traffic through Vertex becomes "the presubmits route through Vertex; the
  nightlies through the Developer API with the project's own key, and why".
- `hack/ci-deploy.sh`'s rationale comment above `MODEL_PROVIDER`: the default stays `vertex_ai`
  for the presubmits' concurrency; the nightlies override it, and the per-project key is why a
  shared-minute 429 storm cannot recur there.
- `docs/testing-map.md`'s nightly rows name the serving path.
- `docs/eval-gate-roster.md`: the dated switch line.
- The site is not touched: the pool, its projects and its keys are maintainer infrastructure.

### 9. Tests

- `tests/test_ci_deploy_litellm_vertex.py` keeps pinning the `vertex_ai` default; a sibling pins
  the `gemini` branch: the secret is read from the leased project, the job env's key is not used
  for the agent, and a missing secret fails before Helm.
- `scripts/test_verify_ci_pool_project.py` drives the check with fake gcloud answers: each
  finding, a disabled API read as absence, a refused read as not checked, and the names equal to
  the key script's.
- `make shellcheck` on the new script; a unit test over its idempotence shape if the repository's
  installer tests have a pattern for it (`tests/test_install_script.py` does).

### 10. Rollout order

1. Repository change: the key script, the verifier finding, the deploy branch, the docs, the
   tests. Live-validated by running the key script against one pool project while it is leased
   for a hand run, deploying with `MODEL_PROVIDER=gemini`, and driving one eval case through it.
2. Repair the 40 registered projects with the script; verify each.
3. `oss-test-infra`: the five exports.
4. Read the first night's three numbers; write them on the tracking issue.

## Decisions made in this design

- **Per-project Secret Manager over a central secrets project.** Self-contained projects, one
  repair per project, the verifier reads one project.
- **The key stays under the pool's bash provisioning, not the Terraform composition.** The
  composition is the user-facing install engine; the pool's post-registration prerequisites are
  already bash with hand repairs.
- **No fallback to the shared key.** Fail loudly; the failure names the repair.
- **Keep the version key.** Model, judge and harness are unchanged; a serving-path change is
  attributed by date on the roster, not by a new window that would cost every case its admission
  for seven nights.
- **Include the two GitLab nightlies.** Same mechanism, they run alone in their windows, and
  leaving them on Vertex would leave them on the Tier 0 wall for no reason.
- **Grant both runner identities the accessor now.** It costs nothing and spares a second repair
  when the presubmits follow.
- **The verifier finding is lease-silent until the repairs finish, then flipped.** A blocking
  finding during the repair window would blame the pool for the change's own gap.
- **No new reader role for the health bot.** `roles/iam.securityReviewer` already covers the
  check's reads; `roles/serviceusage.apiKeysViewer` would also read key strings.
- **No new collection code for the first night's numbers.** The logs give the 429 count; the
  project's own usage reporting gives tokens per minute and the cache share.
- **The five live nightlies, and only those.** The roster names lanes that are not in the live
  Prow config; the change edits what exists.

## Left to the builder

- Exact names: the key's display name, the secret's name, the verifier finding id, the env
  override for the secret name, the script's flags.
- Whether the key script's idempotence check compares the secret's current value to the key
  string or trusts the key's existence; either is acceptable if a rotated key reaches the secret.
- How the first night's three numbers are extracted from the collected LiteLLM logs (a one-off
  read is fine; a reusable summariser is a follow-on).
- Whether `generativelanguage.googleapis.com` must be enabled for the key to be created, or only
  for it to be used; enable it regardless.
