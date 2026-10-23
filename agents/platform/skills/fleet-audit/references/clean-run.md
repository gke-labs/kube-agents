# fleet-audit — The clean run

Part of the `fleet-audit` skill (`/opt/data/profiles/platform/skills/fleet-audit/SKILL.md`), which says when to open this file.

## The clean run

If the audit finds nothing, still call `finish` with `"findings": []` and a populated
`scope.clusters`. With complete coverage the harness answers any `/remediate` still standing
unanswered in the thread, comments the date, the targets covered (counted by kind: clusters,
subnets, projects) and the same _How this run
checked the fleet_ table the ledger body carries, closes the ledger issue **as completed**, and
closes every remediation pull request still open for the stream. The answers come first,
deliberately: a reply posted after the close would land on an issue nobody is watching. The table
is there because a clean run does not rewrite the body: the comment is the only place the commands
behind an all-clear are published, and an all-clear nobody can re-run is one nobody can audit
after the fact. After the close, the harness changes the title to `0 findings` and puts a banner
above the old body. The banner says that the run found none of the findings below, so the closed
issue does not show them as open. The findings below the banner are not rewritten; they stay as the
record. On a ledger that a coverage gap opened, the banner says that a complete run found no
findings and no gap. A later run
with findings opens a new ledger issue; it does not reopen this one.

**Zero findings plus a coverage gap is not a clean run, and a run with a new or changed gap is not
silent, even on a stream with no ledger.** With gaps, the harness opens one — titled `coverage incomplete (n gaps, 0 findings)`
rather than the all-clear phrasing — so the run leaves a durable artifact saying what it could not
see. Without this, a stream that inspected nothing produced no issue, no comment, and nothing to
notice: four streams did exactly that on 2026-08-03, and the only reason it surfaced is that a fifth
happened to have a ledger open from the day before. A later clean run with a gap rewrites the
title and body of a ledger that a gap opened, so the gap count and the Scope table are this run's.
It does not rewrite a ledger that a findings run wrote. If the gaps are the same as on the stored
previous clean run, it posts no second partial all-clear comment, unless it closed a remediation
pull request. After a stored run that was not `CLEAN`, it posts the comment.

**Zero findings over a finding the run checked again is not a clean run either.** Before it
closes, `finish` reads the previous run's stored report — the same findings `start` handed you as `carried`. For
every finding it carried, if this run's `checks_run` says the check that found it ran on that
cluster — the SOP's fleet-wide `kubectl get clusterrolebindings -o json | jq …` counts; it lists
every binding, `debug-binding` included — and the document neither reports the finding again,
carries a `resolved_because` entry for it, nor lists it under `declared`, the run either saw it gone
or left it out, and from the document the two are the same absence. The ledger stays open and gets
a comment naming each such finding and the check that ran (plus any `resolved_because` reasons and
declared postures the document does carry), no remediation pull request is closed (except the shield's close (SOP 2.7), which rests on the declaration rather than on this run's reading), and `finish` returns
`status: "HELD"` with `resolved: 0`, `silent_ok: false` and the ids in `unaccounted`. Report it as
you would a partial run — the ledger URL and the held ids — and on the next run either report the
finding or, if you re-ran its check and saw the object gone, say so in `resolved_because`. On a
stream that passes `--manifest-file`, a finding the collector still emits a candidate for is held
whatever `resolved_because` says; only the collector no longer emitting it, or a `declared` entry,
releases it. On 2026-09-16 a compliance run closed its ledger as clean over a live cluster-admin binding its own
`checks_run` claimed to have checked; this is the guard that turns that close into a held ledger. A
check declared `checks_not_applicable` on that cluster did not run there and holds nothing — the
excuse is published in the evidence table, where a reviewer can weigh it.

**Zero findings over a conversation the run could not read is held too.** If the ledger's comments
cannot be read — the forge refused, it could not say which login is this install's, or the thread
is past the read limit — the run cannot tell whether a `/remediate` is waiting there, and closing
would leave it no thread to be answered on. Nothing is posted, the ledger stays open, and `finish`
returns `status: "HELD"` with `unaccounted: []`; stderr says `comments could not be read`. There are
no held ids to report and nothing to re-check in the fleet: say the ledger is held because its
thread could not be read, and the next run closes it once it can.

A clean run is usually not news, and the closed issue is the record — but "clean" alone does not
decide it. **`finish` decides it, and returns the answer as `silent_ok`.** Read the flag; do not
reassemble it from `status`, `new`, `resolved`, and `partial` yourself. That arithmetic has more
clauses than it looks like it has, and re-deriving it is how a run talks itself into silence it has
not earned — on 2026-08-03 a run with two partially-covered clusters answered `[SILENT]` and its
ledger URL never reached the operator who had asked for it.

