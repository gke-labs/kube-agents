---
name: alert-history
description: Answer a question about what the alert path saw and did from the on-pod event ledger — what alerts fired overnight, which were held back from chat and why, what the incidents from today were, whether a workload's failure was already alerted and which thread or pull request it has. Never a fleet audit's findings (that is fleet-audit-reports) and never a new look at the cluster.
---

# alert-history — Reading What the Alerts Were

Every event a producer forwards to the agent — a cluster event from the event watcher, a GitOps
drift record, a controller stall — leaves one row in the event ledger on the agent pod, whether or
not it reached chat. This skill answers questions off that ledger. It looks at no cluster and
starts no triage.

## Query it; do not look for the file

Call the `get_alert_history` tool. It is the only route: the ledger is `session_kv.db` on the agent
pod, served over loopback, and your shell runs in the sandbox pod, which reaches neither. Do not
`kubectl get events` instead — that is a new look at the cluster, and it cannot say what the agent
did with an event.

`get_alert_history(since_hours=24, limit=50)` returns one page of the window's rows, newest first,
with `returned`, `truncated` (the window holds more rows than the page) and `window_start`.
The window is at most 31 days (744 hours); the ledger holds only its retention, 14 days by default, so a
wider window returns what it still holds. The page is at most 200 rows, written compact; a `message`
longer than 240 characters is cut there and ends in ` ... [truncated]`.

Each row carries `id`, `timestamp` (UTC), `cluster`, `namespace`, `kind`, `name`, `reason`,
`severity`, `message`, `session_id`, `pull_request`, `duplicate_of` and `status`:

| `status`         | What became of the event                                                                                                                                   |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `injected`       | posted to chat (or dispatched headless) and its triage queued or started                                                                                   |
| `triage_aborted` | posted, but the triage turn never started                                                                                                                  |
| `undelivered`    | admitted, but the chat post failed; it may not have been seen                                                                                              |
| `filtered`       | Info: held back by the severity gate, never offered to chat                                                                                                |
| `suppressed`     | Warning or Critical, not posted and not for any reason below: the daily alert ceiling, mostly                                                              |
| `folded`         | a repeat of the row named in `duplicate_of`, no new alert                                                                                                  |
| `backlog`        | a drift change sent to the digest (late, or a write to kube-agents' own objects), digested or owed a digest (a card on a headless install), no triage turn |
| `excluded`       | held back by an alert-exclude rule                                                                                                                         |

## The questions this gets asked

**"What alerts fired overnight?"** — the rows with `status` `injected`, `triage_aborted` or
`undelivered`: one line each with the time, cluster, namespace, workload, reason and, where set,
the pull request. Then what was held back (`filtered`, `suppressed`, `excluded`, `backlog`) and
folded: name each (namespace/workload, reason) when there are five or fewer, counts by status beyond
that. A held-back event is what the person cannot otherwise see, so it is never only a number when
it can be a name. No rows in the window is "nothing was forwarded", not "nothing is wrong":
the ledger records what producers sent, not what the fleet is doing.

**"Show me the incidents from today."** — the same admitted rows. The triage report itself is in
that alert's thread and on its card; the ledger does not hold it.

**"Was this workload already alerted?"** — filter the rows by `namespace` and `name`; a `folded`
row with a `pull_request` was held against that fix at the time. Whether the fix is still open is
the pull request's state, not the row's.

Widen `since_hours` for "this week" and raise `limit` (to 200 at most) when `truncated` is true;
if it still is, narrow the window (fewer `since_hours`) rather than asking for more rows, and say
the window holds more rows than one call returns. A row is dated at its first
sighting: an `excluded` event that keeps recurring is one row an hour, each dated at that hour's
first sighting.

## Red lines

- **Never read `session_kv.db`, `GET /v1/intercepted-events` or the agent pod's filesystem
  yourself.** The tool is the route.
- **Never report an `ERROR:` from the tool as a quiet night.** A failed read is one plain line in
  your own words ("I couldn't read the alert record just now.") with what would work next: ask
  again shortly, or the operator checks the agent pod. Never the `ERROR:` text, its code or its
  exception.
- **Never diagnose from a row.** A row says an event was forwarded and what the agent did with
  it; the diagnosis is in the thread or on the card.
- **Quiet follow-ups are not here.** The watcher's "quiet since" note lands on the alert's thread
  and card only; the ledger has no row for it.
