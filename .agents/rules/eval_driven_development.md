# Eval-driven development

[`AGENTS.md`](../../AGENTS.md) owns the rule: a change to what an agent does starts from a
failing eval case and ends with that case passing and registered. This file holds the mechanics.
Change the rule in `AGENTS.md`; change how it is done here.

## When it applies

Any pull request that changes what an agent does: a prompt, an SOP, a skill, a tool, the runtime
path an agent takes, or a fix for something an agent did wrong. A chart, operator, image or
configuration change that alters agent behaviour counts; "infrastructure" here means the pool
projects, the seeded fleet, Prow and the workflows. Exempt: docs, CI, that infrastructure, and the
bench harness itself (`bench/kube_agents_bench/`, `hack/ci-eval-pr.sh`). An exempt change says so
in one line under **Live validation** in the pull request body.

The loop needs a dev project with kube-agents installed ([`INSTALL.md`](../../INSTALL.md)) and,
for cases with `fixtures:`, the seeded fleet applied to it once
([`bench/tf/fleet/README.md`](../../bench/tf/fleet/README.md)). Every contributor, human or
agent, is expected to have one. There is no path around the loop: a pull request that changes
agent behaviour without eval evidence is not ready for review.

## The loop

**1. Red.** Before writing the fix, name the case that shows the gap: an existing
`bench/tasks/<id>/task.yaml`, or a new one written to the case format
([`bench/CONTRIBUTING.md`](../../bench/CONTRIBUTING.md),
[`docs/designs/bench-case-format.md`](../../docs/designs/bench-case-format.md),
`make bench-case-check`). Run it against an install of current `main` the way the presubmit does
(`hack/ci-eval-pr.sh`), without `--no-infra`, which skips the deterministic checks and can produce
neither a red nor a green:

```bash
cd bench && uv sync
export PROJECT_ID=<gcp project> CLUSTER_NAME=<cluster> AGENT_CLUSTER_CONTEXT=<kubectl context>
export BENCH_TF_ROOT=./tf
PLATFORM_AGENT_TOKEN=$(kubectl get secret platform-agent-secrets -n <namespace> \
  -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode) \
  JUDGE_PROVIDER=<provider> JUDGE_MODEL=<model> \
  uv run devops-bench ./tasks/<id> --agent-type kubeagents
```

A case with `fixtures:` reads the seeded fleet: with the fleet applied in your dev project, run
[`hack/fleet-kubeconfigs.sh`](../../hack/fleet-kubeconfigs.sh) and export
`BENCH_FLEET_KUBECONFIG_DIR` first, or its fleet checks report `error`, which is broken, not red.

It must fail, and fail for the reason your change addresses. Keep the failing entry from
`verification_report[]` in the run's `results.json` (its `status` and `reason`) and the line of the
agent's report that shows the gap; the pull request quotes both. A case that passes before the
change proves nothing about it. A case that fails for an unrelated reason (a missing fixture, a
token, a 429) is not red, it is broken; fix that first or pick another case.

**2. Implement.**

**3. Green.** Deploy the branch to the same install (take the lease first if the install is
shared: [`pre_pr_review.md`](pre_pr_review.md), "Live validation") and run the same case three
times, the presubmit's repetition count; `devops-bench` runs a task once per invocation. All three
must pass on the deterministic checks; a judged score moving is not a pass. The check that was red
is the one that goes green: loosening the check in between is a new red, not a green. Keep the three
run directories; the pull request cites them.

**4. Register.** A new case is registered in `hack/ci-eval-pr.sh` in the same pull request, with
`owner:` set and a `docs/designs/domains.yaml` slug (or a reviewed `KNOWN_NO_DOMAIN` entry).
Which array is the Registration paragraph's call in
[`docs/designs/bench-case-format.md`](../../docs/designs/bench-case-format.md): `NIGHTLY_TASKS`
for a case you have seen red and green that is too slow for a presubmit seat or whose domain a
probe already covers there; `TASKS` for a fast case in an uncovered domain; commented out in
`TASKS` only for a case whose fixture is not ready yet. A case already registered stays where it is. Admission
to the blocking roster is earned on the case's record afterwards
([`docs/eval-gate-roster.md`](../../docs/eval-gate-roster.md),
[`bench/baselines/README.md`](../../bench/baselines/README.md)); never add a new case to
`BOOTSTRAP_ADMITTED` in the pull request that makes it pass.

## What the pull request records

Under **Testing → Live validation** in the template, which for a change to agent behaviour
is this loop and nothing less:

- the case id, and whether it is new or existing;
- red: the install and the `main` commit it ran against, the failing check and its reason, one
  line of the agent's report;
- green: the three runs (directories or a one-line summary each) against the branch's build;
- where the case is registered, or the one-line exemption.

## What does not count

- A unit test with a mocked model. That is a test; it goes where
  [`AGENTS.md`](../../AGENTS.md) "Where Tests Go" says, and it does not replace the case.
- A case run once, or a green you did not see. Three passing runs, observed.
- A red you did not see. If you cannot run the case before the change, you do not know it tests
  the change.

## Finding a case

[`bench/tasks/DRAFTS.md`](../../bench/tasks/DRAFTS.md) lists spec-ready scenarios per domain and
the planted defects the seeded fleet carries. The fleet is read-only: a case observes a defect
already planted; it never plants one from inside a run.
