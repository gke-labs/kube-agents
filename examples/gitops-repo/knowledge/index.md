---
type: index
title: Operational Knowledge Framework (OKF) — index
tags: [okf, index]
---

# OKF index

The Operational Knowledge Framework is durable, human-reviewed knowledge shared indirectly across
agent tiers (06 §5). It is markdown + YAML frontmatter under `knowledge/`, lives **outside** the
paths the pipeline deploys, and is **never applied to a cluster**. Agents **read** OKF for context
and **propose** updates via PR; humans approve (curate-as-code). OKF holds durable knowledge only —
**not** session state.

Every entry must declare a `type` in frontmatter. The canonical starting set (open convention, not a
hard enum — add new types by PR):

| `type`              | Purpose                                                                                          |
| ------------------- | ------------------------------------------------------------------------------------------------ |
| `cluster-blueprint` | Standard cluster config baseline                                                                 |
| `tenancy-model`     | Namespace isolation standard                                                                     |
| `runbook`           | Operational procedure (SRE CUJ)                                                                  |
| `metric-definition` | Named metric/KPI definition                                                                      |
| `escalation`        | A cross-tier request not yet a change                                                            |
| `observation`       | A durable finding worth sharing                                                                  |
| `decision`          | Forward-looking architectural choice (proposed, [`memory.md`](../../../docs/designs/memory.md))  |
| `lesson`            | Verified incident / PR outcome lesson (proposed, [`memory.md`](../../../docs/designs/memory.md)) |

Entries for live cluster posture (`cluster-blueprint`, `tenancy-model`, `runbook`, `metric-definition`,
`escalation`, `observation`) may also carry `declares:`, a list of `{check, namespace, object}` items (`namespace: ""`
written out for a cluster-scoped object; plus `cluster` for a one-cluster choice, as the qualified `<project>/<location>/<name>` every stream's
findings carry, and `expires`, a `YYYY-MM-DD` UTC date that is the last day the item stands: past it the posture
returns as a finding with a _Declared intent expired_ line naming the note and the date) naming a posture a declaring audit stream (`obtainability-audit`, `compliance-audit`, `security-patch-orchestrator`, `fleet-wide-cost-analysis`, `stockout-prevention`) lists under
_Declared intent_ instead of reporting (`../README.md`, "Declared intent"). Proposed `decision` and `lesson` entries keep pre-push manifest assertions under `scope` and `guard.expect` separate from `declares:` so manifest guards do not suppress live `fleet-audit` findings ([`docs/designs/memory.md`](../../../docs/designs/memory.md)).

## Entries

- [Standard GKE cluster blueprint](cluster-blueprint/standard-gke.md) — `cluster-blueprint`

_Markdown links between entries form the knowledge graph. An optional `log.md` may record history._
