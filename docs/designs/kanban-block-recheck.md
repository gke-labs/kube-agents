# A blocked card's condition is re-checked

Issue #5260. The ask, from the PoC report tracked in #2597: ten cards sat `blocked` for up to
thirteen days, each describing a condition that had already cleared, and the board-health digest
re-reported them. Every way to unblock a card needs a person (Retry and Drop on the digest's card,
`hermes kanban unblock` on the pod). Can a block carry an expiry, after which the harness
re-evaluates the condition?

## What a block records today

A worker blocks its card with `kanban_block(reason, kind)`. `kind` is one of `dependency`,
`needs_input`, `capability`, `transient`; `reason` is prose for the person on the board.
`transient` is the kind upstream reserves for "a flaky failure that may clear", and upstream routes
it to `blocked` like the two human kinds. The harness blocks cards itself in two places: the
dispatcher's circuit breaker (`_record_task_failure`: a `gave_up` event after two non-success
attempts) and the rate-limit exit (`deploy/docker/patches/kanban_guardrail_exit.py`:
`block_task(kind="transient")`). In the customer's run, 13 of 16 blocked cards were breaker trips on
an exhausted iteration budget; #4902 saves the worker's hand-off for the next attempt, and nothing
starts that attempt.

`recompute_ready` never promotes a sticky block or a card at the breaker limit. `unblock_task` is the
one exit, and Retry calls it (`session_kv_server` `POST /v1/board/actions`). A block records a
reason, a kind, and the time of its event. Nothing records a resource or a predicate, so nothing can
re-evaluate a block mechanically. The digest's rule, upstream's `stuck_in_blocked`, goes quiet after
any `commented` or `unblocked` event on the card: a re-check that commented would silence the digest
the customer asked to keep.

## Design

A block carries a condition and an expiry. A scheduled job re-evaluates the condition without a
model call, retries the card when it has cleared, and leaves a note the digest reads when it holds.

### The record

`kanban_block_rechecks`, a side table on the board beside Hermes' own, one row per card
(`agents/platform/scripts/kanban_block_recheck.py`, `TABLE_DDL`):

| Column                        | Meaning                                                                       |
| ----------------------------- | ----------------------------------------------------------------------------- |
| `task_id`, `block_event_id`   | The card, and the `blocked`/`gave_up` event the record describes.             |
| `kind`                        | The block's kind (`transient`, `needs_input`, …, or `gave_up` for a breaker). |
| `condition`                   | JSON: `{"kind": "time"}` or a `k8s` condition (below).                        |
| `check_after`, `expires_at`   | Not evaluated before the first; not evaluated after the second.               |
| `checks`, `last_checked_at`   | How many times, and when last, the job evaluated it.                          |
| `last_outcome`, `last_detail` | What it found: `still_blocked`, `unknown`, `expired`, `retry_limit`.          |

A record is tied to one block event. When the card leaves `blocked`, or blocks again with a newer
event, the job drops the record on its next run and derives a fresh one if the new block qualifies.

### Two condition kinds

