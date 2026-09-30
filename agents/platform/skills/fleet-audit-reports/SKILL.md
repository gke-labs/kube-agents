---
name: fleet-audit-reports
description: Answer a question about a past autonomous fleet audit from the on-pod report store — what a stream last found, how many criticals are open, what changed between two runs, which clusters were skipped or only partly covered, and when each stream last ran.
---

# fleet-audit-reports — Reading What the Audits Found

Every audit run keeps what it published, on the volume, beside the agent. This skill answers
questions off those files. It never runs an audit and never publishes one — the `fleet-audit` skill
owns both ends of that lifecycle.

## The store

`/opt/data/fleet-audit/reports/<audit-id>/<owner>/<name>/`, one directory per stream and GitOps
repository — a stream publishes one ledger per managed repository:

- `latest.json` — the newest run's envelope.
- `runs/<YYYYMMDDThhmmss.ffffffZ>.json` — one envelope per run, newest 14 kept. The only
  run-over-run history that exists anywhere: the ledger issue rewrites itself in place.

An envelope carries `audit_id`, `repo`, `finished_at`, `status`, `issue_number`, `issue_url`,
`partial`, `coverage_gaps`, `declared`, `unaccounted`, `unpublished_candidates`,
`wholly_unpublished_checks`, `uncorroborated_findings`, `prs_opened`, `prs_closed`, `silent_ok`,
`ledger_held_open`, `delta_known`, `new_ids`, `resolved_ids`, `current_ids`, `id_scheme`, `ledger_body`,
`document`, and sometimes `ledger_document`.

- `document` is this run's whole validated findings document — un-clipped, so it holds findings the
  issue body had no room to print.
- `status` is `OPENED`, `UPDATED`, `CLEAN`, or `HELD`. `ledger_held_open` true means a `CLEAN` or
  `HELD` run left the issue open (partial, or over findings it did not account for) without
  rewriting it: the issue still lists the previous run's findings, `current` counts those, and this
  run's `findings: 0` and `critical: 0` do **not** mean the ledger is clear. `streams`, `show` and
  `findings` all carry it. Held open over a lost memory, the envelope's `issue_number` is null and
  `current` is 0: the store does not know what the issue lists, so read the issue. `ledger_document`
  is that previous document, kept for `fleet-audit`; do not answer from it.
- `delta_known` false means `fleet-audit` lost its memory of the previous run and withheld the
  delta: `new_ids` and `resolved_ids` are empty, and `new`/`resolved` 0, because nothing was claimed,
  not because nothing changed. Say the delta is unknown for that run. Envelopes written before the
  key existed lack it; read their counts as they are. `streams` and `show` carry it.
- `current_ids` is exactly what the body's hidden block published: the findings the body rendered
  plus the ids the collector held. Derive this run's full set from `document`.
- `ledger_body` is the issue body the run left on GitHub — `fleet-audit`'s memory of the previous
  run, not something to answer from. No subcommand returns it.

