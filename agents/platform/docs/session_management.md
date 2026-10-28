# Platform Session Management & Incident Triage Flow

This document details the architecture and workflow for routing GKE Kubernetes warning alerts into persistent diagnostic agent sessions, enabling interactive threaded troubleshooting in chat platforms (Google Chat and Slack).

---

## Architecture Overview

AI agent execution is typically stateless and triggered on-demand. To support proactive GKE warning troubleshooting, we run a local stateful proxy server called `session_kv_server.py` (the REST Bridge) on the Platform Agent host on `127.0.0.1:8699`.

This server acts as a bridge between the **GKE Event Watcher** (monitoring target clusters) and the **Hermes Gateway** (running the LLM reasoning turns). The **drift detector** is a second producer on the watcher's side of that bridge, posting out-of-band cluster changes to the same routes; see [the second producer](#the-second-producer-gitops-drift). The `stall-watch` cron script is a third, posting namespaces whose controllers stopped making progress; see [the third producer](#the-third-producer-controller-stall). The turn it starts runs on the gateway's default profile — the **Planning Agent** — which delegates the diagnosis on the kanban board to the **Cluster Agent** of the cluster that raised the event, so the agent that investigates the failure is the one scoped to the cluster it happened on.

It binds loopback rather than `0.0.0.0` because every one of its callers shares this Pod's network namespace: the event watcher and the drift detector, both in the `agent-api-auth` sidecar, the Platform MCP server, the `incident_context` plugin, the gateway's kanban notifier, and the chat adapter's scheduled-report relay, the two findings scripts, the `stall-watch` cron script, and the hermes-bridge sidecar, which records a gateway conversation's route (`PUT /v1/sessions/{id}/route`), stops that conversation's kanban cards (`POST /v1/sessions/{id}/cards/stop`, below), and relays the Slack Home tab's board (`GET /v1/board`). No count here on purpose: the list keeps growing and a stated total is what goes stale first. Every route except `/healthz` also requires a bearer token from the `SESSION_KV_API_KEY` key of the agent's Secret — the rows it serves carry chat identifiers, and loopback inside a shared namespace is not on its own an authorization boundary. Deliberately not `API_SERVER_KEY`, which is the non-secret sentinel `cluster-internal-trusted` and would authenticate nothing. When the key is absent the server answers `503` to every authenticated route and logs why; see [the credential-isolation design](../../../docs/credential-isolation-design.md#the-loopback-only-exception).

### Key Responsibilities:

