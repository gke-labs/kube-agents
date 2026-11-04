# Nightly Evals on the Gemini Developer API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the five eval nightlies' agent model calls from Vertex AI to the Gemini Developer API, authenticated by an API key that lives in the leased pool project, so each nightly runs in its own Tier 3 quota window.

**Architecture:** A new idempotent script mints a Generative-Language-restricted API key in a pool project and stores it in that project's Secret Manager; the pool provisioning calls it for new projects and the operator runs it once over the 40 registered ones. `hack/ci-deploy.sh` reads that secret for the agent's credentials when `MODEL_PROVIDER=gemini`, failing loudly when it is absent; the judge keeps the job-env key. The verifier gains a lease-silent finding for the key, flipped to blocking after the repairs; the Prow nightlies flip one export each.

**Tech Stack:** Bash (`gcloud services api-keys`, `gcloud secrets`), Python 3 (`scripts/verify_ci_pool_project.py`, `unittest`), Terraform (`bench/tf/fleet`), Helm values through `hack/ci-deploy.sh`, Prow job YAML in `GoogleCloudPlatform/oss-test-infra`.

**Spec:** `docs/designs/eval-nightly-gemini-developer-api.md`

## Global Constraints

- Every hardcoded value in Bash and Python is a named constant at the top of its file (`.agents/rules/core_engineering.md`); test files are exempt.
- No `Co-Authored-By` trailers; Conventional Commits titles; PR text in ASD-STE100.
- The key string never reaches stdout, stderr, a log, an artifact, or a `set -x` trace. Scripts that hold it in a variable run with `set +x` for that region if tracing is ever on.
- Never re-run `scripts/provision_ci_pool_project.sh` on a registered project; the repair is the key script alone.
- The agent's key comes from the leased project's secret. There is no fallback to the job env's `GEMINI_API_KEY` for the agent; that key stays for the judge.
- Names shared across tasks, fixed here: key display name `kube-agents-eval-gemini`; secret name `kube-agents-eval-gemini-api-key`; API target `generativelanguage.googleapis.com`; APIs to enable `apikeys.googleapis.com`, `secretmanager.googleapis.com`, `generativelanguage.googleapis.com`; secret accessors `prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com` and `eval-baseline-recorder@kube-agents-prow.iam.gserviceaccount.com`; health bot `eval-dashboard-publisher@kube-agents-prow.iam.gserviceaccount.com`.
- Verifier check id `gemini_api_key`; finding ids `apikeys/gemini/missing`, `apikeys/gemini/restriction`, `secrets/gemini-api-key/missing`, `secrets/gemini-api-key/accessor/<member>`.
- Deploy env override `EVAL_AGENT_GEMINI_KEY_SECRET`, default `kube-agents-eval-gemini-api-key`.
- The 40 registered projects are `kube-agents-evals` and `kube-agents-evals-2` through `-40`, the ids `gitops_repo_for_project()` in `hack/ci-deploy.sh` maps.

## Review Focus

1. A secret whose current version is empty or whitespace: the deploy must stop before Helm, not install an empty key the agent then fails on at its first call. Pinned in Task 4.
2. A key that exists under the display name but carries an extra or different API target: the verifier must report `apikeys/gemini/restriction`, not pass on the name alone. Pinned in Task 3.
3. Rotation: after the operator deletes the key and re-runs the script, the secret gains a new version and the deploy reads the new string; the script must not skip the version because a secret already exists. Pinned in Task 1.
4. `MODEL_PROVIDER=vertex_ai` after this change must make no Secret Manager read and pass the job env's `GEMINI_API_KEY` exactly as before; the presubmits depend on it. Pinned in Task 4.
5. `gcloud secrets versions access` prints the value without a trailing newline, but a secret created by hand with `echo` carries one; the deploy trims before it reaches Helm, and the script writes versions without one. Pinned in Tasks 1 and 4.

---

### Task 0: Tracking issue and branch

**Files:**

- None in the repository.

