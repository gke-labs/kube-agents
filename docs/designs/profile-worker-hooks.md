# Hooks, plugins and the local MCP server for profile workers

A proposal for decision. It says what a profile worker gets in place of the platform agent's five
plugins and its local MCP server. The decider answers each row of [Decisions](#decisions) with
yes or no. Memory recall is not in this design; it belongs to the memory work (#2903).

Related designs: [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) (the persona format and
the per-task configuration directory), [`spec-subagent-profiles.md`](spec-subagent-profiles.md)
(the MCP relay rule and the activity artifact),
[`platform-as-profile-inventory.md`](platform-as-profile-inventory.md) (blocker 11). Three other
designs own a part that this one only names: where a record is shipped (telemetry and log
shipping, #6674), where durable state lives (#6669), and what a profile reaches on the broker
(the broker-surface design for blocker 7).

## What the platform agent has today

Five plugins are enabled in `agents/platform/config.yaml`:

| Plugin             | What it registers                                                             | What it does                                                                                                                                                                                                      | Can it refuse or change                                          | Who reads its output                                                                                 |
| ------------------ | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `pr_opened`        | `transform_terminal_output`, `pre_tool_call`                                  | Reads the `proposal-opened` line in terminal output, claims the proposal at the broker, writes the pull-request ledger and a post-merge record to the Session KV, and adds a `pr_opened` event to the kanban card | Yes: it blocks `submit_suggestion.py` after the card's run ended | The chat notifier, the usage counters, the post-merge check, the autonomy dial, incident fix lookups |
| `incident_context` | `pre_gateway_dispatch`                                                        | Adds incident context to an inbound chat message                                                                                                                                                                  | It rewrites the message; it touches no tool call                 | The model                                                                                            |
| `eval_run_tag`     | A model-request middleware, only with the eval switch on                      | Adds two headers to each model request                                                                                                                                                                            | No                                                               | The replay proxy                                                                                     |
| `hermes_otel`      | Not in this repository: a third-party plugin that the image installs          | Exports session, tool-call and MCP spans                                                                                                                                                                          | Not known                                                        | The OTLP collector                                                                                   |
| `tool_call_audit`  | `pre_tool_call`, `post_tool_call`, two approval hooks, `pre_gateway_dispatch` | Appends one JSON line for each event to the profile's `audit.jsonl`                                                                                                                                               | No                                                               | The fluent-bit sidecar and Cloud Logging, the admin console, the skill-invocation metrics            |

The local MCP server `platform_control` (`agents/platform/scripts/platform_mcp_server.py`) is a
stdio child of the agent with 20 tools: six cluster and Config Connector reads through the
sandbox, three reads of the fleet scope and of Hermes profile directories, two chat tools
(`send_notification`, `report_to_chat`), seven findings-queue tools, the first-run report and
the alert history. None calls the forge or the broker. Each reads the agent pod's own files, the
loopback Session KV, or the sandbox.

The AgentPlugin CRD installs a Hermes plugin image into the PlatformAgent pod, in the default
Hermes profile or a named one. Nothing connects it to an AgentProfile worker.

## What a profile worker has today

- The persona. The manifest is decoded strictly, so an unknown key fails. A skill's frontmatter
  holds four inert keys; a skill that carries `hooks` or `allowed-tools` is refused. The
  `harness` hints map exists, and the Claude Code adapter ignores each key with a warning. An MCP
  server is a broker relay by name; the format has no direct-URL kind.
- The worker's configuration. Each task gets a new configuration directory. The worker writes
  `settings.json` there as an empty object, read-only, and starts the harness with
  `--setting-sources user` and `--strict-mcp-config`. No production code writes a `hooks` key.
- The worker's process. The harness and each child of it run as the adapter's user, with no
  sandbox between them. They can read the task credential file and the broker token. Nothing in
  the worker refuses a tool call; the broker refuses a brokered call.
- The event stream. The adapter reads the harness's stream on both drivers. For each tool call
  it publishes one `activity` entry on the bus (the tool's name and input). The publish is
  best-effort, and the entry has no result, status or duration. The adapter writes no log line
  and no file for a tool call.
- The broker's records. An `/v1/exec` call leaves one audit record with the task id and the
  credential's id, with no argument. An MCP relay call leaves one line. For a `/v1/vcs/` call
  that succeeds, no line with the task or the repository was found. The broker keeps in memory
  which task made which branch and proposal; only its own checks read that record, and a restart
  loses it.
- The opened pull request. The version-control client in the worker image prints the
  `proposal-opened` line. Nothing in the worker reads it, and the profile role has no
  `proposal-claim` verb. The gateway renders a `notice` artifact ("a PR it opened"); nothing
  produces one.
- Telemetry. A worker pod emits no trace, no metric and no structured per-tool log.
- The second harness. The AgentProfile accepts the harness type `claude-code` only, and the
  persona format has no Antigravity adapter. So a hook mechanism can be defined for one harness
  today.

## Options

| Option                                                                                               | What it needs                                                                                                | What the facts say                                                                                                                                                                                 |
| ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A. Hooks declared in the persona manifest, rendered by the worker into `settings.json`               | A `hooks` key in the strict manifest; a renderer for each harness; a rule for which commands a hook can name | One harness only. A hook is the persona author's code on each tool call, beside the task credential. No need in the table below requires it.                                                       |
| B. No hooks. The records that the platform needs are fixed code in the worker adapter and the broker | A structured per-tool record from the adapter's event stream; a record of an opened proposal at the broker   | Both sources exist: the adapter sees each tool call on both drivers, and the broker already records the proposal that a task made. The records do not depend on a persona or on a harness feature. |
| C. Hooks declared on the AgentProfile by the install's admin, from an allowed image                  | A CRD field, the operator, the worker's renderer, an image rule                                              | One harness only. It serves an install's own need; the inventory names no such need.                                                                                                               |

Recommended: B. The plan for this epic named "declared hooks" as the first step. The facts do
not support that step: each need of the five plugins has a source that is not a hook.

## Decisions

| Id  | Decision                                                                                                                                                                                                                                                                                                                                                 | Where                                              |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| 1   | No hook mechanism for profile workers. `settings.json` stays empty and the worker's own, and the persona format gets no `hooks` key.                                                                                                                                                                                                                     | none; the tests that hold this stay                |
| 2   | The per-tool record. The worker adapter writes one structured log line when a tool call starts and one when its result is in: the task id, the tool's name, the status and the duration. The drivers carry what that needs (an id that pairs the two events, and the error flag). The input is not in the line, as it is not in the broker's record.     | `a2a/worker-adapter` (the drivers and the adapter) |
| 3   | The `activity` artifact stays as it is. It is a live view on the bus and can lose an entry, so it is not the record.                                                                                                                                                                                                                                     | none                                               |
| 4   | The opened proposal. The source of "this task opened proposal N" is the broker: when a profile task's `proposal-create` succeeds, the broker writes one audit record with the task id, the credential's id, the repository and the number. The home of the ledger and of the post-merge record, and their readers, are rows of the state design (#6669). | `agents/platform/scripts/credential_proxy.py`      |
| 5   | The chat notice of an opened proposal. The gateway's `notice` artifact is the place; which component produces it for a profile task is a row of the chat delivery work (#6668).                                                                                                                                                                          | none here                                          |
| 6   | Traces. A span for a session, a turn and a tool call comes from the adapter's event stream, not from a hook. What is exported, and where, is the telemetry design (#6674).                                                                                                                                                                               | none here                                          |
| 7   | `incident_context` has no profile counterpart. It rewrites an inbound chat message before dispatch, which is on the side of the session and the incident store.                                                                                                                                                                                          | none                                               |
| 8   | `eval_run_tag` has no profile counterpart now. If the replay proxy must tag a profile worker's model requests, the adapter sets the headers from its own environment.                                                                                                                                                                                    | none now                                           |
| 9   | The refusal of `submit_suggestion.py` after a run ended has no profile counterpart. A profile task's credential ends with the task, and the worker removes the token file when the task ends.                                                                                                                                                            | none                                               |
| 10  | No local MCP server process in a profile worker. The rule stays: a profile's MCP servers are broker relays. The broker-surface design owns the relay's rows (which server and which tools a profile reaches); this design does not change them. The table below says where the need of each `platform_control` tool goes.                                | none                                               |
| 11  | The AgentPlugin CRD does not reach profile workers. A skill that an AgentPlugin ships today (for example the stockout investigator) reaches a profile through its persona artifact.                                                                                                                                                                      | none; the plugin's move is its own change          |
| 12  | Option for later, not in this design: a declared hook. It needs first: a need that rows 2 to 9 do not meet; a second harness with a hook mechanism, or the decision that one harness is enough; and a rule for whose code a hook is (the persona's author or the install's admin). Answer "later" or "no".                                               | none now                                           |

### Where the need of each `platform_control` tool goes

| Tools                                                                       | Need on a profile                          | Where it goes                                                      |
| --------------------------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------ |
| `verify_gke_cluster`, the four Config Connector tools, `audit_log_searcher` | Cluster and cloud reads                    | The profile's brokered commands; rows of the broker-surface design |
| `fleet_scope`                                                               | The clusters in scope                      | Reads with no cluster (#6667)                                      |
| `list_cluster_profiles`, `get_cluster_profile_name`                         | None: they read Hermes profile directories | Not needed                                                         |
| `send_notification`, `report_to_chat`                                       | A chat post from a task                    | The chat delivery work (#6668)                                     |
| The seven findings tools, `get_first_run_report`, `get_alert_history`       | Durable state of the agent pod             | The state design (#6669)                                           |

## What stays out

- Memory recall and a memory provider (#2903).
- The shipping of logs and traces, and the fields that a compliance reader needs in them (#6674).
- The homes of the ledger, the post-merge record and the findings queue (#6669).
- The broker's relay rows and verbs, also `proposal-claim` (the broker-surface design).
- A persona adapter for Antigravity. When one exists, rows 2 and 6 hold for it with no change,
  because they read the adapter's own event stream.
- The claim line that the version-control client prints in a worker. Nothing claims it there;
  the broker-surface design decides `proposal-claim` for profiles.

## Done when

For the group issue of this design:

1. Each row of [Decisions](#decisions) has the decider's answer.
2. The worker adapter writes the per-tool record of row 2 on both drivers, with tests.
3. The broker writes the record of row 4, with a test that a refused `proposal-create` writes
   none.
4. Each row of the inventory for the five plugins, the `platform_control` server and the
   AgentPlugin CRD names its destination: a row of this design, another design's issue, or "not
   needed".
5. The persona format's page says that a persona declares no hook, and why.

## Checked, and not checked

Checked in the code on `main` at `be60d590d`: the list of enabled plugins; the hooks that
`pr_opened`, `incident_context` and `tool_call_audit` register and the middleware of
`eval_run_tag`; the block action of `pr_opened`; the count of `platform_control` tools; the
strict decode of the manifest and the refusal of `hooks` in a skill; the empty `settings.json`
and the two harness flags; that the harness runs as the adapter's user; the `activity` entry of a
tool call; the broker's in-memory record at `proposal-create`; the profile's relay tools; the
harness type that the AgentProfile accepts and the missing Antigravity adapter; that no profile
controller names an AgentPlugin; that the worker's code names no trace or log shipper; that
nothing in the bus code produces a `notice` artifact.

Not checked: the hooks of `hermes_otel` (its source is not in this repository); each reader of
the `pr_opened` event and of `audit.jsonl` (taken from a code search, not read in full); that no
log line of the broker carries the task for a `/v1/vcs/` call that succeeds (none was found);
which hook events the pinned Claude Code version has; a running install.
