# Stall Watch on the AutoOps Inject Path

The `stall-watch` cron entry finds controllers that stopped making progress without erroring. This
design moves what it does with a new stall from filing a kanban card itself to sending a
`controller-stall` inject to the Session KV server, the route `k8s-event-watcher` and the drift
detector already use. The Cluster Agent still runs `gke-stall-detection`; what changes is where its
report lands and whether a reply to it can be acted on.

## 1. Why

Before this design, `stall_watch.py` filed a card straight to the cluster's Cluster Agent and
subscribed it to the home channel with no thread. The Cluster Agent answered in
`gke-stall-detection`'s own report shape, which has no `## What to do` section and no `To authorize:`
line. Two consequences followed, both observed on a live install:

- The kanban notifier saves a report as an `incidents` row only when it was posted into a thread and
  carries that section (`kanban_notifier.actionable_report`, `store_incident_report`). A stall report
  met neither condition, so it was never saved.
- A reply of "apply" therefore reached the Planning Agent with nothing attached. The
  `incident_context` plugin found no row for the thread, and an unthreaded message gets only a list
  of report titles that says "You do NOT have their contents". The Platform Agent, handed a bare
  "apply", investigated the stall again from the start.

The event and drift paths do not have this problem. The Session KV server posts the alert, records
its thread, and starts a Planning Agent turn that files the card with the triage template; the
notifier posts the result into the alert's thread and saves it; a reply in that thread carries the
report to whoever acts on it ([`session_management.md`](../../agents/platform/docs/session_management.md)).
[`autoops-architecture.md`](../../agents/platform/docs/autoops-architecture.md) names this as the
path a new signal plugs into: an adapter that detects, filters its own noise, and emits the inject
envelope with a new `kind`. The stall watch already detects and filters; it only needs to emit.

## 2. Target model

```
stall-watch tick (no_agent, every 30 min)
  └─ sweep, ledger, diff                      unchanged
  └─ new stall episode in <cluster>/<namespace>
       GET /healthz                            kind advertised? else nothing is sent
       POST /sessions                          -> session id
       ledger saved                            with the episode, before the inject
       POST /sessions/{id}/inject              kind: controller-stall
         session_kv_server
           ledger row (reason ControllerStall) no alert limit
           alert posted, thread recorded
           Planning Agent turn                 one kanban_create to the named Cluster Agent
         Cluster Agent                         gke-stall-detection, triage template
         notifier                              report into the alert thread, incidents row
       episode records the session id; the card is found later by tasks.session_id
  └─ new object / cleared                      comment on / complete that card, as before
```

The payload carries what the old card body carried and nothing more: cluster, project, location,
namespace, the Cluster Agent profile the watch resolved, when the watch first saw the stall, and one
`{object, heuristic, stalled_for}` entry per ledger row. Row detail (condition reasons, spec paths,
event messages) stays out, for the reason it stayed out of the card: it is text a tenant writes, and
the Cluster Agent reads it again when it runs the skill.

## 3. Decisions

### 3.1 The watch stops filing cards

The thread is attached because the Planning Agent files the card inside the inject's session:
`kanban_event_routing.py` rewrites the card's subscription to the alert's thread as it is written. A
card the watch filed itself has no session, so it would get no thread and no `incidents` row unless
the watch rebuilt that subscription itself, which is a second way into the same pipeline. So
`open_card`, `subscribe_card`, `home_targets`, `card_title`, `card_body` and the idempotency-key
generation go, and so does the per-open "stall noticed … card opened" chat line: the inject's alert
is the notice.

### 3.2 The watch still closes what it opened

An episode stores the session id. The watch finds the card by `tasks.session_id` on the board (the
column Hermes stamps with the session that filed the card, the API session the Planning Agent's turn
runs in) and from then on comments on and completes it exactly as
before. An episode written by the old code carries `card` and no `session`, and keeps working
unchanged. A session whose card never appears (the Planning Agent turn failed) ends its episode a day
after the alert was raised, and the alert is raised again in that tick. A stall episode has no expiry
of its own, so without the bound a failed turn would silence the namespace for as long as the stall
lasted; a shorter one would post a fresh alert every hour or so for as long as turns kept failing.
The watch raises no alert at all on a board whose tasks table lacks `session_id`, and says so once. An unreadable board is not counted as a missing card: the episode waits for a board that
answers. A new episode records `card` as empty rather than leaving the key out, because the
card-filing watch read the key unguarded; a rollback then ends such an episode instead of crashing
every tick.

### 3.3 The ledger is saved before the inject

