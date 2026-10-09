# Fleet Audit — The Report Store

> **Status — implemented.** `finish` writes the store and reads its own previous-run memory from
> it; `report_status.py` projects it, the `fleet-audit-reports` skill answers questions off it, and
> `make fleet-audit-view` renders it for an operator.

**Scope:** where an audit run's structured output is kept after `finish` publishes it, what reads
it, and what a run does when it is missing.
**Builds on:** [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md), which stays the design
of record for the ledger, delta, promotion and rendering contracts. This document changes one thing
there: where the delta's memory of the previous run comes from.
[`fleet-audit-collector-manifest.md`](fleet-audit-collector-manifest.md) §3.3 and §4 describe the
held set and the coverage gap a lost memory costs.

## 1. The problem

Two costs share one cause. A user asking "what did the last compliance audit find?" or "what changed
since last week?" cost a `gh issue view` plus model turns re-parsing rendered prose back into facts
the harness held in structured form seconds before it published. The findings document survived
only until the next run overwrote it in scratch, and the ledger rewrites itself in place, so
run-over-run comparison had no source at all. And `finish` re-fetched the previous ledger body every
run to parse its own hidden `<!-- audit-findings: … -->` block back out: a public issue body, which
anyone with write access can edit between runs, was the harness's database. It is still read on
every run, but as a check that the store is current (§4), and as the memory only once, to seed a
store that has never held this ledger (§4).

Both are the same missing thing: the run's structured output, kept where it was produced.

## 2. The store

`finish` keeps what it publishes. On the exit-0 path only — clean and findings branches alike, never
on `--dry-run`, never after a rejection, never from `remediate` — it writes one envelope under
`<root>/<audit-id>/<owner>/<name>/`, the directory of the repository the run published to:

- `runs/<finished-at, UTC, filename-safe>.json` — the envelope. The ring prunes to the newest 14 at
  write time: two weeks of a daily stream.
- `latest.json` — a byte-identical copy of the newest envelope. A copy rather than a symlink, so the
  store asks nothing of the mount.

The repository is part of the path because every SOP walks its `managed_repos` and runs `finish`
once per entry: one stream publishes one ledger per repository, and a directory per stream would
hold whichever repository finished last, so each run would find the other's memory and read it as
lost. `repo` must be exactly `owner/name` with no `.` or `..` segment before it becomes a path; a
run whose repository fails that stores nothing. The path is lower-cased, because GitHub's names are
not case-sensitive: an on-demand `--repo Acme/GitOps` and a ConfigMap's `acme/gitops` are one
ledger, and two directories for it would each trust a memory the other had moved past.

Both writes are atomic (`os.replace` from a temp file in the same directory). The envelope carries
`audit_id`, `repo` and `finished_at`, the run's outcome (`status`, `issue_number`, `issue_url`,
`partial`, `coverage_gaps`, `declared`, `unaccounted`, the PR URL lists, `silent_ok`,
`ledger_held_open`), the collector keys and the phase timers (`inspect_s`, `publish_s`, `collect_s`)
the JSON line carried, the delta as id lists (`new_ids`,
`resolved_ids`, `current_ids`, `id_scheme`), `ledger_body` — the body this run left on the issue —
and `document`, this run's validated findings document, whole rather than clipped to the body's
budget. The body's redaction backstop is applied to every string on the way in, so the envelope
never holds a credential shape the public issue blanked — except a finding's `id`, which the hidden
block publishes raw, and which a long object name can make look like a token. `current_ids` is
exactly what the body's hidden block lists: the findings the body rendered plus the collector-held
ids.