**`time`** clears when `check_after` passes. The job derives it, with no record from anyone, for the
blocks the harness files itself, so cards blocked before this job existed are covered on its first
run: a breaker trip (`gave_up`) retries one hour after the trip; a `transient` block (the rate-limit
exit, or a worker's `kanban_block(kind="transient")`) thirty minutes after the block
(`RETRY_AFTER_SECONDS`). A `needs_input`, `capability`, `dependency` or untyped block is a question
for a person and gets no record.

**`k8s`** names an object on a cluster and the predicate its kind implies (`PREDICATE_BY_KIND`): a
pod is `Ready`, a deployment is `Available`.

```json
{
  "kind": "k8s",
  "project": "my-project",
  "location": "us-central1",
  "cluster": "prod-a",
  "namespace": "shop",
  "resource": "deployment/web"
}
```

The job reads it with one `kubectl get <resource> -n <namespace> -o json` through the shell sandbox,
on the per-cluster kubeconfig `stall_watch.py` keeps there (`fetch_credentials`, `run_sandbox`): the
agent container carries no kubectl. An object the cluster says is not there holds the block (the
named object never becomes Ready); a read that fails for any other reason is recorded as `unknown`
and retried on the next run. A `k8s` condition is recorded with `--watch` on the script's command
line on the pod, or through `record_condition`; without an expiry it stops after a week
(`DEFAULT_K8S_EXPIRY_SECONDS`).

### The re-check

`kanban-block-recheck` is a `no_agent` job on the Platform Agent's roster, four times an hour. A
run derives the time records, drops the stale ones, and evaluates each record whose `check_after`
has passed and whose `expires_at` has not:

- Cleared: the card is retried through Hermes' `unblock_task`, the call Retry makes, which returns it
  to `ready` (or `review`, where it left off) with its failure counter reset. The record is removed
  and a `block_rechecked` event with `outcome: retried`, the condition and the time goes on the card.
  The run prints one line, which `deliver: "chat"` posts: "Retried **Investigate web**: it blocked 1
  hour ago and the wait has passed." A quiet run prints nothing.
- Holds: the card stays blocked; the record's `checks`, `last_checked_at` and `last_outcome` are
  refreshed. No comment is written, for the reason above.
- Past its expiry (`k8s` only): `outcome: expired`, said once on the card and once in chat ("…did
  not become Available before the re-checks ended."), and the record is left for the digest. The
  retry limit below is said the same way, once, so a card that stays blocked for a person is not
  silent for it.

The job opens the board the way the Session KV server's card stop does: a plain connection with
Hermes' PRAGMAs and a three-second busy timeout, not `kanban_db_connect.connect`, whose first call in
a process takes a cross-process init lock, probes the whole file and runs the migrations, a cost
with no benefit four times an hour on a board the dispatcher initialised. Before the unblock it
re-reads the card's newest block event and drops the record when that event is not the one it
describes: a slow cluster read earlier in the run is time enough for a person's Retry and a worker's
new `needs_input` block. The `retried` event is written before the unblock, so the once-per-card
bound holds even if the process dies between the two writes.

Bounds, so a retry cannot spin:

- One automatic retry per card for its life (`MAX_AUTO_RETRIES_PER_CARD`, counted from the card's own
  `block_rechecked` events). A second cleared condition is recorded as `retry_limit` and waits for a
  person. Upstream's own unblock-loop breaker still routes a second same-kind `kanban_block` to
  `triage`.
- At most three retries per run (`MAX_RETRIES_PER_RUN`), so the first run after an upgrade does not
  start every old breaker trip at once; the rest wait for the next run.
- The respawn guard still applies to a retried card in `ready`, as it does after a person's Retry.

### The digest

The board-health row's status line gains a second sentence from the record, so a repeat is not bare
(`kanban_board_health.recheck_line`): "Re-checked 2 hours ago, still blocked.", "Retries
automatically in 40 minutes." before the first check, "Re-checks ended 3 days ago, still blocked."
after the expiry, "Re-check 1 hour ago could not read the cluster.", or "Condition cleared 2 hours
ago; already retried once, so it waits for someone." A row that groups cards carries the
longest-waiting card's sentence. The card file and its Retry and Drop buttons are unchanged; the
daily cadence and the once-a-week repeat do not change.

The digest's rows come from upstream's `stuck_in_blocked` rule, which keys off a `blocked` event. A
breaker trip writes `gave_up` and no `blocked` event, so a breaker-parked card never had a row; and
the rule goes quiet after any `unblocked` event, which this job's retry writes, so a `transient` card
the job retried and the breaker then parked would have left the digest for good. The digest now
reads breaker-parked cards from the board itself (`kanban_board_health.breaker_parked`: `blocked`,
a `gave_up` as the newest of its block, unblock and comment events, a day old or more) and lists
them beside the engine's, once each, as "Stopped N days ago before finishing." with the record's
sentence.

## What this slice does not do

- A worker cannot record a `k8s` condition when it blocks. That is an optional `condition` on the
  `kanban_block` tool, a change to tool text the model acts on, so it runs the eval loop
  (`.agents/rules/eval_driven_development.md`) as its own change.
- A condition the harness cannot read: an external ticket, a person's answer, a block whose reason
  names no object. Those stay with the digest and its buttons.
- Predicates beyond a pod's `Ready` and a deployment's `Available`. A caller that needs a
  `statefulset`, a `job`, or a custom resource's condition adds it to `PREDICATE_BY_KIND` with its
  check.

## Where it lives

- `agents/platform/scripts/kanban_block_recheck.py`, tests beside it; the roster entry in
  `agents/platform/cron/jobs.json`; the digest line in `kanban_board_health.py`.
- The operator's notes: `agents/platform/cron/README.md`, "`kanban-block-recheck`".