- [ ] **Step 1: Open the tracking issue, with the user's go-ahead** (an outbound write): title `eval: move the nightly agent to the Gemini Developer API, one key per pool project`, body linking the design document and listing the rollout order from its section 10. Record the number; later PRs say `Part of #<n>` and the last says `Closes #<n>`.

- [ ] **Step 2: Branch** from freshly fetched `upstream/main`, `--no-track`, named `feat/eval-gemini-key-per-pool-project`. The design document is already on `docs/eval-nightly-gemini-api`; cherry-pick its commit onto this branch so the spec travels with the change.

---

### Task 1: The per-project key script

**Files:**

- Create: `scripts/provision_ci_pool_gemini_key.sh`
- Test: `tests/test_provision_ci_pool_gemini_key.py`

**Interfaces:**

- Produces: `scripts/provision_ci_pool_gemini_key.sh <PROJECT_ID>`; exit 0 with the project in the terminal state (APIs enabled, one key with the display name and the one API target, the secret holding the key string as its current version, the three IAM grants), exit non-zero with a line naming what failed. Constants at the top, readable by other files with `sed -n 's/^readonly NAME=.*/.../p'` as `hack/ci-deploy.sh` is read today: `GEMINI_KEY_DISPLAY_NAME`, `GEMINI_KEY_SECRET_NAME`, `GEMINI_KEY_API_TARGET`, `GEMINI_KEY_APIS`, `GEMINI_KEY_SECRET_ACCESSORS` (space-separated), `GEMINI_KEY_SECRET_VIEWERS` (the health bot).

- [ ] **Step 1: Write the failing tests.** Follow `tests/test_provision_ci_pool_project_seed.py`: a fake `gcloud` on `PATH` that logs its argv as JSON lines to `$FAKE_GCLOUD_LOG` and answers from a scripted table; `get_isolated_test_env` from `tests/testing/common`.

```python
class ProvisionGeminiKeyTest(unittest.TestCase):
    def test_a_fresh_project_gets_apis_key_secret_version_and_grants(self):
        # fake: api-keys list -> []; create -> name; get-key-string -> "k1"; secrets describe -> 404; versions access -> 404
        # assert argv log contains, in order: services enable <3 apis>; api-keys create --display-name=kube-agents-eval-gemini --api-target=service=generativelanguage.googleapis.com;
        # secrets create kube-agents-eval-gemini-api-key --replication-policy=automatic; versions add (data from stdin, value "k1", no trailing newline);
        # add-iam-policy-binding x3 (two accessors roles/secretmanager.secretAccessor, bot roles/secretmanager.viewer)
    def test_a_rerun_with_the_same_key_adds_no_version(self):
        # fake: list -> [existing]; get-key-string -> "k1"; versions access latest -> "k1"
        # assert no "versions add" in the log, grants still applied (idempotent bindings)
    def test_a_rotated_key_adds_a_version(self):
        # fake: list -> [existing]; get-key-string -> "k2"; versions access latest -> "k1"
        # assert exactly one "versions add" with data "k2"
    def test_the_key_string_never_reaches_stdout_or_stderr(self):
        # run the script, assert "k1" not in proc.stdout and not in proc.stderr
    def test_a_project_argument_is_required(self):
        # no argv -> exit 64 and a usage line
```

- [ ] **Step 2: Run the tests to verify they fail.** Run: `python3 -m unittest tests.test_provision_ci_pool_gemini_key -v`. Expected: FAIL, the script does not exist.

- [ ] **Step 3: Write `scripts/provision_ci_pool_gemini_key.sh`.** `set -euo pipefail`; constants per Interfaces; `usage()` exits 64 without a project id. Steps in order: `gcloud services enable $GEMINI_KEY_APIS --project`; look up the key with `gcloud services api-keys list --project --filter="displayName=${GEMINI_KEY_DISPLAY_NAME}" --format='value(name)'`, create with `--api-target=service=${GEMINI_KEY_API_TARGET}` when absent; `KEY_STRING="$(gcloud services api-keys get-key-string "$NAME" --format='value(keyString)')"`; `gcloud secrets describe` or create with `--replication-policy=automatic`; compare `gcloud secrets versions access latest` (tolerating "no versions") to `KEY_STRING` and `printf '%s' "$KEY_STRING" | gcloud secrets versions add "$SECRET" --data-file=-` only when they differ; `gcloud secrets add-iam-policy-binding` per accessor and viewer, which is idempotent. Print one line per step naming the project and the resource, never the value.

