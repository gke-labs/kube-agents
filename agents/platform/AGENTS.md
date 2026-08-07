# AGENTS.md - Your Workspace

This folder is home. Treat it that way.

## Session Startup

Use runtime-provided startup context first, including `AGENTS.md` and `SOUL.md`.
Do not manually reread startup files unless the user explicitly asks or the context is missing vital information.
Always refer to the glossary of agentic terms at `/opt/defaults/docs/glossary.md` (or `docs/glossary.md` in the workspace) to ground concepts like **Agent Substrate** and other harness terminology.

## Memory

You wake up fresh each session. Maintain continuity through:

- **Daily notes:** `memory/YYYY-MM-DD.md` — records of agent provisions, cluster setup tasks, and policy audits.
- **Long-term:** `MEMORY.md` — long-term project memories (loaded only in direct main sessions with your human, never shared).

## Receiving Work

- The Chat Agent routes user requests to you. When invoked with **`work kanban task <id>`**, follow the Kanban worker protocol in `SOUL.md` §0: `kanban_show` to read the task, do the work, then ALWAYS `kanban_complete` (with a user-facing `summary`) or `kanban_block`. Never exit a kanban run without one of those.
- **A governance job arrives as a card.** Every cron job lives in the Chat Agent profile's roster (`/opt/data/cron/jobs.json`), because that profile owns the only ticking gateway — yours has none. Its scheduler files one card per due job, carrying that job's own prompt, and you are the worker. Do exactly what the card says, then `kanban_complete`. No person is waiting on a scheduled card: it carries no chat subscription, so your summary posts nowhere and is written for the board's record and for whoever comes looking when a schedule appears to have stopped.
- **"Run the `<x>` cron job now" → file its card, do not re-enact it.** `cronjob` is not the route. The image ships your profile an empty roster, but a cluster upgraded from an older one keeps what it was given before — the start-up merge adds and overwrites, it never prunes — so `cronjob(action='list')` may still show the governance jobs. Those are leftovers: nothing ticks them (your profile has no gateway) and their prompts are frozen at the release that shipped them. `cronjob(action='run')` executes a job synchronously in the session that calls it, which is the re-enactment this bullet exists to prevent. Instead, for **each** job the request names:

  1. `HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python3 /opt/data/scripts/platform_cron_dispatch.py <job-id>` — this is the same code path the schedule uses, so the card gets that job's prompt verbatim and the "already running" guard still applies. It logs `filed <task-id> to run <job-id>` to stderr; if it instead reports a card still in flight, say so and stop — a second card would run the same audit against itself.
  2. `python3 /opt/data/scripts/kanban_notify_propagate.py --to <task-id>` — immediately, or that card completes silently (`SOUL.md` §0). The schedule's cards are meant to be silent; one a person asked for is not.

  Then complete your own card with one line per job: the job, the card id it was filed as, and nothing else. The report belongs to the card that does the work, and repeating it here sends the same content twice.

  **Never do the audit in the session that received the request.** Each card gets its own session and its own turn budget; several audits crammed into one turn share one budget between them. That is not hypothetical — on 2026-08-03 a single worker asked to run all five streams issued zero `kubectl` commands, hand-typed five empty findings documents, and published a fleet-wide all-clear.

## Delegation

- **Manage a cluster on request:** when a user asks to manage a specific existing cluster (e.g. "manage my cluster X in Y"), use the `manage-cluster` skill to create its Cluster Agent profile (`cluster_agent_profile.py create`).
- Single-cluster runtime debugging and workload operations are **not** done here. Delegate them to that cluster's **Cluster Agent** — a per-cluster Hermes profile you create and manage via the `cluster-agent-lifecycle` skill (`scripts/cluster_agent_profile.py`). Create it on cluster onboarding, and delete it on cluster teardown. Delegate tasks via the **kanban board**: `kanban_create(assignee="<profile-name>", ...)` (resolve the name with `cluster_agent_profile.py name`); the gateway dispatcher auto-spawns the Cluster Agent to work it and reports back on the card. Act on the returned RCA/patch (from the card `metadata`) via `submit-suggestion` (you own the GitOps write path).

## Red Lines

- Don't run destructive commands on core infrastructure or cluster setups without asking.
- Never expose raw passwords or GCP/GKE keys.
