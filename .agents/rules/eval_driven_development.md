# Eval-driven development

[`AGENTS.md`](../../AGENTS.md) owns the rule: a change to what an agent does starts from a
failing eval case and ends with that case passing and registered. This file holds the mechanics.
Change the rule in `AGENTS.md`; change how it is done here.

## When it applies

Any pull request that changes what an agent does: a prompt, an SOP, a skill, a tool, the runtime
path an agent takes, or a fix for something an agent did wrong. A chart, operator, image or
configuration change that alters agent behaviour counts; "infrastructure" here means the pool
projects, the seeded fleet, Prow and the workflows. Exempt: docs, CI, that infrastructure, and the
bench harness itself (`bench/kube_agents_bench/`, `hack/ci-eval-pr.sh`).

The loop needs a dev project with kube-agents installed ([`INSTALL.md`](../../INSTALL.md)),
refreshed to the commit under test: build the images with `deploy/docker/cloudbuild-ci.yaml`
(`gcloud builds submit` in your project, as `hack/ci-deploy.sh` does), then point the install at
them (`make -C k8s-operator install` and `deploy IMG=...`, then the `PlatformAgent` CR's image
and tag, as [`scripts/dev/dev_rebuild_agent.sh`](../../scripts/dev/dev_rebuild_agent.sh) does;
INSTALL.md "Method 3" is the local-iteration path). `hack/ci-deploy.sh` itself is the presubmit's path and assumes its
secrets. For cases that read the seeded fleet, whether through `fixtures:` or by naming
`seeded-a`/`-b`/`-c` directly, the fleet must be applied to the dev project once
([`bench/tf/fleet/README.md`](../../bench/tf/fleet/README.md)). A stock install sandboxes the
agent, which the harness's `kubectl port-forward` cannot reach;
[`bench/README.md`](../../bench/README.md#sandboxed-installs) has the ways round that. Every pull
request that changes agent behaviour must include the eval case that covers it.

## The loop

**1. Red.** Before writing the fix, name the case that shows the gap: an existing
`bench/tasks/<id>/task.yaml`, or a new one written to the case format
([`bench/CONTRIBUTING.md`](../../bench/CONTRIBUTING.md),
[`docs/designs/bench-case-format.md`](../../docs/designs/bench-case-format.md),
`make bench-case-check`). Run it against your dev install of current `main` with the exports the
presubmit uses (`hack/ci-eval-pr.sh`), without `--no-infra`, which skips the deterministic checks
and can produce neither a red nor a green:

```bash
cd bench && uv sync
export PROJECT_ID=<gcp project> CLUSTER_NAME=<cluster> AGENT_CLUSTER_CONTEXT=<kubectl context>
export BENCH_TF_ROOT=./tf
export GCP_PROJECT_ID="$PROJECT_ID"   # the judge: Vertex AI through your gcloud ADC, as in CI
PLATFORM_AGENT_TOKEN=$(kubectl --context "$AGENT_CLUSTER_CONTEXT" get secret platform-agent-secrets \
  -n kubeagents-system -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode) \
  JUDGE_PROVIDER=google JUDGE_MODEL=gemini-3.1-pro-preview \
  uv run devops-bench ./tasks/<id> --agent-type kubeagents
```

Without `GCP_PROJECT_ID` the judge fails to construct (`No API key was provided`); the eval
still needs the judge even though only the deterministic checks decide.

A case that reads the seeded fleet needs it in your dev project: run
[`hack/fleet-kubeconfigs.sh`](../../hack/fleet-kubeconfigs.sh) and export
`BENCH_FLEET_KUBECONFIG_DIR` first. The runner refuses to write kubeconfigs on your own
credential unless told to: either set `FLEET_ALLOW_RUNNER_CREDENTIAL=1` (a fleet only you
use), or apply the fleet stack with `user:<you>` added to `fleet_reader_token_creators` and
set `FLEET_READONLY_SA=seeded-fleet-reader@<project>.iam.gserviceaccount.com` — the default
grants token-creator to the CI identities only, and `roles/owner` does not include it. Without the fleet the case
fails every time with the fleet phrases absent, which is broken, not red.

Verify that the case fails on `main` for the reason your change addresses. A case that passes
before the change proves nothing about it. A case that fails for an unrelated reason (a missing
fixture, a token, a 429) is broken, not red; fix that first or pick another case.

**2. Implement.**

**3. Green.** Deploy the branch to the same install (if the install is shared, take the lease first
— see [`docs/designs/live-test-lease.md`](../../docs/designs/live-test-lease.md)) and run the same
case three times, the presubmit's repetition count; `devops-bench` runs a task once per invocation.
All three must pass on the deterministic checks; a judged score moving is not a pass. The check that
was red is the one that goes green: loosening the check in between is a new red, not a green.

**4. Register.** A new case is registered in `hack/eval/nightly-cases.txt` in the same pull
request, with `owner:` set and a `docs/designs/domains.yaml` slug (or a reviewed
`KNOWN_NO_DOMAIN` entry). The nightly is where a new case lands
([`docs/designs/bench-case-format.md`](../../docs/designs/bench-case-format.md),
"Registration"): it runs every night from the night it merges and builds its record; a
presubmit seat is a later pull request that cites that record — one edit that moves the
line to `hack/eval/presubmit-cases.txt` and adds the name to `hack/eval/blocking-roster.txt`
(an `eval-crew` approval; since 2026-09-22 the presubmit runs the blocking roster only, plus
the held-out seat a coverage tracker may take first, `presubmit-cases.txt`'s last section,
which `scripts/test_eval_rosters.py` pins) — never the one that makes the
case pass. A case whose fixture does not exist at all is a `FIXTURE_NOT_READY` entry in
`scripts/validate_bench_cases.py` with its issue instead. A case already registered stays
where it is. That seat is the admission, earned on the case's record
([`docs/eval-gate-roster.md`](../../docs/eval-gate-roster.md),
[`bench/baselines/README.md`](../../bench/baselines/README.md)); never add a new case to
the roster in the pull request that makes it pass.

## When the fix is not yours

The loop above is for a change you are making. A gap you found but will not fix — another
owner's SOP, a defect in a domain you do not work in — still lands as a case rather than as an
issue, and the marker is how: write the case, run it red against `main` exactly as step 1 says (a
case that fails for a broken fixture is broken, not red), then register it with
`expected_fail: true` at the top level of its `task.yaml`. `bench-gate` inverts a marked case:
failing is the declared outcome and is reported as `EXPECTED_FAIL`, never `FAILED`; collapse
(rung 4) and the judged comparison (rung 6) skip it; and passing every repetition reds the job
(rung 5) until the marker is flipped. The pull request that closes the gap therefore removes the
marker in the same diff. Registration follows the same rule as any case, and a marked case is never
added to the blocking roster. `make bench-case-check` rejects a marker that is not a bare YAML
boolean: `expected_fail: "false"` is a string, and `bench-gate` would otherwise refuse it only
after the cluster lease.

Do not mark a case for your own change to flip. A marked case waiting for a fix is a placeholder
that reds the job the moment anyone's change happens to fix it, which is the right behaviour for a
gap with an owner and noise for a gap you are about to close.

## What does not count

- A unit test with a mocked model. That is a test; it goes where
  [`AGENTS.md`](../../AGENTS.md) "Where Tests Go" says, and it does not replace the case.
- A case deferred to another pull request in a stack. The change merges with this pull request, so
  the case (or the removal of `expected_fail: true` on an existing case) must land with it. Where
  the behaviour genuinely cannot be observed until a sibling lands, merge the prerequisite sibling
  first and then include the case here; where neither change is observable without the other, they
  are one change and belong in one pull request.

## Finding a case

[`bench/tasks/DRAFTS.md`](../../bench/tasks/DRAFTS.md) lists spec-ready scenarios per domain and
the planted defects the seeded fleet carries. The fleet is read-only: a case observes a defect
already planted; it never plants one from inside a run.
