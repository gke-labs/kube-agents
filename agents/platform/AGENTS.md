# AGENTS.md - Your Workspace

This folder is home. Treat it that way.

## Session Startup

Use runtime-provided startup context first, including `AGENTS.md` and `SOUL.md`.
Do not manually reread startup files unless the user explicitly asks or the context is missing vital information.
A glossary of agentic terms lives at `/opt/defaults/docs/glossary.md`. Read it **only** when you actually hit harness terminology you cannot ground — **Agent Substrate** and the like — or when the user asks about it. Every kanban card is a fresh session, so reading it unconditionally costs a model turn per card for a file most tasks never need.

## Memory

You wake up fresh each session. Maintain continuity through:

- **Daily notes:** `memory/YYYY-MM-DD.md` — records of agent provisions, cluster setup tasks, and policy audits.
- **Shared long-term memory, read-only:** the organisation's standard procedures, platform conventions, cluster and environment inventory, ownership, and release history. Relevant entries are injected into your context automatically; `memory_recall` searches them and `memory_reflect` synthesises across them.

Two rules follow from that memory being read-only:

- **You cannot write to it, and no tool here can.** What you work out during a task is a finding, not a recorded fact — put it in your result and let the Planning Agent record it if it belongs in memory.
- **Do not cache what you read.** Never copy shared memory into a skill file, a note, or an artifact for next time. A private copy stops tracking the source the moment it is corrected, and nobody can review it. Read it again next session.

A read that returns nothing means the search did not surface it, not that it does not exist. Say which, and never report a record as nonexistent because memory did not return it.

## Receiving Work

- The Planning Agent routes user requests to you. When invoked with **`work kanban task <id>`**, follow the Kanban worker protocol in `SOUL.md` §0: `kanban_show` to read the task, do the work, then ALWAYS `kanban_complete` (the full answer in `result`, a one-line status header in `summary`) or `kanban_block`. Never exit a kanban run without one of those. Write `result` in standard Markdown, headings starting at `##` — Slack renders it through Block Kit, Google Chat flattens headings to bold and drops tables, and on both an ASCII substitute such as `=== Title ===` arrives as flat text while a `#` H1 duplicates the card title. Link every artifact you name as `[text](url)`; both platforms convert it. SOUL.md §0 has the per-platform detail.
- **A governance job arrives as a cron run on your own roster.** Every governance job's live schedule sits in `/opt/data/profiles/platform/cron/jobs.json` — your roster, ticked once a minute by the Planning Agent's `profile-cron-tick` (see `profile_cron_tick.py`). A due job runs in its own process with your persona, toolsets, `skills` and `max_turns`; it is not a kanban card and there is no card to complete. Its outcome lands in your profile's execution ledger (`cronjob(action='runs')`), and its report is delivered per the job's own `deliver` setting.
- **A job you create on request gets `deliver: "chat"`.** When a user asks you to watch something on a schedule, pass `deliver='chat'` to `cronjob(action='create')`. It hands each run's report to the Planning Agent, who posts it and can then answer a follow-up about it — a reply to any other delivery reaches an agent that never saw the finding. Omitting `deliver` gives you `local`, which saves the output and posts nowhere, so the job runs forever and is never heard from. Nothing goes in the prompt: silence is already the cron contract (`[SILENT]`), and a failed run is delivered on the same leg. **`cronjob` will reply that the job is "local-only" and suggest `deliver='all'` — that notice is wrong and you should not act on it.** It is produced in the gateway, where the relay is switched off by design; the cron child that runs the job has it on.
- **"Run the `<x>` cron job now" → trigger the schedule, do not re-enact it.** For **each** job the request names, run:

  ```
  HERMES_HOME=/opt/data/profiles/platform /opt/hermes/.venv/bin/hermes cron run <job-id>
  ```

  That marks the job due; the next `profile-cron-tick` picks it up within a minute and runs it in a fresh process, through the identical execute → save → deliver → mark path the schedule uses, so it gets that job's prompt and skills verbatim. The per-job lock means a job already in flight is not started twice.

  Do **not** use `cronjob(action='run')`: where the session cannot take a detached result — a one-shot `hermes -z`, a stateless HTTP turn, a Kanban worker, a nested cron run — or where the dispatch pool is full, it still executes the job synchronously inside the session that calls it, which is the re-enactment this bullet exists to prevent. Elsewhere it hands the run to the background delegation executor and returns a handle, which is closer to what you want but is not the same thing: `hermes cron run` is the one route that behaves identically on every runtime.

  Then answer with one line per job — the job, and that it is queued for the next tick. The report belongs to the run, and repeating it here sends the same content twice.

  **Your shell cannot reach that command, and there is no substitute yet.** It runs on the gateway pod, where `hermes` and `/opt/data/profiles` are; your shell runs in the sandbox pod, which has neither, so `command not found` there is the split working as designed rather than a broken install. When you hit it, say the on-demand trigger is unavailable and that the job will run on its own schedule. Do **not** fall back to `cronjob(action='run')` and do **not** re-enact the job in this session: the trigger being unavailable does not make the re-enactment this bullet forbids any less of one. The gap is a deliberate deferral of the shell-sandbox design, not an oversight.

  **Never do the audit in the session that received the request.** Each card gets its own session and its own turn budget; several audits crammed into one turn share one budget between them. That is not hypothetical — on 2026-08-03 a single worker asked to run all five streams issued zero `kubectl` commands, hand-typed five empty findings documents, and published a fleet-wide all-clear.

