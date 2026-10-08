# Release Automation Scripts

This directory contains executable scripts supporting the environment pipelines: the
release-candidate (RC) pipeline, the staging promotion pipeline that promotes a validated candidate to
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

- `common.sh`: Centralized registry/repository helpers (`DEFAULT_REGISTRY_PREFIX`, `DEFAULT_RELEASE_REPO`, `REQUIRED_RELEASE_IMAGES`, and `required_release_images_at`, which reads that array from `scripts/release/common.sh` at a candidate commit — the list the candidate's own publish run built from, so a candidate or a release line from before the list grew is not refused for a name it never built, and a line that grew its list by backport is held to every name it has — and falls back to this checkout's array, saying so on stderr, when the commit, the file or a readable array is not there, or `git show` cannot read the file; every rung that iterates the list reads it this way, for its own candidate), commit discovery (`find_latest_built_commit`), validation check (`is_rc_candidate_commit_already_validated`, anchored to the `rc_*_validated` family so no other tag family can answer for it), the release commit-range read and the Conventional Commits breaking-change test (`release_read_commit_range`, `commit_messages_have_breaking_change` — both shared by the version calculator and the scheduled-release gate, so a bump and a halt cannot end up scoped to different commits or disagreeing about what "breaking" means; the range read also keeps git's stderr out of the captured subjects, since an ambiguous-refname warning on a successful, empty range would otherwise read as "there are commits to ship"), the staging promotion tag transform and the shape-anchored lookups the GA gate reads (`staging_tag_for_rc`, `get_existing_staging_tag`, `STAGING_TAG_SHAPE_REGEX`, `get_latest_staging_tag`, `staging_promotion_tags_at_commit` — the last three match `staging_<ts>_<sha>` rather than the `staging_` prefix `staging-deploy.yml` triggers on, so a hand-pushed trigger tag cannot read back to the release path as evidence the nightly matrix passed), the eval-candidate tag family that sits one step ahead of it (`evalcand_tag_for_rc`, `get_existing_evalcand_tag`, `EVALCAND_TAG_SHAPE_REGEX`, `evalcand_tags_at_commit` — the tag whose push fires the release-candidate eval, shape-anchored for the same reason and mirrored by the `branches` regex on `post-kube-agents-eval-rc` in `GoogleCloudPlatform/oss-test-infra`), the shared `rc_tag_core` both families compose through, so an `evalcand_` tag always reads back to the `staging_` tag it becomes without a lookup, the GA tag family and its lookups (`GA_TAG_SHAPE_REGEX`, `get_latest_ga_tag` — the numerically highest, which the rollback runbook and the `latest` mark on a GitHub Release read — and `get_base_ga_tag_for_commit` — the GA tag a candidate's history descends from, qualified against main's head for a main candidate: the base the version bump, the scheduled-release range and the release notes all start from, by ancestry, so a patch cut on a line never becomes main's base and a release cut by hand from a later main commit still does; its ceiling argument is how the publish step finds where the notes start once the release's own tag is in its history), container image promotion (`promote_release_images`), automated bot tagging (`ensure_git_tag`), the release-line helpers (`release_line_for_version`, `release_branch_for_line`, `release_line_head`, `release_line_candidate`, `release_line_resolve_candidate`, `validated_rc_tags_at_commit`, `release_branch_placement`, `set_release_branch_locally`, and `ensure_ga_release_refs`, which `tag_ga_release.sh` runs: it creates `release/<X.Y>` at the line's first release and fast-forwards it at a patch, and pushes the tag and the line in one atomic push through `release_push_refs`, of which `release_push_ref` is the one-ref form the candidate rungs use), `list_tags_on_main` (the newest-first tag listing behind `get_latest_validated_rc_tag`, `get_latest_staging_tag` and the eval's candidate resolver, restricted to commits on `main` so a tag cut on a release line never reads as main's candidate; in CI it reads the release repository's `main` through `release_main_ref`, fetched then and there, and fails when that fetch fails or a shallow checkout cannot be deepened, rather than fall back to a tracking ref that may be stale; off CI it uses the tracking or local `main` and says so, and in a shallow checkout returns the list unfiltered with a warning), and `release_fetch_tags` — the CI-only tag sync the resolvers (`resolve_rc_tag.sh`, `resolve_deploy.sh`, `resolve_promotion_candidate.sh`, `resolve_scheduled_release.sh`) run first, since a shallow or tagless checkout otherwise resolves "no such tag" rather than failing; the publish step relies on its job's full checkout instead. It also holds `release_resolve_target`, which resolves the cluster the kubectl-facing scripts act on. **In CI that resolution has no defaults**: `GKE_CLUSTER_NAME`, `GCP_REGION`, `GCP_PROJECT_ID` and `AGENT_NAMESPACE` must all be set — they come from the job's `env:` block, which reads them from the workflow's GitHub environment — and the script exits non-zero naming whichever is missing. A release script guessing which project it targets is the failure this prevents, since the old default pointed a teardown-and-reinstall at `kube-agents-rc` whatever the caller meant. Outside CI (`CI` unset or falsy) the developer defaults still apply, so running these by hand after `install.sh` needs no extra exports.
- `resolve_rc_tag.sh`: Validates candidate commit SHAs, resolves input tags/commit inputs, discovers the latest built commit on `main` during scheduled runs, checks for an existing `rc_*_validated` tag to skip redundant runs, and sets workflow step outputs. With no `rc_tag` input it reuses the pipeline's own tag for the commit, `rc_<ts>_<its short sha>`, if one exists and mints one otherwise; a hand-named `rc_*` tag is never reused, the pipeline's shape with another commit's sha included, and a hand-named input draws a warning, because the marker it earns (`<name>_validated`) is one a release line's gate does not read.
- `verify_candidate_images.sh`: Verifies that the prebuilt container images (`REQUIRED_RELEASE_IMAGES` as `common.sh` lists it at the candidate) exist in GHCR/registry for the target candidate SHA.
- `tag_commit.sh`: The one place a candidate-rung Git tag is created and pushed; the GA tag goes with its release line, atomically, through `ensure_ga_release_refs`, so `tag_ga_release.sh` prints the same banner but is not a wrapper over this. Prints a banner naming what is being tagged, then calls `ensure_git_tag`, which no-ops when the tag already points at the same commit and fails when it points at a different one. Every candidate-rung tagger below is a wrapper over it, keeping only what is genuinely its own; a second copy of this body is how the rungs of the release ladder drift apart.
- `create_release_tag.sh`: Creates and pushes candidate release tags (`rc_YYMMDDHHMM_<short_sha>`, derived from commit timestamp) safely and idempotently. When executed locally outside CI, runs in dry-run mode (creates tag locally and skips remote push).
- `resolve_promotion_candidate.sh`: Picks the candidate the staging promotion pipeline tests and decides whether passing it promotes anything. Emits `commit_sha`, `rc_tag`, `evalcand_tag`, `staging_tag`, `skip_reason`, `skip_pipeline` (the run does nothing at all — either no validated candidate exists, or the newest one is refused because its tree predates the shared-pipeline restructure; see "Workflow Mapping" below) and `skip_promotion` (the candidate already carries an `evalcand_*` or `staging_*` tag, so the matrix still runs, no eval is started and nothing is pushed — both families are checked because a candidate promoted before the eval gate landed carries the second and not the first). Selection and the validated check come from `common.sh`, so the promotion gate and the RC gate answer the same question. Every skip is exit 0; the exits that are not are a tag that does not resolve, and a hand-passed tag the RC pipeline never validated or whose tree predates the shared-pipeline restructure.
- `record_promotion_candidate_summary.sh`: Renders step 1 of the staging promotion pipeline into the job summary — which candidate the run picked, and whether a green matrix and a green eval will move staging. Keeps the two skips distinct: `SKIP_PIPELINE` means no run at all and carries `SKIP_REASON` to say which of its two causes applied, `SKIP_PROMOTION` means the matrix runs against a commit already nominated or promoted and a pass pushes nothing.
- `dispatch_rc_pipeline.sh`: Starts `rc-release-pipeline.yml` for a candidate `rc-scheduler.yml` resolved. Since the scheduler is the only thing that starts the pipeline, a failure here means no candidate is being tested at all, so it raises an `::error` annotation saying so before exiting non-zero, rather than leaving a bare exit code for the reader to interpret. The default `GITHUB_TOKEN` is enough: GitHub's recursion suppression exempts `workflow_dispatch`, which is why the staging tag push needs a PAT and this does not.
- `rollback_environment.sh`: Runs the rollback runbook against an installed environment from a clean checkout of the newest GA tag (`upgrade.sh --upgrade-mode=operator` then `--upgrade-mode=harness`, `--image-tag` that GA), checks that every first-party image in the gateway and operator Deployments carries the tag, that the `PlatformAgent` reports `Ready` and that the gateway rollout is complete, then rolls forward with the same pair from the candidate checkout. The rollback passes `--drop-undeclared-values` when the GA's `upgrade.sh` takes it, so a value the candidate's install recorded and the GA's chart does not declare is dropped, and the roll-forward renders it from the candidate chart's default rather than restoring it. Before either move it checks that the target tag exists in the registry each of the install's images is pulled from, so a missing tag is a refusal rather than an `ImagePullBackOff` after the CRDs have moved. When the live `litellm-policy` is the operator's and the GA is `0.5.0` or earlier (the last chart that renders it), it performs the chart README's handoff first, with the operator scaled to zero for the operator step and back up after it, because Helm refuses to adopt an object carrying another manager's ownership labels; before the roll-forward it then annotates the object `helm.sh/resource-policy: keep`, so the candidate's `helm upgrade` leaves it for the operator to adopt instead of pruning it. Inputs: `CANDIDATE_SHA` (required), `ROLLBACK_TAG`, `ROLL_FORWARD`, `KUBE_AGENTS_INSTALL_ENV` (rendered from the environment when unset, not strictly, because the re-tag applies nothing from it), `ROLLBACK_MODE=resolve` to print the tag and stop. Writes `diagnostics_dir` to `GITHUB_OUTPUT`. Called by `staging-promotion-pipeline.yml` step 3b.
- `record_rc_scheduler_skip.sh`: Records a quiet three-hourly tick. Because such a tick deliberately leaves no pipeline run behind, this summary is its only trace, and it says outright that a green scheduler reports nothing about the last pipeline run's result.
- `dispatch_promotion_pipeline.sh`: Starts `staging-promotion-pipeline.yml` for a candidate `staging-promotion-scheduler.yml` resolved. Since the scheduler is the only scheduled mechanism that starts the pipeline, a failure here raises an `::error` annotation saying so before exiting non-zero, rather than leaving a bare exit code for the reader to interpret.
- `record_promotion_scheduler_skip.sh`: Records a quiet daily tick in the scheduler's job summary when no candidate requires staging promotion, leaving no pipeline run behind.
- `run_optional_e2e_suites.sh`: Runs `e2e-run.yml`'s `optional_suites` list one suite at a time. Every suite runs regardless of what the ones before it did — a failure that short-circuited the loop would silently drop the coverage behind it — and the script exits non-zero if any failed, which the `continue-on-error` step turns into a red-but-tolerated result with the failing suites named in the job summary. The list is comma-separated because `workflow_call` has no list input type.
- `tag_eval_candidate.sh`: Pushes the `evalcand_<ts>_<sha>` tag that nominates a validated candidate for the release-candidate eval. What the push has to reach is the `branches` regex on `post-kube-agents-eval-rc` in `GoogleCloudPlatform/oss-test-infra`, which is why the script checks the tag's whole shape rather than just its prefix — a tag that clears the prefix guard but not the shape fires nothing, and the pipeline then waits out its entire poll deadline on a job that was never going to start. Inside this repository the tag has one reader, `resolve_promotion_candidate.sh`, which treats a commit carrying one as already answered about and skips it — so the tag is also what keeps a rejected candidate rejected, and what `drop_eval_candidate.sh` deletes when no verdict arrived. It applies the same guards as `tag_staging_promotion.sh`, including the staging-trigger check, deliberately early: a candidate that would be refused at promotion is a candidate whose eval is hours of a leased project spent on a verdict nobody can act on. Same GitHub App token, for the tag-protection rulesets rather than for the workflow trigger.
- `tag_staging_promotion.sh`: Pushes the `staging_<ts>_<sha>` tag that `staging-deploy.yml` deploys on. It derives the tag from the candidate rather than trusting one passed in, and refuses anything outside the `staging_` namespace — the tag is a live deploy trigger. It must be pushed with the dedicated GitHub App token (`kube-agents-release-bot`, configured via `RELEASE_BOT_APP_ID` and `RELEASE_BOT_APP_PRIVATE_KEY`); a tag pushed with the default `GITHUB_TOKEN` triggers no workflow, so the promotion would go green having deployed nothing.
- `poll_rc_eval_verdict.py`: Waits for the release-candidate eval's verdict on one nominated commit, by polling the GCS artifact archive of `post-kube-agents-eval-rc` (the eval job in `GoogleCloudPlatform/oss-test-infra`) for a build whose `started.json` names that commit. Newest build first, so a re-run from Deck supersedes the build it re-runs, and an in-flight re-run holds the answer rather than the stale verdict beneath it. The verdict comes from that build's `artifacts/rc-eval-summary.md`, not from the build's own pass/fail: `hack/ci-eval-rc.sh` writes GREEN, RED or NOT RUN there, and the exit status collapses the last two into the first — a Boskos project that never leased and an eval catalog that went red both exit non-zero, and a dormancy gate that measured nothing exits 0. A summary that is missing entirely reads as NOT RUN for the same reason: the driver writes it on every path that reaches its reporting step, so its absence means the run stopped before then. `finished.json` is consulted only to contradict the summary, and only in one direction: a GREEN summary under a build Prow failed becomes NOT RUN, while a RED summary stays RED whatever the build says. Symmetry there would be a promotion gate that a passing build could talk out of a red verdict. Exit codes: 0 green, 10 red, 11 not run, 12 the deadline passed with a build still running, 13 no build ever appeared, 14 the poller itself crashed. None of them is 1 or 2, which belong to an uncaught exception and to argparse, so a poller that died cannot be read as a release that failed. Alongside the exit code it writes `verdict`, `settled`, `build_id` and `log_url` to `GITHUB_OUTPUT`; `settled` is true only for green and red, which is what tells the caller whether the candidate was judged or its lane broke and may be retried. The short clock is the important one: nothing appearing within 45 minutes means the `evalcand_` tag shape and the job's `branches` regex have stopped matching, and waiting out the 330-minute deadline to say so would hide a configuration failure behind what looks like a slow eval. `--not-before` is what makes a second attempt at one candidate a retry rather than an echo: a re-nomination reuses the same tag at the same commit, so the previous attempt's build matches on commit alone and would answer in seconds, during the minute or two before Prow uploads the new `started.json` — the caller passes the unix second it pushed the tag and builds that began earlier are skipped. Omitting it is allowed, for a hand-run against a commit, and warns. Step 5 of `staging-promotion-pipeline.yml` runs it, between the nomination and the staging tag, passing the second step 4 recorded as it pushed.
- `drop_eval_candidate.sh`: Deletes the `evalcand_<ts>_<sha>` tag again, which is how a candidate gets a second eval. `ensure_git_tag` skips silently when the tag already points at the commit, so re-pushing the same name fires no webhook and starts no eval; the tag is therefore read as "this candidate has been answered about", and withdrawing the nomination is the only way to ask again. Whether the run may withdraw at all is not this script's decision — step 5b of `staging-promotion-pipeline.yml` runs it only when `poll_rc_eval_verdict.py` reported no settled verdict, so a RED candidate keeps its tag and is not retried. What the script owns is deleting nothing else: it refuses a tag that fails `EVALCAND_TAG_SHAPE_REGEX` and one that points at a commit other than the one it was given, which is what stands between a mis-set variable and a deleted `staging_` or `rc_*_validated` tag, both of which are in scope of the release bot token it holds. A tag that is not there is success, because that is the state it was asked to produce — checked against the remote as well as the local clone, since `release_fetch_tags` swallows a failed fetch and a clone that knows about no tags would otherwise read every tag as already withdrawn. A failed remote delete is fatal: the retry did not happen, the candidate is stranded until someone deletes the tag by hand, and this job is the only place that says so. Reding it costs nothing, because step 6 does not depend on it and step 5 has already failed.
- `peel_tag_commit.sh`: Resolves the ref a push event fired on to the commit it points at and writes it to `GITHUB_OUTPUT` as `commit_sha`. `staging-deploy.yml` runs it because `ensure_git_tag` creates annotated tags: a push event's `github.sha` is the new value of the ref, which for an annotated tag is the tag object's SHA, and passing that to container image tags names a GHCR image that was never published. Peeling a lightweight tag or a branch head returns the same SHA, so a caller need not know which it got.
- `resolve_deploy.sh`: Resolves candidate target commit SHA and validates lease policy for long-lived environments (`autopush` and `staging`). Configured via `TARGET_ENVIRONMENT` environment variable (`autopush` or `staging`). Called by `autopush-deploy.yml` and `staging-deploy.yml`.
- `validate_and_log_deploy_summary.sh`: Validates required environment variables and secrets, then logs a formatted deployment matrix and GCP cluster target overview for auditing before provisioning.
- `teardown_common.sh`: Sourced by the two scripts below, which both call `uninstall.sh` and read the same three outcomes out of its exit code (`./uninstall.sh --help` lists them). Holds the invocation, the `TEARDOWN_STRICT` parsing, and the job-summary rendering; each caller decides for itself what a failure means. The variable is read under both names — `TEARDOWN_STRICT` first, then the legacy `RC_TEARDOWN_STRICT` — because reading only the new one would have left the parser on an unset variable until the settings caught up, and unset is "off" with no error. Both `rc` and `nightly` now define `TEARDOWN_STRICT`, so `deploy-environment.yml` forwards that name alone; the fallback stays for anyone running these scripts by hand against an environment nobody has migrated, and is dropped when the old variable is deleted from both settings pages.
- `provision_environment.sh`: Tears the environment down with `uninstall.sh`, then reinstalls it at the candidate commit with `install.sh`. Which environment is entirely `GCP_PROJECT_ID` / `GCP_REGION` / `GKE_CLUSTER_NAME` and the rest of the install inputs, which `deploy-environment.yml` reads from the GitHub environment named by its `github_environment` input — `rc` for the RC pipeline, `nightly` for the nightly one. A failed teardown raises an `::error` annotation and a job-summary entry carrying the teardown output, and provisions anyway unless `TEARDOWN_STRICT` is truthy — the choice between validating a candidate against stale state and letting a teardown problem block every release. It also forwards the GitOps repository and, with it, the GitHub token minter; see "Enabling the GitHub token minter on the RC" below for what to set. With `PLATFORM_AGENT_MODE=next` (the workflow's `mode` input) it sets `spec.mode: next` on the `PlatformAgent` once the install has finished, because `install.sh` takes no mode, and runs `platform_agent_mode.sh`'s gate before it exits; unset or `today` runs nothing extra, and any other value is refused before the teardown. `next` is refused on `autopush`, `autopush-next` and `staging` too: on a long-lived cluster the patched mode outlives the run, because the chart renders no `spec.mode` and the installer does not read the key, so no upgrade, reconcile or drift report sees it, and Google Chat stays on the A2A gateway until a `today` rebuild. It is for the ephemeral `rc` and `nightly` clusters.
- `render_install_env.sh`: The one mapping from a GitHub environment's `vars.*`/`secrets.*` to the installer's `install.env`. A runner is ephemeral and has no hand-authored one, so every job that drives the installer renders one and points `KUBE_AGENTS_INSTALL_ENV` at it. `--strict` additionally requires every setting whose absence would _remove_ something from an install that already exists — the gVisor node pool, Hindsight, the backup plan, the Chat topic — and names all the missing ones in one annotation rather than one per run. That distinction is the whole difference between the two families of environment: `rc` and `nightly` are rebuilt every run, so an omitted setting costs a feature; the long-lived environments stay up for weeks, so the same omission is a `terraform apply` that plans a destroy. A key with no value is omitted from the file rather than written empty, because `KEY=` beats `install.defaults.env` and means "explicitly nothing". `PLATFORM_AGENT_MODE` is written only as `next`: an absent `spec.mode` is `today` to the `PlatformAgent` CRD, so `today` renders as no key at all.
- `platform_agent_mode.sh`: Sourced by `provision_environment.sh`, `render_install_env.sh` and `wait_for_gke_readiness.sh`. Holds the one check of `PLATFORM_AGENT_MODE` (`today` or `next`, unset meaning `today`, anything else refused), the merge patch that sets `spec.mode: next` on the `PlatformAgent`, and the gate that waits, inside one budget (`PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS`), for the `PlatformAgent` to report `Ready` for its current generation, for `BusProvisioned` and `BusCredentialsReady`, and for the NATS, auth callout, verifier, agent and A2A gateway rollouts. The A2A gateway is skipped when the `A2AGateway` condition says the install has no chat backend. Under `today` the variable is unset before anything runs, so the scripts behave as they did before it existed.
- `reconcile_environment.sh`: Applies `terraform/examples/full-install` to a long-lived environment, or reports what applying it would change. Renders the configuration strictly, waits out any `*-redeploy-*` run already in flight (both drive `helm upgrade` on the release `helm_release.kube_agents` owns), takes the live-test lease for an apply, and then runs `upgrade.sh --plan` or `upgrade.sh --upgrade-mode=full`. `LEASE_POLICY` picks what a held lease means: `defer` for the scheduled path, `fail` for a manual one. A plan takes no lease and holds no lock, because it changes nothing. Called by `reconcile-environment.yml`.
- `report_drift.py`: Turns a non-empty plan into one tracked issue per environment, labelled `infra-drift`, edited in place while the drift lasts and closed by the first clean plan. Found again by a marker in the body rather than by title, so retitling one does not orphan it. A plan that failed to run leaves whatever is open exactly as it is: a failure is evidence of nothing either way. Called by `drift-detect.yml`.
- `teardown_environment.sh`: Destroys the environment after a run that passed end to end, so the cluster exists only for the length of a run rather than idling between them. A failure here is always fatal and `TEARDOWN_STRICT` does not apply: nothing runs afterwards, so the alternative to a red job is a GKE cluster billing under a green pipeline. It runs only when the deploy and the matrix succeeded (in the staging promotion pipeline the rollback leg's result is deliberately not consulted), which is what leaves a failed run's environment standing to be examined live — until the next scheduled run reclaims it (up to three hours later on the RC, and on the next daily run for the nightly).
- `wait_for_gke_readiness.sh`: Connects `kubectl` to the target cluster, configures Artifact Registry credentials, optionally verifies the gateway is running the candidate commit's image — delegated to `scripts/confirm_agent_image.sh`, which the agent redeploy workflow runs for the same purpose — and waits for `litellm` and `platform-agent-gateway` to report ready. Right after it connects, it checks `PLATFORM_AGENT_MODE` (`e2e-run.yml`'s `mode` input, `today` when unset) against the installed `PlatformAgent`'s `spec.mode` (absent is `today`) and refuses a mismatch before any gate, because the deploy and E2E jobs take the mode separately. With `next` it then runs `platform_agent_mode.sh`'s gate, so the suites do not start while what that mode renders is still coming up.
- `tag_validated_release.sh`: Attaches the `rc_*_validated` marker to a candidate commit upon 100% test pass, by appending `_validated` to its `rc_*` tag.
- `resolve_scheduled_release.sh`: Decides whether an unattended run of `release-publish.yml` should publish. Three conditions — a candidate carries a shape-valid `staging_<ts>_<sha>` tag, commits exist between the GA release that candidate descends from (`get_base_ga_tag_for_commit`, qualified against main's head) and that candidate, and nothing in the range is a major breaking change on a stable GA release (`>= 1.0.0`) — emitted as `should_release`, `release_commit`, `gate_tag` and `skip_reason`. Under SemVer 2.0 Clause 4, pre-1.0 breaking changes bump minor (`0.4.0 -> 0.5.0`) and release unattended; on stable releases (`>= 1.0.0`), a major breaking change halts with exit 1 and raises an `::error` so a human publishes by hand. The first two failing are skips with exit 0. A repository with no GA tag yet skips both remaining conditions rather than evaluating them against all of history, matching what `calculate_next_version.sh` does in that state — checking would halt on some long-shipped `feat!:` with no range left to shrink, permanently. There is no weekday or elapsed-time check in it — the cron is the cadence — and no "already released?" condition either, because a GA tag on the gated commit empties the range and the second condition covers it. It takes the candidate lookup (`get_latest_staging_tag`), the commit-range read (`release_read_commit_range`) and the breaking-change definition (`commit_messages_have_breaking_change`) from `common.sh` rather than re-implementing any of them: the first keeps it agreeing with `verify_release_eligibility.sh` about which candidate has been promoted, and the other two keep it agreeing with `calculate_next_version.sh` about which commits are in the range and which of them count as breaking. See "The weekly GA release" below for what the gate is actually buying over the publishing path's own behaviour.
- `decide_release_gate.sh`: Chooses which way into `release-publish.yml` a run is taking and emits the verdict the publish job is gated on. A dispatch reads the workflow's `schedule_gate` input — `bypass` (the default, and what every dispatch did before the gate existed, emergency path included), `dry-run` (run the resolver, report the verdict, publish nothing) and `evaluate` (act on it, as dispatched by `release-scheduler.yml` on candidate match). A defense-in-depth `schedule` event branch also forces `evaluate`. An unrecognised mode exits non-zero rather than falling back to publishing, and so does `dry-run` or `evaluate` dispatched alongside a `target_commit`: those two modes let the resolver pick the commit from the tag graph, while the publish job's `TARGET_COMMIT` prefers the input, so honouring both would release a commit whose range the breaking-change halt never scanned. Naming a commit is `bypass`, which is the default.
- `dispatch_release_pipeline.sh`: Starts `release-publish.yml` with `-f schedule_gate=evaluate` when `release-scheduler.yml` evaluates that candidate conditions are satisfied (`should_release=true`). Verifies `gh` CLI presence defensively, emits error annotations on failure, and records the dispatch event into the Job Summary.
- `record_release_scheduler_skip.sh`: Records a quiet weekly tick into the Job Summary when `release-scheduler.yml` candidate evaluation determines no GA release should be published (e.g. no staging tag present, or zero commits since the GA release the candidate descends from). Because a quiet tick deliberately leaves no `release-publish.yml` run behind, this summary is its only trace, explicitly clarifying that a green scheduler run reflects a clean evaluation and reports nothing about publishing status.
- `calculate_next_version.sh`: Automatically calculates the next SemVer 2.0 version from Conventional Commits since the candidate's base GA release (`get_base_ga_tag_for_commit`; in CI the tag list is first pruned to the release repository's, so a tag only the checkout holds is never the base). With `RELEASE_LINE=X.Y` the candidate is the head of `release/X.Y` (or, when that head is a stamped release commit, the commit it was cut from), the base must be on the line, and the bump is PATCH: a `feat:` or a breaking change on a line is refused by name, with `explicit_release_version` as the way to release one on purpose. Once a base's line has a branch, a fix-only range on main bumps MINOR (`bump_type=minor-line`) and an explicit patch version on that line is refused on main; patch numbers belong to the line.
- `verify_release_eligibility.sh`: Release gatekeeper. On every path it performs tag collision detection and verifies every image in `REQUIRED_RELEASE_IMAGES`, as `common.sh` lists it at the candidate commit (`required_release_images_at`), exists in registry. The validation gate differs by path: a release from `main` needs a shape-valid staging promotion tag (`staging_<ts>_<sha>`, meaning the full nightly matrix and the release-candidate eval both passed on the commit; it does not also require `rc_*_validated`, since a staging tag is only ever derived from a candidate that carries one, and two gates would be two to keep in step), and with `RELEASE_LINE=X.Y` the candidate must be the line's own (as the calculator resolves it, through `release_line_resolve_candidate`) and the gate is the RC pipeline's own `rc_<ts>_<its short sha>_validated` tag on it (`validated_rc_tags_at_commit`, bound to the candidate's sha), earned by dispatching `rc-release-pipeline.yml` against the line's head. `skip_staging_validation` with an audit reason is the emergency bypass of either gate.
- `tag_ga_release.sh`: Creates and pushes official GA SemVer Git tags (`X.Y.Z`) on a detached HEAD commit stamped with the release version in installer scripts (`install.sh`, `uninstall.sh`, `upgrade.sh`), Helm charts (`charts/kube-agents/Chart.yaml`), and Terraform defaults (`terraform/examples/full-install/variables.tf`, `terraform.tfvars.example`). It pushes the same commit to the version's _release line_, `release/<X.Y>`, together with the tag (`ensure_ga_release_refs` in `common.sh`): the line's first release creates it at the stamped commit (a minor, or the first patch of a minor that predates the lines), and a later patch — stamped from the line's head, so its single parent is that head — fast-forwards it. Every release commit on a line stays reachable from the branch, nothing is ever force-pushed, and a line anywhere but at the release commit, at its candidate, or already past the release commit (the re-run case below) is an error. Before anything is pushed, `release_branch_placement` makes the same read, so a line already elsewhere stops the run with nothing pushed. The tag and the line then go in one atomic push (`ensure_ga_release_refs`), so neither exists on the remote without the other: a merge that lands on the line meanwhile rejects both and the re-run stamps from the new head, and a run that fails after the push re-runs like one that failed at image promotion, reusing the tagged commit — a line that took a merge in between is already past the release commit and is left where it is. For a patch from a line, name the version on that re-run (`release_line` with `explicit_release_version`): the line's candidate is then the commit that release was cut from, whatever merged since, where a plain re-dispatch would cut the next patch from the new head. A rejected push takes the tag and the line back out of the checkout that ran it, so nothing local outlives it; and in CI both reads — is the tag there, where is the line — are of the remote, never of the checkout, so a tag or line left behind some other way (a dry run, a run killed before its push) is recreated at the release commit and pushed with the other, stamped afresh if the candidate moved, while a local line that has moved on from the remote's is refused rather than moved. Outside CI both are created locally and neither is pushed. A merge Tide pushes to a line publishes SHA-tagged images (`docker-publish-ghcr.yml`; its `decide` job runs `decide_image_publish.sh`, which builds nothing for a push by anyone else — the GA tagger's push of a stamped release commit included, since the tagger pushes as the release App — or for a commit whose images already exist, since a rebuild under the same `:<sha>` tags would replace the manifests the ladder validated, and fails the run rather than build when the registry cannot be asked; `:latest` never moves. The pusher test guards against a mistaken push, not a deliberate one: the script runs from the pushed commit, so who may push a release line at all is a branch rule, not this). Releases 0.1.0 to 0.7.0 predate the lines and sit on per-release `release/<X.Y.Z>` branches, left as they are; pushes to those build nothing. Note: candidate commits must carry the `^BAKED_RELEASE_VERSION=` placeholder line in root installer scripts, `version`/`appVersion` fields in Helm charts, and `image_tag` defaults in Terraform examples.
- `promote_release_images.sh`: Promotes verified container images from candidate commit SHA to GA release tag in GHCR without rebuilding; the images are the ones the candidate's own `common.sh` lists.
- `sign_release_images.sh`: Signs promoted GA release container images in GHCR using Keyless Cosign OIDC; the images are the ones `common.sh` lists at the release tag's commit (`required_release_images_for_release`: a stamp changes installer, chart and Terraform files only, so that is the candidate's list).
- `publish_helm_chart.sh`: Packages, publishes, and signs the official kube-agents Helm chart to GHCR as an OCI artifact. Extracts the chart tree directly from the release commit SHA via `extract_commit_tree`.
- `generate_release_sbom.sh`: Generates Software Bill of Materials (SBOM) in SPDX 2.3 JSON (`.spdx.json`) and CycloneDX 1.5 JSON (`.cdx.json`) formats using Syft for the staged filesystem bundle (both formats) and each release container image in `REQUIRED_RELEASE_IMAGES` as `common.sh` lists it at the release tag's commit (SPDX only). Staged in an isolated temporary directory and moved atomically into `DIST_DIR`. The `syft` CLI is mandatory in CI (exits 1 if missing or if image SBOM generation fails) and optional locally (warns and skips).
- `package_release_bundle.sh`: Assembles self-contained offline distribution archives (`kube-agents-<version>.tar.gz` and `.zip`) for air-gapped environments. Extracts tracked files directly from the resolved release commit via `extract_commit_tree` to ensure dirty or untracked files are never packaged. Stamps `BAKED_RELEASE_VERSION` into root installer scripts, Helm `Chart.yaml`, and Terraform example defaults, writes the `.release-bundle` provenance marker, packages Helm charts, invokes `generate_release_sbom.sh`, sanitizes sensitive files (tokens, credentials, keys, real tfvars while preserving `terraform.tfvars.example`), computes SHA256 checksums into `checksums.txt`, and promotes verified assets atomically into `DIST_DIR`.
- `sign_release_artifacts.sh`: Signs `checksums.txt` in `DIST_DIR` using Keyless Cosign OIDC, producing `checksums.txt.bundle` to provide verifiable cryptographic supply-chain provenance for all offline distribution archives and SBOMs. The `cosign` CLI is mandatory in CI (exits 1 if missing or signing fails) and skipped with a dry-run warning locally.
- `publish_github_release.sh`: Publishes official GitHub Releases with notes GitHub generates (`gh release create --generate-notes`) from the pull requests merged since the release commit's own previous GA tag (`get_base_ga_tag_for_commit` below the release: `0.7.1`'s notes start at `0.7.0`, and `0.8.0`'s at `0.7.0` even when `0.7.2` exists on the line), grouped by the label categories in `.github/release.yml` and excluding Dependabot's and anything labelled `duplicate`, `invalid` or `wontfix`; discovers and attaches all distribution artifacts (`.tar.gz`, `.zip`, `.tgz`, `*.spdx.json`, `*.cdx.json`, `checksums.txt`, `checksums.txt.bundle`) from `DIST_DIR`; and handles idempotent re-runs via `gh release upload --clobber`. Those notes and the compare view, not a milestone, are where a release's contents are: `.github/workflows/auto-assign-milestone.yml` still runs after every merge to `main`, but exits without assigning anything when no milestone is open, and none is kept. The notes start from the previous GA tag, passed explicitly as `--notes-start-tag`: GA tags sit on stamped commits that never return to `main`, and left to pick the start tag itself GitHub started 0.4.0's notes from 0.2.0 and 0.5.0's from the first commit. The tag is `get_base_ga_tag_for_commit` from `common.sh` with the release as its ceiling, or `PREVIOUS_VERSION` from the environment for a hand run (`release-publish.yml` sets none). With no lower GA tag the flag is omitted and the script warns: that is the first release, or a checkout that did not fetch its tags, and the script cannot tell them apart locally, so the banner's `Notes Start Tag` line and the warning are the check. It does not call `release_fetch_tags`; like `resolve_release_commit`, it relies on the publish job's `fetch-depth: 0` checkout.

## Pipeline Cadence & Execution Flow

The end-to-end pipeline (`.github/workflows/rc-release-pipeline.yml`) is dispatched by a scheduler and can also be triggered manually:

- **Scheduled Cadence (`rc-scheduler.yml`, every 3 hours `17 */3 * * *`, best-effort)**:
  - Automatically scans recent commits on `main` (`FETCH_HEAD`) for published container images in GHCR, using the same `resolve_rc_tag.sh` the pipeline runs.
  - **Redundant Run Skipping**: If the latest candidate commit already carries an `rc_*_validated` tag or was previously attempted, the scheduler dispatches nothing and records why in its job summary. The pipeline gets no run at all, which is the point — a skipped pipeline run concluded `success` and painted over the last run that failed.
  - Dispatches with the default `GITHUB_TOKEN` and `actions: write` on the job. `workflow_dispatch` and `repository_dispatch` are the two events GitHub exempts from the rule that suppresses runs triggered by that token, so no PAT is needed here — unlike a tag push, where the suppression is real and the release bot GitHub App token is required.
  - _Note_: Scheduled runs are scheduled at minute `17` to avoid GitHub Actions peak top-of-the-hour queue congestion; actual start times are best-effort based on GitHub scheduler availability.
- **Staging Promotion Scheduled Cadence (`staging-promotion-scheduler.yml`, daily at `17 2 * * *`, best-effort)**:
  - Automatically resolves the latest validated candidate (`rc_*_validated`) on `main` using `resolve_promotion_candidate.sh`.
  - **Redundant Run Skipping**: If no eligible validated candidate exists, the scheduler dispatches nothing and records the reason in its job summary via `record_promotion_scheduler_skip.sh`.
  - Dispatches `staging-promotion-pipeline.yml` using `dispatch_promotion_pipeline.sh` with the default `GITHUB_TOKEN` and `actions: write`.
- **Weekly GA Scheduled Cadence (`release-scheduler.yml`, weekly on Fridays at `17 6 * * 5`, best-effort)**:
  - Automatically resolves whether an eligible candidate exists using `resolve_scheduled_release.sh` (requiring a valid `staging_<ts>_<sha>` tag and unreleased commits since the GA release the candidate descends from, while halting on major breaking changes on stable `>= 1.0.0` releases).
  - **Redundant Run Skipping**: If no eligible candidate exists or no new commits have merged, the scheduler dispatches nothing and records why in its job summary via `record_release_scheduler_skip.sh`.
  - Dispatches `release-publish.yml` using `dispatch_release_pipeline.sh` (`-f schedule_gate=evaluate`) with the default `GITHUB_TOKEN` and `actions: write`.
- **Manual Trigger (`workflow_dispatch`)**:
  - Schedulers and pipelines support manual trigger via `workflow_dispatch`. For manual dispatches, the RC pipeline requires `commit_sha` (mandatory), the staging promotion pipeline accepts an optional `rc_tag` candidate override (defaulting to the newest eligible validated candidate on `main` when omitted), and `release-publish.yml` accepts `schedule_gate` (`bypass` by default, `dry-run`, or `evaluate`), `explicit_release_version`, `target_commit`, `release_line`, and emergency override flags. Schedulers take no inputs (evaluating live state).

## What Happens to the RC Cluster

The pipeline builds a full GKE cluster per candidate and destroys it twice over: step 2 removes whatever was there before it installs, and step 5 removes what the run itself built. A run that passes therefore leaves nothing behind and nothing billing.

A run that fails anywhere does leave its environment standing, deliberately — step 5 hangs off the success of every earlier job (in the staging promotion pipeline, every earlier job but the rollback leg, as its section says), and the E2E failures worth diagnosing are the ones that only reproduce on the cluster that produced them. Two consequences to know about:

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

The App's private key is separate, because it is signing material rather than configuration and never enters Terraform state. Import it into the minter's KMS key once Ahead-Of-Time following the [upstream guide](https://github.com/abcxyz/github-token-minter#readme) (see also [`terraform/modules/github-minter/README.md`](../../terraform/modules/github-minter/README.md)). For the RC, the parameters it asks for are project `kube-agents-rc` and location `us-central1` (the KMS location is `GCP_REGION` with any zone suffix stripped, so it moves if the region does), with the default `github-token-minter-keyring` and `github-token-minter-key` names.

Do not hand-create the key from the `gcloud kms keys create` in that module's Terraform without `--skip-initial-version-creation`: KMS rejects an import-only key that does not skip it, which is why `skip_initial_version_creation = true` is set on the resource and why `install.sh`'s own pre-create passes the flag.

That import is a one-off. The key ring survives the teardown/reinstall cycle — `terraform destroy` cannot delete a Cloud KMS key ring, so `lifecycle.sh adopt-kms` re-adopts it on every apply — and both the enable decision and `install.sh`'s own import step short-circuit on an existing enabled version. Confirm with:

```bash
gcloud kms keys versions list --key=github-token-minter-key \
  --keyring=github-token-minter-keyring --location=us-central1 \
  --project=kube-agents-rc --filter=state=ENABLED
```

## The nightly environment

`staging-promotion-pipeline.yml` resolves the newest `rc_*_validated` candidate on `main`, builds a whole environment
at that commit, and runs the `nightly` matrix on it. A green matrix no longer promotes the
candidate; it nominates it, by creating and pushing `evalcand_<ts>_<sha>`. That tag starts the
release-candidate eval in Prow, `poll_rc_eval_verdict.py` waits for its verdict, and only a green
one produces `staging_<ts>_<sha>`. The nightly environment is destroyed as soon as the matrix
finishes — teardown depends on none of those jobs, because none of them touches the cluster
and waiting on the eval would hold it for hours.

Two of the three verdicts end the candidate's night. Green promotes it; red holds it back, the
`evalcand_` tag staying put so `resolve_promotion_candidate.sh` skips the commit from then on. The
third is no verdict at all — a Boskos pool with nothing free, a job that never started, the
poller's own deadline — and that is the one the tag must not outlive, because a commit recorded as
answered about that nobody ever answered is a candidate stranded until a newer one supersedes it.
So `drop_eval_candidate.sh` runs in that case and deletes the tag again, which is enough on its
own: the next nightly resolves the same commit, finds no nomination on it, and nominates it afresh.

The staging tag is the deploy trigger: pushing it starts `staging-deploy.yml`, which atomically
reconciles the composition, Helm release, and images together via `upgrade.sh --upgrade-mode=full`.

Similarly, `autopush` and `autopush-next` receive atomic deploys through `autopush-deploy.yml` whenever
container images are published to GHCR. Both workflows enforce atomic full upgrades, preventing image drift and
contention.
[`environment-reconcile.md`](../../docs/environment-reconcile.md) is
the canonical page for that whole path.

After the matrix, and before the teardown, step 3b runs the rollback runbook
([`rollback.md`](../../docs/site/src/content/docs/deploy/rollback.md)) on the same cluster:
`rollback_environment.sh` checks out the newest GA tag, runs its `upgrade.sh --upgrade-mode=operator`
and `--upgrade-mode=harness` back to that GA, checks that both images carry the GA tag, that the
`PlatformAgent` reports `Ready`, and that the gateway rollout is complete, then runs the same pair
from the candidate checkout to bring the cluster back to the candidate. The job carries
`continue-on-error` and is not in step 4's `needs`, so a red leg is reported in the run and in the
`promotion-rollback-leg` artifact (the upgrade logs and, on failure, the pod list, events, operator log and Helm history)
without holding the promotion; admitting it as a gate is a decision taken on its record, by moving it
into that list and dropping the flag together. Step 5 depends on it for ordering only and its condition names the deploy and matrix results rather than the leg's, so a red leg still tears the cluster down.

It reuses the RC pipeline's machinery unchanged. `deploy-environment.yml`, `teardown-environment.yml`,
`e2e-run.yml` and `reconcile-environment.yml` each take a `github_environment` input that renders
into the job's `environment:` key and into its concurrency group, so `rc` yields `rc-environment`
and `nightly` yields `nightly-environment` and the two pipelines never contend for a cluster. The
input is required and has no default, because a nightly caller that omitted it would tear down and
rebuild the RC.

It is scheduled via a decoupled trigger workflow (`staging-promotion-scheduler.yml`, daily at `17 2 * * *`),
which resolves the newest validated candidate on `main` and dispatches this pipeline only when promotion is needed.
Unlike the RC pipeline (which marks attempted candidates to avoid repeating failing runs), the nightly
scheduler retries nightly until a candidate passes the E2E matrix and receives a `staging_*` tag or a newer
validated candidate arrives. A candidate whose eval returned no verdict is picked up again by that same
retry, because the withdrawal above leaves it looking exactly as it did before it was nominated.

One case is not retried, and it is the "or a newer validated candidate arrives" half of the sentence
above that ends it: a candidate whose eval returned GREEN and whose `staging_*` push then failed.
The withdrawal is keyed on the eval not having answered, so a green verdict followed by a failed
push leaves the `evalcand_` tag in place, and the resolver skips that commit from then on. It is
not promoted at all; staging waits for the next validated candidate, which the RC pipeline produces
within hours. Recovering it sooner means re-running the failed job from the Actions UI, which
reuses the verdict already on record. See #1767.

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
   credentials and inherits the repository-scope refresh token fails the Chat token exchange whenever
   the suite falls back to it (the service account being denied `spaces.messages.list`) — and on the
   RC pipeline, where `gchat` is the blocking suite, that stops the candidate tag as a Chat regression.

### Integrations the nightly matrix needs and the RC does not

`nightly` is a superset of `rc`, so the nightly environment needs everything the RC
environment needs and three things more. Setting them up is environment configuration rather than
repository code, and none of it happens automatically.

| Integration               | RC                                                          | Nightly                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **GitHub token minter**   | Configured                                                  | Required. `nightly` runs `test_agent_fleet_audit.py`, which contains `test_github_token_minting_and_connectivity`, and here it is a **blocking** suite where on the RC it sits behind `continue-on-error`. Needs `GITOPS_ORG`, `GITOPS_REPO` and the `GH_APP_ID` secret on the `nightly` environment, the App installed on that repository, and its private key imported into the nightly project's KMS key — the whole of "Enabling the GitHub token minter on the RC" above, against the new project. |
| **Google Chat**           | Configured                                                  | Required, same shape: `GOOGLE_CHAT_ENABLED`, `GOOGLE_CHAT_MODE`, `CHAT_TOPIC_NAME` and `E2E_CHAT_SPACE_ID`; the three OTA `E2E_CHAT_*` secrets until the service account's read scope is approved (`tests/e2e/README.md`, "Hybrid Auth Model"). `gchat_agent_test.py` is in the matrix.                                                                                                                                                                                                                 |
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

The gate reads the newest `staging_<ts>_<sha>` tag on `main`, and that tag is the only evidence a
GA release from `main` needs. It means the full nightly matrix and the release-candidate eval both passed on the commit,
where an `rc_*_validated` tag means only the narrow three-hourly suite did. `verify_release_eligibility.sh` reads the same family, so
the gate and the publishing path agree about which commits are releasable.

An `rc_*_validated` tag is not checked alongside it, because it is implied: a staging tag is only
ever created by `tag_staging_promotion.sh`, which refuses a commit that does not already carry one.
Requiring both would re-check a property the first guarantees and leave two gates to keep in step.

What replaces it as the defence against a fabricated tag is the **shape**. `staging-deploy.yml`
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

- **The halt.** On stable releases (`>= 1.0.0`), nothing in the publishing path stops for a major
  breaking change, and an unattended run is exactly where one should not go out unwatched. It will not
  clear itself either — every following run takes the same branch and GA releases stop — so it fails
  the job rather than skipping. Publish that one by hand. During initial pre-1.0 development (`0.y.z`),
  breaking changes bump minor under SemVer 2.0 Clause 4 (`0.4.0 -> 0.5.0`) and release unattended.
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

**Every candidate selected for a GA release from `main` must carry a `staging_<ts>_<sha>` tag
produced by the staging promotion pipeline** — whether released by cron or dispatched by hand.
(A patch from a release line has the other gate, `rc_*_validated` on the line's head; see
"Patch releases from a release line" below.) `bypass` short-circuits the gate job, but in the
publish job `verify_release_eligibility.sh` verifies staging promotion on the candidate commit and
exits 1 if unpromoted:

```
❌ BLOCKED: Commit <sha> has NOT been promoted to staging!
   No tag matching 'staging_<ts>_<sha>' points to this commit.
```

Staging tags are pushed by `staging-promotion-pipeline.yml`, on the nights whose matrix and release-candidate
eval both came back green. The gate works as designed
to ensure only thoroughly validated commits reach GA; `skip_staging_validation` with an audit
reason is strictly the emergency override for hotfixes rather than a way to cut an ordinary release.

### Dispatching a release by hand

Before dispatching:

1. The target commit exists on `main` (a patch from a release line names no commit; it passes
   `release_line` instead — see below).
2. It carries a `staging_<ts>_<sha>` tag from the staging promotion. An `rc_*_validated` tag is
   not checked alongside it: a staging tag is only ever derived from a candidate that already
   carries one.
3. The release images in `REQUIRED_RELEASE_IMAGES`, as `common.sh` lists them at that commit
   (on `main` today `k8s-operator`, `platform-agent`, `credential-proxy`, `agent-sandbox`,
   `replay-proxy`, `pubsub-platform`, `gke-stockout-investigator`, `a2a-gateway`, `a2a-worker`,
   `a2a-authcallout`, `a2a-console`, `hermes-bridge`), exist in GHCR under that commit. The gate reads the
   candidate's list, not the checkout's (`required_release_images_at`), so a commit or a
   release line from before the list grew needs only the images it was published with.
4. `gh`, authenticated with `repo` and `workflow` permissions (`gh auth status`).

Then dispatch `release-publish.yml` from the Actions tab or the CLI:

```bash
# Standard release: the next version is calculated from Conventional Commits.
gh workflow run release-publish.yml --repo gke-labs/kube-agents

# A specific staging-promoted commit.
gh workflow run release-publish.yml --repo gke-labs/kube-agents \
  -f target_commit="<TARGET_COMMIT_SHA>"

# An explicit version, which is how 0.y.z graduates to 1.0.0.
gh workflow run release-publish.yml --repo gke-labs/kube-agents \
  -f explicit_release_version="1.0.0"
```

A dispatch runs with `schedule_gate=bypass` unless told otherwise, so it publishes without the
scheduled gate's verdict; `verify_release_eligibility.sh` still enforces staging promotion in the
publish job. `dry-run` reports the verdict in the job summary and publishes nothing.

### Emergency hotfix

`skip_staging_validation: true` skips the live GKE validation gate and nothing else. It is
reserved for two situations: a zero-day CVE in a container dependency that needs immediate
publication, or a production regression where waiting for the next nightly promotion would
prolong user-facing downtime.

Three invariants hold under the bypass:

1. `verify_release_eligibility.sh` still requires every image in `REQUIRED_RELEASE_IMAGES`, as
   `common.sh` lists it at `<TARGET_COMMIT>`, to exist in GHCR under that commit; an unbuilt
   commit hard-fails.
2. `emergency_override_reason` must carry a non-whitespace justification; an empty one aborts the
   workflow.
3. The target SemVer tag must not already exist on another commit; a collision aborts the release.

Where the hotfix goes decides the number. Once a minor's release line exists, its patch numbers
belong to the line: an emergency release from `main` takes the next MINOR (a fix-only range bumps
MINOR, and an explicit patch version on that line is refused on `main`), and a patch is released
from the line with `release_line` (the same bypass applies, and `target_commit` is refused because
the line names its own head). For a release from `main`, always pass `target_commit`: omitting it
defaults to the tip of `main` and releases every intervening commit without live validation:

```bash
# Version calculated automatically.
gh workflow run release-publish.yml --repo gke-labs/kube-agents \
  -f skip_staging_validation=true \
  -f emergency_override_reason="CVE-2026-XXXX: critical vulnerability in base container dependencies" \
  -f target_commit="<HOTFIX_COMMIT_SHA>"

# Explicit version, before the base's line has a branch (afterwards a patch on
# that line is refused here; release it from the line, below).
gh workflow run release-publish.yml --repo gke-labs/kube-agents \
  -f skip_staging_validation=true \
  -f emergency_override_reason="Critical regression fix for gateway admission deadlock" \
  -f target_commit="<HOTFIX_COMMIT_SHA>" \
  -f explicit_release_version="0.3.1"

# An emergency patch from a release line: the fix is already merged onto
# release/<X.Y>, and the RC validation is what is being skipped.
gh workflow run release-publish.yml --repo gke-labs/kube-agents \
  -f release_line="<X.Y>" \
  -f skip_staging_validation=true \
  -f emergency_override_reason="Critical regression fix for gateway admission deadlock"
```

Afterwards:

1. Confirm the release, its tag and the attached assets with `gh release view <VERSION>`. The
   promoted images and the OCI chart are not release assets; check them separately, as the
   scripts do, with `docker manifest inspect ghcr.io/gke-labs/kube-agents/<image>:<VERSION>` and
   `docker manifest inspect ghcr.io/gke-labs/kube-agents/charts/kube-agents:<VERSION>`.
2. Dispatch `rc-release-pipeline.yml` against the hotfix commit
   (`-f commit_sha="<HOTFIX_COMMIT_SHA>"`, the same SHA passed as `target_commit`) so the full
   GKE E2E suite runs on it. Do not pass the tagged release commit: `tag_ga_release.sh` creates a
   stamped commit on detached HEAD that has no SHA-tagged images in GHCR, and the RC pipeline's
   image verification fails on it. For an emergency patch released from a line there was no
   `target_commit`; the commit to validate is the line's head before the stamp,
   `git rev-parse upstream/release/<X.Y>^`.
3. Attach the Actions run URL and the justification to the tracking issue or incident report.

### Patch releases from a release line

A release line, `release/<X.Y>`, is where a minor's patch releases come from. The GA tagger creates
it at the line's first release (a minor, or the first patch of a minor that predates the lines) and
each later patch fast-forwards it; a line takes fixes only, and
patch numbers belong to it, so once a line has a branch a fix-only week on `main` releases as the
next MINOR. The nightly matrix, the release-candidate eval and the Friday cron never look at a
line; a patch is validated by the RC pipeline and released by hand.

1. **Land the fix on the line.** Cherry-pick the merged `main` commit onto a branch cut from
   `upstream/release/<X.Y>`, open a pull request whose base is `release/<X.Y>` with a `fix:`
   title, and let Tide merge it (a `feat:` or a breaking change is refused at release time). The
   merge builds the commit's SHA-tagged images. The lines carry `main`'s protection in three
   repository settings, none of them files in this tree. A `release/*` branch protection rule
   requires conversation resolution and restricts pushes to Tide and the release bot. The
   `kube-agents-release-lines` ruleset lets only those two create, push or delete a line. The
   `kube-agents-release-lines-checks` ruleset requires `main`'s status contexts, copied in by
   hand (the list is in `docs/pull-request-workflow.md`, "Branch protection is not the gate", and a
   context added to `main`'s set has to be added to the ruleset too), with the release bot as its only
   bypass actor. The contexts live in a ruleset rather than on the branch
   protection rule because a branch protection rule cannot exempt an app from required checks,
   and the tagger's push of a freshly stamped commit, which has had no checks run, was rejected
   by exactly that when it created the first line. Tide is deliberately not a bypass actor of the
   checks ruleset, so a backport merge still needs every context. If a backport pull request
   shows no required checks or merges without them, those three settings are where to look. A
   checkout of a line between stamps carries the previous release's
   `BAKED_RELEASE_VERSION`; run from it, with the release's tag and full history fetched,
   `install.sh` recognises the shape and defaults to the checkout's own commit, as on `main`,
   and `upgrade.sh` asks for `--image-tag`. `--image-tag <sha>` of the backport's images works
   from anywhere, as for any `main` commit.
2. **Validate the line's head.**

   ```bash
   gh workflow run rc-release-pipeline.yml \
     --repo gke-labs/kube-agents \
     --ref main \
     -f commit_sha="$(git rev-parse upstream/release/<X.Y>)"
   ```

   The pipeline has one concurrency group, so a dispatch during the three-hourly `main` run waits
   for it. It ends by tagging the head `rc_<ts>_<sha>_validated`, which is the gate the next step
   checks — that name exactly, with the head's own short SHA in it, as the staging gate reads its
   own shape: a hand-typed `rc_hotfix_validated` does not count, and neither does a composed
   `rc_<ts>_0000000_validated`. Leave the `rc_tag` input empty for the same reason: a
   run dispatched with `rc_tag=rc_hotfix` ends by tagging `rc_hotfix_validated`, which the gate
   refuses. If that has happened, dispatch again with the input empty; `resolve_rc_tag.sh` reuses
   only the pipeline's own tag for the commit, `rc_<ts>_<its short sha>`, so the re-run mints one
   beside the hand-named tag and validates under it. The nightly promotion ignores the line's tag either way: it reads
   `main`'s candidates only.

3. **Release.**

   ```bash
   gh workflow run release-publish.yml \
     --repo gke-labs/kube-agents \
     --ref main \
     -f release_line="<X.Y>"
   ```

   No `target_commit` and no `schedule_gate` other than `bypass` (the default): the line names its
   own candidate — its head, or, when the head is already a stamped release commit, the commit
   that stamp was cut from, which is how an idle line or a re-run lands in the idempotent skip.
   The version is the line's last release plus one PATCH; `explicit_release_version` must stay on
   the line.

4. **Check.** The tag `X.Y.Z` on a stamped commit whose parent is the line's previous head,
   `release/<X.Y>` fast-forwarded to it, the images promoted from that parent's SHA tags, and
   release notes starting at the line's previous release. A patch below the newest minor is
   published without GitHub's `latest` mark, so `releases/latest` keeps pointing at the newest
   version.

If the release job fails with "Release line 'release/<X.Y>' already exists ... but points to
commit", or earlier with "Release line X.Y resolves to its own head ..., not to ...", the line
moved after the candidate was picked, usually a merge that landed meanwhile: nothing was pushed,
and a re-run releases from the new head. A line that moved _after_ the push landed is not an
error: a re-run finds it past the release commit and carries on to promotion. If that re-run is
for a patch and the plain `release_line` dispatch now computes the next patch instead, add
`explicit_release_version=<the version>` to finish the one that was cut. If a hand push moved the
line somewhere else, put it back first.

### Scheduled execution & testing the gate

**Scheduled execution is owned by `.github/workflows/release-scheduler.yml` via the decoupled
trigger pattern (`cron: "17 6 * * 5"`), while `release-publish.yml` remains dispatch-only.** The gate
reads the staging tag produced by `staging-promotion-pipeline.yml` (dispatched by `staging-promotion-scheduler.yml`).
The activation ladder progresses in order:

1. **Staging promotion is green and scheduled.** `staging-promotion-pipeline.yml` has successfully promoted
   candidates (producing real `staging_<ts>_<sha>` tags), and its automated daily dispatch is
   established via `staging-promotion-scheduler.yml` (`17 2 * * *`). Everything below is reachable now that
   staging tags exist.
2. **Exercise the gate via `dry-run` dispatch.** `workflow_dispatch` on `release-publish.yml` with
   `schedule_gate: dry-run` — the resolver runs against the real tag graph and reports what a cron
   tick would decide. Nothing is published. Note that a dry run still goes **red** if the verdict
   is a halt on `>= 1.0.0`: it reports what the cron would do, and going red is part of that.
3. **Validate evaluation end-to-end.** Same again with `evaluate` — the verdict is honoured, so a
   `should_release=true` publishes a real GA release. This is a cron tick in every respect except
   what started it.

   **On stable releases (`>= 1.0.0`), major breaking changes halt red until published by hand.**
   When a breaking change sits between the candidate's base GA tag and the gate tag on a repository with a
   `>= 1.0.0` GA release, condition 3 halts with exit 1 (`🛑 HALTED — A HUMAN HAS TO PUBLISH THIS ONE`)
   and step 3 publishes nothing. That is the gate doing its job, not a fault in it. The way out is
   the one the `skip_reason` names — dispatch `release-publish.yml` with `schedule_gate: bypass`
   and publish that release yourself. In initial pre-1.0 development (`0.y.z`), breaking changes
   bump minor under SemVer 2.0 Clause 4 (`0.4.0 -> 0.5.0`) and release unattended.

4. **Automate on weekly schedule via dedicated scheduler.** Automated execution is owned by
   `.github/workflows/release-scheduler.yml` via the decoupled trigger pattern (`cron: "17 6 * * 5"`).
   The schedule runs overnight from Thursday into Friday at 06:17 UTC so typical nightly staging
   promotion runs (`02:17 UTC` scheduled, `02:40–03:10 UTC` actual start, `05:28–05:59 UTC`
   completion) finish pushing `staging_*` before the Friday release evaluation runs. Because the
   pipeline waits on the release-candidate eval between its matrix and its staging tag (a step
   allowed up to 345 minutes on its own), an unusually slow run that goes the full timeout distance
   can still push `staging_*` after 06:17 UTC; that costs latency and never correctness, because the
   gate is a poll — a candidate promoted after this tick is picked up the following week rather than
   missed.

Two things to know about a weekly cadence, neither of them a reason to change it. A Friday that
produces nothing costs a full week, because there is no rate limiter inside the resolver to buy the
week back — the cron is the cadence, which is what keeps wall-clock arithmetic out of the decision
entirely. Against the staging gate that is a real risk rather than a rarity: a release needs a
staging tag on a commit above main's last release, which needs a fresh validated candidate _and_ a green
matrix on the same night, so the interval will sometimes be a fortnight. And because
`release-scheduler.yml` dispatches `release-publish.yml` only when `should_release=true`, a quiet
week leaves no `release-publish.yml` run behind at all. On the scheduler itself, a green run can mean
either a successful dispatch or a quiet skip; reading the scheduler's job summary is how you tell.

## Workflow Mapping

These modular scripts back the corresponding child workflows in `.github/workflows/`:

| GitHub Workflow                   | Release Step                                | Executed Scripts                                                                                                                                                                                                                                                                                       |
| --------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `rc-create-tag.yml`               | Step 1 - Create Candidate Tag               | `resolve_rc_tag.sh`, `verify_candidate_images.sh`, `create_release_tag.sh`                                                                                                                                                                                                                             |
| `deploy-environment.yml`          | Step 2 - Deploy Environment                 | `resolve_rc_tag.sh`, `render_install_env.sh` (lease check only), `validate_and_log_deploy_summary.sh`, `provision_environment.sh`                                                                                                                                                                      |
| `e2e-run.yml`                     | Step 3 - GKE Readiness & E2E Validation     | `install_e2e_deps.sh`, `install_pubsub_platform.sh`, `wait_for_gke_readiness.sh`, `execute_e2e_tests.sh`, `run_optional_e2e_suites.sh`                                                                                                                                                                 |
| `rc-tag-validated.yml`            | Step 4 - Validate Candidate Commit          | `resolve_rc_tag.sh`, `tag_validated_release.sh`                                                                                                                                                                                                                                                        |
| `teardown-environment.yml`        | Step 5 - Tear Down Environment              | `resolve_rc_tag.sh`, `teardown_environment.sh`                                                                                                                                                                                                                                                         |
| `staging-promotion-pipeline.yml`  | Nightly promotion to staging                | `resolve_promotion_candidate.sh`, `verify_candidate_images.sh`, `record_promotion_candidate_summary.sh`, `rollback_environment.sh`, `tag_eval_candidate.sh`, `poll_rc_eval_verdict.py`, `drop_eval_candidate.sh`, `tag_staging_promotion.sh`, plus the shared workflows listed elsewhere in this table |
| `rc-scheduler.yml`                | Three-hourly RC trigger                     | `resolve_rc_tag.sh`, `record_rc_scheduler_skip.sh`, `dispatch_rc_pipeline.sh`                                                                                                                                                                                                                          |
| `staging-promotion-scheduler.yml` | Daily nightly promotion trigger             | `resolve_promotion_candidate.sh`, `record_promotion_scheduler_skip.sh`, `dispatch_promotion_pipeline.sh`                                                                                                                                                                                               |
| `staging-deploy.yml`              | Staging deploy on a promotion tag           | `resolve_deploy.sh`, `verify_candidate_images.sh`, `reconcile-environment.yml`, `verify_deploy_result.sh`                                                                                                                                                                                              |
| `autopush-deploy.yml`             | Autopush deploy on a published image set    | `resolve_deploy.sh`, `verify_candidate_images.sh`, `reconcile-environment.yml`, `verify_deploy_result.sh`                                                                                                                                                                                              |
| `reconcile-environment.yml`       | Reconcile a long-lived environment in place | `render_install_env.sh`, `reconcile_environment.sh`                                                                                                                                                                                                                                                    |
| `drift-detect.yml`                | Daily drift report on long-lived envs       | `render_install_env.sh`, `reconcile_environment.sh`, `report_drift.py`                                                                                                                                                                                                                                 |
| `release-scheduler.yml`           | Weekly GA release trigger                   | `resolve_scheduled_release.sh`, `record_release_scheduler_skip.sh`, `dispatch_release_pipeline.sh`                                                                                                                                                                                                     |
| `release-publish.yml`             | GA Release Orchestration                    | `decide_release_gate.sh`, `resolve_scheduled_release.sh`, `calculate_next_version.sh`, `verify_release_eligibility.sh`, `tag_ga_release.sh`, `promote_release_images.sh`, `sign_release_images.sh`, `publish_helm_chart.sh`, `publish_github_release.sh`                                               |

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
check on the redeploy trigger, since it is reachable by hand, and
`tag_eval_candidate.sh` applies the same check one rung earlier so that a
candidate the promotion would refuse does not first consume an eval.

Those last two outlive the transition, unlike the script-name fallbacks above.
`staging-promotion-pipeline.yml`'s `rc_tag` input offers any validated candidate, and the
tag graph keeps every candidate it ever validated, so naming an old one by hand
stays possible long after the default path stops resolving one. So: delete the
two fallbacks once no `rc_*_validated` tag predates the rename, and keep the two
checks.

While any candidate is refused this way, a nightly dispatch reports that it did
nothing rather than exercising the pipeline — including the by-hand run the
"The nightly environment" section above asks for. Run the RC pipeline first and
let it validate a post-restructure commit.