The card path had an idempotency key, so a tick whose ledger was not saved filed nothing new the next
time round. The inject route has no equivalent. The watch therefore saves the ledger, with the new
episode in it, before it sends the inject, and sends nothing when that save fails: a ledger that
cannot be written means no alert, rather than the same alert on every tick. A refused alert (the
server unreachable, not advertising the kind, or answering anything but `injected`) ends the tick's
attempts and is said once in chat, with a line when alerts are raised again. The refusal text names
no session, since every attempt opens a new one and the ledger compares the text to decide whether
the refusal is new. A record the server refuses with 400 is that record's fault, not the server's:
it skips only that namespace. Object names longer than the server's 200-character limit are cut to
it before the record is sent.

### 3.4 No alert limit

The event path claims a per-severity daily limit and the drift path its own. A stall claims none,
by the maintainer's decision: the per-tick cap already bounds the volume, and a separate daily
ceiling can be added once there is a measured stall volume to size it against. The watch's per-tick cap stays, renamed `MAX_ALERTS_PER_TICK`
(3 a tick), and is the only bound.

### 3.5 The Planning Agent is told the assignee

The event and drift queries make the Planning Agent find the cluster's agent in its specialist list.
The watch already knows the exact profile, so the stall query names it, and keeps the
`list_agents` refresh as the fallback when the list does not show it. The Planning Agent turn stays,
rather than the watch filing the card under a borrowed session, so that this kind rides the same
path as the other two (§3.1).

### 3.6 The card body is the stall's own template

`_stall_task_body` follows `_drift_task_body`: its own question and evidence block, the shared
contract literals (`## What's wrong`, `## Why`, `## What to do`, `Option A`, `To authorize:`) in the
same places. It tells the Cluster Agent to run `gke-stall-detection` and to quote the scan's
`stalled resources: <count>` line in **Why**, and says what to write when the scan finds nothing
left. `bench/tests/test_triage_delivery_contract.py` holds it against the notifier gate and the new
eval case, as it does the other two templates. `gke-stall-detection`'s "Recording the finding"
section defers to a card body that specifies a format, so the skill and the template do not
disagree.

### 3.7 The daemon fails closed on a malformed stall payload

Unlike the event path, which defaults every missing field, a `controller-stall` payload with no
namespace, no cluster or no objects is rejected with 400 before any alert is posted: the only
producer is ours, and an alert for an unnamed namespace helps nobody. Heuristic names outside the
four `stall_report.py` emits and durations not in its format are rendered as `unknown`. Names
(objects, namespace, cluster, project, location) go through `_defang_drift_field`, the drift path's
defence against a value escaping its backtick span inside the copy-verbatim block; the assignee is
used only when it matches the profile-name pattern.

### 3.8 The end-of-day recap excludes stall rows

`eod_report_generator.py` reports the event watcher's ledger rows and excludes drift rows by
`reason`. Stall rows get the same treatment under `reason = 'ControllerStall'`.

## 4. Work breakdown

1. Daemon: `controller-stall` kind (advertised in `/healthz`), payload validation, ledger row,
   alert, `_stall_agent_query`, `_stall_task_body`; EOD exclusion.
2. Eval case and its prebuilt stack (§6).
3. Producer: `stall_watch.py` injects instead of filing, finds the card by session id.
4. Skill and docs.

Items 1–2 and 3–4 are separable: the eval case plants the inject itself, so the daemon half is
testable without the producer change.

## 5. Files touched

- `agents/platform/scripts/session_kv_server.py`, `test_session_kv_server.py`
- `agents/platform/scripts/eod_report_generator.py`, `test_eod_report_generator.py`
- `agents/platform/scripts/test_triage_reply_roundtrip.py`
- `bench/tests/test_triage_delivery_contract.py`
- `bench/tasks/autoops-controller-stall-triage/task.yaml` (new)
- `bench/tf/prebuilt/controller-stall/main.tf`, `variables.tf` (new)
- `hack/ci-eval-pr.sh`, `hack/eval/nightly-cases.txt`, `scripts/test_eval_rosters.py`
- `agents/platform/scripts/stall_watch.py`, `test_stall_watch.py`
- `agents/cluster/skills/gke-stall-detection/SKILL.md`
- `agents/platform/cron/README.md`
- `agents/platform/docs/autoops-architecture.md`, `agents/platform/docs/session_management.md`
- `docs/site/src/content/docs/concepts/autonomous-watchdogs.md`, `concepts/cluster-agents.md`,
  `overview/proactive-autonomy.md`, `reference/cron-jobs.md`, `reference/credential-isolation.md`,
  `reference/security-and-iam.md`
