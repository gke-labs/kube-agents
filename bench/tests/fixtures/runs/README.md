# Captured run fixtures

Five real devops-bench run directories, kept as captured but for one redaction
(below). #899 asks for exactly this — "capture a handful of real `results.json`
records now and treat them as fixtures" — because the record's shape is not
documented anywhere and guessing at it is how a scorer ends up keying on a
field that does not exist. It did: the first draft of the ladder bound rung 3's
liveness check to `metadata.session_id`, and there is no `metadata` key on a
devops-bench record at all.

These are test **inputs**, not test results. Nothing here is an artefact the
suite produces; they are recorded upstream output that the suite consumes, in
the same sense as a checked-in sample payload. Regenerating them is not a
`make` target — it needs a live cluster, a cloudtop runner and a paid judge, so
the offline `bench-tests` job could never do it. That is why they are in git.

Every failure-mode fixture the tests need is **derived in the test** by
mutating a copy of one of these, so there is exactly one place where a real
record's shape is asserted.

They are also **inputs to the tests of the scorer, not inputs to the gate**.
Nothing in `kube_agents_bench/` reads this directory; `hack/ci-eval-pr.sh` hands
`bench-gate` the run directories devops-bench just wrote. A fixture's
`VerificationCorrectness: 0.5` is not a claim about how the agent scores today —
it is a vehicle for asserting that 0.5 is below the floor. So these do not go
stale when the agent improves, and they are never updated because a pull request
changed the results.

**What they do not do.** They are frozen, so they cannot detect that
devops-bench changed its output. A key renamed upstream leaves every test here
green — the capture still carries the old name — and surfaces as a rung-3 block
on the first live run. The parse layer's real test is that first live run;
`test_every_captured_run_reads_as_a_live_record` only pins `load_run` against
this sample. Re-capture when the `devops-bench` SHA in `bench/pyproject.toml`
moves, which is the event that can change the schema.

## Provenance

Captured 2026-08-24 against a live management-cluster install from a cloudtop
runner, at `b35543c`, with devops-bench pinned at `4670d76`.

**One field is redacted.** `kanban_red_1`'s `output` was a 2,252-character
agent report that had delegated to the platform agent and pasted back a fleet
inventory — project name, cluster names, node counts, control-plane version,
console links. It is replaced by a short placeholder. Nothing else in any of
the five is altered.

The redaction is safe because `output` is one of the 21 keys on the record that
no scorer code reads: the ladder reads `scores`, `status`, `trajectory`,
`tokens`, `latency`, `verification_report` and `verification_parse_errors`, and
nothing else. The placeholder is deliberately **non-empty** and deliberately
does **not** contain the phrase `devops-bench smoke probe`, so it stays
consistent with that record's `report-states-the-probe-title: fail`, and so
`test_rung_3_ignores_an_empty_output` — which blanks `output` and asserts the
case still greens — keeps testing a real transition instead of a no-op.

| Directory        | `runId`                      | Task                 | Correctness | `OutcomeValidity` |
| ---------------- | ---------------------------- | -------------------- | ----------- | ----------------- |
| `kanban_red_1`   | `run_20260824_190758_251089` | `agent-kanban-smoke` | 0.5         | 0.9               |
| `kanban_red_2`   | `run_20260824_191145_500787` | `agent-kanban-smoke` | 0.5         | 1.0               |
| `kanban_red_3`   | `run_20260824_191325_408901` | `agent-kanban-smoke` | 0.5         | 0.2               |
| `kanban_green_1` | `run_20260824_192134_593628` | local prompt variant | 1.0         | 1.0               |
| `kanban_green_2` | `run_20260824_192454_771682` | local prompt variant | 1.0         | 1.0               |

**The three reds are the pre-#893 prompt.** At capture time
`agent-kanban-smoke` asked the agent for the card's _id_ while its
`report-states-the-probe-title` check required the _title_, so a correct answer
failed one objective by construction. #893 has since fixed the prompt to ask
for both. The records are still real and still the right fixtures — a
single-objective miss at `VerificationCoverage: 1.0` is a shape the ladder has
to grade correctly whatever caused it — but they are not what the task on
`main` does today.

**The two greens came from a local prompt variant**, never committed, that
asked for the title and the id together. That is, as it turns out, almost
exactly what #893 landed, so these are the closer match to the current task.
Their records carry `folder: agent-kanban-smoke-green`, which is not a
directory in `bench/tasks/` and never was.

`gpu-stress-test-diagnosis`, the other active presubmit task, is **not**
captured: it needs the `tofu` GPU stack. Tests that need a spec-carrying
catastrophic-safeguard record synthesise one by mutation and say so.

## What the reds are evidence of

The three reds are byte-for-byte the same task, prompt, agent and judge. Their
deterministic `VerificationCorrectness` is 0.5 on all three. Their judged
`OutcomeValidity` is 0.9, 1.0 and 0.2 — the 0.2 faulting the agent for
"violating the Generation-Only override", which the other two judges did not
apply to the same behaviour.

That spread is the measured argument for the two-speed gate: gating on the
judge would have redded one of these three runs for nothing, while the
deterministic signal did not move. `test_scoring.py` asserts it, so the claim
stays true or the test fails.
