# The kube-agents build harness

A Claude Code harness that builds kube-agents end-to-end from the design set in `docs/design/`
(01–08), phase by phase, verifying and regression-checking after each phase, iterating until the
**Definition of Done** ([07 §3](../design/07-implementation-roadmap.md)) passes.

It is not a separate program — it's a set of Claude Code skills, a persistent ledger, a verification
workflow, and a schedule that together drive the roadmap's verification loop
([07 §5](../design/07-implementation-roadmap.md)) autonomously.

## How it works

```
          ┌─────────────────────────────────────────────────────────────┐
          │  read LEDGER.md → pick next unit of work                      │
          └───────────────┬─────────────────────────────────────────────┘
                          ▼
   break down ─▶ (detailed design) ─▶ implement ─▶ verify ─▶ regress ─▶ PR
      │  read the phase's Work items + referenced spec sections            │
      │  code on a branch; prettier/make/go build; Conventional Commits    │
      │  run phase acceptance + touched specs' Verification sections       │
      │  re-run prior-phase acceptance + load-bearing suites               │
      └─▶ iterate until green ─▶ update LEDGER ─▶ advance phase ───────────┘
```

- **Break down / implement / verify / regress** are the steps of `harness-run`.
- **State** persists in [`LEDGER.md`](LEDGER.md) so any session (day 2, day 5) resumes correctly.
- **Guardrails**: every change is checked against [`.claude/harness/invariants.md`](../../.claude/harness/invariants.md)
  before it can merge. The existing `.agents/skills/review-security-k8s-*` suite is the review gate.

## Components

| Piece              | Path                                       | Role                                                       |
| ------------------ | ------------------------------------------ | ---------------------------------------------------------- |
| Ledger             | `docs/build/LEDGER.md`                     | Persistent build state; read first, updated last every run |
| Phase breakdowns   | `docs/build/phase-<N>.md`                  | Concrete task list for a phase (created on entry)          |
| Orchestrator skill | `.claude/skills/harness-run/SKILL.md`      | The per-phase loop                                         |
| Verify skill       | `.claude/skills/harness-verify/SKILL.md`   | Runs acceptance + Verification suites, logs results        |
| Invariants gate    | `.claude/harness/invariants.md`            | 5 load-bearing rules, checked before merge                 |
| Verify workflow    | `.claude/harness/verify-phase.workflow.js` | Optional parallel fan-out of all suites                    |

## Running it

**Manual (recommended to start):**

```
/harness-run          # do the next unit of work, then checkpoint the ledger
/harness-verify       # run current phase's acceptance + touched Verification suites
```

**Autonomous (multi-day):** a durable scheduled task re-enqueues `/harness-run` on an interval so
the build progresses unattended. Claude Code cron tasks auto-expire after 7 days and fire only while
the REPL is idle — re-arm as needed. See "Stopping" below.

## Safety posture

- **The product is read-only by design.** Agents never mutate cluster/cloud APIs; the only write
  path is a reviewed PR applied by the customer's CI/CD (invariant #1–2). This holds for the build
  process too.
- **Destructive tests** (negative security, chaos — deleting agents, killing the hub, bad-RBAC
  applies) run **only on Kind or an ephemeral scratch GKE cluster**, never on production. The harness
  halts if a destructive test is aimed anywhere else.
- **PRs, not direct pushes.** Per [AGENTS.md](../../AGENTS.md): Conventional Commits, PR template,
  format before commit, stage only targeted files, push branches to a fork.
- **Load-bearing halts.** The harness stops and surfaces rather than auto-advancing when a security
  negative test (03 §11) or chaos test (05 §8) fails, or an invariant would break.

## Stopping / pausing

- Pause autonomy: delete the scheduled task (`/harness-run` won't self-trigger). Use the task list
  UI or ask "stop the harness schedule."
- The `/goal` Stop hook (if set) keeps the session working toward completion; clear it with
  `/goal clear` to let the session stop.
