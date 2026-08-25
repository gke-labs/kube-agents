# The bench case format

A case is one directory under `bench/tasks/` holding one `task.yaml`. This document is
the contract for that file: which fields exist, which of them the runner honours, which
the repository's own lints honour, and how to decide whether a claim about a run belongs
in an exact check or in the judge's prose score.

The format is executable today and most of it is already written down in
`bench/tasks/compliance-rbac-overgrant/task.yaml`, which annotates nearly every decision
this document generalises. Read that file alongside this one. `bench/CUSTOM-TASKS.md` is
the step-by-step for writing a case, including the Terraform stack and the agent harness;
where the two touch, this document is the rule and that one is the procedure.

## Who parses what

Two different readers consume a `task.yaml`, and they honour different fields.

**devops-bench** parses it into `devops_bench.tasks.schema.Task`, pinned to the SHA in
`bench/pyproject.toml`. Its model config is `ConfigDict(strict=True, extra="ignore")`
(`devops_bench/tasks/schema.py:25`), so a field the model does not declare is discarded
in silence — no error, no warning. The fields it reads are `id`, `name`, `prompt`,
`expected_output`, `retrieval_context`, `chaos_spec`, `verification_spec`,
`recoverable_safety`, `infrastructure`, `documentation` and `validated`. Strict mode
means no coercion: `critical: yes` is a string, not a boolean, and fails validation.

**This repository's lints** read the same file for fields devops-bench discards: `domain`
and `fixtures`. Those are ours. A typo in either cannot fail a run, which is exactly why
`scripts/validate_bench_cases.py` exists.

## The id key

Use `id:`. It is the field name on the schema, and it is the one every case in the tree
uses — twelve of the thirteen already did, and the thirteenth was renamed rather than
grandfathered, on the principle in "Changing the format" below.

`Task.from_dict` accepts `task_id:` as an alias — it reads `raw.get("id")` first and
falls back to `raw.get("task_id")` when that is absent
(`devops_bench/tasks/schema.py:198-200`) — so both spellings load, and a file carrying
both silently loses the `task_id` value. The alias is not a second supported spelling in
this repository; it is upstream compatibility for other people's corpora. The validator
rejects `task_id:` in a case here.

The value must equal the directory name. The directory name is what
`hack/ci-eval-pr.sh` derives `TASK_NAME` from, what the results file is copied to, and
what every lint and every table in `bench/tasks/DRAFTS.md` keys on. An id that disagrees
with its directory means two names for one case and a search that finds half the
references.

## The fields, and what each one is for

`id` and `name` identify the case. `name` is prose for a human reading a results table;
`id` is the key.

`prompt` is what the agent is given. Write it as the user would write it, not as a
checklist — a prompt that enumerates the answer measures instruction-following rather
than the journey. `{{CLUSTER_NAME}}` and `{{PROJECT_ID}}` are substituted by the runner.

`expected_output` is the judge's reference. It feeds `OutcomeValidity` and, where the
case declares `documentation.constraints`, `ChecklistScore`. It never gates a case that
carries a `verification_spec` (see the gate, below), so write it for the reader of a
trend line rather than for a merge decision.

`infrastructure` selects the deployer. `deployer: noop` provisions nothing and costs the
presubmit seconds rather than a cluster; `deployer: tofu` names a stack under `bench/tf/`
and costs a multi-minute provision on every pull request. `hack/ci-eval-pr.sh` reads
`infrastructure.deployer` out of the file text to classify a crashed run, so keep it on
one line.

`documentation` carries reference documents and the per-constraint `critical` flags the
checklist metric scores. It is judged, not exact.

`validated` marks a case as vetted. It defaults to `False` and stays there until someone
has watched the case both pass and fail.

`domain` is a slug from `docs/designs/domains.yaml`. It is how coverage is counted, and
it is discussed below in its own right.

`fixtures` is a list of fixture role slugs from `bench/tf/fleet/fixtures.json` — the
seeded defects the case depends on. (`docs/designs/fleet-fixtures.yaml` overlays the
day-N gates and the project-scoped fixtures on that catalogue, and contributes the roles
that have no cluster slot; it may not rename one.) It is documentation the validator
enforces: it makes a case's fixture dependencies greppable, so the fleet owner replacing
a cluster knows which cases go quiet. The same slugs name a `fleet_resource_property`
check's `fixture_role:`, and a check naming a role the case does not list is rejected —
one planted defect, one name, however the case refers to it. Cases address fixtures by
role and never by cluster name or project id; `docs/designs/bench-fleet-catalog.md` is
the contract for why.

