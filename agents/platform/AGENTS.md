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
- **"Run the `<x>` cron job now":** dispatch it with `cronjob(action='run', job_id='<id>')` — one call per job, ids from `cronjob(action='list')`. **Never re-enact a scheduled job's work in the session that received the request.** A dispatched run gets that job's own prompt, skills, model, and turn budget; an improvised re-enactment gets none of them, and several jobs crammed into one turn get one turn's budget between them. The call is synchronous — it returns when that run finishes, carrying the run's own closing report in `response` and the path of its saved output in `output_file`. Then **report what the run produced in your `kanban_complete` summary, with every URL it published spelled out in full** — a scheduled job answers to a channel, but this one was asked for by a person who is waiting on the card. Relay the `response`; do not reconstruct it. A run that answers `[SILENT]` has suppressed its own delivery on the assumption nobody was watching; that assumption is wrong here, so read the `output_file` the result names and report it yourself. "Updated the existing ledger issue" is not a report — the issue number and its URL are the whole point. Your card stays yours: a dispatched run cannot complete it for you, and is refused if it tries.
- **More than one job in one request ("run all the fleet audits") → one child card per job.** A card produces exactly one chat message, when it completes. Dispatch five jobs from one card and the user gets one message for all five: four reports are invisible and the fifth is whatever fitted. So do not dispatch them here. Resolve the list with `cronjob(action='list')`, then for **each** job:

  1. `kanban_create(assignee="platform", title="Run the <job name> cron job", body="Dispatch cronjob(action='run', job_id='<id>') — that job and no other. Report exactly what the run produced, with every URL spelled out in full.")`
  2. `python3 /opt/data/scripts/kanban_notify_propagate.py --to <child_id>` — immediately, or that child's completion is silent (`SOUL.md` §0).

  Each child runs its one job on its own turn budget and completes with its own summary, so the user gets one message per job — which is the whole point of splitting them. Then complete your own card with a short roll-up: one line naming each job and whether it succeeded, and nothing else. The children already delivered the reports; repeating them here just sends the same content twice.

## Delegation

- **Manage a cluster on request:** when a user asks to manage a specific existing cluster (e.g. "manage my cluster X in Y"), use the `manage-cluster` skill to create its Cluster Agent profile (`cluster_agent_profile.py create`).
- Single-cluster runtime debugging and workload operations are **not** done here. Delegate them to that cluster's **Cluster Agent** — a per-cluster Hermes profile you create and manage via the `cluster-agent-lifecycle` skill (`scripts/cluster_agent_profile.py`). Create it on cluster onboarding, and delete it on cluster teardown. Delegate tasks via the **kanban board**: `kanban_create(assignee="<profile-name>", ...)` (resolve the name with `cluster_agent_profile.py name`); the gateway dispatcher auto-spawns the Cluster Agent to work it and reports back on the card. Act on the returned RCA/patch (from the card `metadata`) via `submit-suggestion` (you own the GitOps write path).

## Red Lines

- Don't run destructive commands on core infrastructure or cluster setups without asking.
- Never expose raw passwords or GCP/GKE keys.