Whether a run is in flight comes from the lease `fleet-audit`'s `start` takes, not from a file in
this directory. `streams` and `runs` report it as `liveness`: `never` (no run stored), `completed`,
`running`, `died` (started over two hours ago and never finished), or `error`. A stream-level
problem (a sibling repository's unreadable store, a bad lease note) comes back under `error` with
exit 2 even when the listing is complete; report it, do not drop it.

## Query it; do not read it

**Never open a store file** — not `latest.json`, not a ring entry, not with a file tool and not
with `cat`, `head`, `grep` or `jq` in the shell. Each embeds `document` and `ledger_body`, which pass
60,000 characters each on a finding-heavy stream — times every stream, times fourteen runs.
Answering "how many criticals are open?" that way spends tens of thousands of tokens on an integer.
When `report_query.py` says a stream has no record, that is the answer: do not search the directory
for a file it did not find.

`python3 ./skills/fleet-audit-reports/scripts/report_query.py <subcommand>` prints one small JSON
object per call.

| Subcommand                          | Answers                                                               |
| ----------------------------------- | --------------------------------------------------------------------- |
| `streams`                           | one row per stream and repository: last run, status, counts, liveness |
| `show <stream>`                     | one run's envelope **without** `document`                             |
| `findings <stream>`                 | finding id, severity, title, cluster, check — filterable              |
| `finding <stream> <id>`             | one finding in full; the only call that returns prose                 |
| `checks <stream>`                   | the command behind each check that ran, plus exclusions               |
| `diff <stream> [--from S] [--to S]` | ids and titles added and resolved between two runs                    |
| `runs <stream>`                     | the stamps the ring holds, so a `diff` can name real ones             |

- Exit 0 answered the question. Exit 2 could not, and stdout still holds one JSON object whose
  `error` says why — absent or unlistable store, absent stream, absent stamp, a file that would not parse.
  Arguments that do not parse are the exception: argparse prints usage to stderr and stdout is
  empty. Every answer carries an `error` key, null on success. `streams` exits 2 when any one stream is
  unreadable; its other rows still stand — report them and name the unreadable ones.
- Every subcommand but `streams` takes `--repo owner/name`. Leave it off when the stream has
  published to one repository; when it has published to several, the answer is exit 2 with the
  `repos` to choose from — ask which, or run once per repository, never pick one silently.
- `--run` takes a stamp from `runs`, with or without the `.json`. Default is the newest run:
  `latest.json`, or the newest ring entry when a later run deleted it and failed. `streams`, `show`,
  `findings`, `finding` and `checks` then carry `latest_missing: true` — say the answer is from that
  run and the issue may be newer.
- `--severity`, `--cluster` and `--check` on `findings`, and `--cluster` and `--check` on `checks`,
  are exact matches, case-insensitive.
- `findings`, `checks` and `diff` cap at 100 rows, raisable with `--limit`. `findings` and `checks`
  report `matched`, `returned` and `truncated`, and `checks` caps its exclusions separately under
  `not_applicable_matched`, `not_applicable_returned` and `not_applicable_truncated`; `diff` caps
  `added` and `resolved` independently and reports `added_total`, `resolved_total`, `unchanged` and
  `truncated` — quote `added_total`, not the length of `added`. Findings sort severity-first, so a
  cap drops only the least severe; `checks` keeps the document's order, so a capped answer lines up
  with the issue's table.
- `--root` overrides the store root. In the pod, leave it alone.

## The four questions this gets asked

**"What did last night's compliance audit find?"**

```bash
python3 ./skills/fleet-audit-reports/scripts/report_query.py show compliance-audit
python3 ./skills/fleet-audit-reports/scripts/report_query.py findings compliance-audit --severity critical
```

`show` gives, under `envelope`, `status`, `findings`, `critical`, `partial`, `ledger_held_open`, the
delta counts and `issue_url`; name the criticals from `findings`. When `ledger_held_open` is true,
this run found nothing but the issue still carries the previous run's findings — say both, and point
at the issue. Always hand back the issue URL — the store is where you read, the ledger is where a
human acts.

**"What changed since the last run?"**

```bash
python3 ./skills/fleet-audit-reports/scripts/report_query.py diff compliance-audit
```

Defaults to the newest two runs and returns ids and titles under `added` and `resolved`. When
`from_partial` or `to_partial` is true, that run could not see the whole fleet: a finding under
`resolved` may be one it did not look at, so say "not seen", never "fixed". When `to_held_open` is
true, the audit itself refused to resolve them; they are still on the issue. When `from_held_open`
is true, the earlier run found nothing while the issue still listed older findings, so `added` may
be findings the ledger already carried — say "found again", not "new". For a wider span, list the
ring first and name two stamps, older as `--from` — a reversed pair is refused:

```bash
python3 ./skills/fleet-audit-reports/scripts/report_query.py runs compliance-audit
python3 ./skills/fleet-audit-reports/scripts/report_query.py diff compliance-audit --from <stamp> --to <stamp>
```

**"Tell me about that finding."**

```bash
python3 ./skills/fleet-audit-reports/scripts/report_query.py finding compliance-audit <finding-id>
```

The evidence command and excerpt, the impact, and all three `recommendation` fields. This is the
expensive call: make it for the finding that was asked about, not for the list.

**"The issue says findings or commands were omitted — what were they?"**

```bash
python3 ./skills/fleet-audit-reports/scripts/report_query.py findings obtainability-audit
python3 ./skills/fleet-audit-reports/scripts/report_query.py checks obtainability-audit --cluster prod-us-east
```

The ledger drops its least-severe findings, then its evidence table whole, to stay inside GitHub's
body limit, and says so in a notice that points here. `findings` lists every finding the run
carried. `checks` is one row per check the run says it performed, with the command that performed
it, plus each `checks_not_applicable` exclusion and its reason; filter by cluster or check — a
16-cluster stream carries upwards of 150.

Fleet-wide, `streams` is the whole answer: one row per stream with its liveness, so "when did each
last run" and "is anything stuck" come back in one call. Coverage questions read `show`'s
`coverage_gaps`; `streams` carries only the count.

## Two things the store is not

- **Not the live issue.** It is the last _published_ state. A finding a human closed by hand since
  that run, or a `/remediate` posted since, is not in it until the next run rewrites the store. When
  the question is about the issue, read the issue.
- **Not written for every invocation.** Only a `finish` that exits 0 writes — never `--dry-run`,
  never a run that exited 2, never `remediate`. The write is best-effort. `finish` deletes
  `latest.json` before it rewrites the ledger, and after the clean close lands (a close that fails
  leaves it, since the ledger is still the one it describes), so a run that fails partway leaves no
  superseded envelope reading as current; the ring stays, and answers come from it flagged
  `latest_missing`. When the close landed but the run failed before storing itself, the newest ring
  entry is an `OPENED` or `UPDATED` run whose issue is now closed, and a human can close one by
  hand: either way it is not the open ledger. Check the issue's state, or say
  the findings are as of that run.
  **No stored run means unknown, not clean** — say the store has no record, and read the ledger
  issue.

## Red lines

- **Never report an absent, unreadable, `never` or `died` stream as a clean fleet.** "I could not
  look" and "nothing is wrong" are different answers; pass `error`, `liveness` and any
  `stream_error` through to the user.
- **Never read `document` or `ledger_body` whole** to answer a question a subcommand answers.
- **Never run, publish, or remediate from here.** Dispatching a stream, rewriting a ledger and
  opening remediation pull requests belong to `fleet-audit`
  ([Running a stream on demand](../fleet-audit/SKILL.md#running-a-stream-on-demand)).
- **Never act on a `running` or `died` stream's lease.** It is `fleet-audit`'s; report the state.
- **Never quote a count the store did not give you.** The counts are keys; re-deriving one by hand
  from prose is how a stale number reaches a user with the authority of a file read.
