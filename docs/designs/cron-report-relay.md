# Scheduled-report relay: the specialist's report, in a thread the Chat Agent answers

**Status:** implemented and validated end to end on a live GKE cluster; every
report-producing job on
[the Platform Agent's roster](../../agents/platform/cron/README.md) delivers this
way. The one exception, `chat-delivery-watch`, is the job that notices when this
path is broken; see [Detecting a broken leg](#detecting-a-broken-leg).

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

The specialist keeps the work: it runs on its own schedule, in its own profile,
with its own tools, and produces a finished report. It does not deliver that
report. It hands it to the Session KV server, which posts it into a thread and
stores it against that thread, so a reply there reaches the Chat Agent with the
report attached and the Chat Agent owns the conversation that follows.

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
                          ├── hermes send                     → the report, as written
                          │   or slack_blocks_post            → a fleet-audit headline as Block Kit
                          │                                     (Slack, KAGE_SLACK_UX on; below)
                          │   or a2a notify --chat-file       → the same card under spec.mode: next
                          │
                          └── INSERT INTO incidents (chat_id, thread_id, report)
                                       │
                          user replies in the thread
                                       ▼
                          incident_context prepends the report → the Chat Agent has it
```

This is the event watcher's delivery path with the reasoning step removed. There,
an out-of-band signal starts an agent turn that investigates and reports; here
the investigation already happened, and no agent turn runs at all (see
[Why no agent turn runs over the report](#why-no-agent-turn-runs-over-the-report)).
The pieces that make an alert answerable — a thread, a routing row bound to it,
and the report stored against that thread — are reused unchanged.

Under `spec.mode: next` the post leg is `a2a notify` through the A2A gateway rather
than `hermes send`, for the platform the gateway holds (spec-chatops-gateway.md,
"Proactive posts: the chat.notify route"). The report still lands in a thread, but a
reply typed into it reaches the gateway, which has no binding to the report, so the
`incident_context` step above does not apply there.

With `KAGE_SLACK_UX` on, the Slack leg of a fleet-audit job's report sends a
headline in place of the report (`slack_audit_report.py`, called from
`relay_cron_report`), provided a chat id or a Slack
home channel is known, the job's `skills` (a list or one string, or the legacy `skill`) include
`fleet-audit`, the message ends
with a link to an issue in a managed repository, and enough of the posts' budget is
left to read that issue. The headline reads that issue if it is open and labelled
`agent:audit`: the title's critical count when the body lists a critical, the body's
summary line for the other severities (the findings its sections list when it has none), the findings
themselves, and the Scope section's skipped clusters. Held rows are not findings,
a `#` line inside a finding's fenced evidence ends no section, and a finding title,
a skipped cluster or the audit name keeps a link's text but not its target. It
carries one number, the count of the most severe findings a section lists ("<Name>: 2
critical findings", the name as the title writes it), and lists the critical
findings (up to ten), or the top two of the highest severity listed when there is none, each
row led by its severity and ending "· _new_" when fleet-audit marked the finding
new since the last run (a `<!-- finding-new -->` line under its heading, written
only when the run knows what the last one carried). Under the rows, a line counts
the rest by severity ("5 more (1 major, 4 minor) are in the ledger issue."), left
off when there are none; a line names the clusters the run skipped ("Couldn't
reach seeded-c, so this run didn't check it." when every reason says the cluster
was unreachable, "Didn't check seeded-c this run." otherwise, or a count past
three names), or, when the body has no such table, gives the relayed line's own
words for what was not scanned, since a silent gap reads as clean; then the
ledger link. The relayed line itself, which carries the resolved count and any
remediation pull requests, is left off the headline and is not posted under it either: every headline links the ledger issue, whose delta comment lists the resolved findings and which the remediation pull requests link back to. The relayed line is the report's last line; when that line is only the ledger link,
the last unindented, non-list line above it that reads as the audit line (its coverage, a
findings total or a change count) stands in for it, else the last such line with any count. A closed issue is a clean run, which closes the ledger and then retitles it to
0 findings above its old body, so the headline is "<Name>: clean. Ledger closed."
and the link, unless the relayed line counts a finding. When the issue cannot be read or does not parse,
the leg posts the relayed line in bold with the link. Not parsing includes a title
of 0 findings, a body listing no finding, a "<n> new" above the
title's count, a title whose count disagrees with the finding total the relayed
line states, and a relayed line with no total that counts no non-zero new,
resolved or severity count or calls the run clean, held, carried or nothing
reproduced (a zero-finding partial or held run leaves the ledger open over its old
title, or over a refreshed coverage title when a gap opened the ledger). A run that speaks only
because findings are no longer reported under a standing gap (`unreported_under_gap`) gets the
card only when its line's finding total matches the title's count, or the line states a non-zero
new, resolved or severity count; otherwise it gets the bold line. A report whose last line is only the link and with no line carrying a
count has no headline and goes out unchanged. A truncation notice leads the leg's
message but is never read as the relayed line. Nothing is posted into the headline's
thread, but the incident row stores the full report, so a reply in the thread is
answered with the whole report. Under a fallback headline, that reply is the only
way to what the report said beyond its line, such as lines above it or the link
targets the bold line drops.

`_failed_audit_line` posts the first sentence of a report's "What's wrong" section
(`slack_audit_report.failed_line`, or `failed_chat` under `spec.mode: next`) for a report
that ends with no ledger link, and only for a job that runs the fleet-audit skill. The
scheduled delivery of a job the image ships posts what its `finish` rendered or a notice
that it did not finish
([A fleet-audit report comes from `finish`](#a-fleet-audit-report-comes-from-finish-not-the-final-message)),
and neither has that section, so the line is not posted for it. A fleet-audit job created
at runtime, and a mid-run `report_to_chat`, still post the model's text, and the line
still applies to them.

Every other report (Google Chat, one that does not end with
a managed-repository issue link and is not such a failed run, a job the scheduler already delivered to Slack
itself when it also left another platform to the relay, or any report with the
flag off) is posted unchanged. A job whose `deliver` value claims
every platform is relayed to all of them anyway, so its Slack leg gets the
headline beside the scheduler's raw copy.

When the credential proxy's Slack relay is also in the environment and the issue
parses, the headline goes out as Block Kit instead of text. `_post_audit_blocks`
builds it with `slack_audit_report.blocks_from_issue` (the same headline, rows and
lines, a "Look at the first one" button when a finding is listed, and a link button
to the ledger; nothing folds) and posts it once through `slack_blocks_post`
to the relay's `chat.postMessage`, because `hermes send` takes text only. A click on
a choice button runs as the clicker's turn in the thread, carrying the button's
label, which is also its value. The leg posts the text headline
through `hermes send` instead when there is no relay, when the report carries a
truncation notice (the blocks have no place for it), when the issue was not read or
does not parse, when Slack refuses the message for any reason, since
nothing smaller is left to retry with, when too little of the posts' budget is left, and
when the relay call fails. That last case includes a failure that may have posted,
such as a timeout after the request was sent: nothing posts the report again, since
a Slack leg that posts nothing is only recorded as undelivered (the route fails
when it is the only leg, and answers 200 with Slack in `undelivered` beside another),
so declining to post the text would trade a possible second headline for a missing
report. A post Slack accepted without returning a message ts counts as delivered,
with nothing threaded under it. The caller gives up after the relay plugin's
`RELAY_TIMEOUT_SECONDS` (360 s), its clock starting first. So the ledger read and
the Block Kit post share a budget of half that timeout from the route's start,
which leaves the other half for the sends to every platform: the Block Kit post
stops short of it with room left for the text send, and when too little is left to
read the ledger the report goes out as text in one send.

Under `spec.mode: next` the gateway holds Slack and the broker's relay is gone, so the
same card goes to the gateway as a `kube-agents.chat/v1` message instead
(`slack_audit_report.chat_from_issue`, posted with `a2a notify --chat-file`), and the
gateway lays it out, buttons included, as it lays out a card's post in a conversation.
The text headline rides beside it as the fallback the gateway posts when the message
breaks a rule or Slack refuses it; the leg's own fallbacks above are unchanged.

## Why no agent turn runs over the report

The relay first ran one Chat Agent turn over each report, told to reproduce it
verbatim with at most one orienting sentence on top, and posted what the turn
returned. The aim was that the Chat Agent should present the report and so own
the conversation that followed. The turn was taken out (#2742) for what it did
in practice and for what it did not provide:

- **It replaced reports.** On toshiowang-gkedemos3 on 2026-10-08 the turn over
  `fleet-wide-cost-analysis` filed a kanban card and replied "investigating the
  connectivity issues in project `toshiowang-gkedemos3`."; stall-watch's
  all-clear came back as "noted."; on 2026-10-11 a networking audit's report was
  answered with a question. Each reply was posted as the report and the run was
  recorded as delivered, because the transport had succeeded.
- **Its work went nowhere.** A card filed from the turn is subscribed to the
  turn's own API session. `kanban_event_routing` re-addresses a subscription to a
  chat thread only when the session's routing row names one, and the turn ran
  before the post that registers it, so the results of the three cards the
  2026-10-08 cost report's turns filed were delivered back into a session no one
  reads.
- **The report was its input.** The report quotes third-party text (see
  [The report is untrusted input](#the-report-is-untrusted-input)), and the turn
  read it with the `kanban` toolset in reach. The gateway API has no per-request
  way to run a turn without the profile's tools.
- **It did not provide the context.** A reply in the thread runs in the thread's
  own session, not in the turn's, and reaches the report through the stored row
  ([Why context works without an append-only endpoint](#why-context-works-without-an-append-only-endpoint)).
  What the turn added was the one sentence its instructions allowed.

Platform formatting was never the turn's either: the instructions told it to keep
the report's markdown, and the adapters render it for each platform on the send.

## A fleet-audit report comes from `finish`, not the final message

Posting the final message as written leaves one gap the turn used to close by
accident: the final message is the model's whole last turn. On 2026-10-15 a
`compliance-audit` report arrived after several paragraphs of the model
reasoning over `finish`'s line and quoting its own instructions (#3217). The turn
had rewritten that away; posting as written does not.

So for a job that runs the fleet-audit skill, the relay does not post the final
message at all. The run keeps a record in `chat_report_<audit>.json` in its
scratch directory, with one entry per repository it starts. `start` adds the
repository's entry, and `audit_report.py finish` fills it with the chat report
it renders from its own line and the findings document (`render_chat_report`;
`summary` on a no-forge run). An SOP that walks several repositories runs
`start` and `finish` once for each, so a record whose last step is within two
hours (the stream lease, which covers one step) and that holds no finished
report for this repository is the same run; any other `start` opens a new
record, and so does a `start` that finds no finished part, which a run that
never reached `finish` left. A `start` after a `finish` that died after saving its verdict keeps the record and the verdict, so the next `finish` of that repository is not silent. A `finish` applies the
age and repository rule, with one exception: a later `finish` of a repository whose entry
`start` opened and an earlier `finish` filled, while the record's last step is within the
two hours, is the same run. It adds its report under the earlier one's, below a label line,
and gives the record a new `run` stamp; when the earlier `finish` died before it wrote its
report, the later one fills the entry in its place. The first `finish` of the repository in the
run saves the previous run's finding ids on the entry before it touches the ledger, with every id
that run filed, rendered or not, so a finding a body over budget cut is not the run's new; a later
`finish` counts its chat report against them (`run_baseline`): its label says the counts cover the
whole run, and its line states the run's new and resolved findings, or says the change is unknown
when the first `finish` had no trusted memory of the previous run. Its JSON line still counts
against the earlier `finish`. Only an entry no `finish` has filled takes the ids, so a later
`finish` does not save the mid-run memory it reads after an earlier one filled the entry without
them. A no-forge or HELD report, or an entry without those
ids, keeps a label that says its counts are against the earlier `finish`: the report above, or
what it published. The earlier `finish` saves its
`silent_ok` on the entry as each pull request its sweep opens is created, and again before
it writes the report store. When that was `false` the later one is not silent either and its automatic
sweep opens no pull request (`earlier_finish`). A `finish` with no `start` fills no entry,
but while a current record holds this repository's entry with a saved verdict it is taken for a
later `finish` of that run, and the
relay posts that run's report again with its own under it. The relay
reads the record, over the gateway's sandbox login where the shell runs in the
shell sandbox (`sandbox_exec.read_bytes`).

The relay posts the finished repositories' reports as one message, followed by
a line naming each repository the run started and did not finish. It posts each
run once: the run it last posted per job is kept in `audit_report_runs`. The
onboarding report's audits note reads that table's `posted_at` too: a post at or
after a run's claim is that run's, a summary or a failure notice. Once a failure notice
has replaced the row, nothing in it says whether an earlier run posted, so the note
names that audit (`bootstrap_delivery._relay_posts`). A run
that failed is delivered as the scheduler's failure line, which the relay
plugin recognises by its exact opening and the job's name
(`adapter.SCHEDULER_FAILURE_PREFIXES`, pinned against the scheduler at image
build) and flags; the relay posts that line in plain words (below) and marks
the record on disk posted, so a later run cannot post an earlier run's report
as its own. The flag is read off the text, which the model can also write, so
when the record's last step is at or after the delivering run's start (its
`running` row in the profile's `cron/executions.db`, read-only) and it holds a
finished part, the run reached `finish` and its report is posted instead. Otherwise a record with no finished repository, one already posted,
or one whose last step is older than two hours means the run did not reach
`finish`, and the relay posts a one-line notice that the job did not finish in
place of the final message. A read of the file
that fails, rather than finding none, posts a second notice naming the failure,
and leaves the run unposted. `[SILENT]` is unchanged: the scheduler drops a
silent run before delivery. Only the scheduler's delivery leg is replaced: the relay plugin sends
`also_delivered_to` on every delivery and `report_to_chat` never does, so a
mid-run `report_to_chat` is posted as written.

The scheduler's failure line names a CLI and a config file nobody in the
channel can reach, so for any job, audit or not, the relay posts it as one plain
line instead (`scheduler_failure_line`): "**The Workload Reliability Audit didn't
run.** The AI model's usage limit was reached. It will try again tomorrow."
The reason is the scheduler's own, in plain words, or "It hit an error" for one
the relay does not name, and the scheduler's line goes to the server's log
(`logs/session_kv_server.log`, which fluent-bit ships). "tomorrow" is for a job
whose roster schedule fires once a day, every day (`cron_schedule.is_daily`);
any other says "It will try again at its next scheduled run." A failure no
retry clears says so instead of promising one: rejected credentials, and the
scheduler's blocked-configuration line (which it sends once, running the job no
more until the configuration is fixed), end with "An admin needs to fix this
before it can run; the details are in the agent's log." There is no clock time: under `spec.mode: next`, the default
for a first install, the gateway escapes Slack's `<!date^…>` token, and a time
in one zone is wrong for most readers. A failure posts once: `scheduler_failure_posts` keeps the reason each
job last posted, and the same reason is withheld until the job's `completed` row
in the run ledger starts after that post, or until any delivery from the job
that is not a failure line, whose own report is the sign it runs again. A
different reason posts at once, and a ledger the relay cannot read posts rather
than withholds.

Each report ends with a `Ledger: <url>` line, which the Slack headline reads,
so a run over one repository gets its card. A message that carries the line
naming unfinished repositories, or several reports followed by a closing line
that counts them, does not end with a ledger link, and Slack posts it whole with
no card, because a card shows one repository's ledger and drops the rest.

The substitution applies only to a job the image's Platform Agent roster ships
with the fleet-audit skill (`/opt/platform-template/cron/jobs.json`), because
only such a job runs under the stream id that names the record; a job created at
runtime keeps posting its final message. A record over 1 MiB posts the
unreadable notice.

The match is by job id, because Hermes hands the relay no run id, and three
cases fall outside it. A repository whose `start` was refused, or never run,
has no entry, so nothing names it. A run that neither fails nor calls `start`
(the model skips the SOP, or an on-demand run holds the stream) within two hours
of the last step of a run whose report was never posted, an on-demand run or a
silent one the scheduler dropped, posts that run's report. And an on-demand run
of one later repository, followed within two hours by a scheduled run, leaves
its part in the record the scheduled run joins; the scheduled report then leaves
out its own first repository, whose ledger issue is still published.

## Which key the loopback call presents

The relay no longer calls the gateway; the event-triage path
(`trigger_agent_troubleshooter`) still does, and this is why it presents the key
it does. Not `os.environ["API_SERVER_KEY"]`, which is what every other in-pod caller of
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

`_gateway_api_token()` resolves the name instead of reading it, in
`load_hermes_dotenv`'s own order: managed `.env`, then `$HERMES_HOME/.env`, then
the environment. It reads per call rather than caching at import, because `.env`
is rewritten seconds _after_ this process starts.

**Fixed at source, 2026-08-19 (issue #786).** The resolver above is now a
backstop rather than the mechanism. `renderManagedEnv` pins `API_SERVER_KEY` to
the sentinel in the operator's managed `.env`, unconditionally, and Hermes
applies the managed scope **last with `override=True`** — after the PVC file it
was losing to. The PVC file still carries whatever stage2 generated; it simply
no longer wins. One value is now true in four places: the container env, the
sidecar's `AGENT_API_UPSTREAM_KEY`, the sidecar's own copy, and the managed pin,
all from the `loopbackAgentAPIKey` constant.

An earlier version of this section said the other direction — writing the
sentinel into `.env` so the two agree — was tried and is not available because
the API server then declines to bind. That observation was confounded: the pod
had lost its credential-proxy sidecar in the same window. Hermes' actual
constraint is `has_usable_secret(min_length=16)` in the `api_server.py` startup
guard, which the 24-character sentinel clears. (The managed pin does not write
`.env` in any case.)

This was never specific to the relay. Every in-pod caller that trusts the
environment had the same 401, `trigger_agent_troubleshooter` included, which is
why both call sites moved — and why the fix belongs in the operator rather than
in each caller. `docker-entrypoint.sh` now checks the agreement before `exec`
and names it in the log if it breaks, because the container's startup and
readiness probes call that same API: a fifth party disagreeing no longer
degrades quietly, it holds the pod out of Ready.

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

Before this design a scheduled report was inert text posted into a channel: it
entered no agent's context. Now the stored copy is spliced into every later
threaded reply — so what a report says matters in a way it did not before.

The text is not the specialist's own words alone. Every audit on the roster is
required to back each finding with an `evidence.excerpt` — literal
`kubectl … -o yaml` output, trimmed to the lines that prove it
([`obtainability_audit_sop.md`](../../agents/platform/governance/obtainability_audit_sop.md),
"Evidence discipline") — so object names, labels, annotations and event text
written by whoever deploys into the fleet reach the report body verbatim.
And the receiving profile is the delegation surface rather than a bystander: its
`kanban` toolset, with `dispatch_in_gateway: true`, can file work for specialists
that hold `terminal`, `gcloud` and `kubectl`.

The post itself reaches no agent: it is the report, sent as written, and a report
about system components can legitimately contain a `### System:` heading the reader
should see. The one hop where an agent reads it is the replay into a later reply,
and that hop is framed. `incident_context` never shows its output to a human, so it
can be blunt: the stored report is wrapped in an `<untrusted_report>` fence under a
`[SECURITY NOTICE: …]` header, stripped of the tokens that could close the fence or
forge a second notice, and followed by the user's own words under a label saying
that line alone is theirs.

The pattern is not new here —
[`platform_mcp_server.py`](../../agents/platform/scripts/platform_mcp_server.py)'s
`_sanitize_log_text` already fences pod diagnostics the same way. The token list
is duplicated in the plugin rather than imported because a gateway plugin is
loaded by file path and cannot reach the platform agent's scripts.

### Under `platformFrontDoor`

The experimental
[`platformFrontDoor`](../site/src/content/docs/operator/platformagent-crd.md)
flag re-homes the gateway onto the platform profile, so the agent that answers a
reply in a report's thread is the Platform Agent rather than the Chat Agent.
The relay route itself is unaffected: `hermes send` still does the posting, apart
from a Slack fleet-audit headline posted as Block Kit (above). What changes is
whether anything reaches the route at all. Two things, both worth stating rather
than discovering.

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
  report as posted, which quotes third-party text, and this block is prepended to
  every unthreaded message in the space. A preview line would carry that text
  into all of them. `job_id`, `title` and `profile` are fields
  the Session KV server wrote itself.
- **Bounded on both axes.** A window shorter than `CLEANUP_TTL_DAYS`
  (`SESSION_KV_RECENT_REPORTS_HOURS`, default 24) and a row cap
  (`SESSION_KV_RECENT_REPORTS_LIMIT`, default 8), so the injected block costs the
  same whatever the reports weigh — a fortnight of an eight-job roster would
  otherwise tax ordinary chatter with a hundred lines.

The by-thread path is untouched: a threaded reply that finds its report behaves
exactly as before, and the index only runs where the hook previously did nothing.

## The first reply into a report's thread (Google Chat DMs only)

Google Chat opens a thread around every top-level message, so the thread name on
an inbound payload cannot say whether the user posted at top level or replied
inside a real thread. Chat's `message.threadReply` flag does say (it is true only
for a reply in a thread; the `mode: next` gateway reads it, per
`spec-chatops-gateway.md`), but the Hermes adapter does not read it. It settles
the question by counting inbound messages per thread: a thread it has
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

## Thread lifetime: one per job, per UTC day

One thread per _report_ — what the watcher's `per-incident` mode does — gives a
daily watchdog a new thread every tick. One thread per _job_, kept forever, piles
weeks of reports under one message. The routing row is keyed
`cron-<profile>-<job_id>-<YYYYMMDD>`, between the two: consecutive reports from
one job share a thread for the day. No gateway session is created under that id.
Yesterday's thread does not go dark at the rollover, because `incident_context`
resolves a reply by thread rather than by routing id, and those rows live for
`CLEANUP_TTL_DAYS`.

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

The cap truncates rather than rejects. It answered HTTP 413 until #1094, and the
finding then existed only in the job's saved output with a line in
`last_delivery_error` that nothing read until `chat-delivery-watch` (see
[Detecting a broken leg](#detecting-a-broken-leg)); #1094 records three runs lost that way
over 30 and 31 August 2026, across `stockout-prevention` and `compliance-audit`,
whose fleet-wide output runs to several times this cap. The head survives, and
the message opens with a `[truncated]` line naming where the whole report is:
`cron/output/<job_id>/`, or for a fleet-audit job's record its
`chat_report_<audit>.json` and the ledger issue.

That line leads the posted message rather than ending it: a reader has to know
the report is incomplete before reading it, and the Slack audit headline reads
the report's last line, which the notice must not be.

## Which platforms a report is posted to

Every chat platform the install has enabled, resolved by
`session_kv_server.enabled_chat_platforms`. Three sources, most specific first,
and consulted **per platform** so that one naming Slack cannot hide a Google Chat
another knows about: the operator's managed scope
(`/etc/hermes/config.yaml`, which carries `platforms.<p>.enabled` as explicit
booleans and therefore settles it outright on a deployed pod), then `CONFIG_PATH`
— `/opt/data/config.yaml`, the front door's own writable file rather than a named
profile's — then per-platform environment signals for an install neither file
describes.

An install with no chat route gets no send at all: when the managed scope turns
every platform off and nothing routes one through the gateway — a `next` install
whose only chat platform is held by the gateway with no home channel, or an
install with neither Google Chat nor Slack — the relay answers 502 without posting. The detail names the gateway's
platform from `A2A_NOTIFY_CONVERSATIONS` and the CR field that sets its home
channel (`composed but not delivered to slack (no home channel: set
spec.integration.slack.homeChannel / SLACK_HOME_CHANNEL)`), or says that no route
exists when no platform can be named.

The one narrowing is `also_delivered_to`: a platform the cron child already
posted the raw report to is skipped, and where that would empty the set the
relay fans out to every platform anyway (see "What this costs" below).

A dual-platform install gets the report on both. Picking one is what #1094 was:
the resolver returned on its first match, Slack led the order, and an install
with Slack enabled but no home channel lost every scheduled governance report for
seven days while Google Chat — which had a home channel — received nothing. A
partial fan-out is still a 200, because the report is in a channel and a re-run
would double-post there; the platforms it missed come back in the response body's
`undelivered`, which the relay adapter turns into `last_delivery_error`.

**This widens who sees a report, and that is a deliberate change rather than a
side effect.** A governance report carries `evidence.excerpt` — literal
`kubectl -o yaml` from workloads other teams deploy — and on an install that
enabled two platforms for two different audiences it now reaches both, with no
per-job or per-platform opt-out and no CR edit required to take effect. The
alternative is the status quo, where the same install silently reaches one
audience and the operator cannot tell which; a report going somewhere unintended
is visible and correctable, and a report going nowhere was not. An install that
wants one destination should enable one platform.

Each leg keeps its own thread. A thread id is platform-local, so the routing row
carries `platform_threads` — one `(chat_id, thread_id)` per platform — and every
leg replays only its own. Keeping a single address instead is how a leg that
failed once stayed unthreaded for the rest of the day: the next report found the
row naming another platform, posted a fresh top-level message, and did it again
on every run. The top-level `platform`/`chat_id`/`thread_id` triple still holds
one address, because the event-triage card has one destination; it goes to the
leg the row was already routed to, or to the first that landed.

An incident row is stored for every leg that landed **on that run**, so a reply
in any channel the report reached replays it. Storing only one left a reply in
the other channel answered by an agent that had never seen the report. The
qualifier is load-bearing in the other direction: `platform_threads` is additive
and never pruned, and the routing id is per job per UTC day, so the map also
holds legs that landed earlier today and failed just now. Writing a row for one
of those would overwrite that channel's stored context with a report it never
received, and the store is `INSERT OR REPLACE` on `(chat_id, thread_id)`, so the
report still on its screen would be the one lost.

`get_active_platform` is the single-destination sibling, called by the alert
path — `trigger_agent_troubleshooter`, which drives `_post_initial_alert`. An
incident alert registers the thread it gets back as the session's routing and
the triage card's completion is addressed there, so two threads on two platforms
would leave the report addressable to only one. It returns the first enabled
platform and logs a warning naming the platforms that will not receive the
message — and the alert path then tries each enabled platform in turn until one
accepts, so a broken first leg costs a retry rather than the alert. Ordering
alone would have mirrored #1094 onto whichever installs have the _other_
platform broken.

The warning fires on the pick rather than inside the fall-through, and that is
why the pick goes through this function instead of taking `platforms[0]`
directly. The fall-through logs only after a leg has already refused, so on the
common dual-platform install — where the first leg accepts — nothing would say
the second was skipped, and #1094's other half would be documented as fixed
while emitting nothing.

One send that reports success can still fail to yield a thread id, and the alert
path treats that as delivered rather than retrying it: the alert is already in
that channel, so trying the next platform would post it a second time to chase
an id. The run is recorded as an unconfirmed delivery instead.

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
either. The relay posts that failure summary in plain words, once until the job
runs again, and for a fleet-audit job it posts the rendered report in place of
any other final message. One case the scheduler's check misses: it matches the bare marker only,
and a run that emphasised it (`**[SILENT]**`, where the SOP asked for the bare
marker as the whole response) reached both senders. The adapter therefore
carries its own `is_silent_report`, which undresses the marker before testing,
and the Slack sender imports the same predicate so the two ends agree. The
scheduler's direct Google Chat leg, which a job on `"all"` uses, is upstream's
sender and carries no such guard; no roster job uses `"all"`.

## How the switch is wired: `chat` is a platform

Nothing in Hermes is patched. Upstream already routes `deliver=<name>` through
the platform registry, and `cron/scheduler_delivery.py::_plugin_cron_env_var` says so in
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
build, and the same check pins the adapter's `SILENT_MARKER` to the scheduler's
spelling, so the marker the silence predicate undresses is the one the
scheduler emits.

### What this costs

Being a real delivery target rather than an interception has consequences, and
every one of them is visible to a job author:

- **The token is not exclusive.** `deliver: "chat,slack"` relays _and_ posts to
  Slack. A job that wants one voice names one target.
- **`deliver: "all"` includes the relay**, because `_expand_routing_tokens`
  expands to every platform with a configured home channel and the relay now has
  one. A job left on `all` therefore reports twice on a platform the scheduler
  can address — once from the scheduler and once through the relay. The
  relay leg subtracts what it can: `adapter.sibling_delivery_targets` resolves
  the job's `deliver` in the cron child, where alone `all` is known to have
  expanded, and sends the set as `also_delivered_to`; `relay_cron_report` skips
  those platforms. An explicit `platform:chat_id` part counts only when its id
  is that platform's home channel, the one place the relay's leg goes; a DM the
  relay never addresses cannot get a duplicate from it, and claiming it would
  cost the home channel its only relayed copy. The subtraction is one-sided by design — where it would empty
  the list it fans out to every platform anyway, because `also_delivered_to`
  says what the scheduler _intended_ to send and a channel that resolved can
  still fail on the send. Two copies is a nuisance; none is a missed audit. So
  on an install where the child can address every enabled platform, a job on
  `all` still posts twice; the subtraction bites only where the child could
  address a strict subset. That is why every report-producing job on the
  Platform Agent roster names `"chat"`:
  the `deliver` token itself remains additive, and `"chat"` is the only way to
  ask for one relayed copy without depending on a subtraction that is designed
  to give up rather than risk silence.
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
  The response body also carries `"relay"` and `relay_detail`, which the relay
  plugin and `platform_mcp_server` read. They reported a failed Chat Agent turn
  posted as a raw report marked `[unrelayed]`; with no turn, the route answers
  `"relay": "ok"` and an empty `relay_detail`, and the fields stay so a later
  cause can land in the route without a client change.
  A send that lands on one platform and not another is a different case, and not
  a degradation — the report reached a channel, so `relay` stays `ok` — but one
  audience has nothing, so the body carries `undelivered`: the platforms that
  missed it, comma-joined. Holding the two fields apart is what stops a
  consumer reporting the same miss twice, once as prose and once as a list — see
  [Which platforms a report is posted
  to](#which-platforms-a-report-is-posted-to).
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

## Detecting a broken leg

Everything above records a failed delivery faithfully and reports it to nobody.
`last_delivery_error` is written on every failed run; #1094 is the account of
what that was worth (seven days of six audits composed, dropped, and recorded as
dropped, with the only signal being that the reports had stopped arriving) and
#1102 is the gap it left open. The watcher on the other end of the field is
`chat-delivery-watch`
(`agents/platform/scripts/chat_delivery_watch.py`), a `no_agent` job on the
Platform Agent's roster that runs every half hour.

**What it counts.** The field holds only the latest run. The next run that
delivered cleanly sets it back to `None`, and so does a run that delivered
nothing: a `[SILENT]` answer skips delivery and clears the field with it, so on
a quiet fleet the evidence of a dead leg is erased every morning. The watcher
therefore keeps its own ledger, `profiles/platform/cron/chat_delivery_watch.json`,
keyed by `<profile>/<job id>` across every store on the volume (the chat
profile's, the Platform Agent's, every Cluster Agent's). A job's streak advances
only when `last_run_at` has changed since the last tick, so a half-hourly tick
over a daily job counts runs (a job that runs more often than the watcher is
under-counted, never over-counted); a failing run adds one, a clean run resets to zero,
and a silent run, recognised from the job's newest file under `cron/output/`,
moves the high-water mark and changes nothing else. Failures are graded from the
strings this design produces: `hard` when the report reached no platform (a 502,
an unreachable relay, no key), `partial` when it landed somewhere but not on the
platforms the `undelivered` field names, `degraded` when it posted but the relay
said the delivery degraded (only a route that still ran a Chat Agent turn over
the report did; the grade stays for a cause the route may name later). The scheduler's own two failures, a platform named in
`deliver` that is not enabled and a `deliver` that resolved to no target, also
grade `hard`: nothing was delivered either way. A run the scheduler recorded as
anything but `ok` and that saved no document (interrupted by a gateway shutdown,
or failed before delivery) is no evidence either, like a silent one; a failed
run that did save its document had its failure summary delivered, which is a
working leg. A note the scheduler files under the same field for a report that
arrived (a thread it fell back from, an attachment it could not confirm) is not
a failure. A job that is disabled or paused holds no streak, since it has no
next run to recover with.
`CHAT_DELIVERY_ALERT_THRESHOLD` (default 2) is how many consecutive failing runs
make a job degraded.

**Where it reports.** Not chat: a chat message saying that chat is down is the
one message guaranteed not to arrive. The job's `deliver` is `"local"`, the only
platform-roster entry allowed that value (the README's rule and its test carry
the exemption by name), and it reports through two channels that need no
privilege the pod does not already hold.

- A **GitHub ledger issue**, one per install, labelled `agent:delivery-watch`
  and carrying a hidden `<!-- chat-delivery-watch -->` marker. It lives in the
  repository `CHAT_DELIVERY_LEDGER_REPO` names, or else in the install's one
  managed GitHub repository; several managed repositories and no override is
  refused rather than guessed, as `resolve_repo` refuses it. It is opened when
  any job crosses the threshold, edited when the picture changes (a job joins or
  leaves the degraded set, a further failing run is counted, an error string
  changes; a fingerprint in the ledger keeps a tick that saw no new run down to
  one read, which is how an issue a person closed by hand is noticed and
  replaced rather than edited while closed), and closed with a comment once
  every leg has recovered. The call is
  `forge.call` — a version-control verb, over the sandbox hop into the
  credential broker — the same route `github-repo-watcher` takes; the minted
  token already holds `issues: write`.
  The issue resolver's search excludes the label, so the agent never triages its
  own ledger. With no repository to use, the job falls back to the log line
  alone. A forge with no issue verbs, or no labels, keeps no ledger either: the
  ALERT line says `ledger=log-only`, and that is not a fault. Two installs that manage one repository would share one issue; the
  fleet-audit ledgers have the same property, and an install-specific ledger
  repository is the answer for both.
- An **`ALERT chat_delivery_watch` line** appended to
  `<agent home>/logs/chat_delivery_watch.log`. The gateway pod's fluent-bit
  sidecar ships that directory's log files to Cloud Logging (the pipeline is
  described on the site's
  [observability page](../site/src/content/docs/concepts/observability.md)), so
  a log-based alert reaches it with
  `resource.type="k8s_container" resource.labels.container_name="fluent-bit"
jsonPayload.log:"ALERT chat_delivery_watch"`. This is the file rather than
  the job's own stdout on purpose: the scheduler captures a `no_agent` job's
  stdout into `cron/output/` and, with `deliver: "local"`, sends it nowhere.
  The watcher's own failures land on the same line as `self=error`, and the
  ledger's `last_tick_at` answers whether it has run at all.

**Why not a `PlatformAgent` condition, a Kubernetes Event or a metric yet.** Each
would be a better fit for an alerting system, and none has a carrier today. The
agent container mounts no service-account token, the operator grants the agent
identity no write on anything a watcher could use (the site's
[security reference](../site/src/content/docs/reference/security-and-iam.md) is
the canonical account of what it does grant), the credential proxy refuses
every write verb before RBAC is consulted, and the operator read nothing the
pod wrote when this was designed, so a condition or an Event needed a new
pod-to-operator path and a new grant first. One such path exists now: the
operator reads the event watcher's and the credential broker's metrics
listeners for the usage counters ([design](usage-counters-producer.md)), and it
carries integer totals and nothing about a report, so the grant is still the
missing piece. The operator binds no metrics endpoint of its own in the shipped
deploy; the ones the agent's pods expose are the event watcher's and the
credential broker's, scraped through the chart's `PodMonitoring`s beside
LiteLLM's and Hindsight's, and neither carries anything about a report. Those are the next step, with this
section as the record of why the first step took the channels it did.

## Related

- [`agents/platform/cron/README.md`](../../agents/platform/cron/README.md) — the
  roster's own rules, including why exactly one job sets `deliver: "local"` and
  why an id must not appear on two rosters.
- [`agents/platform/docs/session_management.md`](../../agents/platform/docs/session_management.md)
  — the Session KV server, its callers and its auth.
- [`concepts/autonomous-watchdogs`](../site/src/content/docs/concepts/autonomous-watchdogs.md)
  — what fires the schedule.