The write is best-effort: a store that cannot be written logs a warning and never changes the run's
exit code. A failed write deletes `latest.json` on its way out, because the file left behind
describes an older run and nothing in it says so — the next run would trust it, and a reader would
quote it as current. A held-open clean run is the exception: it only comments, so the body is the
one the file describes, and a failed write keeps the file rather than cost the next run its memory. An absent store is unknowable, and every reader handles that; a stale one
passes the issue and repository checks, and only the comparison against the live block in §4 tells
it from a fresh one. For the same reason `finish` deletes `latest.json` just before each call that
rewrites the ledger: the findings rewrite and the coverage issue a clean run opens. A run killed
between that call and writing the store leaves no envelope rather than one describing the run before.
The clean close is the exception, and `finish` deletes the file just after it: a close leaves the
body as it was, so until it lands the stored memory is still exactly the open ledger, and a close
that fails leaves the memory in place. A run killed between the close and the delete, or a close that
lands on GitHub but reports failure, leaves a record naming an issue that is now closed. A later run
does not trust it while that issue stays closed, since the trust check needs it to be the open
ledger; a reader quoting it shows the last findings run as latest until the next run rewrites it.
Deleting first would trade that for a lost memory on every close that genuinely fails. A lookup that fails earlier, a run killed in the comment read or label sync that precede the
findings rewrite, or a clean run held open that only comments, has changed nothing the envelope
describes and leaves the memory in place. The ring is left alone, and the readers answer from its
newest entry with `latest_missing: true`: the stream did run, and a later run may have changed the
ledger unrecorded. A ring entry newer than a `latest.json` that is present is answered the same way,
flagged: the ring entry is written first, so a failure between the two writes leaves `latest.json`
one run behind the ring, and the readers compare its `finished_at` against the newest entry's name
rather than trust whichever file exists. Pruning runs in its own `try`: a
failed prune has not damaged the memory the run just wrote.

`issue_number` and `ledger_body` are a claim about the live ledger, so a clean run that leaves the
ledger open — over a coverage gap or an unaccounted previous finding — only commented on it, and its
body still renders the previous run's findings. That path stores the previous body and ids forward
instead of its own empty set; recording `[]` would hand the next run a trusted memory of an empty
ledger, and every finding the body carries would be announced as new. The document that body renders
rides beside it as `ledger_document`, which only `finish` reads, for titles. It is known only from
a run that wrote the body; after a seed, whose memory has no document, it is absent until a
findings run rewrites the body, rather than a held-open run's empty document standing in for it. `document` stays this
run's, because it answers what this run checked and skipped, and a reader asking that must not be
handed the previous run's scope under this run's status; `ledger_held_open` tells that reader the
issue still lists findings this run's zero does not. Where the previous memory is itself lost, the
envelope names no issue, so the next run's trust check fails as a lost memory should. Any run over
a lost memory also writes `delta_known: false`: its `new_ids` and `resolved_ids` are empty because
the delta was withheld, and the readers (`make fleet-audit-view`, `report_query.py streams` and
`show`) render it as unknown rather than as a run that changed nothing. Envelopes written before the
key existed lack it and read as they always did. A run that
closes the ledger names no issue either: reopened by hand, the issue is not the empty ledger that
run left.

## 3. Where it lives

The root is `/opt/data/fleet-audit/reports`, overridable with `FLEET_AUDIT_REPORTS_DIR` (the test
suites point it at a temp tree). It is fixed rather than under `$HERMES_HOME` because the cron or
kanban worker that runs `finish` has `HERMES_HOME` set to its profile directory and a chat session
has it set to `/opt/data`; a store rooted there is written to one path and read from another, and
the only symptom is a chat path that never finds a report.

`finish` runs in the agent's shell, so the store is on whichever pod that shell runs in. With the
shell sandbox enabled that is the sandbox pod's `shell` container, whose `/opt/data` is a volume of
its own, separate from the gateway's; without it, the gateway pod's `platform-agent` container. The
in-flight notes `start` leaves in `/opt/data/scratch` are on the same pod. `make fleet-audit-view`
probes the agent pods for both containers and reads the sandbox first.

The store is written as 0755 directories and 0644 files whatever the writer's umask, and a `finish`
run as root, which is what a hand-run over `kubectl exec` is, gives each directory and file it
creates to the owner of the nearest directory that already existed. Mode alone would not do: the
tick and every session run as uid 1000, which can read a root-owned directory but cannot write the
next run into it. The residual is a store a root run wrote before this handover existed, or one
under a root-owned ancestor; the next run's warning names the path and both uids, and only an
operator restoring ownership clears it.

## 4. `finish`'s own memory

The previous run's memory is the `latest.json` in this run's repository directory, trusted when its
`issue_number` is the open ledger `find_existing_issue` just returned and its `repo` is this run's.
The path already selects the repository; the envelope's own `repo` is checked as well, so a file
moved or copied between directories is not trusted for a ledger it was not written for.