> **`silent_ok` is `true` only when the run moved nothing an operator needs to hear about:** nothing
> new, nothing resolved, no new or changed coverage gap, no held close, no remediation pull request opened or
> closed, and — on a stream that passes `--manifest-file` — no collector candidate the document
> dropped.

Two rules follow, and they are the whole rule:

- On a **scheduled** run, `silent_ok: true` → the final response is exactly `[SILENT]`. Otherwise
  report, and every report carries `issue_url` in full, with every URL in `prs_opened` and
  `prs_still_open`: a fix already waiting for review answers "was a fix proposed?" as much as one
  this run opened. A `NO_FORGE` run has no `issue_url`; its report is `summary` (see
  [No forge configured](finish.md#no-forge-configured)). A `STORE_LEDGER` run has no `issue_url`
  either; its report is `summary`, with the pull request links (see
  [A forge with no issue tracker](finish.md#a-forge-with-no-issue-tracker)).
- **An on-demand run is never silent.** `silent_ok` is the _scheduled_ verdict — it answers "would a
  channel want this?", and it cannot know a person asked. If someone dispatched this job, from a
  kanban card or straight from chat, they are waiting on the answer and
  `[SILENT]` throws it away. Report the outcome and the ledger URL whatever the flag says.

A zero-finding run comes back `silent_ok: false` in each of these cases, and all of them matter:

- **`resolved > 0`** — the fleet was carrying findings yesterday and is not today. Something got
  fixed, and that is the best thing this audit ever gets to say. Reporting `partial` failures while
  swallowing this one would leave the operator hearing only bad news.
- **`partial: true` with a new or changed gap** — the ledger stayed open because the fleet was not
  fully read. "I found nothing" and "I could not look" must not arrive as the same silence. A gap
  that is the same as on the stored previous run was said on the run that got it, and is still in
  the ledger and in `coverage_gaps`; it does not make `silent_ok` false again. With no trusted
  stored record, every gap counts as new. `finish` cannot tell a scheduled run from an on-demand
  one, so a gap first seen on an on-demand run is not reported again by the next scheduled run.
- **`unreported_under_gap` on the JSON line** — a finding that the stored run listed is not in what
  this run published, and a gap holds `resolved` at 0. The gaps may be the same as before; the run
  still speaks. Say in your one line that this many findings are no longer reported while a
  coverage gap stays, with the issue URL. Do not call them resolved. Each id is listed once; a later
  run with the same gap does not list it again.
- **`status: "HELD"`** — the ledger stayed open because the run did not account for findings it was
  carrying, or because it could not read the issue's comments, where a `/remediate` may be waiting
  for an answer. "I found nothing" and "I did not write it down" must not arrive as the same
  silence either.
- **A dropped collector candidate** — on a stream that passes `--manifest-file`, the collector
  flagged something the document did not carry. The check reads as having run and found nothing;
  the JSON line's `unpublished_candidates` says otherwise, and it must not arrive as silence.

There is one case where the harness reports `new: 0, resolved: 0` without knowing it: when the
report store's record of the open ledger is missing, unreadable, written for a different issue, or
lists a different id set from the ledger's hidden block (or the issue listing returned no body to
check it against) (where the store has never held the ledger at all, `finish` seeds it once from the issue's hidden
block instead, and a block that is absent or cannot be fetched is the same loss) — the previous
run's findings are unknowable, so the run announces nothing rather than declaring every live finding
new, and logs a line containing `the previous run's findings are unknowable` to stderr. A `finish`
that rewrites the body deletes the record just before the rewrite, and a clean close deletes it just
after the close lands, so that a record older than the issue is never trusted; a `finish` that fails
after either delete leaves the run after it one of these. A held-open run that only comments keeps
the record when its store write fails, because the record still describes the body. A held-open run
that refreshes a coverage ledger rewrites the body: it deletes the record before the edit, and
stores the body the edit wrote, or the previous body when the edit failed. It holds nothing it
cannot name: the body is rewritten from this document, and a findings run leaves the next run a
trusted record. A clean run never closes the ledger (the shield's close of a shared-account pull request aside): the ledger stays open with a coverage gap saying the store
had no trusted record, `partial: true` and `silent_ok: false`, and it stays open run after run until a
findings run rewrites the body or a human who has checked the findings closes the issue. Say so in
your report: the gap names both ways out only when the collector flags nothing, and otherwise says
the collector still flags something this run did not report. Without a manifest it also answers no `/remediate` — it cannot tell a held id
from a typo — and logs `No trusted stored report and no manifest` to say so; report that as you would any
other partial run. With a manifest it answers a standing `/remediate` whose target the collector does
not flag by saying the report store lost its record of this ledger, so the run cannot tell whether
the target was among the findings the ledger carried — never "no longer reproduces", and never a wait
for complete coverage, which a clean fleet already has. Its comment on the issue says the record was
lost rather than that the run did not see the whole fleet, unless a coverage gap stands beside it.
