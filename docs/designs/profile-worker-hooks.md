# Hooks, plugins and the local MCP server for profile workers

A proposal for decision. This is the design for #6671, a part of the epic #6526. It says what a
profile worker gets in place of the platform agent's five Hermes plugins and its local MCP
server. The decider answers each row of [Decisions](#decisions) with yes or no. Memory recall is
not in this design; it belongs to the memory work (#2903).

Related designs: [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) (the persona format and
the per-task configuration directory), [`spec-subagent-profiles.md`](spec-subagent-profiles.md)
(the MCP relay rule and the activity artifact),
[`platform-as-profile-inventory.md`](platform-as-profile-inventory.md) (blocker 11). Other work
owns a part that this design only names: the task keys of the log line of a tool call and where
it is shipped (telemetry and log shipping, #6674), where durable state lives (#6669), the dial
and post-merge state (#6673), how a task posts to chat (#6668), reads across the fleet (#6667),
what a profile reaches on the broker (#6670), delegation from a profile worker (#6789), and a
plugin surface for agents (#1726). A design is written for five of these parts: the broker
surface (#7120), the state homes with the dial and post-merge state (#7123), the unattended write
permit (#7130), telemetry and log shipping (#7250) and reads with no cluster (#7260).

## What the platform agent has today

Five Hermes plugins are enabled in `agents/platform/config.yaml`:

| Plugin             | What it registers                                                             | What it does                                                                                                                                                                                                                                                 | Can it refuse or change                                                                                    | Who reads its output                                                                                                                 |
| ------------------ | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `pr_opened`        | `transform_terminal_output`, `pre_tool_call`                                  | Reads the `proposal-opened` line in terminal output, claims the proposal at the broker with the line's one-time token, writes the pull-request ledger and a post-merge record to the Session KV, and adds a `pr_opened` event to the kanban card             | Yes: it removes the token from the output, and it blocks `submit_suggestion.py` after the card's run ended | Among them: the chat notifier, the usage counters, the post-merge check, the autonomy dial, incident fix lookups, the board snapshot |
| `incident_context` | `pre_gateway_dispatch`                                                        | Adds incident context to an inbound chat message. It also records an `apply` that a person writes in an incident's thread                                                                                                                                    | It rewrites the message; it touches no tool call                                                           | The model; the incident's recurrence hold                                                                                            |
| `eval_run_tag`     | A model-request middleware, only with the eval switch on                      | Adds two headers to each model request                                                                                                                                                                                                                       | No                                                                                                         | The replay proxy                                                                                                                     |
| `hermes_otel`      | Not in this repository: a third-party plugin that the image installs          | Exports spans and metrics (the kinds are not checked: the source is not in this repository)                                                                                                                                                                  | Not known                                                                                                  | The OTLP collector                                                                                                                   |
| `tool_call_audit`  | `pre_tool_call`, `post_tool_call`, two approval hooks, `pre_gateway_dispatch` | Appends one JSON line for each event to the profile's `audit.jsonl`. A tool line holds the session id, the tool's name, the redacted arguments and the result. Other lines hold an approval request, its answer, and a dispatched message with its principal | No                                                                                                         | The fluent-bit sidecar and Cloud Logging, the admin console, the skill-invocation metrics                                            |

The local MCP server `platform_control` (`agents/platform/scripts/platform_mcp_server.py`) is a
stdio child of the agent with 20 tools: six cluster and Config Connector reads, three reads of
the fleet scope and of Hermes profile directories, two chat tools (`send_notification`,
`report_to_chat`), seven findings-queue tools, the first-run report and the alert history. The
six reads run the credential-proxy shims in the sandbox. `send_notification` runs `a2a notify`
for a platform that the gateway holds and `hermes send` for another platform; it reads the
thread from the Session KV and writes the incident record there. The other tools read the agent
pod's files or call the loopback Session KV (reads and writes), which this server also starts;
`get_first_run_report` reads the report from the sandbox's volume. No tool calls the forge.

The AgentPlugin CRD installs a Hermes plugin image into the PlatformAgent pod, in the default
Hermes profile or a named one. It also carries a `config` text, which the operator merges into
that profile's Hermes configuration, and an `env` list. Two AgentPlugins ship in `agentplugins/`:

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
- MCP processes. In a profile worker, the harness starts one stdio child for each relay server
  that the persona names: the adapter's own proxy to the broker. No other MCP server starts. The
  adapter's delegate tool is a stdio server of a session pod only: the operator sets
  `A2A_DELEGATE_TOOL=off` on each profile worker.
- The worker's configuration. Each task gets a new configuration directory. The worker writes
  `settings.json` there as an empty object, read-only, and starts the harness with
  `--setting-sources user` and `--strict-mcp-config`. No code of the worker writes a `hooks` key
  into it. (The hermes-bridge adds a `hooks.outbound` entry to the Hermes configuration of the
  platform agent; that path does not reach a profile worker.)
- The worker's process. The harness and each child of it run as the adapter's user, with no
  sandbox between them. They can read the task credential file and the broker token.
- Refusals. No platform code runs before a tool call. The harness refuses a tool that the
  persona does not grant (`--disallowedTools`), and the broker refuses a brokered call. After a
  kubectl write that the broker's policy refused (`kubernetes.read-only` or
  `caller.kubectl-flag`), the adapter stops the harness. A refused version-control write does
  not stop it. A worker has no approval prompt: the harness runs headless.
- The event stream. The adapter reads the harness's stream through a driver. For each tool call
  it publishes one `activity` entry on the bus (the tool's name and input, and the call's id
  where the driver reads one). The publish is best-effort. The event for a tool result of the
  Antigravity driver carries no field: no id, no error flag, no result, no duration. The event of
  the core-agent driver carries the tool, the text and a failed flag, and not the call's id.
  Since row 2 was built, the event of the Claude Code driver carries the call's id and the error
  flag, and the adapter writes the log lines of [The per-tool record](#the-per-tool-record) for
  the tool calls of a profile task. The adapter writes no file for a tool call.
- The broker's records. An `/v1/exec` call leaves a start record and an outcome record, each
  with the task id and the credential's id in its principal and with no argument. An MCP relay
  call leaves two lines. A `/v1/vcs/` call that succeeds leaves a metric and the HTTP request
  line, with no task and no repository; a profile task's `publish`, `proposal-create` or
  `proposal-update` also leaves the line of row 6. The broker keeps in memory which task made
  which branch and proposal; only its own checks read that record, and a restart loses it.
- The opened pull request. The broker adds a one-time claim token to a `proposal-create` answer
  that names the proposal's url and number, for each role but the profile role (row 7). The
  version-control client in the worker image prints the `proposal-opened` line on stderr, with
  no token for a profile task. The profile role has no `proposal-claim` verb. The gateway
  renders a `notice` artifact ("a PR it opened"); nothing produces one.
- Telemetry. A worker pod emits no trace and no metric. Its one structured per-tool log is the
  record of row 2.
- The second harness. The AgentProfile accepts the harness type `claude-code` only, and the
  persona format has no Antigravity adapter. So a hook can be rendered for one harness of a
  profile worker today. [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) says that
  nothing can require one harness's feature, so a record that the platform needs cannot depend
  on a persona hook.
- Skills from AgentPlugins. No persona holds the stockout investigator's skill.
  [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) plans shared skill packs that mount
  as plugins (#1726). No code does that.

## Options

| Option                                                                                               | What it needs                                                                                                                                 | What the facts say                                                                                                                                                                                                                                                                                                                                              |
| ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A. Hooks declared in the persona manifest, rendered by the worker into `settings.json`               | A `hooks` key in the manifest, or a key below the `harness` hints map; a renderer for each harness; a rule for which commands a hook can name | One harness only. A hook is the persona author's code that runs on each tool call with no decision of the model, can refuse or change the call, and is not in the event stream. A skill script is the same author's code beside the same credential, but the model starts it as a tool call that the adapter sees. No need of the five plugins requires a hook. |
| B. No hooks. The records that the platform needs are fixed code in the worker adapter and the broker | Driver and adapter work for a structured per-tool log line; a log line for an opened proposal at the broker                                   | The records do not depend on a persona or on a hook feature of a harness. They depend on fields of the harness's stream (the call id and the error flag of a tool result), which are not checked against the pinned version. Each harness needs its driver work, and one driver can run a profile task today. The per-tool line is what the harness reports.    |
| C. Hooks declared on the AgentProfile by the install's admin, from an allowed image                  | A CRD field, the operator, the worker's renderer, an image rule                                                                               | One harness only. It serves an install's own need. The five plugins give no such need, and the plugin surface for agents has its own track (#1726).                                                                                                                                                                                                             |

Recommended: B. Issue #6671 asks, for each need, for a declared mechanism or for an equivalent
built into the worker or the broker. The facts support the second form: no need of the five
plugins was found that only a hook can meet. For three fields of the opened proposal the source
is not decided (see [The opened proposal](#the-opened-proposal)).

## Decisions

In rows 2, 3 and 6, "record" means a log line that a component writes. It is not stored state.
For a row with "none" under Where, a "no" means that the need gets a design of its own.

The security approver (bnaylor) decided rows 1, 3, 6, 7 and 15 on 2026-11-26
([the comment](https://github.com/gke-labs/kube-agents/pull/7253#issuecomment-7180059137)).
His decision A is row 6, B is row 7, C is row 3, and D is rows 1 and 15. The other rows wait
for an answer.

| Id  | Decision                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Where                                                                                                   |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| 1   | No hook mechanism for profile workers. `settings.json` stays empty and the worker's own, and the persona format gets no `hooks` key. This follows the dispatch spec's rule that no persona depends on one harness's feature. Decided: yes.                                                                                                                                                                                                                                               | `a2a/lib/persona/README.md` says so; the tests stay                                                     |
| 2   | The per-tool record. The worker adapter writes one structured log line when a tool call starts and one when its result is in: the task id, a call id, the tool's name, the status and the duration. It follows the approver's decision on the telemetry design (one line for a tool call, #7250) and the decision on row 3. See [The per-tool record](#the-per-tool-record).                                                                                                             | `a2a/worker-adapter` (the drivers and the adapter)                                                      |
| 3   | The per-tool record holds no input and no result. It holds the skill's name as a field of its own when the tool is the harness's skill tool. Decided: yes. See [The per-tool record](#the-per-tool-record) for what the readers get.                                                                                                                                                                                                                                                     | the same                                                                                                |
| 4   | The approval lines and the message line of `tool_call_audit` have no counterpart on a worker. A worker has no approval prompt. The message line records an inbound chat message at the Hermes gateway; a worker receives a task, not a chat message. A record of the chat message is a front-door row (#6538).                                                                                                                                                                           | none                                                                                                    |
| 5   | The `activity` artifact stays as it is. It is a live view on the bus and can lose an entry, so it is not the record. The subagent spec says today that it is "always in the audit replay".                                                                                                                                                                                                                                                                                               | the `activity` row of the artifact table of `spec-subagent-profiles.md`                                 |
| 6   | The broker's line for a write. The source of "this task opened proposal N" is the broker: when a profile task's `publish`, `proposal-create` or `proposal-update` succeeds, the broker writes one audit log line (not stored state). Decided: yes, for the three write verbs. See [The opened proposal](#the-opened-proposal) for what the line does not hold and what it does not decide.                                                                                               | `agents/platform/scripts/credential_proxy.py`                                                           |
| 7   | No claim for profiles. The broker puts no claim token in a `proposal-create` answer to the profile role, and the profile role gets no `proposal-claim` verb. The line of row 6 is in place of the claim. The broker-surface work (#6670) records this result for the verb. Decided: yes.                                                                                                                                                                                                 | `agents/platform/scripts/credential_proxy.py`                                                           |
| 8   | The chat notice of an opened proposal. The gateway's `notice` artifact is the place. If a profile task uses it, and which component produces it, is a decision of the chat delivery work (#6668).                                                                                                                                                                                                                                                                                        | none here                                                                                               |
| 9   | Traces. A span for a session, a turn and a tool call comes from the adapter's event stream, not from a hook. What is exported, and where, is the telemetry work (#6674). A metric counterpart is also that work.                                                                                                                                                                                                                                                                         | none here                                                                                               |
| 10  | `incident_context` has no profile counterpart. It rewrites an inbound chat message before dispatch and records an `apply`; both are on the side of the session (#6538) and the incident store.                                                                                                                                                                                                                                                                                           | none                                                                                                    |
| 11  | `eval_run_tag` has no profile counterpart now. The tag is for one card or session. If the replay proxy must tag a profile worker's model requests, the adapter sets the headers from the task.                                                                                                                                                                                                                                                                                           | none now                                                                                                |
| 12  | The refusal of `submit_suggestion.py` after a run ended has no profile counterpart. The adapter stops the harness and its process group when the task has its result. The broker's permit gate holds each write of a profile task; no component revokes the task credential before it expires.                                                                                                                                                                                           | none                                                                                                    |
| 13  | No MCP server with logic or state of its own in a profile worker. The worker's one fixed stdio server stays: the adapter's proxy to a broker relay. A persona adds none. The delegate tool is off in a profile worker today; the delegation work (#6789) decides if it starts there. The rule stays as the profile spec has it: a persona names MCP servers on the broker's relay only. Which server and which tools a profile reaches is a decision of the broker-surface work (#6670). | one comment of `spec-subagent-profiles.md` (the worked `platform` profile lists `mcp-platform_control`) |
| 14  | The AgentPlugin CRD does not reach profile workers. See [What an AgentPlugin carries](#what-an-agentplugin-carries) for where each part goes. The code of an install's own plugin has no counterpart there; a yes on this row accepts that loss for work that moves to a profile.                                                                                                                                                                                                        | none; each move is its own change                                                                       |
| 15  | A declared hook is not built now, and this design does not close it. A later design can open it when three things exist: a need that rows 2 to 12 do not meet; a second harness with a hook mechanism, or the decision that one harness is enough, which changes that rule of `spec-profiles-dispatch.md`; and a rule for whose code a hook is. Decided: yes, it stays open for a later design.                                                                                          | none now; the plugin track (#1726) is the home                                                          |

### The per-tool record

The adapter's per-tool line is the one line for a tool call. The hooks design
(`profile-worker-hooks.md`) owns its source and its fields; the telemetry design
([`profile-pod-telemetry.md`](profile-pod-telemetry.md)) owns its task keys and how it ships.
This line is the trail of the telemetry design, and the adapter copies no activity entry to
stderr.

What the drivers get. The Claude Code driver and the adapter have it; the two other drivers do
not have it today:

- The Claude Code driver reads the call's id on the tool call, and the same id and the error
  flag on the tool result. The result event carries them to the adapter.
- The adapter measures the duration between the two events. The Claude Code driver reads no
  duration for one tool.
- An Antigravity driver gets the same from its stream. A tool step has a step index (today it
  only removes a duplicate), an end state of DONE or ERROR with an error type and message, and a
  duration. The driver maps the two end states to one event today and reads none of these
  fields. A denied call is a step that ends in ERROR, and it is also in the list of denied
  actions at the end. Antigravity cannot run a profile task today.
- The core-agent driver has more today: its result event carries the tool and a failed flag,
  and its wire has the call's id, which the driver does not pass on. The AgentProfile does not
  accept that harness type today.

Limits of the record:

- As built, a call does not always leave both lines. A call with no result event (a stopped
  harness, a cancel, a deadline) leaves its start line only. Events that the adapter drains
  after an abandon, or after it stops the harness at a write refusal, leave no line. An end line
  has no duration when its call id was cut for the line or when more than 1024 calls wait for a
  result.
- The harness reports it. The harness and each tool run as one user, so treat a line as one
  that a process in the worker can omit or forge. For a `/v1/exec` call and an MCP relay call,
  the broker's record is the one that counts. For a `/v1/vcs/` call the broker's record is the
  line of row 6, for each write verb that succeeds; a read verb leaves none.
  On the platform agent, the tool commands run in a different pod and cannot write
  `audit.jsonl`.
- The admin console and the skill-invocation metric read the `audit.jsonl` of the platform
  agent. If the console reads a profile task's lines, which is a question of the telemetry work
  (#6674), it shows a tool call with its status and without its arguments; the status comes
  from the error flag, not from the result.
- Proposed on this decided row, for the approver's answer: a bound on the skill's name. The name
  comes from the tool's input. The adapter writes it only when it is the name of a skill of the
  task's persona, and it writes `other` for each other value. So the field holds no free text.
- The metric reads the skill's name from the arguments of the Hermes tool `skill_view` today.
  For a profile task it will read the skill's name field of the adapter's line. That reader
  change is part of this group.

### The opened proposal

The line of row 6 holds the task id, the credential's id, the repository, the number and the
url. For `publish` it holds the branch in place of the number and the url. The three write
verbs go through one success path of the broker's dispatch. Today a profile task opens a
proposal only through `proposal-create`, from a branch that the same task made (the broker's
per-task bar). Row 8 of the unattended write permit design (#7130) puts a schedule bar in
place of the per-task bar for an unattended write. A later run of a schedule can then update
a proposal that an earlier run opened. With a line for each write verb, each run that
changes the proposal leaves a line of its own.

The line does not hold three fields that the post-merge record and the ledger hold today: the
finding's target, which the caller sets and never sends to the broker; the origin; and the chat
thread of the card. The home of the ledger and of the post-merge record, their readers and a
source for these three fields are decisions of the state work (#6669, and #6673 for the
post-merge record). This design gives that work one source fact and no more.

Row 6 is an audit line of the broker. It is not the post-merge record and not the ledger count;
the form of those is a question of the state work, and the decision on row 6 does not answer it.

On the platform agent the claim proves that a `proposal-opened` line in terminal output names a
proposal that the broker made. For a profile task the broker writes the line itself, so nothing
needs that proof.

### What an AgentPlugin carries

| Part                                                                   | Example                                           | Where it will go                                                                                                                                                                                                              |
| ---------------------------------------------------------------------- | ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A skill                                                                | The stockout investigator's skill                 | A persona artifact. No persona holds it today. Its first step posts to chat, so the move waits for the chat delivery work (#6668)                                                                                             |
| An ingress route or a platform adapter                                 | The Pub/Sub subscription route; `pubsub-platform` | The event-trigger path into profile tasks. The inventory puts it in the #6528 group and records no bus or trigger counterpart; the issue's list does not name it                                                              |
| `config`, merged into a Hermes profile's configuration                 | The `platforms` block of the stockout plugin      | No counterpart. A profile's configuration is the AgentProfile and the persona                                                                                                                                                 |
| `env` and a Secret                                                     | The optional service-account key                  | No counterpart. A worker holds no long-lived secret; a credentialed call goes through the broker                                                                                                                              |
| The plugin's own code: the tools and hooks that it registers in Hermes | An install's own AgentPlugin image                | No counterpart on a profile worker. The plugin surface for agents (#1726) is its home. Until that exists, the plugin keeps running on the Hermes path of the platform agent, and work that moves to a profile runs without it |

Row 14 is about the AgentPlugin CRD only. It does not replace the line of
[`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) that plans shared skill packs as
plugins; that plan stays with the plugin track (#1726).

### Where the need of each `platform_control` tool goes

| Tools                                                                       | Need on a profile                                                                          | Where it goes                                                                                                                                                                                                                                                       |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `verify_gke_cluster`, the four Config Connector tools, `audit_log_searcher` | Cluster and cloud reads                                                                    | The profile's brokered commands (#6670)                                                                                                                                                                                                                             |
| `fleet_scope`                                                               | The projects in scope, the declared projects that were not read, and the excluded clusters | Not decided here. The tool reads the Cluster Agent reconcile's snapshot (`fleet_scope.json` on the agent pod's volume) and, with no snapshot, the operator's render of `spec.scope`. The inventory lists it under blocker 4 (#6667); #6669 if a state home gives it |
| `list_cluster_profiles`, `get_cluster_profile_name`                         | None: they read Hermes profile directories                                                 | Not needed                                                                                                                                                                                                                                                          |
| `send_notification`, `report_to_chat`                                       | A chat post from a task                                                                    | The chat delivery work (#6668)                                                                                                                                                                                                                                      |
| The seven findings tools, `get_first_run_report`, `get_alert_history`       | Durable state of the agent pod                                                             | The state work (#6669)                                                                                                                                                                                                                                              |

In each row the surface that the model calls is a brokered command, a broker route or a tool of
a broker relay. The named work decides which one. None is a new process in the worker.

Two parts of the tools have no destination here: the neutralising of cluster text in a tool's
answer, and the incident record that `send_notification` writes for its thread. The named work
decides each.

## What stays out

- Memory recall and a memory provider (#2903).
- The task keys and the shipping of logs and traces, and what a compliance reader needs in them
  beyond the fields of rows 2 and 3 (#6674).
- The homes of the ledger and the findings queue (#6669) and of the post-merge record (#6673).
- The broker's relay rows and verbs, apart from row 7 (#6670).
- The home of the `fleet_scope` read (blocker 4, #6667; a state home, #6669, is the other
  candidate).
- A delegate tool for a profile worker (#6789).
- A persona adapter for Antigravity. When one exists, rows 2 and 9 need its driver work, as
  [The per-tool record](#the-per-tool-record) says.
- Hooks and gates outside the five plugins. The inventory has five such rows, and none needs a
  hook in a profile worker. The `bootstrap_onboarding` plugin (a `pre_llm_call` hook on the
  first turn of a person) and the `chat_message_audit` and `chat_feedback_reactions` hooks are
  front-door rows (#6538). The approval gate for unattended runs (`cron_risk_gate`) has no
  counterpart, because a worker has no approval prompt; the persona's tool list, which the
  adapter renders as the harness's allow list and deny list, and the broker hold an unattended
  run. The web-search cap and the stream deadline (`max_web_searches` under
  `tool_loop_guardrails`, `stale_timeout_seconds`) are run limits of the worker, not hooks; they
  need an owner outside this design.

## Done when

For the group issue (#6671):

1. Each row of [Decisions](#decisions) has the decider's answer.
2. If row 2 has a yes: the worker adapter writes the per-tool record for each driver that can
   run a profile task, with tests for a success, an error, two parallel calls and the skill's
   name field of row 3.
3. The broker writes the line of row 6 for `publish`, `proposal-create` and `proposal-update`,
   with a test for each verb that a success writes one line and that a refusal writes none.
4. The broker's answer follows row 7, with a test for the profile role and one for another
   role.
5. In the inventory, each row for the five plugins, the `platform_control` server and the
   AgentPlugin CRD names its destination: a row of this design, another issue, or "not needed".
   That includes `fleet_scope` and the run limits that have no owner here.
6. The persona format's page says that a persona declares no hook, and why.
7. [`spec-subagent-profiles.md`](spec-subagent-profiles.md) follows rows 5 and 13: its artifact
   table says that the `activity` artifact is a live view and that the per-tool record is the
   adapter's log line, and the comment of its worked `platform` profile does not list
   `mcp-platform_control`.
8. The memory ask of the issue is recorded as moved to #2903.
9. The skill-invocation metric counts a skill of a profile task from the skill's name field of
   the adapter's line, with a test.

## Checked, and not checked

Checked in the code on `main` at `be60d590d`: the list of enabled plugins; the hooks that
`pr_opened`, `incident_context` and `tool_call_audit` register and the middleware of
`eval_run_tag`; the block action of `pr_opened` and its removal of the token; the fields of an
`audit.jsonl` line and the two readers that use the arguments and the result; the count of
`platform_control` tools and what each group calls; the two sources of the `fleet_scope` read;
the fields of the AgentPlugin CRD and the parts of the two shipped AgentPlugins; the strict
decode of the manifest and the refusal of `hooks` in a skill; the empty `settings.json` and the
two harness flags; that the harness runs as the adapter's user; the `--disallowedTools` list,
the headless start and the stop after a refused kubectl write; the one stdio MCP server that a
profile worker starts, and that the pool turns the delegate tool off; the `activity` entry of a
tool call and the tool-result event of each driver (the core-agent driver came to `main` after
that commit and was read at `ca2fe48e4`); the fields of a tool step in
the recorded Antigravity stream; the broker's records for `/v1/exec` and the MCP relay; the
broker's in-memory record and the claim token at `proposal-create`; that the worker image's
client prints the token and that no worker code removes it; the profile's relay tools; the
harness type that the AgentProfile accepts and the missing Antigravity adapter; that no profile
controller names an AgentPlugin; that the worker's code names no trace or log shipper; that
nothing in the bus code produces a `notice` artifact.

Not checked: the hooks, the span kinds and the metrics of `hermes_otel` (its source is not in
this repository); each reader of the `pr_opened` event and of `audit.jsonl` (taken from a code
search, not a complete list); that no log line of the broker carries the task for a `/v1/vcs/`
call that succeeds (none was found by search; the success path of the version-control broker was
not read in full); if `proposal-create` can answer with a proposal that exists already; if a
process in the worker can write to the harness's output pipe; that the pinned harness returns the
stderr of a shell call to the model; if the text of a profile task's result can reach a terminal
that `pr_opened` reads; how a worker's line reaches the reader of the skill metric, which follows
the `audit.jsonl` files on the agent's volume today; which hook events and stream fields (the
call id, the error flag) the pinned Claude Code version has, because the repository has
hand-written fixtures only; if Antigravity has a hook mechanism; a running install.