Those two say the record is about this ledger, not that it is the latest word on it. Anything that
rewrites the ledger without writing this store leaves a record that passes both: a `finish` from an
image that predates the store, during a revert or a mixed rollout, or the other pod's copy after the
shell sandbox is toggled (§3). Joined against it, the delta would re-announce what that window added
and resolved, and a clean run could close over findings only the window reported. So the record is
also checked against the ledger itself: `find_existing_issue` lists the open issue with its body, and
the record is trusted only when that body's hidden block names the same ids as the stored
`ledger_body`'s. The block rather than the whole body, because it is what every join reads and it
survives the newline and prose edits GitHub or a person can make around it. The listed body is a
check on the store, never a memory in its place: a mismatch, or a listing that returned no body, is
a lost memory, and the store is not re-seeded from the issue. One consequence is deliberate: a
public edit that changes the block's id set makes the memory lost. That fails safe — no delta claim,
and a clean run holds the ledger open rather than closing it — and it lasts, because a held-open
clean run stores `issue_number: null`, until a findings run rewrites the body. While it lasts, a
clean run's comment gives the lost-record way out — a findings run or a maintainer, since coverage
may be complete and better coverage does not close it — and its heading says the store lost its
record unless a coverage gap stands beside it. With a manifest, the run answers a standing
`/remediate` the same way, except on a target the collector flags or holds, a posture withheld for
want of a declared-intent search, or one a declaration covers, which get their deferral or refusal
instead.

The identity scheme is not a trust condition. The stored body carries its own `audit-id-scheme`
stamp, and the readers that join against it re-spell a previous scheme's rows exactly as they did
when the body came from GitHub, so a scheme bump costs what it always cost.

`finish` parses the previous ids and titles out of the stored `ledger_body` with the same readers it
used on a fetched body, so every join — delta, held set, carried held rows, the clean-close hold —
is unchanged. Titles are also read from the stored document the body renders (`ledger_document`
where the body was carried, `document` otherwise), which names findings the body budget cut. The
issue body is not a fallback for a store that exists, because two memories with a precedence rule
is how a divergence becomes undetectable.

`start` joins against the same memory for the `carried` list it hands the model, seed included.

The issue body is used as the memory once, where the store has never held this ledger (every run
still reads it, but only for the check above): no directory for the stream
and repository at all, as on the first run after an upgrade that introduces the store or after the
volume is replaced. That run would otherwise have no previous ids, so the guard that refuses to
close over findings the document does not account for would have nothing to check, and an empty
document would close the ledger and its pull requests. The body's hidden block is the id set the
last run published, so it stands in for the store this once; the run writes the store, and every
later run reads that. The seed is the body `find_existing_issue` already listed, so it has no failure
point of its own; `gh issue view` fetches it only when the listing brought no body.

When no ledger is open, the run is first and everything present is new. When a ledger is open but
its `latest.json` is missing, unreadable, written for another issue, or names a different id set from the ledger's
block, or the listing returned no body to check it against — or there is no store and
the body has no readable block or cannot be fetched — the memory is **lost**, unknowable rather than
empty:

- The run publishes with no delta claim: `new: 0`, `resolved: 0`, the delta comment skipped, and a
  log line saying the previous run's findings are unknowable.
- The body is rewritten. Freezing it until a run could read its memory would freeze it for good,
  since only a run that writes the body restores the store. Ids held on the lost body are no longer
  carried; with a manifest their pull requests stay protected by the still-flagged set.
- A clean run never closes the ledger and retires no pull request, the compliance shield's close (a fix a declaration forbids) aside. It files a lost-memory coverage gap, stays open, and reports partial:
  the collector's gap while it still flags something the document does not carry, and otherwise the
  gap saying nothing shows whether the ledger's findings were fixed. A manifest covers only the
  collector's checks, and without one there is nothing, while an empty document would close the
  ledger and the pull requests of findings no collector looks at.
- A run with neither a memory nor a manifest answers no `/remediate`; the next run with a memory
  answers them.

A lost memory therefore never puts a wrong count in a public issue. The delta annotation costs one
cycle when that run writes the body, which a findings run does. A clean run held open writes nothing
to it, so the memory stays lost, and each such run files the gap again, until a findings run
rewrites the body or a human who has checked the findings closes the ledger; the gap names both
ways out. The held rows cost more: a findings run rewrites the body
without them, so the next run's memory has no marker id to hold and they are not rendered again.
While the collector flags them they stay on each run's JSON line as `unpublished_candidates`, and
their pull requests stay open; what is lost for good is their row on the ledger. A seeded run keeps
them, since it holds the body's marker ids as any other memory does.

