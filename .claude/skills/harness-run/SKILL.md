---
name: harness-run
description: Drive the kube-agents build forward one unit of work. Reads docs/build/LEDGER.md, picks the next task in the current roadmap phase, runs the break-down → implement → verify → regress loop, and checkpoints the ledger. Use to start or continue the autonomous build of kube-agents from docs/design/.
---

# harness-run — the kube-agents build loop

You are driving the autonomous build of kube-agents from its design set (`docs/design/` 01–08),
following the roadmap in `docs/design/07-implementation-roadmap.md`. Do **one coherent unit of work**
per invocation, then checkpoint. State lives in `docs/build/LEDGER.md`.

## 0. Orient (always do this first)

1. Read `docs/build/LEDGER.md` — current phase, current task, blockers, halt conditions.
2. Read `.claude/harness/invariants.md` — the gate every change must pass.
3. Read the current phase in `docs/design/07-implementation-roadmap.md` §2 (its **Work** items and
   **Accept** criteria) and the phase breakdown `docs/build/phase-<N>.md` if it exists.
4. If there are open **Blockers** or a triggered **halt condition**, do not proceed autonomously —
   summarize the blocker and stop.

## 1. Pick the next unit

- If the current phase has **no** `docs/build/phase-<N>.md`, the unit is **break down the phase**
  (see §2). Otherwise pick the first task with status `todo`/`in-progress` in that file.
- Keep a unit small enough to finish + verify in one run. Prefer finishing an in-progress task over
  starting a new one.

## 2. Break down a phase (when entering a new phase)

Create `docs/build/phase-<N>.md`:

- Expand the phase's **Work** items into concrete, individually-verifiable tasks (`P<N>-T1`, …), each
  with: what to build, which spec sections it implements (cite doc + §), files it will touch, and the
  **acceptance signal** (which phase-accept bullet and/or spec Verification check proves it).
- List the **Verification suites** this phase touches — always a subset of: 02 §10, 03 §11, 04 §9,
  05 §8, 06 §10, 08 §7 — plus the phase's own **Accept** bullets.
- Mirror the task list into `LEDGER.md`'s phase table and set status 🟡.

## 3. Detailed design (only when a task warrants it)

For a task that is architecturally non-trivial or spec-silent, write
`docs/build/phase-<N>/<task>.md` with an implementation plan before coding. For mechanical tasks,
skip straight to §4. If a spec is genuinely silent, pick the simplest option consistent with the
invariants and record it in the ledger's **Decisions & deviations** table (README rule #3).

## 4. Implement

- Work on a branch (fork per `AGENTS.md`); use a git worktree if parallel tasks would conflict.
- Ground new code on existing patterns — new personas follow `agents/platform/` shape; runtime work
  extends `k8s-operator/`; the review gate reuses `.agents/skills/review-security-k8s-*`.
- Before committing: `npx prettier --write` changed md/json/yaml; `make`/`go build` if
  `k8s-operator/` changed; `docker build` the relevant target if an image changed.
- Conventional Commits, scoped staging, PR template — never `gh pr create --fill`.

## 5. Verify (invoke `harness-verify`)

Run the phase **Accept** criteria + the **Verification** suites the task touched, on the right target
(Kind for inner loop; scratch GKE for identity/cloud criteria). Record every result in the ledger's
**Verification log** with evidence. Fix and re-run until green — do not advance on a failing suite.

## 6. Regress

Re-run prior phases' **Accept** criteria plus the two load-bearing suites — security negative tests
(03 §11) and chaos (05 §8) — to confirm the new work didn't break the rest of the design. A
regression is a halt condition, not a "note and move on."

## 7. Gate + checkpoint

1. Run the invariants checklist (`.claude/harness/invariants.md`); every item PASS or justified N-A.
2. Update `LEDGER.md`: task status, verification log, decisions, blockers, `Last updated`.
3. If the phase's **Accept** all pass and invariants hold: open a PR (per `AGENTS.md`), mark the
   phase 🟢/✅ in the ledger, and — since autonomy is **fully autonomous** — advance to the next
   phase. **Halt instead** if any load-bearing halt condition (ledger §Status) is triggered.

## Halt conditions (stop, surface, do not auto-advance)

- A security negative test (03 §11) or chaos test (05 §8) fails.
- A change would violate an invariant.
- A destructive test would run anywhere but Kind / scratch GKE.
- A spec conflict with no simplest-option resolution, or an unresolved blocker.

Keep each run tight: orient → one unit → verify → checkpoint. The ledger, not memory, carries state
to the next run.