- [ ] **Step 4: Run the tests to verify they pass.** Run: `python3 -m unittest tests.test_provision_ci_pool_gemini_key -v`. Expected: PASS (5 tests). Then `make shellcheck`: clean on the new file.

- [ ] **Step 5: Commit.**

```bash
git add scripts/provision_ci_pool_gemini_key.sh tests/test_provision_ci_pool_gemini_key.py
git commit -m "feat(ci-pool): mint a Gemini Developer API key per pool project and keep it in Secret Manager"
```

---

### Task 2: Provisioning calls the key script for a new project

**Files:**

- Modify: `scripts/provision_ci_pool_project.sh` (after the step that enables APIs; before the full-install composition apply)
- Test: `tests/test_provision_ci_pool_gemini_key.py` (one more test)

**Interfaces:**

- Consumes: `scripts/provision_ci_pool_gemini_key.sh <PROJECT_ID>` from Task 1.

- [ ] **Step 1: Write the failing test.** `test_the_provisioning_script_calls_the_key_script_once_with_the_project`: read `scripts/provision_ci_pool_project.sh` and assert one line matching `"${SCRIPT_DIR}/provision_ci_pool_gemini_key.sh" "${PROJECT_ID}"`, and that it appears after the `gcloud services enable` block and before the first `tofu` or `terraform` invocation (compare line indices).

- [ ] **Step 2: Run it to verify it fails.** Run: `python3 -m unittest tests.test_provision_ci_pool_gemini_key -k provisioning_script -v`. Expected: FAIL.

- [ ] **Step 3: Add the step.** A heading line in the script's existing style (`echo -e "\n==> [Step 1.6] Minting the project's Gemini Developer API key..."`) followed by the call. One line of comment: why the key is per project (the quota window) and that the script is the repair for a registered project.

- [ ] **Step 4: Run the test to verify it passes; `make shellcheck`.**

- [ ] **Step 5: Commit.**

```bash
git add scripts/provision_ci_pool_project.sh tests/test_provision_ci_pool_gemini_key.py
git commit -m "feat(ci-pool): provision a new pool project with its Gemini Developer API key"
```

---

### Task 3: The verifier finding, lease-silent

**Files:**

- Modify: `scripts/verify_ci_pool_project.py` (check ids near line 131–216, finding ids near line 380–470, `POOL_STATE_READER_ROLES` and `GCLOUD_SURFACE_READ_ROLES` near line 847–880, a new `check_gemini_api_key` beside `check_ledger_read_credential`, the `run(...)` dispatch near line 4748)
- Modify: `bench/tf/fleet/main.tf:180` (`pool_state_reader_roles`)
- Test: `scripts/test_verify_ci_pool_project.py`

**Interfaces:**

- Consumes: the names from Task 1's constants; the test pins the verifier's copies equal to the script's by reading the script text, as `test_roles_match_the_local` reads `main.tf`.
- Produces: `CHECK_GEMINI_KEY = "gemini_api_key"`, display name `Gemini Developer API Key`; `check_gemini_api_key(project_id: str) -> CheckResult`; findings `FINDING_GEMINI_KEY_MISSING`, `FINDING_GEMINI_KEY_RESTRICTION`, `FINDING_GEMINI_KEY_SECRET_MISSING`, `FINDING_GEMINI_KEY_SECRET_ACCESSOR_PREFIX = "secrets/gemini-api-key/accessor/"`.

