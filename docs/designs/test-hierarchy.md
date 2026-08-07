# A Test Hierarchy for an Agentic Harness

**Status:** Proposal, not yet adopted. Written from a week of profiling the first-run onboarding
flow, so every example below is a defect this repository actually hit, not a hypothetical.

## The problem in one paragraph

Most of this system is not code in the sense a test suite understands. The SOPs, the kanban card
bodies, the onboarding templates and the personas are all **prompts**, and a prompt is a program
whose behaviour changes when you edit a sentence. Today they are edited with no test at all. I made
roughly eight prompt changes in a day and the only way I learned whether any of them worked was to
run the whole system and read the output - twice discovering that a change had made things
measurably worse. Meanwhile the deterministic half has decent unit coverage, so the suite is green
while the part that decides what a customer sees is untested.

Smoke tests are worth building and they are not this. They answer "did it come up", which is
tier 3 of 5.

## The evidence

Six real defects from this week, and the cheapest tier that would have caught each. Note how many
are **silent successes**: exit code 0, report written, pod healthy, board says done.

| Defect                                                                                                                                                                                  | Symptom to a smoke test                     | Caught by                                |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- | ---------------------------------------- |
| Sweep silently skips its own checklist items (8 / 8 / 13 finding classes over three identical runs; `seccomp`, `shielded`, `dataplane`, `hpa`, `podmonitoring` missing in two of three) | passes - a report is produced               | **T5** variance                          |
| Report tells the operator to set `runAsNonRoot` on `konnectivity-agent` in `kube-system`                                                                                                | passes - well-formed report                 | **T4** known-answer                      |
| Roster read fails, worker improvises for 31 more calls, ~200s wasted                                                                                                                    | passes - correct report still written       | **T4** budget assertion                  |
| `cluster_agent_reconcile` can never create a profile; **exits 0** (#566)                                                                                                                | passes - job reports `ok`                   | **T4** known-answer                      |
| Slack status loop never stops after a turn; 30 API calls/min forever (#576)                                                                                                             | passes - turn completed, response delivered | **T3** smoke, _if_ it asserts quiescence |
| `ownership` absent from digest payloads while unit tests and the schema ledger both passed                                                                                              | passes                                      | **T4** on the real wire path             |

One of six is reachable by smoke tests as usually written, and only if the smoke test asserts that
the system goes **quiet** after a turn rather than that it responded.

The pattern is the point. These do not fail loudly. They succeed while doing the wrong thing, so
every assertion of the form "did it work" is green. What catches them is asserting on **budgets and
coverage**, not on success.

## The hierarchy

Five tiers. Each is defined by what it can catch and what it is structurally blind to.

### T0 - Unit. Per-commit, seconds, no cluster.

Deterministic code only: marker logic, claim atomicity, parsers, Go controllers. Already exists and
is decent.

**Blind to:** anything involving a model, and anything involving a real cluster.

### T1 - Contract and wire. Per-commit, seconds, no cluster.

Golden files, schema freezes, field-set ledgers, fingerprint vectors. `k8s-lookout` does this well
(`TestSchemaV1_FieldSetsFrozen`, `TestFingerprint_PinnedVectors`) and it is the reason an additive
schema change there is safe to review.

**Blind to:** whether the field is populated on the path a consumer actually reads. A field was
declared, ledgered, documented and absent from the real payload this week, with T1 green throughout.

### T2 - Prompt contract. Per-PR, seconds, no cluster. **The cheapest tier, and almost absent.**

Assertions over generated prompt text. Not "what does the model do with it" - only "does the
instruction still say what we decided it must say".

This tier is not hypothetical here: it already exists for exactly one file.
`agents/chat/scripts/test_bootstrap_onboarding_scripts.py` pins the sweep card body against
`bootstrap_scan_gate._task_body()`:

```python
def test_body_drives_per_cluster_fan_out_and_covers_management(self):
    body = bootstrap_scan_gate._task_body()
    self.assertIn(bootstrap_scan_gate.RECONCILE_SCRIPT, body)  # roster first
    self.assertIn("management cluster", body)                  # platform covers it itself
    self.assertIn("kanban_create", body)                       # one child per cluster
    self.assertIn("parents=", body)                            # fan-in collects the results

def test_body_propagates_idempotency_keys_to_the_fan_out(self):
    body = bootstrap_scan_gate._task_body()
    self.assertIn(bootstrap_scan_gate.AGGREGATE_IDEMPOTENCY_KEY, body)
    self.assertIn(bootstrap_scan_gate.CLUSTER_IDEMPOTENCY_KEY_PREFIX, body)
```

Each pins a decision whose loss would be silent. The second is the instructive one: those keys are
what stop a retry launching a second fleet-wide sweep, and a contributor tidying the card body has no
way to know that from reading it. The test is the only thing that says so.

The proposal is to extend that pattern to the prompts that have none. Every SOP under
`agents/platform/governance/`, every onboarding template under `agents/chat/defaults/onboarding/`,
and every card body carries guards of the same kind, and none of them are pinned. A guard that took
a day of debugging to discover can be removed by a well-meaning edit with a green suite.

Costs nothing to run - no cluster, no model, no minutes.

**Blind to:** whether the instruction works. It only proves the instruction is present.

### T3 - Smoke. Per-PR, cluster, minutes.

Does it come up and function. Pod ready, Slack connects, a card can be filed and worked, a trivial
task completes end to end. This is what is being built now and it is necessary.

Two assertions worth adding that are not obvious:

- **Quiescence.** After a completed turn, the system must go quiet. #576 burned 30 API calls a
  minute indefinitely with the turn marked complete. Assert that background traffic drops to
  baseline within N seconds of a turn ending.
- **Non-zero exit is not the only failure.** Assert on the _content_ of a job's summary line where
  one exists. `created=0 pruned=0 kept=0` alongside a skip warning is a different outcome from
  `created=0` on a healthy cluster, and both exit 0.

**Blind to:** anything about output quality, coverage or cost.

### T4 - Known-answer behavioural. Nightly, cluster, tens of minutes.

A fixture with planted defects at known severities, and assertions scored against ground truth. One
exists now: `Posture Test Fixture 2026-08-06.md`, ten workloads across two namespaces, each carrying
a `fixture/expect` annotation naming the finding classes it should produce.

What it asserts, and why each is chosen:

| Assertion                                                          | Why                                                                     |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| The correct-on-every-dimension control produces **zero** findings  | false positives are what teach people to ignore a tool                  |
| Every planted tier-1 and tier-2 defect appears in the raw findings | catches a checklist silently not being run                              |
| No delivered item names a provider-managed object as actionable    | catches impossible advice                                               |
| Item count is within the stated ceiling                            | catches a rule being read generously                                    |
| Call count and wall clock are within a **budget**                  | catches the 31-call improvisation loop, which produced a correct report |

That last row is the one teams skip. A correctness-only suite passed happily while half the sweep's
calls were wasted.

**Blind to:** run-to-run variation, since it runs once.

### T5 - Variance and soak. Weekly, and gating any prompt or model change. Hours.

Run the identical input N times and measure the spread. This is the only tier that catches
non-determinism, and non-determinism is the defining risk of the whole system. It found:

- ranking selecting **3, 6, 6, 3, 5, 5** items from identical input, with only two findings common
  to all six runs, one of which surfaced a security finding the others never mentioned;
- the sweep returning 8, 8 and 13 finding classes from an unchanged cluster.

Neither is visible in a single run, and both are worse than most bugs a smoke test would catch.

**A model version change should gate on this tier.** Nothing else will notice that an upgrade
altered what your product tells customers.

## What to assert over model output, and what never to

The objection to testing an LLM stage is that output is not reproducible. True, and not
disqualifying: assert deterministic properties **over** non-deterministic output.

| Assert                                                 | Strictness           |
| ------------------------------------------------------ | -------------------- |
| the control produces zero findings                     | hard, fail the build |
| every planted defect appears somewhere in raw findings | hard                 |
| no provider-managed object is presented as actionable  | hard                 |
| item count within ceiling; report within size bounds   | hard                 |
| call count and wall clock within budget                | hard, generous bound |
| **ordering, wording, exact item count, phrasing**      | **never assert**     |

Asserting phrasing produces a permanently red suite that everyone learns to ignore, which is worse
than no suite. Coverage and false-positives are binary and belong in CI. Style is not.

## Three operational traps, learned the expensive way

**Grace windows, and there are two of them.** Workload findings require the _object_ to have been
misconfigured for 10 minutes; namespace-grain findings require the _watch itself_ to have run for 10
minutes. They are separate clocks and a harness has to satisfy both. A 130-second scan against a
three-minute-old fixture satisfies neither and returns a clean, convincing, entirely wrong empty
result - which is how I first "discovered" that four planted defects were missed. Either keep a
long-lived fixture namespace, override the grace in test config, or wait; waiting is what makes this
nightly rather than per-PR.

**Score from the right surface.** lookout fingerprints by _class_, so a digest carries one entry
per finding class naming a single representative object. Scoring per-object coverage against a
digest reports false misses; I did exactly that tonight and nearly filed a bug on it. Coverage
assertions read the occurrence store; shape assertions read the digest.

**Correct silence and broken silence look identical.** This is the subtle one. `podmonitoring_missing`
fired for zero namespaces, which is indistinguishable from the check not running - until you find the
two `ClusterPodMonitoring` resources that legitimately cover the cluster. A suite that only asserts
positives cannot tell "found nothing because nothing is wrong" from "found nothing because it is
broken", and the second is the failure mode that matters.

The fix is to plant **negative controls** deliberately, in matched pairs. The fixture has two: a
workload correct on every dimension that must stay silent, and `shop` / `shop-staging`, identical
except one has a LimitRange, so `limitrange_missing` must fire for exactly one of them. A pair
proves the check discriminates; a single negative only proves it did not fire. Every check worth
asserting on wants a pair.

## Where I would start

1. **T2, this week.** No cluster, no model, no runtime. Pin every guard currently living in an SOP
   or card body. Highest value per hour available by a distance.
2. **Two assertions into the existing T3 work.** Quiescence after a turn, and summary-line content
   rather than exit code alone. Small additions to something already being built.
3. **T4 nightly, reusing the fixture that exists.** The manifests and ground truth are written; what
   is missing is a scorer that exits non-zero and a place to run it.
4. **T5 before shipping a prompt change or accepting a model upgrade.** Does not need automating
   first - three runs and a diff, done by hand, would have caught everything it caught this week.

The order matters more than the completeness. T2 costs almost nothing and protects work already
done; T5 is the one nobody builds and the one that governs whether customers see the same product
twice.