- `docs/designs/eod-event-watcher-daily-report.md`, `docs/credential-isolation-design.md`
- `bench/tasks/cluster-agent-stalled-controller-diagnosis/task.yaml` (its header names the card
  body this design moves)
- `docs/designs/stall-watch-inject.md` (this file), `docs/README.md` (this file's row, and the rows
  for the stall watch's identifiers and `autoops-architecture.md`)

## 6. Testing

**Unit.** The daemon: a stall inject writes one ledger row and starts one background turn; the
query names the assignee and copies `_stall_task_body`; a malformed payload is a 400 with nothing
posted; no alert limit is claimed. The producer: a new episode creates a session and injects once;
the cap holds; the card is adopted by session id; comment, clear and complete work through it; an
old-shape episode keeps working; a daemon that refuses or does not advertise the kind leaves the
namespace waiting and is said once, at either step; a ledger that cannot be saved raises nothing; a
session that files no card is raised again a day later; the record `stall_payload` builds is the one
the daemon's route accepts. `test_triage_reply_roundtrip.py` drives a report cut from `_stall_task_body`
through the real notifier, server and plugin, and shows the pre-inject stall report shape earns no row.

**Eval.** `autoops-controller-stall-triage`, modelled on `gitops-drift-out-of-band-triage`. Its
stack plants a Deployment gated on a pod readiness condition nothing sets, waits until the image's
own `stall_report.py` reports it (its Deployment threshold is ten minutes, and the Cluster Agent
runs the same scan when it works the card), posts the `controller-stall` inject built from that
scan's rows, and waits for the card to finish: an agent turn that answers while the card is still
running grades an acknowledgement rather than the report. A readiness gate rather than a missing ConfigMap: a missing
ConfigMap's pods raise `Failed` events, so the event watcher would file a card in the same namespace
and the prompt's "most recent card concerning that namespace" would have two candidates. The
install's own `stall-watch` tick can still raise a second card for the namespace once the threshold
passes; the case header says why its checks hold either way. The prompt
retrieves the card's result. Objective checks: the board was read (`kanban_list`); the report names
the workload and the readiness gate; it carries the triage contract (`What to do` plus `Option A`
or `To authorize:`); it contains `stalled resources:`. Safeguards: the readiness gate and the replica
count are unchanged. A daemon without the kind renders the record as a `Pod` event with reason
`Unknown`; the card asks for an event triage, not the stall scan, so the `stalled resources:` check
is the expected red there. The stack posts
the record rather than running the watch because a real tick sweeps every cluster on the roster and
would file for every planted defect in the fleet. It stops at the triage report, as both sibling
cases do; the apply step is covered by the unit test above.

## 7. Accepted risks

- **A lost inject reply posts a second alert.** The inject route answers before any chat or model
  work, over loopback, so a reply lost after the daemon accepted the record is unlikely; if it
  happens the next tick injects again. The old card path had an idempotency key; the inject route
  has no equivalent.
- **The Planning Agent turn is a model turn.** It can paraphrase or fail. The body is copied between
  markers, as on the other two paths. A failed turn leaves an alert with no report for a day before
  the alert is raised again (§3.2).
- **The alert reaches one chat platform.** The card-filing watch subscribed its card to every
  enabled platform's home channel. The inject path posts the alert to the first platform that
  accepts it and threads the report there, as it does for events, because a thread belongs to one
  platform. On a dual-platform install the other platform sees the watch's own "stall cleared" and
  "held" lines, which cron delivery fans out, and not the alert or the report. When the alert post
  fails outright the report has no route at all, the same failure the event path has.
- **A kill between saving the ledger and sending the inject delays the alert a day.** The episode is
  saved with a session that never got the record, and the watch waits the day it gives any session
  that files no card. The window is the inject call itself, at most its 30-second timeout.
- **An undelivered stall alert is reported nowhere.** The daemon marks the ledger row's
  `delivery_error`, and the only reader of that column, the event watcher's daily recap, excludes
  stall rows as it excludes drift rows. The old path's "stall noticed" line went through cron
  delivery, which `chat-delivery-watch` monitors; the alert does not.
- **The event watcher and the stall watch can report the same defect.** A Deployment whose Pods fail
  with a reason on the watcher's list is triaged by the watcher on the Pod and, ten minutes later,
  by the stall watch on the Deployment. That happens today and this design does not change it.
- **Episodes opened before the upgrade whose subscription write failed stay unsubscribed.** The
  retry goes with `subscribe_card`; such a card is still worked, its report just does not reach chat.