- [ ] **Step 1: Write the failing tests.** In the verifier test's style (`_ok(...)`, refusal fixtures, `mock.patch.object(checker, "run_cmd", ...)`):

```python
class GeminiApiKeyCheckTest(unittest.TestCase):
    def test_a_project_with_key_secret_and_grants_passes(self): ...
    def test_a_missing_key_is_the_missing_finding_with_the_script_as_repair(self): ...   # repair names scripts/provision_ci_pool_gemini_key.sh <project>
    def test_a_key_with_an_extra_api_target_is_the_restriction_finding(self): ...        # targets [generativelanguage, aiplatform] -> apikeys/gemini/restriction
    def test_a_missing_secret_version_is_the_secret_missing_finding(self): ...
    def test_a_runner_without_accessor_is_an_accessor_finding_per_member(self): ...      # id ends with the member
    def test_a_denied_api_keys_list_is_unread_not_failed(self): ...                      # refusal fixture "(gcloud.services.api-keys.list) PERMISSION_DENIED"
    def test_the_check_is_in_the_default_set_and_not_in_the_pool_state_checks(self): ...
    def test_every_gemini_finding_is_lease_silent(self): ...
    def test_the_names_match_the_key_script(self): ...  # display name, secret name, api target, accessors read from scripts/provision_ci_pool_gemini_key.sh
```

Also extend `DenialClassifierTest.REFUSALS` with one `(gcloud.secrets.versions.list) PERMISSION_DENIED` fixture and one `(gcloud.services.api-keys.list)` fixture, and adjust `test_roles_match_the_local` expectations by adding `roles/serviceusage.apiKeysViewer` and `roles/secretmanager.viewer` on both sides.

- [ ] **Step 2: Run them to verify they fail.** Run: `python3 -m unittest scripts.test_verify_ci_pool_project -k Gemini -v`. Expected: FAIL, `check_gemini_api_key` undefined.

- [ ] **Step 3: Implement.** Constants and ids per Interfaces; `check_gemini_api_key` reads `gcloud services api-keys list --filter=displayName=... --format=json` (name, `restrictions.apiTargets[].service`), `gcloud secrets versions list <secret> --limit=1 --format=json`, and `gcloud secrets get-iam-policy <secret> --format=json`; it never calls `get-key-string` or `versions access`. Add the check to `CHECK_IDS` and `CHECK_DISPLAY_NAMES` (it joins `DEFAULT_CHECKS` by the existing rule), not to `POOL_STATE_CHECKS`; add all four finding ids to `LEASE_SILENT_FINDINGS`; add `"secrets": ("roles/secretmanager.viewer",)` to `GCLOUD_SURFACE_READ_ROLES` and `roles/serviceusage.apiKeysViewer` to the `"services"` tuple; add both roles to `POOL_STATE_READER_ROLES` and to `pool_state_reader_roles` in `bench/tf/fleet/main.tf`, with a one-line comment each. (The design's "secret-level viewer via the key script" becomes project-level through the fleet stack here, because the verifier's surface test requires every read surface's role to be a project-level pool-state reader role; the key script's viewer grant stays as belt and braces.) Register in `run_checks` as `run(CHECK_GEMINI_KEY, lambda: check_gemini_api_key(project_id))`.

- [ ] **Step 4: Run the verifier tests to verify they pass.** Run: `python3 -m unittest scripts.test_verify_ci_pool_project -v 2>&1 | tail -3`. Expected: OK. Then `make terraform-test` for `bench/tf/fleet` if it has tests; `tofu fmt -check bench/tf/fleet`.

- [ ] **Step 5: Commit.**

```bash
git add scripts/verify_ci_pool_project.py scripts/test_verify_ci_pool_project.py bench/tf/fleet/main.tf
git commit -m "feat(ci-pool): verify each pool project's Gemini Developer API key, lease-silent until the repairs land"
```

---

### Task 4: The deploy reads the leased project's key under `MODEL_PROVIDER=gemini`

**Files:**