A case whose spec reads live cluster state must declare it. `fixtures: []` is the
declaration for a case that plants its own state — `gpu-stress-test-diagnosis` brings up
its own Terraform stack and depends on no fixture — and an absent key on such a case is a
finding, because a grep that returns one case for a role has to mean one case uses it.

`verification_spec` is the exact half of the grade, and the rest of this document is
mostly about it.

## Exact check or judged score

The test is who chose the words.

We planted the noun, so a match is fair and it blocks per run. `debug-binding` is a name
the fleet's Terraform wrote; a correct audit cannot avoid saying it, and an incorrect one
cannot stumble into it. Assert it exactly, and let one red run block one merge.

The agent composed the sentence, so it gets judged and blocks only as a distribution.
Whether the remediation advice is any good, whether the post-mortem reads coherently,
whether the explanation of the privilege-escalation surface is right — nobody planted
those words, there is no exact string that is fair to demand, and a model grading a model
is a noisy instrument. Record the score, watch the trend, and do not red a build on one
sample of it.

Two corollaries fall out. A phrase the agent might legitimately spell two ways is still
exact — that is what `any_of_phrases` is for (`HPA` or `HorizontalPodAutoscaler`), not a
reason to hand the claim to the judge. And a phrase that a correct run can reach without
doing the work is not exact enough to block: the compliance case moved `cluster-admin`
off the ledger body and onto `scope: finding_ids` precisely because the body's evidence
table restates every check slug the run declared, so the phrase was in every ledger
including an empty one.

## The shape of a verification entry

Each entry is a named, scored unit
(`devops_bench/verification/spec.py`, `VerificationEntry`), and the vocabulary is small:

`name` labels the entry on the result record. Names must be unique within a spec; a
duplicate is dropped with a parse error.

`role` is `objective` or `safeguard`. An objective is a thing the run was supposed to
achieve. A safeguard is a thing that must never have happened. Objectives roll up into
`VerificationCorrectness`; safeguards roll up by severity.

`severity` is required on a safeguard and forbidden on an objective. `catastrophic`
means one failure fails the case outright — the gate reads it as a boolean.
`recoverable` means contained and reversible, and rolls into a weighted fraction.

`mode` is `converge` or `assert`, defaulting per role: objectives converge because they
describe a state the run is working toward, safeguards assert because polling a violation
would only wait for it to heal. Override it to `assert` for any check whose subject is
immutable once the run ends. Every transcript-reading check is in that category — the
report does not change after the last turn, so a converging one just burns the timeout
before failing. `hold` parses but is rejected: it is not built.

`weight` scales an entry's contribution within its role. Default 1.0, must be positive.

`check` is the check subtree. It carries a `type` discriminator and the type's own
fields, and it may be a compound node: `sequence` (ordered, fail-fast), `parallel` /
`all`, `any`, and `none`. `none` is how a safeguard says "this never happened".

## The check types