1. **Deduplication:** Maps repeat events to the same troubleshooting session, preventing alert flooding and saving LLM token costs.
2. **Dynamic Thread Resolution:** Captures the Chat API message ID returned from the first alert, saving it as the persistent thread key.
3. **Incident Triage Context Preservation:** Persists completed triage reports inside the local SQLite database.
4. **Gateway Message Rewriting Hook:** Integrates the `incident_context` plugin to intercept user replies on active incident threads and automatically prepend the triage report, allowing the fixer agent session to run with full context.
5. **Severity Gate & Event Ledger:** Records every forwarded event in `intercepted_events`, then alerts on the warning ones only. The drift detector and the stall watch write to the same table under `reason`s of their own; see [the second producer](#the-second-producer-gitops-drift) and [the third](#the-third-producer-controller-stall). Informational events are held back from chat and reported as a count by the daily recap.
6. **Daily Alert Ceiling:** Caps how many alerts of each severity reach chat in one UTC day, bounding the volume that survives deduplication.
7. **Scheduled-Report Relay:** Accepts a finished report from a specialist's cron job on `POST /v1/cron-reports`, posts it as written (for the scheduled delivery of a fleet-audit job the image ships, it posts the report `finish` rendered for each repository, with a line naming any it did not finish, or a notice that the run did not finish, and on Slack a one-repository report is replaced by a headline linking its ledger issue), and stores the full report against the thread it lands in, so a reply there reaches the Chat Agent with the report attached. Its caller is the scheduler, not the model: `deliver: "chat"` resolves to a delivery-only platform plugin whose sender POSTs here, and `report_to_chat` remains for a job that needs to report mid-run. Deliberately not a mode of `/sessions/{id}/inject`: a scheduled report has no severity and must not spend the alert ceiling above. See [the design](../../../docs/designs/cron-report-relay.md).
8. **Triage Routing:** Instructs the front door to hand the diagnosis to the Cluster Agent of the cluster the event came from, and records the chat route that carries the report back.

### Triage Routing

The session lands on the front door and cannot land anywhere else. Hermes selects a profile by URL prefix (`POST /p/<profile>/api/sessions`), only when `gateway.multiplex_profiles` is enabled — it is off by default and this install does not set it — and only against that profile's own `API_SERVER_KEY`. A `profile` key in the request body is accepted with a `201` and dropped, so it looks like routing and is not. Routing is therefore a prompt, not a parameter.

`_build_agent_query` writes that prompt for the front door — for an event; a `gitops-drift` or `controller-stall` inject is routed to its own builder at the top of the same function — and it is addressed to a router rather than to a diagnostician: make one `kanban_create` call, assign it to the `cluster-*` agent scoped to the event's cluster, and copy the body between two markers verbatim. `_triage_task_body` builds that body — the event details and the report template — and it is the front door's job to move it across unread. Everything in that design is a response to the front door being helpful: given the brief as instructions rather than as cargo, it summarised, and filed extra cards asking other agents to deliver the report. When `INCIDENT_TRIAGE_OPEN_PULL_REQUEST=true` (a `spec.deployment.env` entry, passed through by the operator like `INCIDENT_WORKLOAD_DEDUP_SECONDS`), `_build_agent_query_with_pull_request` asks the front door for a second `kanban_create` call assigned to `platform` with `parents: ["<triage_card_id>"]` and a verbatim body from `_triage_pr_task_body`; the dispatcher holds that second card in `todo` until the diagnosis card settles (`done` or `archived` — while the diagnosis card is `blocked`, the second card stays held in `todo` and is surfaced by `kanban_board_health`, and if the blocked parent is archived without a report, step 2 of `_triage_pr_task_body` sees `status != 'done'` and completes without opening a pull request), and when the diagnosis completes the Platform Agent opens the GitOps Pull Request for the recommended fix without waiting for a chat reply. That card's branch (`_incident_branch`) is keyed on the workload the way the incident fold keys it (`_fold_key`, [below](#folding-a-workloads-repeat-events)), so every replica of a Deployment lands on one branch, and an event folded into an open incident starts no turn and files no pull-request card.

Delivery is the card itself. Hermes subscribes every card to the session it was filed from, and posts a subscribed card's `result` to chat when it turns terminal — so the Cluster Agent finishes with `kanban_complete` and nothing else, and the report reaches the thread the alert was raised in. The body's whole job on that point is to insist the entire report goes in `result`, since `result` is verbatim what the reader sees.

Two more things travel in that prompt since #656. The call is made without `goal_mode`: a goal-mode card is graded by Hermes's judge against its title and body before `kanban_complete` is allowed through, and a body that is a report template cannot be satisfied that way — on 2026-08-12 a worker whose correct report the judge rejected had no exit but a sticky `needs_input` block with `result = NULL`. And the body states a **Done when** (the `State at HH:MM UTC:` opener from the agent's own read, the root cause with evidence, at least one GitOps option or an explicit no-change finding, the report recorded via `kanban_complete`) and asks for those facts in the one-line `summary` too, because the judge grades `summary` before `result`; a card graded anyway then has something gradeable and an answer that states it. The backstop for a card that blocks regardless is the `kanban-board-health` job on the Platform Agent's roster (`agents/platform/cron/README.md`), which names any card blocked past a day in the home channel, by title and how long it has waited, when it first gets stuck and then weekly while any stays stuck.

The address is what used to be wrong. An event-triage turn arrives over the REST gateway, whose single ingress chokepoint stamps `platform="api_server"` and puts the session id in `chat_id`, so the subscription was written well-formed and pointing at no chat platform that exists — the report was produced, stored on the card, and delivered nowhere. That is issue #630. `deploy/docker/patches/kanban_event_routing.py` closes it inside the image: it looks the session id up in `session_metadata`, which `_register_session_routing` wrote before the turn started, and substitutes the alert's real `platform`/`chat_id`/`thread_id` at subscription time. A session with no recorded route, or one recorded as non-chat, falls through unchanged. The hermes-bridge records a route the same way for its `a2a-*` sessions, so a card filed in a gateway conversation reports back to that conversation, and the lookup follows Hermes's compression lineage when the session has since been renamed; [the gateway spec](../../../docs/designs/spec-chatops-gateway.md) has the conversation route.

The session id is therefore the thread key, and `session_metadata` records the `platform` alongside the `thread_id`: a thread belongs to exactly one chat platform, and a report addressed to the other is not degraded to the home channel but refused outright.

### Daily Alert Ceiling

Deduplication bounds how often _one_ failure is reported. It does nothing about many _distinct_ failures at once — a node draining or a namespace collapsing produces a hundred unrelated pods, each a legitimately new incident. The ceiling is the backstop for that case.

`inject_message` classifies severity (`get_severity_details`), applies the [severity gate](#severity-gate), and then spends one of that severity's daily allowance before anything is posted or any agent turn is started. This is the only place both actions pass through, and severity is not known any earlier — `POST /sessions` carries no payload.

A [`gitops-drift` inject](#the-second-producer-gitops-drift) reaches the ceiling by a different route: it is graded `Warning` unconditionally and skips both the classifier and the gate, because the detector that sent it already decided the record was worth a human's attention. It is billed to a bucket of its own (`GitOpsDrift`) rather than to the `Warning` one its ledger row records it as, because `_claim_alert_quota` keys the table on the string it is handed and the two traffic shapes are not comparable: one `kubectl apply` over a directory is several audit entries and several injects, where the watcher's warnings arrive one incident at a time. Sharing the bucket therefore let routine drift cap-drop a deployed signal. The cost is that the ceilings add up, so a busy day of both posts more total alerts than the single budget allowed; neither ceiling bounds the fan-out itself.

#### Folding a workload's repeat events

The watcher deduplicates on the involved object's UID and the reason, so one broken Deployment reaches `inject_message` once per replica, once per replacement pod, and once per reason in a cascade (`Unhealthy`, then `BackOff`). With `INCIDENT_WORKLOAD_DEDUP_SECONDS` above `0` (default `0`, off; capped at `86400`; a negative or unparseable value reads as `0`), a Warning-or-worse event for a `(cluster, namespace, object kind, fold_key)` that already has an admitted row younger than the window is written with `duplicate_of` set to that row (a Critical only when that row is Critical too, below). It claims no allowance, posts nothing and starts no turn, and the route answers `folded`. The watcher keeps its dedup entry unflagged and counts the event in `k8s_event_watcher_events_folded_total`, not as injected or as a triage session; `filtered` would let the next Warning sighting reopen the entry. A watcher that predates the status reads it as delivered, which keeps the entry the same way and miscounts it. The window runs from the anchor, so once it passes, the next new pod or reason from a workload that is still failing is announced again.

`fold_key` is what makes two events one workload, and the inject payload carries no owner reference (the watcher never fills in `context.controller_ref`), so it is read from the pod's name. A Deployment's pod (its ReplicaSet's pod-template hash, then its own suffix: `checkout-6cfdb6b98b-zwv24`) folds on the workload `clean_workload_name` gives it. The hash is `SafeEncodeString` of a decimal number, so its characters are only `4`–`9`, `b`, `c`, `d` and `f`, and the fold key is set only for a segment of 8–10 of those. That keeps out a CronJob run's pod (`report-29812345-abcde`) and a date-stamped Job's (`db-migrate-20261021-abcde`), which the display cleaner shows under the same prefix: folded, a Job's failure would sit under a same-named Deployment's incident. The hash is shorter than 8 characters for about 0.2% of pod templates, and those Deployments do not fold: a lower bound would also match short name segments and fold different owners together. A non-Pod object folds on its own name. Any other Pod name has no key, so it neither folds nor anchors: a StatefulSet's `web-0` and `web-1` keep their ordinals, and a DaemonSet's or a Job's pods end in a five-character segment the cleaner also strips from a bare pod, where it is not a replica suffix (`api-cache` and `api-proxy` both display as `api`). Folding those would let one workload's alert silence another's, so they each still alert. A Critical event folds only into a Critical anchor: a workload that gets worse after its Warning went out is announced again.

An admitted row is one with `notified` non-zero or a `delivery_error`. A failed chat post resets `notified` to `0` within seconds while its triage keeps running, so anchoring on `notified` alone would let every sibling open its own triage on an install whose chat route is broken. A row the gate filtered, the ceiling dropped, or that was itself folded never anchors a window, and neither does a row whose triage start was given up: the triage pool retries a start that provably did not begin ([When a triage cannot start](#when-a-triage-cannot-start); a 2xx, a 5xx other than 503 or an error while waiting for the `/chat` answer counts as started, since the turn may have run), and when it gives up it clears the row's `fold_key`, so a sibling that arrives after that is admitted and starts the triage. While the start is queued or retrying the row still anchors: a start is on its way, and a sibling admitted then would open a second triage on the same incident. The same release happens when the background task dies on an exception before the start is queued, and every start of the server clears all fold keys, because a row whose task never ran (the process died between the insert and the task) or whose queued start the restart lost has nothing else to release it; a workload whose triage did start loses its window, at the cost of one more triage (one per severity, since a Critical folds only into a Critical) for a sibling that arrives after the restart. Siblings that folded into the row before it was released stay folded. For a start the pool gave up on, they are the ones that arrived while it was queued or retrying, up to the queue limit plus the retry budget, and the give-up notice in the anchor's thread is the record that no triage ran for the workload; for a task that died, they are the ones that landed while it ran. The daily recap does not count a folded row as withheld by the ceiling, and `GET /v1/intercepted-events` lists it as `folded`, with the anchor's id in its `duplicate_of`.

A [`controller-stall` inject](#the-third-producer-controller-stall) claims no allowance at all: the stall watch caps itself at three alerts a tick, and that cap is its only bound. Neither does a drift backlog record that is digested (`POST /v1/drift-backlog`): it starts no turn and is counted in one digest (a chat message, or one unassigned board card on an install with no chat platform enabled). A late record that is carded instead — a late `kubectl` write, or a late row of a principal with fewer than three in the digest — bills `GitOpsDrift` as a live record does. A refused minority row stays in the digest; a refused late `kubectl` write is suppressed as a live record is.

| Bucket        | Env var                      | Default |
| ------------- | ---------------------------- | ------- |
| `Critical`    | `ALERT_DAILY_LIMIT_CRITICAL` | `10`    |
| `Warning`     | `ALERT_DAILY_LIMIT_WARNING`  | `5`     |
| `Info`        | `ALERT_DAILY_LIMIT_INFO`     | `5`     |
| `GitOpsDrift` | `ALERT_DAILY_LIMIT_DRIFT`    | `20`    |

Three of the four are severities and the fourth is not; `GitOpsDrift` is what drift records bill,
whatever they display as.

The `GitOpsDrift` default of 20 is a judgment, not a measurement. One apply spends one inject for
each object, because the detector does not combine live records (late ones are digested and bill
nothing; see the drift bullets below). `install.sh` and `upgrade.sh` enable the
detector by default, so this value applies to the installs that these scripts manage. Only changes by
people alert, so 20 covers a few hand applies a day. A record that the detector cannot join to a live
object still sends a card with less detail, unless its cluster is outside the install's scope. That
card spends the same budget. Each admitted record starts a triage turn. The detector also reads the
audit logs of each scoped project, so a wider scope sends more records. These are the reasons not to
go higher.

`Info` events do arrive — nothing on the path from the kubelet to `inject_message` filters on `Event.Type`, and `BackOff` is on the watcher's default reason list emitted as `type: Normal` for image-pull back-off — but none of them ever bills this bucket, because the [severity gate](#severity-gate) drops every `Info` event before the claim. The `Info` row is kept regardless: deleting it would turn the entry into a `.get(severity, 0)` miss, and `_claim_alert_quota` treats that miss exactly as it treats a limit of `0` — allowed through, uncapped. Narrowing that gate afterwards would therefore send an unbounded `Info` stream to chat rather than restore a ceiling. Setting a limit to `0` turns that severity's cap off entirely, by the same branch.

All four are tunable without rebuilding the image. Set them at install time: the `ALERT_DAILY_LIMIT_*` keys in `install.env`, the chart value `platformAgent.deployment.alertDailyLimits`, or the Terraform variable `alert_daily_limits`. Each route renders the keys that are set into the CR's `spec.deployment.env`. The [site's chatops page](https://gke-labs.github.io/kube-agents/concepts/chatops/#daily-alert-ceiling) gives the routes and a sizing method. They reach the container because they are on the sandbox env allowlist in `safeSandboxEnvOverrides` (`k8s-operator/internal/controller/platformagent_manifests.go`) — `spec.deployment.env` is filtered, so an arbitrary variable set there is dropped, and a ceiling left off that allowlist is an override that renders, validates and silently does nothing.

An entry on the CR itself also works:

```yaml
spec:
  deployment:
    env:
      - name: ALERT_DAILY_LIMIT_CRITICAL
        value: "25"
      - name: ALERT_DAILY_LIMIT_WARNING
        value: "0" # uncapped
```

On an install that the installer, the chart or Terraform manages, do not edit the CR. Each apply that changes the env list that the chart renders replaces the full list on the CR, and entries added by hand are lost. The [site's chatops page](https://gke-labs.github.io/kube-agents/concepts/chatops/#daily-alert-ceiling) tells when this occurs. Use the install-time route for a ceiling, and `platformAgent.deployment.env` for another name.

Behaviour worth knowing before relying on it:

- **Suppression is silent.** Nothing is posted to say the ceiling was reached — announcing it would spend a message to say no more messages are coming. The consequence is that once the cap bites, a quiet channel no longer distinguishes "nothing is wrong" from "the budget is spent", so the accounting lives outside chat: every suppressed alert is counted in `alert_quota`, logged at `WARNING` with the workload it dropped, and readable from `GET /v1/alert-quota`.
- **The counter is fleet-wide,** not per cluster. One collapsing cluster can therefore exhaust the day's budget for every other cluster.
- **It fails open.** If the quota table cannot be read or written, the alert goes through. A ceiling is a comfort feature and must never be the reason an incident is withheld.
- **The suppressed alert is still acknowledged** to the producer with `200 {"status": "suppressed"}`, rather than an error code. A 4xx or 5xx would land in `k8s_event_watcher_inject_errors_total`, which exists to say the daemon is broken; refusing an alert over a configured ceiling is it working. The watcher reads the body, drops its dedup entry and re-offers the workload on its next sighting — deliberately, because the entry's window is 24h and this ceiling resets at 00:00 UTC, so keeping it would mute the workload long after the reason for it expired. The price is a session row per re-offer until the day rolls over. **The drift detector cannot do any of that**, and the asymmetry matters: an audit entry is delivered once, and its `insertId` is marked seen before the reply is read, so a refused drift record is not re-offered on any later sighting. It is lost from chat for the day. The ledger row is the only place it survives, and nothing reports it — the recap below is the event watcher's and excludes drift rows. A `suppressed` reply therefore means "try again later" to one producer and "this change was never escalated" to the other.
- **The budget survives restarts,** because it is on the `system-metadata` PVC rather than in memory. A crash-looping session server would otherwise hand out a fresh day's quota on every restart, which is precisely the condition the cap exists for.
- **The day boundary is UTC midnight,** not the operator's local midnight.
- **The severity gate runs first, and only alerts that survive it are billed.** A budget is a count of alerts sent, so an event that was never going to be posted must not spend one. Claiming first would bill the `Info` bucket for every suppressed image-pull `BackOff` and leave `GET /v1/alert-quota` reporting a day's worth of alerts nobody received. It cannot starve a real alert of its budget — anything graded `Warning` or `Critical` draws on a different bucket from the `Info` churn — so this ordering is bookkeeping, not a safety property.

### The second producer: `gitops-drift`

Everything above describes the k8s-event-watcher, which was the only thing posting to
`/sessions/{id}/inject` until the drift detector
([`k8s-operator/cmd/drift-detector/`](../../../k8s-operator/cmd/drift-detector/README.md)) started
sending a second kind. The route dispatches on the payload's `kind`: `gitops-drift` takes its own
branch, as `controller-stall` does ([the third producer](#the-third-producer-controller-stall)), and
everything else is an event and behaves exactly as described above. Nothing about the
event path changed.

What the drift branch does differently:

- **It skips the severity classifier and the gate.** There is no `Event.Type` to grade and no
  informational tier to hold back — the detector's own classifier already dropped everything it
  judged to be automation rather than a person, upstream of this route. Every record that arrives
  here is graded `Warning`.
- **It bills a bucket of its own,** `GitOpsDrift`, rather than the `Warning` one it is recorded as.
  [The ceiling section](#daily-alert-ceiling) has the reasoning and what the split costs. The
  `Warning` label reaches no reader: it is the `severity` column of the ledger row and a field of
  the suppressed response, and the chat line names no severity at all.
- **Its `suppressed` is terminal,** for the reason the bullet above gives.
- **Late records skip this route.** A record the detector marks as backlog (it queued while no detector was
  pulling, or waited over 30 minutes) goes to `POST /v1/drift-backlog` instead, with no session. The
  route writes a ledger row with `backlog = 1` and the record's `principal`, spends no quota and
  starts no turn. A worker thread posts one digest per burst, after 60 seconds with no new backlog
  row or 10 minutes after the oldest, and marks the rows `2` (posted) or `3` (filed as one board card,
  on an install with no chat platform enabled: unassigned and blocked, so no agent turn files or
  reads it, and it stays in view until a person acts). A
  principal with fewer than three late rows is not digested: each of its rows gets a session, a quota
  claim and a card, as a live record does, rebuilt from the payload the row stores, and is marked
  `4`. A row the ceiling refuses stays in the digest. A row without a usable stored payload is
  carded from its own columns (summary and cluster). The post holds a lease row in `drift_backlog_lease` but no database lock. The daemon advertises the
  route as `features: ["drift-backlog"]` on `/healthz`; the detector's README has the rule that
  marks a record late and what it costs.
- **It defangs the fields it renders.** The record describes a change someone made, and several of
  the fields describing it are chosen by that person: `fieldManager` is a free query parameter and
  `callerSuppliedUserAgent` is whatever the client declared. Both are rendered inside a block the
  front door is told to copy verbatim, so a backtick in either would close the span it sits in and
  the rest of the value would read as instruction text. The drift renderers substitute the
  characters that end a span or start a line of their own, replace the chat-template control tokens
  `_CONTROL_TOKEN_RE` matches, and truncate, so a field stuffed with instructions cannot
  outweigh the prompt around it. Replacement is with U+FFFD rather than deletion, because the card
  is evidence and a reader should see that the value was altered. The event path does not do this
  and is unchanged; its fields come from the kubelet rather than from the caller being reported on.
- **It writes its own chat line and its own kanban card,** through a second query builder
  (`_drift_agent_query`) and a second body (`_drift_task_body`). The question is different: an event
  says Kubernetes is unhappy and asks for a root cause; a drift record says someone changed a live
  object outside git and asks whether the declared or the live state should win. The card is
  addressed to the agent for the cluster _the change was made on_, which after the detector's
  multi-cluster fan-in is not necessarily the cluster the detector runs in.
- **Its ledger rows carry `reason = 'OutOfBandChange'`,** which is what keeps them out of the event
  watcher's daily recap.

The detector reaches this server the same way the watcher does — over loopback, with the
`SESSION_KV_API_KEY` bearer token — which means it has to run inside this Pod's network namespace.
The agent images carry the detector and the credential proxy's entrypoint starts it, so the inject
has an in-cluster producer — on an install that has set `spec.harness.driftDetector.enabled` and
applied the `drift-pubsub` Terraform module. Without the field the detector does not start; without
the module it starts and retries a pull that cannot succeed. Either way the flag exists and the
route accepts it, and nothing produces one. `terraform/examples/full-install` takes the two as
separate variables and refuses an apply that sets `enable_drift_detector` without
`enable_drift_pubsub`; the installer front doors write both from one `ENABLE_DRIFT_DETECTOR` key.

#### Asking whether the dispatch is there

`GET /healthz` answers `inject_kinds`, the list of kinds the dispatch above understands, and the
detector probes it at startup and refuses to run if `gitops-drift` is not on it. It also answers
`features`; the detector uses `POST /v1/drift-backlog` only when `drift-backlog` is on it.

The dispatch is an equality test on `kind`, and a daemon predating it cannot say so: the drift
payload falls into the event path, where the defaults render it as a `Warning` Pod alert named
`default/` for reason `Unknown`. That bills the watcher's bucket rather than `GitOpsDrift`, writes a
ledger row the daily recap counts as a watcher event, and still answers `200`, so the producer
records it delivered and never retries. Silent at both ends, and one `kubectl apply` over six
objects is six of them.

The skew is an ordinary deployment window rather than a hypothetical: this script is copied to the
shared PVC from the agent image while the Go producers ship in their own, so the two roll
independently. The event watcher negotiates the same class of problem in the other direction with
`X-Watcher-Features`, and the daemon answers in kind: its `injected` reply to an event lists what it
does with the session beyond posting the alert (`features`, today `followup`), and `/healthz` lists
the same under `features`, which the watcher's sweep reads again before each tick's
follow-ups; together they are how the watcher knows a `k8s-event-followup` will reach the thread
rather than open a second alert, whichever way the two images have rolled.

It is on the unauthenticated `/healthz` so the probe is a precondition of starting rather than
something a producer discovers only once it holds credentials, and the key's _absence_ is the
signal: an old daemon answers `{"status": "ok"}` and nothing else, so a producer that requires its
kind fails closed against one. A kind goes on the list only when the dispatch actually handles it:
`k8s-event` because the event path is a real answer for it rather than a fallback, and
`k8s-event-followup` because it has a branch of its own (below).

### The third producer: `controller-stall`

`stall_watch.py`, the `stall-watch` cron script, sends `kind: controller-stall` for a namespace whose
controllers stopped making progress
([design](../../../docs/designs/stall-watch-inject.md)). It probes `/healthz` before every inject
and sends nothing to a daemon that does not list the kind. Its branch differs from the event path in
these ways:

- **It skips the severity classifier and the gate.** Every stall row is graded `Warning`; there is
  no `Event.Type` to grade.
- **It claims no alert quota.** The watch caps itself at three alerts a tick, and that is the only
  bound.
- **It refuses a malformed record** with `400` before anything is posted: a record with no
  namespace, no cluster or no objects. The event path defaults those fields; this branch's only
  producer always sends them.
- **It writes its own chat line and its own card:** `_inject_stall` writes the alert, and
  `_stall_agent_query` and `_stall_task_body` the card. The query names the Cluster Agent profile the watch resolved instead of
  leaving the Planning Agent to find it, and the body sends that agent to `gke-stall-detection` and
  asks for the scan's `stalled resources:` line in the report. Object names go through the drift
  path's defang; heuristics and durations are rendered only from the values `stall_report.py`
  emits.
- **Its ledger rows carry `reason = 'ControllerStall'`,** which keeps them out of the event
  watcher's daily recap.

The watch files no card itself and finds the one the Planning Agent filed by `tasks.session_id`, so it
can comment on it when new objects stall and complete it when the stall clears.

### The watcher's second record: `k8s-event-followup`

The event watcher sends `kind: k8s-event-followup` on an alert's own session once the object has
emitted no new event for the watcher's quiet period (`WATCHER_QUIET_AFTER`, 15m), and only to a
daemon whose `injected` answer to the alert listed `followup` under `features`. `_inject_followup`
reads the session's routing, posts one line into the alert's thread ("Quiet since HH:MM UTC …") and
leaves the same line as a comment on the card the session filed, found by `tasks.session_id` as the
stall watch finds its card. It writes no ledger row, claims no quota and starts no turn: nothing new
happened, something stopped happening. The line says what it is, silence and not a confirmed recovery,
since the watcher reads events only. A session with a card but no thread, every session on an install
without a chat surface, gets the line on its card alone, written before the answer; while the card is still to come (triage queued or running), or the board will not answer, the answer is a 503 the watcher retries each sweep, for an hour from the daemon's first attempt (`FOLLOWUP_CARD_WAIT_SECONDS`); past that, and for a session with
neither, it is answered `unrouted`. It is a reply rather than an edit of the alert: the gateway edits by update key only inside a
conversation request, and the alert was a home-channel post whose key was never bound.

An alert whose investigation never starts gets the same thread's line from `_triage_watchdog`,
armed by `trigger_agent_troubleshooter` as soon as the alert is routed to its thread and before the
gateway is contacted (`TRIAGE_WATCHDOG_SECONDS`, 3600, read like the ceilings so a bad value falls
back): if an hour on the session has filed no card and no report has been stored for the thread, it
says so there, stating those two facts and no cause. A board it cannot read is unknown and keeps it
quiet. An in-process timer, so a daemon restart inside the hour drops it for the alerts before it.

---

## End-to-End Workflow

The diagram below details the lifecycles of alert ingestion, session routing, and interactive GitOps fixes:

> **What joins Phase 1 to Phase 3 is the `incidents` row**, and the kanban notifier writes it in the
> same step that posts the report — the only point where the substituted chat address and the card's
> result are both in hand. Without it `_lookup` finds nothing, the reply passes through unrewritten, and
> the front door gets a bare `apply`; that was `main` between #738, which replaced the egress call that
> used to write the row, and #802, which put the write on the delivery path.

```mermaid
sequenceDiagram
    autonumber
    participant K8s as GKE Target Cluster
    participant Watcher as k8s-event-watcher
    participant Proxy as session_kv_server (Port 8699)
    participant Gateway as Hermes Gateway (Port 8642)
    participant Front as Planning Agent (default profile)
    participant Agent as Cluster Agent for the event's cluster
    participant Fixer as Platform Agent (holds the GitOps write path)
    participant Notifier as Kanban notifier
    participant Chat as Google Chat / Slack
    participant Plugin as incident_context Plugin

    Note over K8s, Watcher: Phase 1: Alert Detection & Initialization
    K8s->>Watcher: Pod Eviction Warning (PDB Violation)
    Watcher->>Proxy: POST /sessions (Creates session ID: k8s-evt-abc123)
    Proxy-->>Watcher: Returns sessionID: k8s-evt-abc123
    Watcher->>Proxy: POST /sessions/k8s-evt-abc123/inject (Payload: Event details)
    Proxy->>Proxy: Spend one of today's alerts for this severity (silently drops if the ceiling is reached)
    Note over Proxy: Record the event in db (intercepted_events table, notified = 1)
    Proxy->>Chat: Post the alert (no diagnosis yet)
    Proxy->>Proxy: Record the alert's platform/chat_id/thread_id under k8s-evt-abc123
    Proxy->>Gateway: POST /api/sessions (session k8s-evt-abc123; lands on the default profile)
    Proxy->>Gateway: POST /api/sessions/k8s-evt-abc123/chat (Route this triage)
    Gateway->>Front: Wake up the front door
    Front->>Agent: kanban_create(assignee=cluster-proj-x-loc, body=the brief, verbatim)
    opt INCIDENT_TRIAGE_OPEN_PULL_REQUEST=true
        Front->>Fixer: kanban_create(assignee=platform, parents=[triage_card_id], body=PR brief, verbatim)
    end
    Note over Front, Agent: The card is subscribed to the alert's thread, not to the api_server origin
    Agent->>Agent: Diagnose (read-only), write the report
    Agent->>Agent: kanban_complete(result=the full report)
    Notifier->>Chat: Post the card's result under the alert's thread
    Notifier->>Proxy: POST /v1/incidents (key the report to that thread, so a reply can be resolved against it)

    Note over K8s, Proxy: Phase 1b: Informational Events Stop Here
    K8s->>Watcher: Normal-type event with a watched Reason (e.g. image-pull BackOff)
    Watcher->>Proxy: POST /sessions/k8s-evt-def456/inject (Payload: Event details)
    Note over Proxy: Record with notified = 0, return {"status": "filtered"}
    Note over Proxy, Chat: No alert, no triage session — counted in the daily recap
    Note over Watcher: Keeps its dedup entry: "filtered" is a policy grade, not a ceiling

    Note over K8s, Watcher: Phase 2: Event Deduplication
    K8s->>Watcher: (Duplicate Warning Event occurs)
    Watcher->>Watcher: Detects active session cache for key
    Watcher->>Proxy: POST /sessions/k8s-evt-abc123/inject (Payload: count=5)
    Proxy->>Chat: Post threaded repeat warning message

    Note over Fixer, Chat: Phase 3: Reporting & Human-in-the-Loop Resolution
    Chat->>Plugin: User replies: "apply" (recommended) or "apply Option B" (Hook: pre_gateway_dispatch)
    Plugin->>Proxy: GET /v1/incidents/by-thread
    Proxy-->>Plugin: Return triage report content
    Note over Plugin: Rewrite message text to prepend triage report context
    Plugin->>Gateway: Dispatch the rewritten message
    Gateway->>Front: Ordinary chat ingress, so it lands on the front door
    Front->>Fixer: Delegate the apply — the Cluster Agent that wrote the report cannot open a PR
    Fixer->>Fixer: Create branch, edit git manifests, open GitOps PR
    Fixer->>Chat: Post threaded reply "Created PR #334"
```

---

## Database Schemas & Storage

Session and incident data are stored in a local SQLite database inside the Platform Gateway pod:

```text
/var/lib/kube-agents/session/session_kv.db
```

### Table Schemas

#### `session_metadata`

Stores the mapping between the troubleshooter session and the platform chat thread:

```sql
CREATE TABLE session_metadata(
  session_id TEXT PRIMARY KEY,
  metadata TEXT NOT NULL,         -- JSON object storing platform, chat_id, thread_id, and timestamps
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### `incidents`

Stores the triage report context for active incident threads:

```sql
CREATE TABLE incidents(
  chat_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  report TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (chat_id, thread_id)
);
```

#### `intercepted_events`

One row per event the watcher forwards, whether or not it was announced in chat, plus one per drift record the detector sends and one per stall alert the stall watch raises. Drift rows are told apart by `reason = 'OutOfBandChange'` and stall rows by `reason = 'ControllerStall'`, and the daily recap excludes both: every number it prints is labelled as the watcher's. Everything below describes the watcher's rows. `notified` is what
lets the `eod-event-watcher-daily-report` cron job report suppressed informational events as a
number instead of losing them; the watcher's own dedup snapshot cannot substitute, because it is a
rolling window of _active_ incidents keyed by `(uid, reason)`, carries no namespace or workload
name, and resets each entry's `count` when its window rolls over.

`notified = 0` covers four unrelated outcomes and the recap must not conflate them. The [severity
gate](#severity-gate) holds back events Kubernetes itself graded informational, and those are the
recap's subject — reported as a count. An alert the [daily ceiling](#daily-alert-ceiling) dropped
carries the same flag but was graded `Critical` or `Warning` and was on its way to chat, so the
recap reads `severity` to tell the two apart. It does not report those: they are outside its scope,
and counting them as informational would inflate the one number it exists to publish. It does count
them privately, because a day that withheld alerts may not be reported as a clean one — the SOP's
"What this recap does not report" owns that contract.

The third is `delivery_error`, and it exists because `notified` is an _intent_ at the moment it is
written. The row goes in before the chat post is attempted: the send runs in a background task, so a
row written afterwards would be lost outright if the process died mid-flight. A send that then fails
comes back and clears the flag, recording why in `delivery_error` — without that correction the row
reads as delivered, and the recap prints its ✅ all-clear over a day an alert never reached anyone.
It is a separate column rather than another `severity` reading because a ceiling drop and a failed
send prescribe opposite remedies: raise the ceiling, or fix the chat credentials. The recap keeps
that distinction in its counts and drops it from its listing: it names neither class, and prints a
separate ⚠️ total for each above the body. No metric counts the delivery-failure class at all, so
that line and this column are together the only record that a delivery failed anywhere in the
system. The SOP's "What this recap does not report" owns the naming-versus-counting line and says
what to query for the detail. Like `cluster`, it postdates the first draft of this table, and the recap selects it only
when `PRAGMA table_info` says it is there — see "Which columns are migrated, and which are not" below for
why that tolerance is in the reader and not in `init_db`, and why `cluster` does not get it.

The fourth is a row folded into its workload's open incident ([Folding a workload's repeat events](#folding-a-workloads-repeat-events)): graded `Critical` or `Warning`, and not posted because an incident for the same workload was already admitted (posted, dispatched headless, or posted and refused by chat). It carries `duplicate_of`, which is how the recap tells it from a ceiling drop. It counts it in neither tally, and keys its group as an alert that went out, so it joins its anchor's group when the reason, severity and delivery match and forms a group of its own when they do not.

It differs from the ceiling in one further respect. `inject_message` returns on a ceiling refusal
before it queues the background task, so no session is ever created. `trigger_agent_troubleshooter`
marks the delivery failure and then still queues the triage start (see "When a triage cannot start"),
so a triage start is queued for every undelivered alert — it only skips
`_register_session_routing` on that branch, leaving the turn with no thread to reply into even
though `_build_agent_query` instructs the model to thread its findings. The turn still writes its
`incidents` row; it just reaches nobody through chat. Nothing announces that session, so the session
log is where it surfaces.

`cluster` is recorded because one session KV database backs every cluster profile in the pod, the
same reason the ceiling is fleet-wide. Without it the recap cannot tell two same-named workloads in
two clusters apart, and it is what lets the recap's header say which clusters a count covers. It
postdates the other columns, and unlike `delivery_error` the reader gets no tolerance for its
absence: `record_intercepted_event` names it in every INSERT, so a table without it records nothing
at all, and the recap reports that as an unreadable ledger rather than a quiet day — see
"Which columns are migrated, and which are not" below.

`object_uid` is recorded, and named in the same INSERT under the same terms, because `workload`
cannot substitute for it: `clean_workload_name` strips the replica suffix before the row is written,
so every pod of one Deployment shares a `workload`, a `namespace` and a `reason`. The recap counts
the alerts the daily ceiling withheld, and those two cases are indistinguishable without it — one
pod re-offered all afternoon writes many rows for one lost alert, while forty replicas failing at
once write rows that look the same and are forty. It is the watcher's own dedup key
(`involvedObject.uid`), which the payload has always carried; the daemon simply did not store it. A
drift row puts the audit entry's Cloud Logging `insertId` here, which plays the same role for the
detector — the one field that separates two rows describing the same object. A stall row puts
the alert's session id here, and names the namespace's stalled objects in `workload`.
A payload without one records `''`, since this pod cannot guess another pod's UID. Rows expire on the same 14-day TTL as the rest of the
database, and on a row cap besides — see "Two bounds, not one":

```sql
CREATE TABLE intercepted_events(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster     TEXT NOT NULL DEFAULT '',
  namespace   TEXT NOT NULL DEFAULT '',
  workload    TEXT NOT NULL DEFAULT '',
  object_uid  TEXT NOT NULL DEFAULT '',  -- the involved object's UID, an audit `insertId` for a drift row, or the alert's session id for a stall row
  object_kind TEXT NOT NULL DEFAULT '',
  reason      TEXT NOT NULL DEFAULT '',
  message     TEXT NOT NULL DEFAULT '',
  severity    TEXT NOT NULL DEFAULT '',
  occurrences INTEGER NOT NULL DEFAULT 1,
  notified    INTEGER NOT NULL DEFAULT 0,  -- 0 when the gate, the ceiling or a failed send held it back; 1 when posted to chat; 2 when dispatched on a headless install with no chat platform enabled
  delivery_error TEXT NOT NULL DEFAULT '',  -- non-empty when the chat post failed after notified = 1
  session_id  TEXT NOT NULL DEFAULT '',  -- the session the inject arrived on, joining the row to GET /v1/sessions/{id}/tasks
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  dispatched_at TEXT,                      -- when the dispatcher created the turn's gateway session (sub-second); a drift-backlog row is stamped at carding; NULL = admitted and not yet dispatched
  backlog     INTEGER NOT NULL DEFAULT 0,  -- drift backlog: 1 owed a digest, 2 digested, 3 filed as a board card (no chat platform), 4 carded
  principal   TEXT NOT NULL DEFAULT '',    -- written on drift backlog rows only, for the digest's count
  payload     TEXT NOT NULL DEFAULT '',    -- drift backlog rows only: the record, to card it from
  triage_state TEXT NOT NULL DEFAULT '',   -- pending when the triage start is queued, then started or aborted; '' when no start was queued (filtered, suppressed, or written before this column)
  duplicate_of INTEGER,                    -- the admitted row this event was folded into; NULL otherwise
  fold_key    TEXT NOT NULL DEFAULT ''     -- what the row folds on: the workload of a pod named like a Deployment's, or a non-Pod object's name; '' when it cannot fold or anchor, was released, or predates the last server start
);
CREATE INDEX IF NOT EXISTS idx_intercepted_events_session_id
  ON intercepted_events (session_id);
```

##### When a triage cannot start

`trigger_agent_troubleshooter` posts the alert, writes `triage_state = pending` on the row, and hands
the session create and the `/chat` turn to a pool of 4 threads (`TRIAGE_START_WORKERS`),
then returns. The pool is the cap: every turn holds a worker of the gateway's default executor, which
has `min(32, vCPUs + 4)` workers (8 on a 4-vCPU node), and session creates and the
readiness probe wait on the same executor. A burst of 20 starts at once on a warm gateway lost half of
them to create timeouts; 4 at once lost none. The 4 depends on that executor's size, so measure it again
(a burst of 20 failing pods against a warm gateway, counting `TRIAGE_STARTED` lines and probe timeouts) on
a Hermes upgrade that changes the executor, or on nodes with fewer than 4 vCPUs.

A start is retried only when it provably did not start: a refused connection, a 429 or 503 (after its
`Retry-After`), a create that timed out or lost its connection (the create waits 25 s, above Hermes'
20 s lock wait, and the retry's 409 counts as created) or got a 5xx, and a `/chat` 404, which creates
the session again once, and a `/chat` request that failed while connecting or sending (a `URLError`;
an error while waiting for the answer is not one). A `/chat` call that was sent and got no answer, or a 5xx other than 503, is
never sent again, because the turn may be running; the row records it as `started`, so `started`
means the turn was sent, not that it ran. The budget is 6 answered attempts and 300 s of time spent on
answered attempts and the waits after them. Refused connections spend neither. A start dequeued more than
20 minutes after it was queued still gets one attempt, and no retry: a long queue behind slow turns is not
a gateway fault, and on `main` the start was at least attempted.

A start that gives up logs `TRIAGE_NOT_STARTED session=<id> source=<event|drift|stall> reason=<...>`,
sets `triage_state = aborted` (the feed reports it as `triage_aborted`, or `undelivered` when the
alert's own delivery failed or is unconfirmed), releases the row's fold anchor
([Folding a workload's repeat events](#folding-a-workloads-repeat-events)), and posts one notice in the
alert's thread: triage did not start, whether the alert comes back, and what to do now. An alert
delivered without a thread gets the notice as a new message that quotes the alert's first line; an
undelivered alert and a headless install get no notice, so the session log and `triage_state` are
their record. `TRIAGE_QUEUED`, `TRIAGE_DEQUEUED` and
`TRIAGE_STARTED` log lines mark the other steps. A restart loses the queued starts, and their rows
stay `pending`.

For installs with no chat platform enabled (or external callers reaching the loopback `127.0.0.1:8699` listener via `kubectl port-forward <agent-pod> 8699:8699`), `session_kv_server.py` also exposes `SESSION_KV_API_KEY`-authenticated read-only feed endpoints (`GET /v1/intercepted-events`, `GET /v1/sessions/{session_id}/tasks`, `GET /v1/tasks`, `GET /v1/tasks/{task_id}`, and the Home tab's `GET /v1/board`) that open `session_kv.db`, `kanban.db`, and the Hermes session stores (`<agent_home>/state.db` and `<agent_home>/profiles/<profile>/state.db`) with `?mode=ro`, scrubbing worker step text through `AuditRedactor` and the shared supplement rules in `agents/platform/scripts/worker_transcript.py` (the same module the bench's trajectory read loads in the pod) before returning it. A `tool_result` whose call read a secret is withheld rather than scrubbed, and the detector fails closed: it reads each simple command of the line on its own and each quoted script (`sh -c '…'`) as a line of its own, serves a `kubectl get` whose resources are other kinds by name, serves one of secrets only when the command is provably the bare list (names, types and ages; no output flag in any spelling, no variable, no substitution), withholds one whose resource it cannot tell (from a file, stdin or a variable), and serves a secret store's command only when its subcommand is one of the store's named lists, so an output form nobody enumerated withholds the result. When `GET /v1/sessions/{session_id}/tasks` is called for a dispatched `intercepted_events` triage session before the front door has filed a kanban card, it returns a sibling `routing` object (`profile = "default"`) beside `tasks: []` (`in_progress` while the front-door turn is active, from the ledger row alone with `live_steps: []` until the gateway session appears in `state.db`, or terminal `completed` / `failed` once `sessions.ended_at` is set for this turn (an `ended_at`, reply or step older than the dispatch belongs to the previous turn of a reused session and is ignored) or the turn — or a ledger row whose gateway session never started in `state.db` — ages past `PRE_KANBAN_STALE_AFTER_SECONDS`, 900 s, measured from the dispatch (`dispatched_at`, stamped by the dispatcher as it creates the gateway session on every path, and by the carding of a drift-backlog row; a row still unstamped when the server starts, from before the column or from a restart between the inject and the stamp, is stamped to `created_at` at startup, so a row unstamped in the running process is queued: `in_progress` from the ledger alone, never aged into `failed`) and the session's start, whichever is later, and the object's `created_at`/`started_at` are that clock; if `<agent_home>/state.db` exists and raises `sqlite3.Error`, `routing` falls back to the ledger-backed object with `live_steps: []` and `routing_error` naming the read error).

One route writes to `kanban.db` and signals processes: `POST /v1/sessions/{session_id}/cards/stop` (#3263). The hermes-bridge calls it when a gateway conversation's `stop` reaches its cards, with the conversation's context id. The route refuses any session but the `a2a-*` one that context id names; that is a consistency check on what the bridge sends, not an authorization. It archives every open card in the conversation through Hermes' own `archive_task`: the cards filed under the session or its compression continuations (not its branches or delegates), every card those cards fanned out, to any depth and whatever session the child carries, and a session-less card subscribed to that session and thread. It kills each running card's worker, and the processes in the worker's group that carry its `HERMES_KANBAN_TASK`, and no others; a command an ssh client already sent to the shell sandbox runs on. Stop doesn't touch cards from other conversations, crons or alerts that depend on this thread's cards: a `task_links` child filed under another session is left alone even though Hermes copies our thread's subscription onto it, and it is promoted once our card is archived, as it would have been when our card finished. The answer names what was stopped, and what it could not finish in time, within 24 seconds (`CARD_STOP_WORST_CASE_S`: a 20-second deadline plus one 3-second busy wait plus a second of slack); an archive still running at the deadline is reported `pending` and finishes on its own. Every holder of `SESSION_KV_API_KEY` can call it for any `a2a-*` session, so anything in the pod that can record a conversation's route can also stop that conversation's cards; the agent itself could already archive cards.

##### Two bounds, not one

This is the only table here whose write rate the cluster sets rather than an operator, so the TTL
does not bound the file. Once the day's ceiling for a severity is spent the daemon answers
`suppressed`, and on that answer the watcher rolls its dedup entry back — so a hundred pods failing
at kubelet's repeat cadence write a row per _sighting_ rather than a row per incident, all day, for
fourteen days. The same database carries thread routing and triage context on the shared session
PVC, so the ledger filling the volume takes those down with it.

`cleanup_old_records` therefore trims the ledger to the newest `SESSION_KV_LEDGER_MAX_ROWS` rows
(200,000) after the TTL delete, and `record_intercepted_event` truncates `message` to
`SESSION_KV_LEDGER_MESSAGE_MAX_CHARS` (512) on the way in. At those bounds the table holds on the
order of a hundred megabytes. The recap's own 120-character cut is a display choice applied at render
time and bounds nothing on disk; 512 keeps more than it shows, which leaves room for a
`FailedScheduling` message that names a predicate per node.

##### Which columns are migrated, and which are not

`init_db` runs `ALTER TABLE intercepted_events ADD COLUMN ...` for `session_id`, `backlog`,
`principal`, `payload`, `triage_state`, `duplicate_of`, `fold_key` and `dispatched_at` (which arrived after the table first shipped in 0.2.0, so released 0.2.0+
databases carry `cluster`, `object_uid` and `delivery_error` and gain `session_id`, `backlog`,
`principal`, `payload`, `triage_state`, `duplicate_of`, `fold_key`, `dispatched_at` and `idx_intercepted_events_session_id` in place on startup), and runs `CREATE TABLE IF NOT EXISTS` for everything else: there is no `ALTER TABLE` for
`cluster`, `object_uid` or `delivery_error` anywhere in the tree. Those three columns landed
before the table ever shipped in a release, so the only databases carrying an earlier shape
without them are pre-0.2.0 dev installs that ran an intermediate commit of the change that
introduced the table (#426), and a migration maintained for a pre-release shape no released
install has is machinery that outlives its reason.

**If you have such a pre-0.2.0 dev install missing `cluster`, `object_uid` or `delivery_error`, you must drop the table before rolling the image.** (Released 0.2.0+ installs already carry all three columns and migrate `session_id`, `backlog`, `principal` and `payload` automatically on startup; do not drop the table on a released install.) `CREATE TABLE IF
NOT EXISTS` is a no-op against a pre-0.2.0 table missing those three columns, so they never appear, and how badly that ends depends on
which column is missing:

- **No `cluster`** — the ledger stops recording anything. `record_intercepted_event` names the
  column in its INSERT, so every write raises `no such column: cluster` into the blanket
  `except Exception` around it, is logged once per event, and is dropped. The table stays empty for
  as long as the shape lasts.
- **No `object_uid`** — the session server does not start. `init_db` creates the partial index
  `idx_intercepted_events_drift_uid` on `object_uid` at import, which raises
  `no such column: object_uid`, and the process exits.
- **No `delivery_error`** — events are still recorded correctly and only the correction fails.
  `mark_delivery_failed` raises `no such column: delivery_error`, the same blanket `except`
  swallows it, and the row keeps `notified = 1`, so the recap counts an alert nobody received as
  one that reached chat.

```bash
sqlite3 "$SESSION_KV_DB_PATH" 'DROP TABLE IF EXISTS intercepted_events;'
```

`init_db` recreates it on the next start. The cost is at most a day of recap data, since the recap
reads a 24-hour window (72 on a Monday) — and on the `cluster`-missing shape there is no data to
lose, because none was ever written.

The reader treats the two shapes the way the writer does. A ledger with no `delivery_error` reads
normally, with the column substituted as empty: that keeps a recap running against a ledger written
by an older _session server_ in the same pod during a rollout, which is a skew a drop cannot fix. A
ledger with no `cluster` or no `object_uid` is a **read failure** — the recap prints the 🔴 card
naming the path
instead of a green all-clear over a table nothing can write to. That card is the only warning this
condition produces; the per-event log lines go to the session server's own stderr, which nobody
reads on a day the recap said everything was fine.

#### `alert_quota`

Tracks how much of each bucket's daily allowance has been spent, and how many alerts the ceiling dropped:

```sql
CREATE TABLE alert_quota(
  day TEXT NOT NULL,              -- UTC YYYY-MM-DD
  severity TEXT NOT NULL,         -- Critical | Warning | Info | GitOpsDrift
  sent INTEGER NOT NULL DEFAULT 0,
  suppressed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, severity)
);
```

The column is named `severity` and three of its four values are one, but the key is whatever
`_claim_alert_quota` was handed: `GitOpsDrift` is a billing bucket for records that display as
`Warning`. `GET /v1/alert-quota` reports the row as it finds it, so a reader sees four buckets.

Rows age out after `SESSION_KV_CLEANUP_TTL_DAYS`, along with `session_metadata` and `incidents`, so roughly two weeks of history is available to answer "what did we drop last week".

The same database also holds `findings` and `queue_publications`, the findings queue's tables. They are exempt from the TTL sweep — a backlog item disappears when its state says it is finished, not when it gets old. [`docs/designs/inventory-findings-queue.md`](../../../docs/designs/inventory-findings-queue.md) §3.1 is the canonical description of both.

---

## Severity Gate

`get_severity_details` classifies an injected event from its Kubernetes `Event.Type` and `Reason`:
a `Warning` whose reason blocks drain, eviction, or scheduling is `Critical`, any other `Warning` is
`Warning`, and everything else is `Info`. The watcher filters on `Reason` alone and never on
`Event.Type` (`k8s-operator/cmd/k8s-event-watcher/filter.go`), so `Normal`-type events do arrive
here — image-pull `BackOff` being the routine one.

`Info` events are recorded and then dropped: no chat post, no troubleshooter session, and `inject`
returns HTTP 200 with `{"status": "filtered"}`. The 200 is deliberate — a non-2xx counts as an
`injectErrors` failure, so returning one would make a suppressed event look like a broken bridge on
the watcher's metrics.

The word in the body is deliberate too, and is not the `"suppressed"` the daily ceiling answers with
above. The watcher discriminates on that string and nothing else: on `"suppressed"` it drops its
dedup entry so the workload is re-offered, which is right for a ceiling that resets at 00:00 UTC and
wrong here. An `Info` grade is a property of the event, not of the day — the next sighting would
grade the same way — so sharing the word would re-offer every quiet workload at its own repeat
cadence, spending a session, an inject and a ledger row per sighting on an alert nobody was ever
going to receive. On `"filtered"` the watcher keeps the entry and counts
`k8s_event_watcher_events_policy_filtered_total`.

The two halves of that exchange ship on different images — the gate in `session_kv_server.py` on the
agent image, the `"filtered"` handling in a sidecar binary — so either can be older than the other,
and the skew is not equally safe in both directions. An older daemon says `"suppressed"` for both,
which reopens: one redundant session, no silence. An older watcher is the harmful direction, because
it reads the unknown status as delivered, keeps the entry, and has no `MarkPolicyFiltered` to flag
it with — so the re-open described in the next paragraph can never fire and the family's `Warning`
members stay deduped behind the entry for as long as they keep arriving. The daemon therefore does
not answer `"filtered"` unless the caller asked for it: the watcher sends
`X-Watcher-Features: policy-filtered`, and a request without that token gets `"suppressed"` and the
reopen-on-next-sighting behaviour an older watcher already handles correctly.
`k8s-operator/cmd/k8s-event-watcher/README.md` owns that contract in full. The pair is pinned by
`test_the_gate_and_the_ceiling_do_not_answer_with_the_same_word`,
`test_a_watcher_that_cannot_handle_filtered_is_not_sent_it` and by
`TestDispatcherKeepsDedupOnPolicyFilter`.

Keeping the entry needs one qualification, because the entry is not keyed on the event that was
graded. The watcher's key is `(uid, canonical reason)`, and `canonicalizeReason` folds a whole
failure family onto one of them: kubelet's `Normal`-type `BackOff` ("Back-off pulling image"), the
`ErrImagePull` beside it, and the `Warning`-type `Failed` that follows are one incident. A bad image
tag can therefore put the routine member in front, and every `Warning` behind it would be deduped
against an entry held on behalf of an event nobody was told about — permanently, since each sighting
slides the window forward. So the watcher lets the first event the daemon would post re-open the
incident, once per window, counted in `k8s_event_watcher_events_policy_reopened_total` and pinned by
`TestDispatcherReopensPolicyFilteredKeyForWarning`. That is `Warning`, or an empty `Type` — the
endpoint coerces with `payload.get("type") or "Warning"` before grading — and nothing else: a type
the daemon would grade `Info` is refused the reopen rather than spending the family's only one on an
event that comes straight back `filtered`. The watcher README owns that rule in full. That re-opened
event arrives with `count = 1` rather than the family's accumulated count, so `occurrences` keeps
meaning "sightings this row stands for" — the invariant the recap relies on when it sums the column
into "Forwarded _N_ events" and ranks its incident list by it.
`TestDispatcherReopenedPayloadCountsFromOne` pins that number.

The gate is a plain `severity == "Info"` test, and `get_severity_details` reaches that label on
`Event.Type` alone: `Warning` grades `Critical` or `Warning` depending on whether the reason matches
a blocker keyword, and everything else grades `Info`. There is no reason-based exception, which
means a `Normal`-typed event is filtered however serious its reason sounds.

That is a real limit rather than an oversight, and it sits one layer up. Kubernetes types some
failures `Normal` — kubelet records node readiness transitions with
`recordNodeStatusEvent(v1.EventTypeNormal, events.NodeNotReady)`, and the node-lifecycle controller
records the same transition the same way — so a `NodeNotReady` reaching the daemon would be graded
`Info` and filtered. None does: the `--reason` list the operator passes in
deploy/shared/start-services.sh does not forward `NodeNotReady`, and neither the daemon nor a second
copy of a reason list is the place to fix that. `deploy/shared/start-services.sh` decides what the
daemon sees at all, and widening it is what would make a node loss reportable.
`test_the_event_type_is_the_only_thing_that_lifts_an_event_above_info` pins the grading rule so it
is not re-derived by accident.

The label also decides which daily ceiling the alert draws on, so a `Warning`-typed node loss is
billed to `ALERT_DAILY_LIMIT_WARNING` rather than to the informational bucket.

---

## Verification & Troubleshooting

### Check Today's Alert Budget

Suppression is silent in chat, so this is how you tell a quiet day from a capped one:

```bash
kubectl -n kubeagents-system exec deployment/platform-agent-gateway -c platform-agent -- \
  sh -c 'curl -s -H "Authorization: Bearer $SESSION_KV_API_KEY" \
    http://127.0.0.1:8699/v1/alert-quota'
```

Pass `?day=YYYY-MM-DD` for a past day. To see which workloads were dropped:

```bash
kubectl -n kubeagents-system logs deployment/platform-agent-gateway -c platform-agent | grep "Suppressed"
```

### Check Persisted Incidents

To view currently registered incident triage reports. Through Python rather than
the `sqlite3` CLI, which the sandbox image does not ship — the interpreter that
runs the server is the one tool guaranteed to be able to read its database:

```bash
kubectl -n kubeagents-system exec deployment/platform-agent-gateway -c platform-agent -- \
  python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/kube-agents/session/session_kv.db')
for r in c.execute('SELECT chat_id, thread_id, created_at FROM incidents ORDER BY created_at DESC'):
    print(r)
"
```

### Verify Inbound Plugin Activity

Filter container logs to trace whether the `incident_context` plugin is successfully intercepting threads and rewriting messages:

```bash
kubectl -n kubeagents-system logs deployment/platform-agent-gateway -c platform-agent | grep -E "incident_context|inbound message"
```