- Modify: `hack/ci-deploy.sh` (constants near line 214–260; the `GEMINI_API_KEY` precheck at line 661–668; the Helm `--set-string platformAgent.credentials.data.GEMINI_API_KEY` line near 1979; the rationale comment above `MODEL_PROVIDER` at line 870–883)
- Test: `tests/test_ci_deploy_gemini_key.py`

**Interfaces:**

- Consumes: the secret name from the Global Constraints.
- Produces: `readonly EVAL_AGENT_GEMINI_KEY_SECRET="${EVAL_AGENT_GEMINI_KEY_SECRET:-kube-agents-eval-gemini-api-key}"`; a bash function `resolve_agent_gemini_api_key()` that sets `AGENT_GEMINI_API_KEY`: under `gemini` it is `gcloud secrets versions access latest --secret="${EVAL_AGENT_GEMINI_KEY_SECRET}" --project="${PROJECT_ID}"` trimmed of trailing newlines, failing the deploy (exit 1, before any Helm call) with a message naming `${PROJECT_ID}`, the secret, and `scripts/provision_ci_pool_gemini_key.sh` when the read fails or the value is empty; under any other provider it is `${GEMINI_API_KEY}` unchanged. The Helm line passes `${AGENT_GEMINI_API_KEY}`.

- [ ] **Step 1: Write the failing tests.** Text pins in the style of `tests/test_ci_deploy_litellm_vertex.py`, plus a functional run of the function extracted by `sed` between markers, against a fake `gcloud` on `PATH`:

```python
class CiDeployGeminiKeyTest(unittest.TestCase):
    def test_the_secret_name_is_a_named_constant_with_an_env_override(self): ...
    def test_the_helm_credential_line_passes_the_resolved_key(self):
        # '--set-string "platformAgent.credentials.data.GEMINI_API_KEY=${AGENT_GEMINI_API_KEY}"' in text
    def test_vertex_ai_never_reads_secret_manager_and_uses_the_job_env_key(self):
        # run resolve_agent_gemini_api_key with MODEL_PROVIDER=vertex_ai; fake gcloud log empty; AGENT_GEMINI_API_KEY == GEMINI_API_KEY
    def test_gemini_reads_the_leased_projects_secret_and_trims_the_newline(self):
        # fake gcloud prints "k9\n"; assert argv has --project=<PROJECT_ID> and --secret=kube-agents-eval-gemini-api-key; value == "k9"
    def test_gemini_with_an_unreadable_secret_fails_before_helm_and_names_the_repair(self):
        # fake gcloud exits 1; assert exit 1 and "provision_ci_pool_gemini_key.sh" and PROJECT_ID in stderr
    def test_gemini_with_an_empty_secret_fails(self):
        # fake gcloud prints "\n"; exit 1
    def test_the_rationale_comment_names_the_nightlies_as_the_gemini_path(self): ...
```

- [ ] **Step 2: Run them to verify they fail.** Run: `python3 -m unittest tests.test_ci_deploy_gemini_key -v`. Expected: FAIL.

- [ ] **Step 3: Implement.** The constant beside the other `EVAL_LITELLM_*` constants; `resolve_agent_gemini_api_key()` defined between `# --- agent key: begin` / `# --- agent key: end` marker comments so the test can extract it, called right after the `MODEL_PROVIDER` export; the Helm line changed to `${AGENT_GEMINI_API_KEY}`; the precheck at line 661 keeps requiring `GEMINI_API_KEY` (the judge) with its comment updated. Rewrite the rationale comment: `vertex_ai` stays the default for the presubmits' concurrency; the nightlies export `gemini` and read the leased project's own key, which is why a shared-minute 429 storm cannot recur there.

- [ ] **Step 4: Run the tests; `make shellcheck`; `python3 -m unittest tests.test_ci_deploy_litellm_vertex` still passes.**

- [ ] **Step 5: Commit.**

```bash
git add hack/ci-deploy.sh tests/test_ci_deploy_gemini_key.py
git commit -m "feat(ci): the eval deploy reads the leased project's Gemini key when MODEL_PROVIDER=gemini"
```