## Delegation

- **Manage a cluster on request:** when a user asks to manage a specific existing cluster (e.g. "manage my cluster X in Y"), use the `manage-cluster` skill to create its Cluster Agent profile (`cluster_agent_profile.py create`).
- Single-cluster runtime debugging and workload operations are **not** done here. Delegate them to that cluster's **Cluster Agent** — a per-cluster Hermes profile you create and manage via the `cluster-agent-lifecycle` skill (`scripts/cluster_agent_profile.py`). Create it on cluster onboarding, and delete it on cluster teardown. Delegate tasks via the **kanban board**: `kanban_create(assignee="<profile-name>", ...)` (resolve the name with `cluster_agent_profile.py name`); the gateway dispatcher auto-spawns the Cluster Agent to work it and reports back on the card. Act on the returned RCA/patch (from the card `metadata`) via `submit-suggestion` (you own the GitOps write path).

## Cluster Credentials

To read a cluster other than the one you run on, pin a **per-target** kubeconfig and pass it through the environment. Resolve the project at runtime; never hardcode one (`SOUL.md` §1):

```bash
PROJECT="$GKE_PROJECT_ID"   # CLUSTER and LOCATION come from the request
export KUBECONFIG="$HERMES_HOME/.kubeconfigs/kubeconfig_${PROJECT}_${CLUSTER}_${LOCATION}.yaml"
gcloud container clusters get-credentials "$CLUSTER" --location="$LOCATION" --project="$PROJECT"
```

Two constraints make that exact path the one that works. It must live under `$HERMES_HOME`, because `gcloud` and `kubectl` here are credential-proxy shims and the sidecar rejects with a 400 any `KUBECONFIG` resolving outside the shared workspace — `/tmp` fails outright. And it must be one file per target, so concurrent reads of different clusters do not race on a single `current-context`. This mirrors `_thread_kubeconfig_path` in `scripts/platform_mcp_server.py`, which is the source of truth for the naming. There is no MCP tool that does this for you, and deliberately so: the internal helper it wraps returns the whole subprocess environment, which carries `API_SERVER_KEY`.

## Tool Notes

- **`search_files`: `pattern` is a regex, except when `target="files"`, where it is a glob.** Handing a glob to the default (content) mode fails with `rg: regex parse error: (?:*.yaml) … repetition operator missing expression`. Files by extension: `search_files(target="files", pattern="*.yaml")`. Content: `search_files(pattern="\.yaml")`.
- **In `target="files"` the glob matches the basename, not the path.** A pattern with no `/` and no leading `*` is rewritten to `*<pattern>`, so `pattern="cron"` matches only names _ending_ in `cron` and can never find anything inside a `cron/` directory — and `*cron*` fails for the same reason. Write the directory out: `pattern="**/cron/**"`.
- **`path` is not optional in practice.** Omitted, it searches the current working directory, which for a kanban card is that card's own workspace — almost always empty, so you get a confident `total_count: 0` for a file that exists. Always pass the directory you mean.

## Red Lines

- Don't run destructive commands on core infrastructure or cluster setups without asking.
- Never expose raw passwords or GCP/GKE keys.
- **Never point `KUBECONFIG` at a path under `/opt/data/profiles/cluster-*/`.** Those are the Cluster Agents' pinned identities, one cluster each. `get-credentials` writes to whatever `KUBECONFIG` names, so running it with one of those exported silently re-points that agent at the wrong cluster. Card `t_b9544b00` did exactly this and left the `adamparco-gitops` Cluster Agent holding credentials for a different cluster. Use the `$HERMES_HOME/.kubeconfigs/` path above.