The hidden block stays in every ledger body. It was never only `finish`'s round-trip state: the
bench verifiers grade audit evals by parsing ids out of the published body, and it is the one way a
human or an external tool recovers a run's id set with no pod access. The identical block in
remediation pull request bodies keeps both its write and its read, because reconciliation has to
work from the live pull request list, which humans change between runs.

## 5. Liveness

Whether a run is in flight comes from the lease `start` takes — the in-flight note
`/opt/data/scratch/inflight_<audit-id>.json` — not from a file in the store. `report_status.py`
reads it with the lease's own TTL (`INFLIGHT_TTL_SECONDS`, copied into `report_status.py` as
`INFLIGHT_TTL_S` and pinned equal by a test) and reports each stream, across all its repositories,
as `never` (no lease and no stored run), `completed`, `running`, `died` (a lease older than the TTL
that never finished), or `error` (a store file that would not parse, a scratch directory or note
that could not be read, or a repository directory spelled in a case no reader opens). The lease is
read first, so an unreadable store file does not hide a run that holds, or died on, the stream; the
error rides beside it. A ring whose `latest.json` was deleted is `completed`, projected from its
newest entry with `latest_missing`. A note that exists but cannot be parsed is a `start` that has
claimed the lease and not yet written it, and counts from its mtime. A first run in flight is
`running` before its store directory exists.

## 6. Readers

**The agent.** The Planning Agent has no file tools; a question about an audit is delegated to the
platform specialist, which runs in the same shell `finish` does. The `fleet-audit-reports` skill
answers from the store through `report_query.py`, whose subcommands (`streams`, `show`, `findings`,
`finding`, `checks`, `diff`, `runs`) each return one small JSON object. `streams` returns a row per
stream and repository; the others take `--repo`, which may be omitted when the stream has published
to exactly one. An owner directory that cannot be listed makes every per-stream answer the unreadable
one `streams` gives, not "no record". Every answer is bounded and
the full document is opt-in: `show` omits `document` so the cheap call stays cheap, and `finding`
returns one finding's prose. `checks` reaches `scope.clusters[].checks_run[]`, which a finding-heavy
ledger drops from its body with a notice pointing at the stored report. The reader is its own skill
because skill selection runs off the description, and `fleet-audit`'s describes publishing; a
question about a past run should not pull the publish procedure into context.

**The operator.** `make fleet-audit-view` streams `report_status.py` into the pod on stdin
(`kubectl exec -i … -- python3 -`), so it works against an image built before the script was, and
renders one row per stream and repository, labelled with the repository only when a stream has more
than one. A row taken from the ring carries the `UNRECORDED` flag, and a stream-level error (the
lease, a stray directory, a sibling repository) is shown on every row of that stream. `STALE` is
the stream's too, from its newest run across every repository: nothing prunes a repository's
directory, so one the stream stopped publishing to keeps its last row, unflagged, until an operator
deletes `<audit-id>/<owner>/<name>/` from the store. `report_status.py` therefore imports nothing outside the standard
library and references no `__file__`; `report_query.py` imports its reading helpers so the two do
not grow two parsers of the same files.

## 7. Rejected alternatives

- **A ConfigMap.** Findings documents at 60,000-character scale, times every stream, times history,
  are the wrong side of etcd's 1 MiB object cap.
- **A SQLite ring beside the agent's own databases.** The unit is one whole document per run, read
  with a query script, not rows anything joins; a second database on the volume buys only a new
  corruption class.
- **Committing reports into the GitOps repository.** Durable and diffable, but it writes machine
  telemetry into the user's repository, a commit per stream per day, and a network read is what the
  store exists to remove.
- **Keeping the ledger read-back as a fallback.** Rejected in §4; the one read left seeds a store
  that has never existed and is never consulted beside one. The body `find_existing_issue` lists
  only vouches for a store, by its id block, and never replaces one.
- **Removing the hidden block from bodies.** Breaks the bench verifiers and every external consumer
  of the published interface; only its read-back was worth retiring.
- **Giving the Planning Agent file tools** to skip a delegation hop. It would turn the one profile
  with no infrastructure access into one with filesystem access, for latency on a question that is
  already asynchronous.