Three read the cluster, from devops-bench: `resource_property` (a JSONPath property of
matched objects, with `op` one of eq/ne/gt/gte/lt/lte/exists/absent/contains/matches),
`pod_healthy` (pods matching a selector reach Ready), and `scaling_complete` (a
deployment's ready replicas land in a range).

Three read what the run produced, from this repository
(`bench/kube_agents_bench/verifiers.py`, registered through the
`devops_bench.verifiers` entry-point group in `bench/pyproject.toml`):
`report_contains` (phrases in the agent's answer), `tool_called` (calls in the
trajectory), and `ledger_issue_contains` (the GitHub ledger issue a fleet audit
published).

Two limits are worth knowing before choosing one. `tool_called` sees the delegating
turn's calls only — a delegated worker's calls never reach the trajectory — so it can
assert what the router did and nothing about what a worker did to a cluster. A mutation
safeguard built on it is blind to the calls it fears; use `resource_property` for those.
And `report_contains` defaults to `scope: final`, the answer the user receives. `full`
also matches a phrase the agent merely quoted in progress chatter, which passes a
required phrase that was never reported and false-fails a forbidden one that only appears
in quoted material.

All six fail closed. A check that cannot observe its subject returns `status: "error"`,
never a pass and never a fail, and an errored check drops `VerificationCoverage` below
1.0, which the gate fails. Silence is not a pass.

## What actually reds a build

`hack/ci-eval-pr.sh` runs one devops-bench invocation per entry in its `TASKS` array and
grades the resulting record. For a case that carries a `verification_spec`, three keys
decide the merge:

| Key                        | Gate                           | Meaning                                    |
| -------------------------- | ------------------------------ | ------------------------------------------ |
| `VerificationCatastrophic` | `1.0` if present               | No catastrophic safeguard tripped          |
| `VerificationCoverage`     | must be present and `1.0`      | No declared check errored or failed to run |
| `VerificationCorrectness`  | must meet the floor if present | Weighted objective pass fraction           |

Only coverage fails on absence. A spec with no catastrophic safeguard emits no
`VerificationCatastrophic` key and that is fine; a spec whose entries all errored emits no
correctness key either, and coverage is what catches it.

The floor is `DETERMINISTIC_CORRECTNESS_FLOOR` in the same script, `"1.0"` today: every
declared objective is meant to hold outright. `OutcomeValidity` and `ChecklistScore` are
recorded on every run and gate nothing on a spec-carrying case — a judged score that
drops is a trend to read, not a merge to block. `VerificationRecoverable` is emitted but
is not in the gate.

Three shapes of absence are failures rather than green:

A case that declares a spec and produces none of the three keys fails. The script
re-reads the file to tell "declared a spec, metric crashed" from "declared none", and
errs toward "spec present" when the read fails, because the fail-closed direction is the
one where a broken metric cannot slide back to the judge.

An errored check fails, through coverage. `rollup` (`devops_bench/verification/rollup.py`)
excludes an errored entry from both numerator and denominator of every signal, so an
all-errored spec would otherwise emit no correctness key at all; coverage is emitted
unconditionally and is what catches it.

A spec entry that does not parse fails. Each parse error adds weight 1.0 to the objective
denominator with no numerator contribution — a spec that never parsed might have declared
anything, and the conservative reading is an unmet objective.

## Every case declares a domain

`domain:` is a slug from `docs/designs/domains.yaml`, and it is how coverage is counted.
`scripts/test_domain_coverage.py` treats a domain as covered when some case claims its
slug, carries a non-empty spec, and is an active — uncommented — entry in `TASKS`.
Everything else is on the allowlist, and the shrinking allowlist is the programme's
progress metric.

A case with no slug is therefore invisible to that count. It can be green for months
while the coverage report shows the domain it actually exercises as a gap, and nobody
finds out until someone writes a second case for a domain that already had one. So a case
declares a domain.

The exception is a case that genuinely covers no row in `domains.yaml` — the way
`gpu-stress-test-diagnosis` is a chat-prompted post-incident RCA and not the event-fired
autoops triage `incident-triage` names. That is a real answer, and the answer is recorded
as a reviewed entry in the validator's `KNOWN_NO_DOMAIN` map with its reason, not as an
absent field. The distinction the map owns is the one between "covers nothing, and we
checked" and "nobody remembered".

## Every case declares a verification spec

`hack/ci-eval-pr.sh` falls back to `OutcomeValidity >= 0.7` for a case with no
`verification_spec`. That fallback is transitional, and the script says so in the comment
above the branch that implements it: once every entry in `TASKS` carries a spec, it is
dead code to delete.

Declare the spec as a block, not inline. The script decides whether a case has one by
grepping for a `verification_spec:` line with nothing after it, so a flow-style spec on a
single line loads, runs its checks, and is graded by the judge-only fallback regardless.
The validator rejects that shape.

Transitional means new cases do not use it. A judge-only case cannot fail for the reason
it was written — it fails when the model has a bad day and passes when a plausible-sounding
answer names nothing in particular — and 0.7 on a five-point rubric is roughly "did not
embarrass itself". Building the first corpus produced about fifty review findings, half of
them cases that could not fail or could not pass; judge-only is the shape most of those
took.

The minimum a case owes is one objective that names something the case itself planted,
and one safeguard for the thing the case must not do. Both can be written before the
fixture exists — `bench/tasks/DRAFTS.md` is ten cases that did exactly that, specs first,
registered commented-out, activated later. Writing the spec is what surfaces "this case
cannot fail" while it is still cheap to fix.

Where the exact check genuinely does not exist yet, the escape is the same shape as the
domain one: a reviewed entry in the validator's `KNOWN_JUDGE_ONLY` map, carrying the
reason and what would close it. An entry there is a debt with a name on it.

## Registration

`hack/ci-eval-pr.sh` runs the tasks in its `TASKS` array and only those. Cases under
`bench/tasks/` are not discovered. A case registered nowhere never runs, and the suite
reports green around it — which is how `agent-kanban-smoke`, a case whose whole purpose
is to smoke the deployed pipeline, sat unregistered while the presubmit ran one case for
months.

A commented-out entry counts as registered. That is the intended state for a case whose
fixture or blocker is not ready: it is written down, it is greppable, and activation is
uncommenting one line. The alternative — leaving it out entirely — is indistinguishable
from forgetting.

## The validator

`scripts/validate_bench_cases.py` checks all of the above without a cluster, and
`make bench-case-check` runs it in about a second. It rejects a `task_id:` spelling or an
id that disagrees with its directory, a `domain:` that is missing or not in
`domains.yaml`, a `fixtures:` role the fleet catalog does not define, a cluster-reading
case that declares no `fixtures:` at all, a missing, empty or inline `verification_spec`,
a check that carries no assertion and so can only pass, and a case that is registered
nowhere. It also applies the entry vocabulary above — role, the severity pairing, the
rejected `hold` mode, a positive weight — which devops-bench enforces too, at spec-load
time, after the lease.

`scripts/test_task_registration.py` calls the same module in CI and asserts that it
returned no findings at all, so the fast local check and the gating lint cannot disagree.
That whole-set assertion is the load-bearing half, not the shared module. The lint spent
one commit asserting only on a hand-listed set of substrings, and every rule whose
wording was not on the list — a missing `id:`, a duplicate verification entry name, an
unknown `severity:` value, a check node with no `type:`, a file that does not parse to a
mapping at all, a dozen in total — was rejected by `make bench-case-check` and merged
green. A list of substrings is a second copy of the rule set; keep the assertion over the
set.

`make bench-case-check` itself is invoked by no workflow, and that is the intended shape.
The lint reaches the same `validate_all()` through `PYTHON_TEST_DIRS` (`Makefile:129`) and
`.github/workflows/python-tests.yml`, so a separate job running the target would re-derive
findings CI already has, on a second checkout, for nothing. The target is the pre-push
copy of the gate rather than the gate; a rule that has to be enforced goes in the
validator, where both reach it. Nothing should be added to the target alone.

"Carries no assertion" is stricter than "the field is present". `required_phrases: [""]`
is a populated list, and the empty string is in every text there has ever been, so the
check reads as an assertion and can only pass; the validator treats a blank phrase as no
phrase.

The three allowlists (`KNOWN_UNREGISTERED`, `KNOWN_NO_DOMAIN`, `KNOWN_JUDGE_ONLY`) all
work the same way: an entry names a case and carries the reason, and an entry whose case
no longer exists fails the lint.

### What it does not catch

A green `make bench-case-check` is not a promise that devops-bench will load the case. The
validator checks the rules above; it does not reimplement the harness's schema, and three
classes of mistake get through it and fail at spec-load, after a cluster lease is spent:

- **An extra or misspelled key on a verification entry or a check node.** The compound
  nodes in `devops_bench/verification/spec.py` are `ConfigDict(extra="forbid")`, so
  `requried_phrases:` is a hard rejection there and merely an unrecognised key here.
- **A missing required field on a check.** That surfaces as pydantic's `Field required`
  against the verifier's own model, which this validator does not have.
- **`critical: yes`.** This one the validator structurally cannot catch, and the reason is
  worth knowing. It reads cases with PyYAML, which is YAML 1.1 and parses the bare `yes`
  into the boolean `True`. devops-bench reads them with `ruamel.yaml`'s `YAML(typ="safe")`
  (`devops_bench/tasks/loader.py:41`), which is YAML 1.2 and parses it into the string
  `"yes"` — the exact value the strict-mode note above warns fails validation. By the time
  the validator sees the field, the trap has already been parsed away.

The gap is deliberate rather than pending. `make bench-case-check` depends on PyYAML alone,
so it runs in a checkout with no `bench/` virtualenv and no harness install, which is what
makes it cheap enough to run on every edit. Importing devops-bench to close these three
would trade that for a second copy of a schema that already exists. Note the divergence
when writing a case; do not add the dependency.

## Changing the format

Whoever changes the format does the migration. Five people each fixing their own files
for someone else's schema change is how a one-hour change costs a week, and it is how
half a corpus ends up on the old spelling — which is the state the `task_id:` split above
records.
