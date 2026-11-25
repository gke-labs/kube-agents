# Hooks, plugins and the local MCP server for profile workers

A proposal for decision. It says what a profile worker gets in place of the platform agent's five
plugins and its local MCP server. The decider answers each row of [Decisions](#decisions) with
yes or no. Memory recall is not in this design; it belongs to the memory work (#2903).

Related designs: [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) (the persona format and
the per-task configuration directory), [`spec-subagent-profiles.md`](spec-subagent-profiles.md)
(the MCP relay rule and the activity artifact),
[`platform-as-profile-inventory.md`](platform-as-profile-inventory.md) (blocker 11). Other work
owns a part that this design only names: where a record is shipped (telemetry and log shipping,
#6674), where durable state lives (#6669), how a task posts to chat (#6668), reads with no
cluster (#6667), what a profile reaches on the broker (blocker 7), and a plugin surface for
agents (#1726).

## What the platform agent has today

Five plugins are enabled in `agents/platform/config.yaml`:

| Plugin             | What it registers                                                             | What it does                                                                                                                                                                                                                                                 | Can it refuse or change                                                                                    | Who reads its output                                                                                 |
| ------------------ | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `pr_opened`        | `transform_terminal_output`, `pre_tool_call`                                  | Reads the `proposal-opened` line in terminal output, claims the proposal at the broker with the line's one-time token, writes the pull-request ledger and a post-merge record to the Session KV, and adds a `pr_opened` event to the kanban card             | Yes: it removes the token from the output, and it blocks `submit_suggestion.py` after the card's run ended | The chat notifier, the usage counters, the post-merge check, the autonomy dial, incident fix lookups |
| `incident_context` | `pre_gateway_dispatch`                                                        | Adds incident context to an inbound chat message. It also records an `apply` that a person writes in an incident's thread                                                                                                                                    | It rewrites the message; it touches no tool call                                                           | The model; the incident's recurrence hold                                                            |
| `eval_run_tag`     | A model-request middleware, only with the eval switch on                      | Adds two headers to each model request                                                                                                                                                                                                                       | No                                                                                                         | The replay proxy                                                                                     |
| `hermes_otel`      | Not in this repository: a third-party plugin that the image installs          | Exports session, tool-call and MCP spans                                                                                                                                                                                                                     | Not known                                                                                                  | The OTLP collector                                                                                   |
| `tool_call_audit`  | `pre_tool_call`, `post_tool_call`, two approval hooks, `pre_gateway_dispatch` | Appends one JSON line for each event to the profile's `audit.jsonl`. A tool line holds the session id, the tool's name, the redacted arguments and the result. Other lines hold an approval request, its answer, and a dispatched message with its principal | No                                                                                                         | The fluent-bit sidecar and Cloud Logging, the admin console, the skill-invocation metrics            |

The local MCP server `platform_control` (`agents/platform/scripts/platform_mcp_server.py`) is a
stdio child of the agent with 20 tools: six cluster and Config Connector reads, three reads of
the fleet scope and of Hermes profile directories, two chat tools (`send_notification`,
`report_to_chat`), seven findings-queue tools, the first-run report and the alert history. The
six reads run the credential-proxy shims in the sandbox. `send_notification` runs `hermes send`.
The other tools read the agent pod's own files or the loopback Session KV, which this server
also starts. No tool calls the forge.

The AgentPlugin CRD installs a Hermes plugin image into the PlatformAgent pod, in the default
Hermes profile or a named one. It also carries a `config` text, which the operator merges into
that profile's Hermes configuration, and an `env` list. Two plugins ship in `agentplugins/`:

- `gke-stockout-investigator`: a skill, a Pub/Sub subscription route in its `config`, and an
  optional Secret with a service-account key. The skill's first step calls `send_notification`.
- `pubsub-platform`: a Hermes platform adapter that makes a Hermes turn from a Pub/Sub message.

Nothing connects an AgentPlugin to an AgentProfile worker.

## What a profile worker has today

- The persona. The manifest is decoded strictly, so an unknown key fails. A skill's frontmatter
  holds four inert keys; a skill that carries `hooks` or `allowed-tools` is refused. The
  `harness` hints map exists, and the Claude Code adapter ignores each key with a warning. An MCP
  server is a broker relay by name; the format has no direct-URL kind. A profile reaches two
  tools of one relay server, `developer_knowledge`.
- MCP processes. In the worker, each relay server is a stdio child of the harness: the
  adapter's own proxy to the broker. Where a pod has delegation, the adapter's delegate tool is
  a second fixed stdio server. No other MCP server starts.
- The worker's configuration. Each task gets a new configuration directory. The worker writes
  `settings.json` there as an empty object, read-only, and starts the harness with
  `--setting-sources user` and `--strict-mcp-config`. No production code writes a `hooks` key.
- The worker's process. The harness and each child of it run as the adapter's user, with no
  sandbox between them. They can read the task credential file and the broker token.
- Refusals. No platform code runs before a tool call. The harness refuses a tool that the
  persona does not grant (`--disallowedTools`), and the broker refuses a brokered call. After a
  write that the broker refused, the adapter stops the harness. A worker has no approval
  prompt: the harness runs headless.
- The event stream. The adapter reads the harness's stream through a driver. For each tool call
  it publishes one `activity` entry on the bus (the tool's name and input). The publish is
  best-effort. The driver's event for a tool result carries no field: no id, no error flag, no
  result. Neither stream, as the drivers read it, has a duration for one tool. The adapter
  writes no log line and no file for a tool call.
- The broker's records. An `/v1/exec` call leaves a start record and an outcome record, each
  with the task id and the credential's id in its principal and with no argument. An MCP relay
  call leaves two lines. For a `/v1/vcs/` call that succeeds, only a metric and the HTTP request
  line were found, with no task and no repository. The broker keeps in memory which task made
  which branch and proposal; only its own checks read that record, and a restart loses it.
- The opened pull request. The broker adds a one-time claim token to each `proposal-create`
  answer, for each role. The version-control client in the worker image prints the token in the
  `proposal-opened` line, and nothing removes it, so the model reads it. The profile role has no
  `proposal-claim` verb. The gateway renders a `notice` artifact ("a PR it opened"); nothing
  produces one.
- Telemetry. A worker pod emits no trace, no metric and no structured per-tool log.
- The second harness. The AgentProfile accepts the harness type `claude-code` only, and the
  persona format has no Antigravity adapter. So a hook mechanism can be defined for one harness
  today.
- Skills from plugins. No persona holds the stockout investigator's skill.
  [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) plans shared skill packs that mount
  as plugins (#1726). No code does that.

## Options

| Option                                                                                               | What it needs                                                                                                | What the facts say                                                                                                                                                                                                    |
| ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A. Hooks declared in the persona manifest, rendered by the worker into `settings.json`               | A `hooks` key in the strict manifest; a renderer for each harness; a rule for which commands a hook can name | One harness only. A hook is the persona author's code on each tool call, beside the task credential. No need of the five plugins requires it.                                                                         |
| B. No hooks. The records that the platform needs are fixed code in the worker adapter and the broker | Driver and adapter work for a structured per-tool record; a record of an opened proposal at the broker       | The adapter sees each tool call, and the broker sees each proposal that a task opens. The records do not depend on a persona or on a harness feature. The per-tool record is only as good as what the harness prints. |
| C. Hooks declared on the AgentProfile by the install's admin, from an allowed image                  | A CRD field, the operator, the worker's renderer, an image rule                                              | One harness only. It serves an install's own need. The five plugins give no such need, and the plugin surface for agents has its own track (#1726).                                                                   |

Recommended: B. The plan for this epic named "declared hooks" as the first step. The facts do
not support that step: each need of the five plugins has a source that is not a hook.

## Decisions

| Id  | Decision                                                                                                                                                                                                                                                                                                                                                                                               | Where                                               |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------- |
| 1   | No hook mechanism for profile workers. `settings.json` stays empty and the worker's own, and the persona format gets no `hooks` key.                                                                                                                                                                                                                                                                   | `a2a/lib/persona/README.md` says so; the tests stay |
| 2   | The per-tool record. The worker adapter writes one structured log line when a tool call starts and one when its result is in: the task id, a call id, the tool's name, the status and the duration. See [The per-tool record](#the-per-tool-record).                                                                                                                                                   | `a2a/worker-adapter` (the drivers and the adapter)  |
| 3   | The per-tool record holds no input and no result. A "no" puts the redacted input in the line. See [The per-tool record](#the-per-tool-record) for what each answer costs.                                                                                                                                                                                                                              | the same                                            |
| 4   | The approval lines and the message line of `tool_call_audit` have no counterpart in this design. A worker has no approval prompt. A record for each message is part of the telemetry work (#6674).                                                                                                                                                                                                     | none                                                |
| 5   | The `activity` artifact stays as it is. It is a live view on the bus and can lose an entry, so it is not the record.                                                                                                                                                                                                                                                                                   | one line of `spec-subagent-profiles.md`             |
| 6   | The opened proposal. The source of "this task opened proposal N" is the broker: when a profile task's `proposal-create` succeeds, the broker writes one audit record. See [The opened proposal](#the-opened-proposal) for what the record does not hold.                                                                                                                                               | `agents/platform/scripts/credential_proxy.py`       |
| 7   | No claim for profiles. The broker puts no claim token in a `proposal-create` answer to the profile role, and the profile role gets no `proposal-claim` verb. The record of row 6 is in place of the claim.                                                                                                                                                                                             | `agents/platform/scripts/credential_proxy.py`       |
| 8   | The chat notice of an opened proposal. The gateway's `notice` artifact is the place; which component produces it for a profile task is a decision of the chat delivery work (#6668).                                                                                                                                                                                                                   | none here                                           |
| 9   | Traces. A span for a session, a turn and a tool call comes from the adapter's event stream, not from a hook. What is exported, and where, is the telemetry work (#6674).                                                                                                                                                                                                                               | none here                                           |
| 10  | `incident_context` has no profile counterpart. It rewrites an inbound chat message before dispatch and records an `apply`; both are on the side of the session and the incident store.                                                                                                                                                                                                                 | none                                                |
| 11  | `eval_run_tag` has no profile counterpart now. The tag is for one card or session. If the replay proxy must tag a profile worker's model requests, the adapter sets the headers from the task.                                                                                                                                                                                                         | none now                                            |
| 12  | The refusal of `submit_suggestion.py` after a run ended has no profile counterpart. The adapter stops the harness and its process group when the task has its result. The broker's permit gate holds each write of a profile task; no component revokes the task credential before it expires.                                                                                                         | none                                                |
| 13  | No MCP server with logic or state of its own in a profile worker. The worker's fixed stdio servers stay: the adapter's proxy to a broker relay and its delegate tool. A persona or a plugin adds none. The rule stays: a profile's MCP servers are broker relays. Which server and which tools a profile reaches is a decision of the broker-surface work (blocker 7); this design does not change it. | none                                                |
| 14  | The AgentPlugin CRD does not reach profile workers. See [What an AgentPlugin carries](#what-an-agentplugin-carries) for where each part goes.                                                                                                                                                                                                                                                          | none; each move is its own change                   |
| 15  | A declared hook is not built now, and this design does not close it. A later design can open it when three things exist: a need that rows 2 to 12 do not meet; a second harness with a hook mechanism, or the decision that one harness is enough; and a rule for whose code a hook is. A "no" closes it.                                                                                              | none now; the plugin track (#1726) is the home      |

### The per-tool record

What the drivers get, because they do not have it today:

- The Claude Code driver reads the call's id on the tool call, and the same id and the error
  flag on the tool result. The result event carries them to the adapter.
- The adapter measures the duration between the two events. The harness's stream gives none.
- An Antigravity driver gets the same. Its stream has a step index, which today only removes a
  duplicate; an error and a success are one event; a denied call is only in the list of denied
  actions at the end. Antigravity cannot run a profile task today.

Limits of the record:

- The harness reports it. The harness and each tool run as one user, so a process in the worker
  can omit or forge a line. For a credentialed call, the broker's record is the one that counts.
  On the platform agent, the tool commands run in a different pod and cannot write `audit.jsonl`.
- With row 3 at yes, the readers of today's record lose two things for a profile task. The admin
  console shows a tool call with its status and without its arguments; the status comes from
  the error flag, not from the result. The skill-invocation metric reads the skill's name from
  the arguments, so it cannot count a skill that a profile task used.
- With row 3 at no, the adapter needs the redaction of `tool_call_audit`, which is Python today,
  as Go code. The line then holds what the model sent to a tool, after redaction, and the
  telemetry work (#6674) decides if that line can leave the pod.

### The opened proposal

The record of row 6 holds the task id, the credential's id, the repository and the number. A
profile task opens a proposal only through `proposal-create`, from a branch that the same task
made, and that verb has one success path in the broker.

The record does not hold three fields that the post-merge record and the ledger hold today: the
finding's target, which the caller sets and never sends to the broker; the origin; and the chat
thread of the card. The home of the ledger and of the post-merge record, their readers and a
source for these three fields are decisions of the state work (#6669). This design gives that
work one source fact and no more.

On the platform agent the claim proves that a `proposal-opened` line in terminal output names a
proposal that the broker made. For a profile task the broker writes the record itself, so
nothing needs that proof. With row 7 at no, the token stays in the answer; the model can read
it, and the profile role cannot use it.

### What an AgentPlugin carries

| Part                                                   | Example                                           | Where it will go                                                                                                                  |
| ------------------------------------------------------ | ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| A skill                                                | The stockout investigator's skill                 | A persona artifact. No persona holds it today. Its first step posts to chat, so the move waits for the chat delivery work (#6668) |
| An ingress route or a platform adapter                 | The Pub/Sub subscription route; `pubsub-platform` | The event-trigger path into profile tasks (#6528). The inventory records that no bus or trigger counterpart exists                |
| `config`, merged into a Hermes profile's configuration | The `platforms` block of the stockout plugin      | No counterpart. A profile's configuration is the AgentProfile and the persona                                                     |
| `env` and a Secret                                     | The optional service-account key                  | No counterpart. A worker holds no long-lived secret; a credentialed call goes through the broker                                  |

Row 14 is about the AgentPlugin CRD only. It does not replace the line of
[`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) that plans shared skill packs as
plugins; that plan stays with the plugin track (#1726).

### Where the need of each `platform_control` tool goes

| Tools                                                                       | Need on a profile                           | Where it goes                                                                                                                                          |
| --------------------------------------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `verify_gke_cluster`, the four Config Connector tools, `audit_log_searcher` | Cluster and cloud reads                     | The profile's brokered commands (blocker 7)                                                                                                            |
| `fleet_scope`                                                               | The clusters in scope, without the excluded | Not decided here. The tool reads the operator's scope snapshot, which is install state: #6667 if a read route gives it, #6669 if a state home gives it |
| `list_cluster_profiles`, `get_cluster_profile_name`                         | None: they read Hermes profile directories  | Not needed                                                                                                                                             |
| `send_notification`, `report_to_chat`                                       | A chat post from a task                     | The chat delivery work (#6668)                                                                                                                         |
| The seven findings tools, `get_first_run_report`, `get_alert_history`       | Durable state of the agent pod              | The state work (#6669)                                                                                                                                 |

In each row the surface that the model calls is a brokered command, a broker route or a tool of
a broker relay. The named work decides which one. None is a new process in the worker.

## What stays out

- Memory recall and a memory provider (#2903).
- The shipping of logs and traces, and the fields that a compliance reader needs in them (#6674).
- The homes of the ledger, the post-merge record and the findings queue (#6669).
- The broker's relay rows and verbs, apart from row 7 (blocker 7).
- A persona adapter for Antigravity. When one exists, rows 2 and 9 need its driver work, as
  [The per-tool record](#the-per-tool-record) says.
- Hooks and gates that are not one of the five plugins. The inventory has three such rows, and
  none needs a hook in a profile worker. The `bootstrap_onboarding` plugin (a `pre_llm_call`
  hook on the first turn of a person) is a front-door row and belongs to the session work
  (#6538). The approval gate for unattended runs (`cron_risk_gate`) has no counterpart, because
  a worker has no approval prompt; the persona's tool list, the worker's deny list and the
  broker hold an unattended run. The web-search cap and the stream deadline (`tool_loop_guardrails`,
  `stale_timeout`) are run limits of the worker, not hooks; they need an owner outside this design.

## Done when

For the group issue of this design:

1. Each row of [Decisions](#decisions) has the decider's answer.
2. The worker adapter writes the per-tool record of row 2 for each driver that can run a
   profile task, with tests for a success, an error and two parallel calls.
3. The broker writes the record of row 6, with a test that a success writes one record and a
   test that a refused `proposal-create` writes none.
4. The broker's answer follows row 7, with a test for the profile role and one for another
   role.
5. Each row of the inventory for the five plugins, the `platform_control` server and the
   AgentPlugin CRD names its destination: a row of this design, another issue, or "not needed".
   That includes `fleet_scope` and the run limits that have no owner here.
6. The persona format's page says that a persona declares no hook, and why.
7. The artifact table of [`spec-subagent-profiles.md`](spec-subagent-profiles.md) says that the
   `activity` artifact is a live view and that the per-tool record is the adapter's log line.

## Checked, and not checked

Checked in the code on `main` at `be60d590d`: the list of enabled plugins; the hooks that
`pr_opened`, `incident_context` and `tool_call_audit` register and the middleware of
`eval_run_tag`; the block action of `pr_opened` and its removal of the token; the fields of an
`audit.jsonl` line and the two readers that use the arguments and the result; the count of
`platform_control` tools and what each group calls; the fields of the AgentPlugin CRD and the
parts of the two shipped plugins; the strict decode of the manifest and the refusal of `hooks`
in a skill; the empty `settings.json` and the two harness flags; that the harness runs as the
adapter's user; the `--disallowedTools` list, the headless start and the stop after a refused
write; the two stdio MCP servers that the worker starts; the `activity`
entry of a tool call and the tool-result event with no field in both drivers; the broker's
records for `/v1/exec` and the MCP relay; the broker's in-memory record and the claim token at
`proposal-create`; the profile's relay tools; the harness type that the AgentProfile accepts and
the missing Antigravity adapter; that no profile controller names an AgentPlugin; that the
worker's code names no trace or log shipper; that nothing in the bus code produces a `notice`
artifact.

Not checked: the hooks of `hermes_otel` (its source is not in this repository); each reader of
the `pr_opened` event and of `audit.jsonl` (taken from a code search, not read in full); that no
log line of the broker carries the task for a `/v1/vcs/` call that succeeds (none was found); if
`proposal-create` can answer with a proposal that exists already; if a process in the worker can
write to the harness's output pipe; if the text of a profile task's result can reach a terminal
that `pr_opened` reads; which hook events and stream fields the pinned Claude Code version has;
a running install.
