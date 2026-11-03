# Proposal origin: which feature opened each pull request

**Status:** proposed, 2026-11-03. Workstream 3 of the telemetry epic that asked who starts the
agent's pull requests. Lands after the second pull request of `human-interaction-readings.md`
(the readings slot with a target per reading kind) and after `install-model-reading.md` (the
summary audit that removes a fixed line); it builds on both. Builds on [`usage-counters-producer.md`](usage-counters-producer.md) (the
status block, its fold rules, its gauges and its zero-PII contract),
[`proposal-outcome-readings.md`](proposal-outcome-readings.md) (the broker's exact
`proposal-create` count, which this design leaves alone) and
[`human-interaction-readings.md`](human-interaction-readings.md) (how a maintainer reads an
install's numbers across installs).

## 1. The question and what answers it today

The development team wants to know which features make customers' installs open pull requests: a
person asking in chat, the event watcher's triage of a cluster event, drift detection, a scheduled
fleet audit, a claimed GitHub issue, the admin console, or a caller on the agent API. They read
the answer from the `status.usage` block, which an internal dataset ingests from every install, so
the split has to land there; Prometheus gets it for free through the per-field gauge.

Today `remediationsProposedTotal` counts every successful `proposal-create` the credential broker
served, with no origin. Nothing can give it one at the broker: every `proposal-create` is one HTTP
call from the shell sandbox pod, authenticated as the sandbox, and the broker holds no task or
session context for that caller. The sandbox process knows only the profile home and, for a
kanban worker, the card id it derives from its working directory. The origin is known elsewhere,
in the agent pod, in scattered places: the A2A bridge writes the chat platform into a session's
route, the Session KV server stamps `platform: k8s-watcher` on the sessions it opens for the event watcher, the drift detector's carry
`kind: gitops-drift`, cron runs carry their job name, the issue watcher's cards carry an
`issue-resolve-` key, and web-console and API sessions have their own id shapes. No one record
says "this session's origin is X".

The maintainers' rule for any such classification is the one `kanban_priority` and the broker's
`Principal` already state: it comes from trusted context, never from what the model types. The
maintainer who asked for this design chose that rule over exactness when the two pulled apart:
**the count must not be fakeable by the model.** So the count is taken in the agent pod from
trusted context, the residuals the mechanism leaves are stated in §2.2, and the broker's
total stays what it is.

## 2. Decisions

### 2.1 The origin, one value per session, card or run

Each unit of work the agent does carries one `origin`, written once by the in-pod code that
starts it. The values, as many as the runtime can tell apart:

| Origin      | The work                                                                                                                                                                                                                          |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `chat`      | a person's message on a chat platform: the A2A bridge's sessions for Google Chat, Slack and Discord, and the Hermes gateway's platform sessions under `today` (Google Chat, Slack, Teams)                                         |
| `console`   | the admin console: sessions whose id the console minted (`web-console-*`, read as a convention by the in-pod reader until the agent-API door writes a record), the A2A gateway's `console` backend, and cards the dashboard files |
| `api`       | a caller on the agent API or an A2A door (`a2a`, `a2a-google`, `inject`): another agent or a script, not a person on a chat platform                                                                                              |
| `event`     | the event watcher's triage: a `k8s-evt-*` session the Session KV server opens for a cluster event, decided by the first injected payload (`k8s-event`, `k8s-event-followup` and `controller-stall` are all `event`)               |
| `drift`     | the drift detector: a `k8s-evt-*` session whose first injected payload carries `kind: gitops-drift`, and the cards the drift backlog files                                                                                        |
| `audit`     | a scheduled fleet audit: a scheduler-started run of a job shipped in the image's `agents/platform/cron/jobs.json` whose `skills` list names `fleet-audit`                                                                         |
| `scheduled` | a scheduler-started run of any other shipped job, the `github-repo-watcher` gate included, and the pull-request follow-up cards that gate files; a job the model created runs under the origin of the session that created it     |
| `issue`     | a claimed GitHub issue: a worker on an issue card the `github-repo-watcher` gate filed; the gate stamps the card itself, so a card the model names `issue-resolve-*` is not `issue`                                               |
| `other`     | work whose origin none of the above resolves, and the value a failed write defaults to                                                                                                                                            |

Three kinds of unit carry the record, each written by in-pod code inside one trust boundary:

- **A session**, under the key `origin` in the Session KV server's per-session metadata, written
  once over its loopback API: by the Session KV server itself for the `k8s-evt-*` sessions it creates (it writes their
  `platform: k8s-watcher` today), at the first inject since the create call carries no body,
  `event` or `drift` from the payload's kind; a watcher in shared mode injects into a session the operator named and the server never created, so that session has no record and its work resolves as `other`, which is accepted since nothing in the repository sets that mode; by the hermes-bridge for every `a2a-*` session, before its first turn (the bridge opens a
  session through the first turn's header), from the backend (`chat` for `gchat`, `slack` and
  `discord`, `console` for `console`, `api` for the doors), a write beside the route record; by the `session_store` plugin for the `today`
  platform sessions it already records (`chat`). The record carries the chat platform beside the origin under its own key,
  `origin_platform`, since `platform` is already a `session_store` key that the Session KV server
  overwrites on an alert's session with the chat platform the alert was posted to. A continuation
  after context compression inherits its parent's record through `parent_session_id`. The `session_store` plugin rewrites a session's metadata blob on every gateway message from the event alone, so it must read the row and write it back in one `BEGIN IMMEDIATE` transaction, carrying the two keys forward and writing an origin only where none is present; that plugin prunes the table after seven
  idle days and the Session KV server's own cleanup after fourteen, so a thread resumed after
  that, or a continuation whose parent row is gone, resolves as `other`, which is accepted. The
  web console runs outside the pod and mints its session ids itself, so it writes no record: the in-pod reader takes `console` from the `web-console-` id prefix, the convention the console's activity view already reads, until the agent-API door writes a record for the sessions it admits. Any caller holding the external agent-API key can mint such an id and count as `console` rather than `api`; both are operator-side callers, the sandbox holds no such key, and the bridge forces its own prefix, so that is accepted until the door's record replaces the convention. A caller holding that key can also join an existing session by id (an alert's `k8s-evt-*` session, a chat session), and its work then counts as that session's origin; the sandbox holds no key, so that too is accepted as operator-side. The `session_store` plugin runs only on gateway dispatch, which an agent-API turn may
  not pass through; `api` for agent-API turns is therefore written by that door when its design
  lands, and until then such work resolves as `other`. A person who replies in an alert's thread runs in the gateway's own chat session (the alert's report is pasted in; nothing maps the reply to the `k8s-evt-*` session), so a proposal they ask for there counts as `chat`.
- **A card**, as two columns the board patch writes at insert, beside the priority
  `kanban_priority` already stamps from trusted context and never from the `session_id` argument
  the model passes. The origin is never an argument of a create command: the `/kanban create`
  parser serves chat users and in-pod scripts alike, so a flag on it would let anyone who can
  chat stamp `issue` or `drift`. Instead the board stamps, at insert and only at insert, the creating turn's trusted context:
  the session record of the turn that files it, read by the trusted Hermes session id (`chat` with its platform, `event`, `drift`, `api`, or `console` by the `web-console-` convention), a tick-started run's `audit` or `scheduled`, the dashboard's `console`; a card a worker files inherits the worker's card, and an auto-decomposed
  child inherits its parent's. The few trusted writers that know better than their own context,
  the issue gate (`issue` on its issue cards, `scheduled` on its pull-request follow-up cards),
  the drift backlog (`drift`) and the chat bootstrap (`chat`), pass the stamp to the insert
  through an endpoint on the Session KV server's loopback API, and the stamp applies only to a
  card that call inserted: a dedupe hit on a key someone filed first keeps the first card's
  stamp, so a chat user who files a card under the gate's predictable key before the gate's tick
  gets a `chat` card, not an `issue` one. That endpoint is reachable with the pod's Session KV key, which the sandbox and chat commands do not hold and the platform MCP server and the hermes-bridge do; no MCP tool exposes it, and none may. A card with no stamp, one filed before this lands, is `other`.
  A worker's origin is its card's, so a cron job dispatched as a worker counts under its card,
  which the run stamped.
- **A cron run**, as a trusted per-run context the Hermes scheduler sets for a run its own tick
  started (the run ledger already tells a tick's run from a direct one, `source` other than
  `direct`), `audit` or `scheduled` only for a job whose definition equals the image's template
  entry in `/opt/platform-template/cron/jobs.json` (same id, same prompt and skills; the profile's
  own jobs file is the agent's to write, so an id alone proves nothing), the way the api_server
  sets the request-scoped session id `trusted_origin_session` reads. The Platform Agent's model
  can create jobs, edit them, and run any job directly, synchronously or in the background, so:
  a tick-started run of a job that no longer matches the template, or that the model created,
  carries the origin recorded for that job id when it was created or last changed, the creating
  or editing session's, in a table of the Session KV server written by the in-pod handler of the
  `cronjob` tool from trusted context, never in the jobs file the agent writes; a job id with no
  such record and no template match resolves as `other`; a direct run is not tick-started and
  resolves as the session that asked for it.
  A tick-started run has no Session KV metadata row of its own and no gateway dispatch, so
  neither of the other two records can reach it.

A write that fails does not fail the work: the session opens, the card is filed, the run starts,
and the unit resolves as `other`.

This is the record the agent-API door design
([`agent-api-door-allowlist.md`](agent-api-door-allowlist.md) §4.4) proposed for session
scoping, under another vocabulary (`console, eval, ops, a2a, triage, scheduled`) and another
failure rule (no record, no read). The two are not one record. This design's `origin` key is telemetry: written by many starters,
fail-open to `other`. The door's scoping record is an authorization fact about the caller's key,
fail-closed; `eval` and `ops` are classes of that key, not origins. Whether that record shares
this key with this vocabulary, or takes another, is that design's call when it lands; §4.4
there carries a note saying so, nothing in this design reads the door's record, and the one
value this design leaves to the door is `api` for agent-API turns.

### 2.2 The count: the agent pod records proposals it verified, per origin, in a durable ledger

The agent pod already verifies every pull request a kanban worker opens: `vcs_client.call` writes
one `kube-agents.event` line to stderr when the broker answers with a new proposal, the
`pr_opened` hook sees that line in the worker's terminal output before the model does, checks it
with the forge (the broker serves the pull request at that url, on a repository the install
manages, open, created within the last fifteen minutes, authored by the broker's own account),
and only then records a `pr_opened` event on the card. That check is the trust anchor this
design reuses: a line the model could type is not a proposal until the forge confirms the agent
opened it.

Three things change in the hook. **The worker gate lifts for the count.** Today the hook returns
at once outside a kanban worker, so a scheduled fleet audit, which opens its proposals inline
in the cron run, or an api_server turn, would never reach the ledger. The check and the ledger write now run in every process that has the terminal, on every profile that does (the platform profile, the Cluster Agent profiles, the front door), where today the hook is enabled on the platform profile alone; the card event and the removal of the whole record line stay worker-only, so what the model reads in any other session changes only by the token field the hook strips. **The record binds to the call that opened the proposal.** The forge check proves the pull request
is the agent's, not that this process opened it, so a worker that echoed another session's fresh
record line could claim that proposal for its own origin. The broker therefore mints a one-time claim token with each `proposal-create` answer and keeps it
with the url for the window; `vcs_client` takes the token out of the answer it returns and prints
(so `vcs.py`'s stdout never carries it) and puts it on the stderr record line alone; the hook
presents the tokens it sees to a new `proposal-claim` verb, which does the forge check itself
with the credential it holds and accepts each token once, so the hook no longer makes its own
forge reads. The claim is one broker call through the sandbox hop per record line, made in line
before the command returns, so the hook keeps today's cap of five record lines per output: the
first five are claimed, or burned, before the model reads them, and the hook strips the token
from every record line in the output it did not claim, those past the cap and those whose claim
failed, in every process, so no live token reaches what the model reads. A proposal whose line
went unclaimed counts as missed. A line without a valid token is left as text
and counts nothing. What remains is the model keeping the broker's answer out of foreground
output (stderr to a file, a pipe, a background run, or a request it writes itself against the
broker instead of `vcs_client.call`) and replaying the token from another session within the
window: that moves one proposal from "missed" to a wrong origin. The claim verb is open to the
shell role like the other `proposal-` verbs, so a model can also burn a token by hand, which
hides that proposal from the count. These are the residuals; the model cannot mint a proposal,
change one already recorded, or name one older than the window. **The count is read, not held.** A kanban worker runs in its own process and leaves
through `os._exit`, while the chat plugin's metrics registry lives in the gateway leader, so an
in-memory counter in the hook would never reach the listener the operator scrapes. Instead, on
every verified and claimed record the hook writes one row to a new Session KV table,
`proposals_opened(url, origin, platform, opened_at)`, keyed by url; and the chat plugin's
listener serves `kubeagents_proposals_opened{origin,platform}` by counting that table at scrape
time, the way `KanbanGaveUpCounter` already reads the board. The ledger is on the pod's volume
and survives a restart. An install with several agent replicas has one ledger per pod and one
listener, the leader's, so its reading is the leader's ledger and can fall at a leadership
change; the slot keeps the last reading through one miss and the next poll reads the new
leader's; that is accepted.

**Resolving the process's origin.** The hook reads, in this order: the card's stamp, for a worker, from the board it already opens for the `pr_opened` event; the run's context, for a process inside a tick-started run; the session's record by the trusted Hermes session id, or the `web-console-` convention on that id; else `other`. The trusted id is the request-scoped one an api_server turn already binds; for a gateway platform turn the `session_store` plugin binds the Hermes session id in the same context at dispatch, since the chat id it holds today is not the row's key. That binding is a named part of the implementation, not an assumption. The Chat Agent profile has no terminal, so in the stock layout a chat turn never opens a pull
request itself: its proposals come from the cards it files, whose stamp carries the platform the
turn arrived on, and that is what the `platform` label holds for `chat` (empty for every other
origin). Under the experimental platform-profile front door a chat turn does run with the
terminal; its session record then resolves it, `chat` with its platform. A Prometheus reader can split chat by platform; the status does not. The hook is enabled on every profile that has the terminal.

What the count misses, stated so the numbers are read right: a pull request opened by a
background process (the hook reads foreground output only); one whose record line the model kept from the hook, or whose token the model burned; one past the five record lines the hook claims per command output; one whose claim failed the forge check or arrived after the fifteen-minute window; and one opened by a request the model wrote itself
instead of `vcs_client.call`. So the origin counts sit at or below
`remediationsProposedTotal` in the usual case, and the difference reads as "proposals the agent
did not see itself open"; it is approximate, since the broker's total is itself folded from a
counter the poller can under-read across a gap while the ledger is durable.

The broker's series gains no `origin` label: it could carry one only from a header the sandbox
sends, which the model can set, and the maintainer ruled that out.

### 2.3 The status fields: nine readings through the readings slot

The ledger's count survives a restart, and the usage poller's counter fold does the opposite of
what that needs: it treats a process it has not seen as counting from zero and adds its whole
sample, so a durable count served as a counter would be re-added in full on every gateway
restart or pod roll. So the nine are **readings**, not counters, through the readings slot
([`proposal-outcome-readings.md`](proposal-outcome-readings.md) §6) as
[`human-interaction-readings.md`](human-interaction-readings.md) §2.2 generalises it in its second pull request, a target per reading with the miss rule following the target, which this design depends on: a new reading kind, `origins`, on the chat plugin's target, present when all nine origins
resolved from the body (the listener serves every origin's series, zero included, so the kind
needs no age series, like the `people` kind), one miss per poll without them, cleared after
two, the largest reading across pods, and its own pruning record ("the nine origin readings"),
so a served CRD that predates them keeps the rest of the block landing.

The fields, named like the block's other readings: `proposalsFromChat`,
`proposalsFromConsole`, `proposalsFromApi`, `proposalsFromEvent`, `proposalsFromDrift`,
`proposalsFromAudit`, `proposalsFromScheduled`, `proposalsFromIssue`, `proposalsFromOther`. Each
is a pointer field: a 0 is a reading; absent until the first reading and while the served CRD
predates it. They move neither `lastActiveTime` nor `lastHumanActiveTime`, as readings do not.
Each gets a row in the per-field gauge `kubeagents_platformagent_usage{field=...}`, so a shared
Prometheus reads the split with one `sum by (field)`.

`remediationsProposedTotal` is unchanged: the exact total, the broker's.

### 2.4 The summary

Both summary copies print one line after the proposal readings:
`  proposals by origin: chat 3, event 1, audit 2 (as the agent reports)`, listing the origins
whose reading is above zero in the table's order, `none` when every reading is zero, and
`no reading` when they are absent. One line rather than nine reads as the answer to one
question. The script's full block is at its forty-line cap today, and the people readings' pull request
adds its own rows first, so both copies' caps rise by one from wherever that pull request leaves
them, said in the commit body. The nine field names are a list pinned between the copies by the
existing copy test, beside the counters and the readings. The privacy audit removes this line
before it checks the block, the removal
[`install-model-reading.md`](install-model-reading.md) §2.2 adds for the model line and this
design depends on: the line carries only fixed text (its label, the nine origin words, `none`, `no reading`, the
suffix) and digits, so nothing on it can leak, and a cluster or project named `chat` or `drift`
is still refused anywhere else in the block.

### 2.5 Trust and privacy

The origin never comes from what the model types: the session record is written by in-pod
starters, the card's stamp is written at insert by the board from the creator's trusted context,
the run's context is set by the scheduler for shipped jobs only, a model-created job carries its creating session's origin from the Session KV server's record, and the process reads its own origin through those records alone. The pull request itself is counted only after the forge confirms the agent opened
it. The ledger and the readings carry no identifier that leaves the pod: the ledger's url stays
in the pod's own store, and the series and the status carry an origin name and a platform name,
never a repository, a url, a user or a session id. The zero-PII contract of the status block
holds as it stands.

## 3. What changes, by component

- **Session KV server** (`agents/platform/scripts/session_kv_server.py`): the `origin` metadata
  key (with the platform) and its write for `k8s-evt-*` sessions at the first inject; the parent
  walk for continuations; the `proposals_opened` table and the endpoint the hook writes it
  through; the stamped-insert endpoint for cards; the drift backlog's `drift` stamp; the per-job-id origin table the `cronjob` handler writes.
- **Board patch** (`deploy/docker/patches/`, beside `kanban_priority.py`): the card's `origin`
  and `origin_platform` columns written at insert from the creating turn's session record, run
  context or dashboard, the worker's and the child's inheritance, and the insert-only stamp a
  trusted writer passes.
- **Credential broker** (`agents/platform/scripts/credential_proxy.py`) and `vcs_client`: the
  one-time claim token on the `proposal-create` answer, taken out of the returned answer and put
  on the stderr record line only, and the `proposal-claim` verb that checks the forge and burns
  the token.
- **hermes-bridge** (`a2a/hermes-bridge/api.go`): the origin write before the first turn, from
  the backend.
- **`session_store` plugin** (`agents/chat/defaults/plugins/session_store/`): `origin` and
  `origin_platform` in its key list; `chat` with the platform for `today` platform sessions.
- **Chat bootstrap** (`agents/chat/scripts/bootstrap_handoff.py`): `chat` on its onboarding
  cards.
- **The in-pod reader** (the hook's resolution): `console` from the `web-console-` id prefix.
- **Hermes scheduler patch** (beside `deploy/docker/patches/apply_cron_run_workdir.py`): the
  per-run origin context, `audit` or `scheduled` from the shipped file's `skills` for tick-started runs of jobs that match the template; the creating or editing session's origin recorded per job id in the Session KV server by the `cronjob` tool's handler and carried by that job's tick-started runs.
- **`github_scan_gate.py`**: `issue` on its issue cards, `scheduled` on its pull-request
  follow-up cards, passed to the insert through the loopback endpoint.
- **`pr_opened` hook** (`agents/platform/plugins/pr_opened/__init__.py`): the claim of every
  record line in every process (the forge check moves to the broker); the origin resolution in
  the order of §2.2; the ledger write.
- **Chat metrics plugin** (`agents/chat/defaults/plugins/chat_metrics/metrics.py`): the
  `kubeagents_proposals_opened{origin,platform}` gauge family, read from the ledger at scrape.
- **Operator** (`usage_counters_scrape.go`, `usage_counters_poller.go`, `usage_gauges.go`,
  `common_types.go`, the CRD copies): the `origins` reading kind on the chat target, nine
  specs, nine pointer fields, one pruning record, nine gauge rows.
- **Summary** (`feedback_summary.py`, `a2a/gateway/feedback_summary.go`): the one line, the
  pinned field list, the audit's removal of the line, the caps.
- **Docs**: the observability page (nine rows, the gauge, the one-line answer to "who started
  the PR" in the list of answers), the CRD page, `usage-counters-producer.md`,
  `human-interaction-readings.md` §2.4's pointer, the ChatOps page's summary paragraph.

## 4. Goals, non-goals, what stays out

Goals: the dataset reads, per install, how many pull requests each feature opened; a Prometheus
reader gets the same with one query; nothing the model types can mint a proposal, change a recorded one, or claim one outside the window, with the residuals §2.2 states.

Non-goals: an `origin` label on the broker's `kubeagents_vcs_requests_total` (ruled out: the
broker has no trusted source for it); a per-platform split of `chat` in the status (the series
carries it; the status does not); origins for proposals opened, or cards filed, before this
lands; exactness against the broker's total (the gap is stated, not closed); the door's scoping record, whose key and rule that design decides.

Anti-goals: a header or body field the sandbox sends that names an origin; a count read from the
model's own report of what it did; a repository, url or user reaching the status or a label.

## 5. Eval exemption

A deterministic guard in the sense of `.agents/rules/eval_driven_development.md`: a session
record, a verified ledger, a reading and a status field, with no model call in any path. The one text the model sees change is a machine record line it already reads today, from which the hook now strips an opaque token field; no prompt, instruction, tool text or
skill text changes, and a worker's output has the line removed exactly as today. Unit tests cover every origin's resolution, the inheritance through a
card, a child card and a continuation, the `other` fallback, the ledger's once-per-url rule, the
reading's presence rule and the summary line.

## 6. Left to the builder

The ledger endpoint's path and the table's indexes; the board columns' names; the scheduler
patch's hook point for the per-run context; whether the hook resolves a card's stamp with one
board read or caches it per process; the order of the nine in the summary line (the table's
order above is the default); the pruning record's name; whether the series is served from a
count query per scrape or a cached count refreshed on each ledger write.
