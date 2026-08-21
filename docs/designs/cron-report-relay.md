# Scheduled-report relay: the specialist reasons, the Chat Agent speaks

**Status:** implemented and validated end to end on a live GKE cluster; every job
on [the Platform Agent's roster](../../agents/platform/cron/README.md) delivers
this way.

## The problem

A watchdog on a named profile runs with the right brain and no voice.

`profile-cron-tick` fires the specialist's job under its own `HERMES_HOME`, so it
keeps that profile's persona, skills, model and turn budget — which is the whole
reason the watchdogs live on the Platform Agent's roster rather than as kanban
cards. What it does not get is a way to say anything useful to the person who
cares.

The report arrives as a monologue from a process that has now exited. A user who
replies "why does that matter?" is talking to the Chat Agent, which never saw the
finding, and there is nothing it can do but apologise.

Getting the words into a channel _at all_ was a second and separate gap, now
closed: `HOME_TARGET_ENV_KEYS` in `profile_cron_tick.py` named the Slack keys and
nothing else, so on a Google Chat install the child resolved `deliver: "all"` to
an empty target list and posted nowhere. It names both platforms now, and
`home_target_env`'s docstring is where that story lives. Closing it does not
close this one: a channel id makes the message appear and still leaves the
follow-up unanswerable.

## The shape of the fix

Separating who reasons (the specialist) from who speaks (the Chat Agent).

The specialist keeps the work: it runs on its own schedule, in its own profile,
with its own tools, and produces a finished report. It does not deliver that
report. It hands it to the Chat Agent, which presents it in the channel and
therefore owns the conversation that follows.

The trigger is the job's own delivery setting: `deliver: "chat"`.

```
chat roster: profile-cron-tick  (no_agent, * * * * *)
        │
        └── hermes cron tick, HERMES_HOME=<profile>
                 │
                 └── specialist cron job — does the work
                          │
                          │  scheduler: _deliver_result(job, final_response)
                          │  deliver == "chat" → relay instead of resolving targets
                          ▼
                 POST /v1/cron-reports        (Session KV server, loopback 8699)
                          │
                          ├── POST /api/sessions/{sid}/chat   → the Chat Agent's turn
                          │        message = the report, system_message = relay instructions
                          │
                          ├── hermes send                     → what the Chat Agent composed
                          │
                          └── INSERT INTO incidents (chat_id, thread_id, report)
                                       │
                          user replies in the thread
                                       ▼
                          incident_context prepends the report → the Chat Agent has it
```

This is the event watcher's delivery path with the reasoning step removed. There,
an out-of-band signal starts an agent turn that investigates and reports; here
the investigation already happened and the turn only presents. The three pieces
that make an alert answerable — a thread, a session bound to it, and the report
stored against that thread — are reused unchanged.

## Why the Chat Agent composes but does not send

The Chat Agent cannot post to a chat platform out of band. Its toolset is
`mcp-router`, `kanban` and `memory`; `terminal` is on its denylist, and
`agents/chat/config.yaml` calls that denylist "the authoritative guarantee that
the front door cannot touch the system."

So the relay reads the turn's response body — `POST /api/sessions/{id}/chat`
returns `{"message": {"role": "assistant", "content": …}}` — and sends that text
itself. The alternative, giving the Chat Agent a send tool, would widen exactly
the boundary that file exists to hold, and buy nothing: the voice and the context
are the Chat Agent's either way.

The relay instruction goes in `system_message`, which the gateway applies as an
`ephemeral_system_prompt`. It steers this turn without being replayed into every
later turn of the thread, so a follow-up reaches a Chat Agent that remembers the
report and not the order to repeat it.

### Which key the loopback call presents

Not `os.environ["API_SERVER_KEY"]`, which is what every other in-pod caller of
the gateway uses and what this one used until the first live run. Three things
disagree about that name, and the environment loses:

- The operator sets it to the literal `cluster-internal-trusted`, a **non-secret
  loopback sentinel** — `platformagent_manifests.go` says so in a comment. The
  premise is that the listener is loopback-only and the envoy credential-proxy
  sidecar authenticates outside callers against `API_SERVER_EXTERNAL_KEY` before
  swapping the sentinel in.
- Hermes does not honour that premise from inside the pod. It prefers
  `$HERMES_HOME/.env` over the process environment, deliberately, so that a key
  rotation in that file is not shadowed by a stale export inherited from a parent
  process.
- Its Docker stage2 hook writes a freshly generated strong key into that file on
  every boot when the file does not already carry one.

So the gateway accepts a value the loopback caller never sees, and the sentinel
401s. Measured on `kage-management`: seven consecutive `github-repo-watcher` relay
turns rejected inside one pod's first two hours, each one degrading to an
unrelayed raw report that the scheduler still recorded as delivered.

`_gateway_api_token()` reads `.env` first and falls back to the environment, so a
deployment where nothing rewrites the key is unaffected. It reads per call rather
than caching at import, because `.env` is rewritten seconds _after_ this process
starts. The other direction — writing the sentinel into `.env` so the two agree —
was tried and is not available: the API server then declines to bind at all.

This is not specific to the relay. Every in-pod caller that trusts the
environment has the same 401, `trigger_agent_troubleshooter` included, which is
why both call sites moved.

## Why context works without an append-only endpoint

There is no way to put a message into a Hermes session's history without running
a turn: `/chat` always infers, `PATCH /api/sessions/{id}` accepts only `title`
and `end_reason`, and `/messages` is GET-only.

It turns out not to matter, because session history is not what makes the event
watcher's alerts answerable. `incident_context`
(`agents/platform/plugins/incident_context/__init__.py`) is a
`pre_gateway_dispatch` hook: on every inbound message it looks up
`(chat_id, thread_id)` in the `incidents` table and, on a hit, prepends the
stored report to the user's words before the agent sees them. Context comes from
the thread, not the session.

The relay writes that row itself rather than calling `POST /v1/incidents` over
loopback — it is that endpoint's own server, so an HTTP call to itself inside a
background task would only add a way to fail.

## The report is untrusted input

Before this change a scheduled report was inert text posted into a channel: it
entered no agent's context. Now it is the message of a real Chat Agent turn, and
the stored copy is spliced into every later threaded reply — so what a report says
matters in a way it did not before.

The text is not the specialist's own words alone. Every audit on the roster is
required to back each finding with an `evidence.excerpt` — literal
`kubectl … -o yaml` output, trimmed to the lines that prove it
([`obtainability_audit_sop.md`](../../agents/platform/governance/obtainability_audit_sop.md),
"Evidence discipline") — so object names, labels, annotations and event text
written by whoever deploys into the fleet reach the report body verbatim.
And the receiving profile is the delegation surface rather than a bystander: its
`kanban` toolset, with `dispatch_in_gateway: true`, can file work for specialists
that hold `terminal`, `gcloud` and `kubectl`.

Both hops are framed, and deliberately not the same way, because only one of them
is reproduced for a human to read:

- **The relay turn.** The framing goes in the ephemeral system prompt — the
  trusted channel, read before the report — which names the user message as
  untrusted data, says to relay it and never act on it, and enumerates the asks
  to ignore. The body itself is touched as little as possible: only chat-template
  control tokens (`<|im_start|>` and friends) are blunted, because the Chat Agent
  reproduces this text essentially verbatim into the user's channel and a report
  about system components can legitimately contain a `### System:` heading.
- **The replay into a later reply.** `incident_context` never shows its output to
  a human, so it can be blunt: the stored report is wrapped in an
  `<untrusted_report>` fence under a `[SECURITY NOTICE: …]` header, stripped of the
  tokens that could close the fence or forge a second notice, and followed by the
  user's own words under a label saying that line alone is theirs.

The pattern is not new here —
[`platform_mcp_server.py`](../../agents/platform/scripts/platform_mcp_server.py)'s
`_sanitize_log_text` already fences pod diagnostics the same way. The token list
is duplicated in the plugin rather than imported because a gateway plugin is
loaded by file path and cannot reach the platform agent's scripts.

### Under `platformFrontDoor`

The separation this design rests on — the specialist reasons, the Chat Agent
speaks — is a property of _which profile the gateway runs as_, not of the relay.
`_run_relay_turn` POSTs to whatever `PLATFORM_API_URL` answers, and the
experimental
[`platformFrontDoor`](../site/src/content/docs/operator/platformagent-crd.md)
flag re-homes the gateway onto the platform profile.

Mechanically the relay route is unaffected: it is one more turn on one more
gateway, the session is created the same way, and `hermes send` still does the
posting. What changes is whether anything reaches the route at all, and who is
composing when it does. Three things, all worth stating rather than discovering.

- **The composer is no longer the locked-down one.** The Chat Agent's
  `platform_toolsets.api_server` is `mcp-router`, `kanban` and `memory`; the
  platform profile's is `mcp-platform_control`, `mcp-gke` and
  `mcp-developer_knowledge`, and the flag's own documentation says the lockdown
  is deliberately not copied across. So the agent reading the untrusted report
  holds fleet tools directly, where by default the worst an injected instruction
  could reach for was a kanban card. Nothing above stops working, but the
  framing is carrying more weight, and "who reasons" and "who speaks" become the
  same agent.
- **The ticker moves, and the relay loses its supply.** The `--profile` flag
  re-points `HERMES_HOME` before anything imports, and Hermes' cron ticker binds
  to whatever home its own process has. So the gateway starts
  ticking the platform store directly — the governance roster fires without
  `profile-cron-tick` in front of it — while `profile-cron-tick` itself, which
  is an entry in the `default` profile's `cron/jobs.json`, is in a store nothing
  ticks any more. It is the only thing that ever sets `CHAT_HOME_CHANNEL`, so
  with it stopped no process in the install has it. `_resolve_delivery_targets`
  then returns `[]` for every `deliver: "chat"` job — the branch
  `verify_chat_relay.py` check 8 asserts — and each governance report is
  composed at full audit cost and posted nowhere, exactly the silence
  `home_target_env` describes for `deliver: "all"`. Every _other_ named profile
  goes dark with it, for the same reason and independently of this design.
- **One known-wrong sentence stays wrong.** `cronjob(action='create')` describes
  a runtime-created relayed job as local-only because it runs in the gateway,
  where `chat` is not an enabled platform. The flag does not change that:
  enablement is a property of the _process_, not the profile — `is_connected`
  reads `CHAT_HOME_CHANNEL` out of the environment, and the gateway is
  deliberately the one process that never has it, whichever profile it wears.

The flag ships default-off and unsupported, so these are recorded as
interactions rather than handled in code: making the relay branch on the gateway
profile would couple it to a switch that may not graduate. The stopped ticker is
the one that would have to be answered before the flag could graduate, and the
fix is not in the relay: it is to keep ticking the profiles the gateway no
longer homes to.

## When the follow-up arrives outside the thread

Context comes from the thread, so a follow-up that does not carry the thread gets
none. That is not a corner case on either platform. A Google Chat reply typed
into the main compose box arrives with no `thread_id` at all; a top-level Slack
channel message arrives carrying its own `ts`, which matches no stored report.
Both leave the agent reading a bare sentence while the reports sit in the channel
above it — and it does not degrade to "I lack context". It binds to the nearest
antecedent in its own history and answers confidently about the wrong report.

`GET /v1/incidents/recent` is the floor under that. On a by-thread miss the hook
asks what was posted in this chat lately and prepends an index — job id, title,
profile, timestamp — telling the agent that these exist, that it does not have
them, and to ask which one is meant. Turning a wrong answer into a question is
the whole of the goal; retrieving the named report is not part of it.

Two properties are load-bearing:

- **Labels only, never report text.** `_store_incident_report` persists the
  relay's composed output rather than the specialist's finding, and this block is
  prepended to every unthreaded message in the space. A preview line would carry
  model-written text into all of them. `job_id`, `title` and `profile` are fields
  the Session KV server wrote itself.
- **Bounded on both axes.** A window shorter than `CLEANUP_TTL_DAYS`
  (`SESSION_KV_RECENT_REPORTS_HOURS`, default 24) and a row cap
  (`SESSION_KV_RECENT_REPORTS_LIMIT`, default 8), so the injected block costs the
  same whatever the reports weigh — a fortnight of an eight-job roster would
  otherwise tax ordinary chatter with a hundred lines.

The by-thread path is untouched: a threaded reply that finds its report behaves
exactly as before, and the index only runs where the hook previously did nothing.

## The first reply into a report's thread (Google Chat DMs only)

Google Chat opens a thread around every top-level message, so an inbound payload
cannot say whether the user posted at top level or replied inside a real thread.
Its adapter settles that by counting inbound messages per thread: a thread it has
never seen one in is read as main flow, and the bot answers in the space rather
than in the thread. Both writers of that counter live in the gateway process. A
relayed report is posted by `hermes send` from the Session KV server, which is a
different process, so a report thread stands at zero however long it sits there
and the first follow-up typed into it is answered in the main space — and starts
a second session besides. The reply after that works, because by then the user's
own first message is in the count.

**DMs only.** The heuristic is one arm of an `if chat_type == "dm"` in the
adapter's `_build_message_event`; the group arm keeps `thread_name` as the
session thread and caches it for outbound unconditionally, with the comment "For
groups, threads ARE meaningful conversational containers … always isolate AND
always reply in-thread." A space or group DM therefore never reaches the counting
branch and never misroutes. Slack is unaffected for a different reason: it sends a
real `thread_ts`, so there is nothing to infer.

The hook recovers the **context**, and only the context. On a by-thread miss it
reads `thread.name` off the raw Chat payload — the thread the user actually typed
into, which the adapter dropped — and looks that up. A stored report means the bot
opened this thread and the user has deliberately replied inside it, so the agent
gets the report it is being asked about instead of a bare sentence. With no report
for the thread the adapter's heuristic stands untouched, and groups never reach
the branch because their `thread_id` survives.

**Routing is not fixable from a plugin, and the hook deliberately does not try.**
Writing the thread back onto `event.source` looks like the fix and is not one:
`pre_gateway_dispatch` is the earliest inbound hook Hermes has, and it fires
inside `_message_handler`, which `gateway/platforms/base.py` calls only _after_ it
has snapshotted the outbound routing off that same source
(`_thread_metadata_for_source(event.source, …)`, ~30 lines earlier in the same
task); every send in that turn reuses the snapshot. Measured live on 2026-08-17:
the hook re-attached the thread and the report reached the agent, and the reply
still went to the space. The assignment would also split the conversation, keying
the session to a thread whose messages are visibly not in one. So the first
follow-up is answered _correctly_ but in the main space; the reply after that
threads normally, because by then the user's own first message is in the count.

**The misrouted first reply is an open issue, deferred rather than solved.** It is
out of scope for this change — it is an upstream Hermes defect, not something the
relay introduced — but it should be fixed, and the fix is known. It belongs in the
adapter, where routing is decided before the event is handed up: the in-process
sender (`_create_message`) already seeds the counter on outbound, with a comment
naming this exact symptom, and the out-of-process sender (`_standalone_send`, the
path `hermes send` takes) does not. Bringing the two to parity fixes every
bot-opened thread rather than the ones this repository happens to hold a report
row for — paired with a reload-on-mtime in `_ThreadCountStore.incr`, since that
store rewrites the whole dict from memory and would otherwise erase a peer
process's write. Until then the cost is one message in the wrong place per report
thread, carrying a correct answer.

## Session lifetime: one per job, per UTC day

One session per _report_ — what the watcher's `per-incident` mode does — gives a
daily watchdog a new thread every tick, so a follow-up lands in a session that
has seen one message. One session per _job_, kept forever, is the opposite
failure: every turn replays the whole history, so a job on a five-minute
schedule grows an unbounded prompt and relaying report N costs proportionally to
N.

`cron-<profile>-<job_id>-<YYYYMMDD>` sits between them. Consecutive reports from
one job share a thread, so the Chat Agent can see it is the third time today;
the history resets before it can grow without bound. Yesterday's thread does not
go dark at the rollover, because `incident_context` resolves a reply by thread
rather than by session id, and those rows live for `CLEANUP_TTL_DAYS`.

## Why not a flag on `/sessions/{id}/inject`

That route is an incident path. It classifies severity, spends `alert_quota`, and
hands the agent the triage template. A scheduled report is not an incident, and
it should not be silently dropped because a node storm spent the day's Warning
budget — the suppression there is deliberately invisible in chat, which is right
for alert volume and wrong for a watchdog that runs once a day.

`/v1/cron-reports` is therefore its own route with no severity and no quota. It
caps report length instead (`CRON_REPORT_MAX_CHARS`, default 12000), which bounds
the accident that route actually has: a job that cats a log into the model and
the channel.

## What a job has to do: nothing

A job relays because of one field. `deliver: "chat"` and no prompt boilerplate —
no instruction to call a tool, no instruction to return `[SILENT]` afterwards.

The reason a prompt contract was rejected is the job nobody writes by hand. A
user asks the Platform Agent to watch something every morning; the agent calls
`cronjob(action='create')` and invents the prompt on the spot. That prompt
carries whatever the agent remembered to put in it. Delivery that depends on a
remembered sentence is delivery that fails on exactly the jobs no reviewer ever
reads. `create_job`'s signature is fixed keywords, so `deliver` is also the only
field such a job _can_ set — which is what makes it the right place for the
switch rather than merely a convenient one.

Making it a mode is free because the scheduler has already done the work by the
time delivery is reached. `run_one_job` applies the `[SILENT]` check and, on a
failed run, substitutes `_summarize_cron_failure_for_delivery(job, error)` —
both _before_ calling `_deliver_result`. So a `deliver: "chat"` job inherits
silence-on-nothing-to-report and audible failures without asking the model for
either.

## How the switch is wired: `chat` is a platform

Nothing in Hermes is patched. Upstream already routes `deliver=<name>` through
the platform registry, and `cron/scheduler.py::_plugin_cron_env_var` says so in
its own words — a plugin that sets `cron_deliver_env_var` gets "cron delivery
support without editing this module". So the relay ships as a bundled platform
plugin, [`deploy/docker/plugins/chat/`](../../deploy/docker/plugins/chat/),
copied into `plugins/platforms/chat/` where Hermes auto-registers it.

It is a platform with no inbound side. The only hook it implements is
`standalone_sender_fn` — the one Hermes calls when cron runs in a separate
process from the gateway, which is exactly what `profile_cron_tick.py` spawns.
`adapter_factory` raises if anything ever tries to start it.

`CHAT_HOME_CHANNEL` is the whole switch, and it is set in one place:
`profile_cron_tick.py`, on the cron children it spawns. It gates enablement
(through `is_connected`) and it is the `cron_deliver_env_var` the scheduler reads
to resolve `deliver: "chat"` to a target. Unset — which is how the gateway
process runs — the enablement pass leaves the platform disabled, no adapter is
ever asked for, and `deliver: "chat"` resolves to nothing.

One thing a plugin manifest is not is documentation. `optional_env` is folded
into `OPTIONAL_ENV_VARS` under the default `category: "messaging"`, and the
subprocess scrub blocklist blocks that bucket whole — so a variable named there
is stripped from every child `build_subprocess_env` spawns, `profile-cron-tick`
included. Declaring `SESSION_KV_API_KEY` made the relay revoke its own
credential: the pod had the key, the gateway had it, and the cron child that
needed it did not. `chat/plugin.yaml` carries the rule and the two names left
out because of it.

The sender is handed the delivery text, not the job. It recovers the job's id and
name from the header `_deliver_result` wraps every cron delivery in, and posts
the report without it. That coupling is the one thing the plugin route
cannot state in code it owns, so it is asserted at image build time:
[`verify_chat_relay.py`](../../deploy/docker/plugins/verify_chat_relay.py) drives
the real `_deliver_result` against a loopback stand-in for the Session KV server
and asserts on what crossed the wire. Upstream changing the wrapper fails the
build.

### What this costs

Being a real delivery target rather than an interception has consequences, and
every one of them is visible to a job author:

- **The token is not exclusive.** `deliver: "chat,slack"` relays _and_ posts to
  Slack. A job that wants one voice names one target.
- **`deliver: "all"` includes the relay**, because `_expand_routing_tokens`
  expands to every platform with a configured home channel and the relay now has
  one. A job left on `all` therefore reports twice — once flat into the channel
  and once through the Chat Agent — on either platform, now that
  `home_target_env` restores the Google Chat channel too. So the whole Platform
  Agent roster names `"chat"` rather than relying on the expansion, which is
  also the only way to say "relay, and do not also post flat" at all: the token
  is additive, so there is no value that subtracts a target from `all`.
  No migration was needed to get there: `deliver` is
  an image-owned key on a named profile, so the entrypoint's existing cron merge
  rewrites it on every live volume at the next pod start (`agents/platform/cron/README.md`
  says which merge and why the default profile is the exception). What that
  cannot reach is a job the agent creates at runtime, which is why `AGENTS.md`
  tells it to pass `deliver='chat'`.
- **A failed relay does not fall back, and does not go quiet either.**
  `POST /v1/cron-reports` relays synchronously and answers with the outcome, so
  every leg — `hermes send` and the message id it has to parse back — reaches the
  scheduler as an error it records in `last_delivery_error`, naming the leg that
  broke; the report itself is still in the job's saved output.
  A failed **Chat Agent turn** is the one leg that is not an error, because the
  finding still belongs in the channel — but it is not a clean run either, and
  saying so only in a log is how the seven consecutive failures above went
  unnoticed. So the degradation is stated twice: the posted message is prefixed
  `[unrelayed]`, naming the profile and job, and the response body carries
  `"relay": "degraded"` next to `"status": "delivered"`.
  That is the whole reason the route blocks rather than accepting into a
  background task: answering `accepted` first would make each of those failures
  invisible, leaving the run written down as delivered with nothing in the
  channel, which is exactly the state
  [`deliver` exists to prevent](../../agents/platform/cron/README.md). Routing a
  failure to `all` would need the interception this design gives up, and it would
  post the report to a channel the job did not choose.
- **`cronjob(action='create')` calls a relayed job local-only.** `cronjob` runs
  in the gateway, where the switch is off, and `_local_delivery_notice` decides
  by asking whether the job resolves to a target _here_ — so a job the agent
  creates at runtime with `deliver: "chat"` is created correctly and described
  wrongly, with advice to use `deliver: "all"` instead. Taking `all` degrades to
  the right behaviour rather than to silence (the child expands it to the
  relay), which is why this is a wrong sentence and not a lost report. Both
  branches are asserted in `verify_chat_relay.py` so the claim stays measured.

The alternative was to set the switch process-wide and disable the platform in
the root `config.yaml` (`_enabled_explicit` is honoured by the enablement pass).
That fixes the notice and costs more than it buys: the file is runtime state the
agent writes, not something the image owns, and with the switch on in the gateway
every Chat Agent job on `deliver: "all"` starts recording "platform 'chat' not
configured/enabled" against a delivery that in fact succeeded.

`report_to_chat` stays, scoped to what the mode cannot do: reporting mid-run, or
sending something other than the final response.

## Related

- [`agents/platform/cron/README.md`](../../agents/platform/cron/README.md) — the
  roster's own rules, including why no job sets `deliver: "local"` and why an id
  must not appear on two rosters.
- [`agents/platform/docs/session_management.md`](../../agents/platform/docs/session_management.md)
  — the Session KV server, its callers and its auth.
- [`concepts/autonomous-watchdogs`](../site/src/content/docs/concepts/autonomous-watchdogs.md)
  — what fires the schedule.