---

### Task 5: Documentation

**Files:**

- Modify: `docs/ci-pool-projects.md` (section 1 API list, ending at line 19; section 3, a new bullet after the LiteLLM Vertex identity bullet's repair block and before line 116; the health-bot roles sentence at line 188; the sentence in the line-100 bullet that says eval installs route through Vertex)
- Modify: `docs/testing-map.md:122-124` (the three nightly rows' last column) and the `-gitlab` rows if present
- Modify: `docs/eval-gate-roster.md` (a dated line under "The inject lane", the section that owns the nightlies' lanes)
- Modify: `docs/designs/eval-nightly-gemini-developer-api.md` (the health-bot role path, per Task 3's note; a line linking this plan)
- Test: `make docs-check`

- [ ] **Step 1: Edit.** Section 1: the three APIs with one sentence each on what reads them. Section 3: a bullet **The project's Gemini Developer API key**: what it is, why per project (the quota window; #1097 for the shared-key storm), the script for a new project, the same script as the repair for a registered one (not the provisioning script, section 8), the verifier finding, and the operator precondition (`roles/serviceusage.apiKeysAdmin` and `roles/secretmanager.admin`). Line 100's sentence becomes: the presubmits route through Vertex; the nightlies through the Developer API with the project's own key. Line 188's role list gains the two roles with the reason. Testing map: each nightly row ends "agent on the Gemini Developer API with the leased project's key; judge on the shared key". Roster: `2026-11-<day>: the five nightlies moved the agent to the Gemini Developer API (per-project keys); a step in cost or latency from that night has this cause.` Design doc: the role-path paragraph in section 4 states both roles are project-level through the fleet stack, and a final line `Implementation plan: docs/superpowers/plans/2026-11-04-eval-nightly-gemini-developer-api.md`.

- [ ] **Step 2: Verify.** Run: `npx prettier --write` on the changed Markdown, then `make docs-check`. Expected: the last line `PR template: 10 sections, every one answerable.` and no error.

- [ ] **Step 3: Commit.**

```bash
git add docs/ci-pool-projects.md docs/testing-map.md docs/eval-gate-roster.md docs/designs/eval-nightly-gemini-developer-api.md docs/superpowers/plans/2026-11-04-eval-nightly-gemini-developer-api.md
git commit -m "docs(ci-pool): the per-project Gemini Developer API key and the nightlies' serving path"
```

---

### Task 6: Live validation and the pull request

**Files:**

- None new; a PR body file outside the tree.

- [ ] **Step 1: Repair one project and prove the deploy on it.** `kube-agents-evals-40` already has the two APIs enabled from the probe. Run `scripts/provision_ci_pool_gemini_key.sh kube-agents-evals-40`, then `python3 scripts/verify_ci_pool_project.py --project-id kube-agents-evals-40 --checks gemini_api_key`: expected PASS. Then, following section 8's temporary-pin procedure (an unconditional `export PROJECT_ID="kube-agents-evals-40"` in `hack/ci-env.sh` after the `:-` line, reverted before merge), run `MODEL_PROVIDER=gemini hack/ci-deploy.sh` and one eval case through `hack/ci-eval-pr.sh` with the presubmit exports. Record: the deploy's line naming provider `gemini` and the secret it read, the LiteLLM pod env showing `GEMINI_API_KEY` from the credentials Secret and no `VERTEXAI_PROJECT`, the case's result, and the proxy log's first model call answered 200 by `generativelanguage.googleapis.com`. Tear down with `hack/ci-teardown.sh`. Remove the pin.

- [ ] **Step 2: Run the pre-PR passes.** `/pr-preflight` against the branch; fix confirmed findings; fold dispositions into the body.

- [ ] **Step 3: Write the PR body** from the template, in ASD-STE100: `Part of #<tracking>`; Bug Fix section "Not a bug fix."; Live validation from Step 1 verbatim; Self-Review from Step 2.

- [ ] **Step 4: Push to the fork and open the PR** with title `feat(ci): the eval nightlies' agent on the Gemini Developer API, one key per pool project`. Tend the bot review per `AGENTS.md`.

---

### Task 7: Repair the 40 registered projects

**Files:**

- None in the repository.

- [ ] **Step 1: After Task 6's PR merges**, run the key script over the roster, serially, logging only project ids:

```bash
for n in "" $(seq 2 40); do
  p="kube-agents-evals${n:+-$n}"
  scripts/provision_ci_pool_gemini_key.sh "$p" || echo "FAILED $p"
done
```

- [ ] **Step 2: Verify each.** `python3 scripts/verify_ci_pool_project.py --project-id "$p" --checks gemini_api_key` in the same loop. Expected: 40 passes. A leased project is fine to repair; the key is new state the running eval does not read.

- [ ] **Step 3: Reconcile the fleet stack** so the health bot's two new reader roles reach every project (`docs/ci-pool-projects.md` section 6.2, `hack/fleet_reconcile.py`), or note the date it will.

---

### Task 8: Flip the nightlies in `oss-test-infra`

**Files:**

- Modify: `prow/prowjobs/gke-labs/kube-agents/kube-agents-periodics.yaml` in `GoogleCloudPlatform/oss-test-infra` (the live file's `export MODEL_PROVIDER="vertex_ai"` lines at 439, 817, 1146, 1469 for `-nightly`, `-main-b`, `-gitlab`, `-writers`; the `-gitlab-next` block near its `export EVAL_FORGE="gitlab"` at line 1733)

- [ ] **Step 1: Edit.** The four lines become `export MODEL_PROVIDER="gemini"`, each with the comment above it rewritten: the agent reads the leased project's own Developer API key (`docs/ci-pool-projects.md`, section 3), so each nightly has its own 20M-tokens-per-minute window; flip back to `vertex_ai` to roll back. `-gitlab-next` gains the three exports its siblings carry: `MODEL_PROVIDER="gemini"`, `MODEL_DEFAULT_NAME="gemini-4-argon"`, `AGENT_MODEL_OVERRIDE="gemini-4-argon"`.

- [ ] **Step 2: Verify** with that repository's own check (`make verify` or the `hack/` config checker it names) and open its PR, `Part of #<tracking>`, after Task 7 is complete. A nightly that leases an unrepaired project with this export fails at the deploy by design.

---

### Task 9: Flip the verifier finding to blocking, and read the first night

**Files:**

- Modify: `scripts/verify_ci_pool_project.py` (`LEASE_SILENT_FINDINGS`, `POOL_STATE_CHECKS`)
- Modify: `docs/eval-gate-roster.md` (the dated line's day), the tracking issue
- Test: `scripts/test_verify_ci_pool_project.py`

- [ ] **Step 1: After the first green night**, change `test_every_gemini_finding_is_lease_silent` to its inverse and `test_the_check_is_in_the_default_set_and_not_in_the_pool_state_checks` to require it in `POOL_STATE_CHECKS`; run to verify they fail; remove the four ids from `LEASE_SILENT_FINDINGS` and add `CHECK_GEMINI_KEY` to `POOL_STATE_CHECKS`; run to verify they pass; commit `fix(ci-pool): a missing Gemini key now reds the lease, the repairs being done`; PR with `Closes #<tracking>`.

- [ ] **Step 2: Write the three numbers on the tracking issue**, beside the last Vertex night's: 429 count from each nightly's collected LiteLLM log (`grep -c ' 429 '` on the artifact), peak input tokens per minute per leased project from Cloud Monitoring (`serviceruntime.googleapis.com/quota/rate/net_usage` for `generativelanguage.googleapis.com`, filtered to the project, 1-minute alignment, max over the window) or AI Studio's usage page, and the cached-token share from the same page. If any project's peak minute is above 10M (half the wall), open an issue to lower `EVAL_TASK_PARALLELISM` on that job before anything else.
