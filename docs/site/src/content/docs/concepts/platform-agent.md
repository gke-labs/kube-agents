---
title: Platform Agent
description: How the Platform Agent persona, kanban guardrails, and MCP tool wiring govern fleet-wide GKE operations.
sidebar:
  order: 1
---

The Platform Agent acts as the **Platform Custodian and Agent Architect** for the managed GKE fleet: it governs cluster lifecycle, multi-tenancy boundaries, fleet-wide compliance audits, per-cluster [Cluster Agent](/kube-agents/concepts/cluster-agents/) lifecycle, and the GitOps pull-request write path. Its operating rules live in [`SOUL.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md), which the Hermes runtime loads as the system prompt.

It runs as the `platform` Hermes profile in the agent pod, scaffolded at pod startup from the `agents/platform/` template by [`profile_scaffold.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/scripts/profile_scaffold.py). It does not receive chat directly: the **Planning Agent** (the pod's `default` profile — see [ChatOps](/kube-agents/concepts/chatops/)) owns chat ingress and delegates work to the Platform Agent as cards on the shared kanban board. The Platform Agent's routing description for the Planning Agent's roster lives in [`agents/platform/CAPABILITIES.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/CAPABILITIES.md).

## How a request reaches it

Per `SOUL.md §0`, the Planning Agent delegates to the Platform Agent exclusively through kanban cards. Scheduled runs on its profile are cron jobs rather than cards; see [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/).

1. A card is assigned to `platform`, usually filed by the Planning Agent, and a worker is invoked with `work kanban task <id>`.
2. The worker reads the task with `kanban_show` and executes the work.
3. On a multi-minute run, it reports milestone updates with `kanban_heartbeat(note=…)`. Notes accumulate into one rolling `⏳` message that the notifier edits in place, keeping the thread current without waking the Planning Agent or notifying the space once per note (`deploy/docker/patches/kanban_progress_lines.py`).
4. It finishes by calling `kanban_complete` or `kanban_block`.

Child cards are reserved for genuine delegation to another specialist (such as per-cluster fan-out to [Cluster Agents](/kube-agents/concepts/cluster-agents/)) or queued follow-up work. Each child card created by a worker inherits the parent card's chat subscription, so its progress notes and completion remain visible in the thread.

## What the user receives

A completed card's `result` field carries the full answer and is posted verbatim into the chat thread. Because the user receives `result` rather than the single-line `summary` status header, `kanban_complete` requires `result` (`deploy/docker/patches/kanban_result_required.py`). Any claim the worker records in `verified_claims` carries the exact terminal command and output excerpt from that card (or from a descendant card it created). `kanban_complete` checks each entry against recorded terminal calls and appends an evidence section to `result` (`deploy/docker/patches/kanban_verified_claims.py`).

Workers write `result` in standard Markdown starting at `##`, and the destination adapter converts it for the chat platform. Slack receives Block Kit headers, tables, and dividers, while Google Chat receives bold headings and flat bullets (splitting at the 4,000-character limit on chat delivery and truncating on cron delivery). At card creation, `kanban_create` appends a report-format stanza to any card body that does not already carry formatting instructions (`deploy/docker/patches/kanban_report_format.py`). That stanza is guidance rather than a completion gate, so a report is never discarded over formatting.

At delivery, the notifier inspects the result's shape and logs defects without blocking delivery. A `#` H1 duplicating the card title or ASCII section markers without Markdown structure logs a `WARNING` naming the fix. Cosmetic issues (a heading followed only by list items, or bare high-precision numbers outside backticks) log at `INFO`.

## Completion guardrails

Image patches under `deploy/docker/patches/` enforce board and delivery invariants on the kanban tools:

| When the worker…                                                                                                                                         | What happens                                                                                                                                                | Patch                          |
| -------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| Completes with a blank `result`                                                                                                                          | Refused once per card within 15 minutes; a retry within that window is accepted with `summary` promoted into `result`.                                      | `kanban_result_required.py`    |
| Passes an unverified entry in `verified_claims`                                                                                                          | Refused once per card within 15 minutes listing every failing claim; on retry within that window, failing claims are dropped rather than shown as verified. | `kanban_verified_claims.py`    |
| Completes a `gke-rightsizing-checks` card missing its opening dependency lines or required checks, or a parent card that drops a child's dependency line | Refused once per card within 15 minutes (sharing `verified_claims`'s gate); a retry within that window is accepted unchanged and logged.                    | `kanban_rightsizing_checks.py` |
| Spawns a `platform` card without gating it behind its own card in `parents`                                                                              | `kanban_create` refuses the concurrent copy so two `platform` workers do not execute the same request in parallel.                                          | `kanban_self_copy.py`          |
| Completes while its fanned-out child cards are unfinished                                                                                                | Refused once per card within 15 minutes and told to wait and synthesize; a retry within that window closes the card with whatever the worker sends.         | `kanban_children_settled.py`   |
| Calls `kanban_block(kind="dependency")` with nothing unfinished to wait on                                                                               | Refused so the board does not immediately re-dispatch the card to a fresh worker lacking the first worker's context.                                        | `kanban_children_settled.py`   |
| Calls `kanban_link` after the child has started (running, claimed, or with a recorded run)                                                               | Refused so an active run is not held behind a new parent; the worker is told to wait on a running child or unblock a blocked one.                           | `kanban_link_running_guard.py` |
| Completes after `audit_report.py start` printed its result line without a matching `finish` result line                                                  | Refused once with instructions to run `finish` or block the card; a retry within 15 minutes closes the card prefixed with a "Not published" notice.         | `kanban_audit_ledger.py`       |
| Omits a ledger URL that `audit_report.py finish` printed                                                                                                 | Each missing ledger URL is appended to `result`.                                                                                                            | `kanban_audit_ledger.py`       |
| Keeps running after someone else blocks the card, or calls completion tools after its run has ended                                                      | An external block stops the worker process; a worker whose run has ended receives a latched refusal telling it to stop rather than retry.                   | `kanban_ended_run.py`          |
| Completes while a declared plan step is not `done` (see [Plans](#plans))                                                                                 | Refused on every attempt until every declared step is `done` or the card blocks.                                                                            | `kanban_children_settled.py`   |

The fleet-audit check inspects tool outputs in the worker's Hermes session store. It fails open if the session record cannot be read or if `audit_report.py finish` output after the last `start` exceeded Hermes's inline result limit and was moved to a file. If the refused `result` cannot be stashed on the card first, the completion is accepted immediately with a "Not published" prefix.

### Plans

A card can carry a plan: `step <key>: planned` lines, one per deliverable, in the card's body or its comments. The worker moves each step to `done` or `failed` with `kanban_comment`, and the latest line per key counts.

A step line is recognized when:

- `<key>` starts with a letter and contains only letters, digits, and hyphens, up to 40 characters.
- The status ends the line or precedes a separated reason (`step substrate: failed - helm has no credentials`). Up to three closing marks (`.`, `!`, or an emoji) may follow the status, a checklist box may lead the line, and surrounding backticks or `*` around the key or status are ignored.

Only comments written by the card's assignee count, so a user reply relayed onto a `needs_input` card by the Planning Agent cannot rewrite the step record. When a requester drops a deliverable after a block, the worker records that change itself as `step <key>: done - dropped at the requester's request`.

While any declared step is not `done`, `kanban_complete` is refused on every attempt, preventing a card with an undelivered step from closing as `done`: the worker either blocks the card naming what remains or exhausts its turn budget. Cards without step lines are unaffected.

## Core truths (from `SOUL.md §1`)

All infrastructure, access-boundary, and workload lifecycle changes route through the active declarative workflow — Git pull or merge requests (via `submit-suggestion` for one-off changes and `fleet-audit` for scheduled audit ledgers and remediation PRs), Config Connector, or ArgoCD/Flux. Direct `kubectl apply` and manual cluster mutations are forbidden.

On startup and during execution, the agent runs `python3 "$HERMES_HOME"/scripts/gitops_preflight.py --list` rather than assuming a single repository or reading an environment ConfigMap variable. Repositories on the `MANAGED:` line (`managed_repos`) are polled, audited, and targeted for pull requests. Repositories on the `CONTEXT:` line (`context_repos`) are consulted strictly for declared intent and are never swept, audited, or sent pull requests.

The operator injects `$GKE_PROJECT_ID` (alongside `$GKE_LOCATION` and `$GKE_CLUSTER_NAME`) from the `PlatformAgent` resource. Commands read those variables at runtime and write resolved values in human-facing reports, and never pass `-` in the project segment of a GCP resource path (GKE accepts `-` only as the location wildcard). For current GKE and Kubernetes schemas, version availability, deprecations, and error semantics, the agent queries `mcp-developer_knowledge` (`search_documents`, followed by `get_documents` when a chunk needs its surrounding page) before falling back to `web_search`. It does not call `answer_query`, whose 50-request daily project quota is shared across every agent in the install.

Tenant workloads are isolated to their allocated namespaces through RBAC, `NetworkPolicy`, and `ResourceQuota`. The agent runs with read-only Kubernetes visibility (no `Secret` reads) and an install-time GCP IAM permission set (`read-only` by default; widening it requires `custom` and explicit roles — see [Security &amp; IAM](/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do)).

When a run hits transient authentication or convergence delays — an expired token, an HTTP 401 `Unauthenticated`, or a fresh binding still propagating — it retries through a bounded recovery ladder. That ladder allows up to 5 iterations or roughly 10 minutes per blocker, stopping early when the same structural obstacle blocks two attempts. Authorization denials are terminal on the first occurrence: a Kubernetes HTTP 403 `Forbidden`, a Google Cloud HTTP 403 `PERMISSION_DENIED`, or a credential-proxy command-policy write refusal stops work on that action without workaround hunting. The runtime also halts the turn on the first `kubectl` write refused by `kubernetes.read-only` (`tool_loop_guardrails.permission_refusal_halt_after: 1` in `agents/platform/config.yaml`, enforced by `deploy/docker/patches/permission_refusal_stop.py`).

Action-granting prompts ("fix it for me", "directly", "do it", "loop until done") authorize the agent to proceed without routine confirmation questions, except for destructive or irreversible operations (cluster deletion, tenant offboarding, broad IAM revocation), which always require explicit human sign-off. On scheduled runs and sweeps, the agent proactively surfaces RBAC or IaC drift, version skew, security-baseline violations, and policy gaps through the declarative workflow. When answering a question in chat, it puts the recommended manifest or diff in the reply and opens a pull request only when the requester asked for a change to be submitted.

## Runtime wiring

The persona runs inside the Platform Agent Deployment on top of the [Hermes runtime](https://github.com/NousResearch/hermes-agent) (`nousresearch/hermes-agent`). Its profile overlay lives in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml), merged over [`deploy/shared/defaults/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/defaults/config.yaml) at image build; see [Config reference](/kube-agents/reference/config/) for the full annotated configuration.

### MCP servers

| Server                | Where                                                                                      | Purpose                                                                                       |
| --------------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| `platform_control`    | In-pod, `agents/platform/scripts/platform_mcp_server.py`                                   | Session and agent-internal operations (chat ingress lives on the Planning Agent).             |
| `developer_knowledge` | Remote via `mcp-remote` and the credential proxy → `developerknowledge.googleapis.com/mcp` | Authoritative Google Cloud and GKE documentation, API schemas, and configuration guidance.    |
| `gke`                 | Remote via `mcp-remote` and the credential proxy → `container.googleapis.com/mcp`          | Read-only Kubernetes and GKE cluster inspection; write tools are neither offered nor relayed. |

Both remote servers route through the credential proxy's `/v1/mcp/<server>` relay, which authenticates on its own credential and enforces the `gke` read-only tool allowlist (`agents/platform/scripts/mcp_policy.py`); profiles that expose `gke` declare that same `tools.include` allowlist. With `lazy: true`, Hermes registers each server's tool schemas at startup from `<profile>/cache/mcp_schema_cache.json` and defers spawning the MCP server subprocess until its first tool call, avoiding cold-start discovery latency on throwaway kanban workers. The agent holds no raw cloud credentials: MCP reads and brokered `kubectl` / `gcloud` wrapper reads in the shell sandbox both route through the credential proxy (see [Credential isolation](/kube-agents/reference/credential-isolation/)), while cluster changes go through a pull request.

### Toolsets

`config.yaml` declares the `cli` (used by Hermes CLI sessions and dispatcher-spawned kanban workers) and `api_server` (used by the Hermes REST API) `platform_toolsets` profiles, each listing `hermes-cli` or `hermes-api-server` alongside `mcp-platform_control`, `mcp-developer_knowledge`, `mcp-gke`, and `memory`. On this profile, `memory` gates read-only recall against shared memory when an install configures a shared provider (`memory.read_only: true`, `memory.memory_enabled: false`); workers cannot write to shared memory and instead nominate durable findings via `memory_candidates` on completed cards.

A top-level `toolsets: [kanban]` key exposes the kanban orchestrator surface (`kanban_create` and companions) outside worker turns, while `agent.disabled_toolsets` strips unused interactive bundles from model requests to keep worker first-token latency low (see [Config reference → `platform_toolsets`](/kube-agents/reference/config/#platform_toolsets)).

### Plugins

- `hermes_otel` — exports OpenTelemetry traces to the configured OTLP collector (GKE Managed OTel by default).
- `tool_call_audit` — appends every tool call and approval decision as a structured record to `logs/audit.jsonl`, which the fluent-bit sidecar ships to Cloud Logging ([Concepts → Observability](/kube-agents/concepts/observability/#tool-call-audit)).
- `incident_context` — registered on both the Platform Agent and Planning Agent profiles; inbound thread replies hit the Planning Agent's gateway, where Kubernetes incident context is injected on reply.
- `pr_opened` — records each pull or merge request opened by a Platform Agent kanban worker as an event on its card, posting it into the Slack thread as a dedicated message when `KAGE_SLACK_UX` is enabled ([ChatOps](/kube-agents/concepts/chatops/)).

Chat-ingress and front-door plugins run on the Planning Agent profile rather than here — see [Config reference → `plugins`](/kube-agents/reference/config/#plugins) and [`agents/chat/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/config.yaml).

## Behavioral shape

`SOUL.md §6` makes the Platform Agent the fleet coordinator rather than a single-cluster runtime debugger. Live workload debugging on one cluster (crash loops, OOM kills, scheduling failures) is delegated to that cluster's read-only [Cluster Agent](/kube-agents/concepts/cluster-agents/) over the kanban board, while fleet-wide audits, cluster lifecycle, and the GitOps write path stay with the Platform Agent.

During incident triage (`SOUL.md §7`), responses lead with the finding and action and stay under a 2,000-character prose target (well inside Google Chat's 4,000-character message limit). They translate Kubernetes status codes in parentheses beside the raw signal and use at most three `##` sections (`What's wrong`, `Why`, and `What to do`).

Across all user-facing messages, raw JSON, tool schemas, and CLI exit codes are omitted, while pull requests, cards, and ledger issues carry direct URLs. Named GCP clusters, workloads, and console views carry links built from [`agents/platform/docs/gcp-console-links.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/docs/gcp-console-links.md), baked into the image at `/opt/defaults/docs/` and inlined in `SOUL.md §5`.

## Where to go next

- [ChatOps](/kube-agents/concepts/chatops/) — how humans reach the agent (and how it reaches back).
- [Cluster Agents](/kube-agents/concepts/cluster-agents/) — the per-cluster read-only specialists it creates and delegates to.
- [Skills](/kube-agents/concepts/skills/) — the loadable capability bundles.
- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the cron surface that makes it proactive.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — the GitOps PR path all mutations take.
- [Config reference](/kube-agents/reference/config/) — full annotated reference for `agents/platform/config.yaml`.
