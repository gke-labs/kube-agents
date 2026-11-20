---
name: first-run-report
description: Answer a question about the first-run (onboarding) inventory report the agent already wrote — what onboarding found, the full inventory behind the ranked report, and whether the report was delivered and where it went — from the report and the delivery markers through the get_first_run_report tool. Never a new scan, never a fleet audit's report (that is fleet-audit-reports), and never the alert record.
---

# first-run-report — Reading What Onboarding Found

Onboarding sweeps the fleet once, ranks what it found into one short report, and delivers that
report: into the chat where the first person wrote, or, when nobody arrived in a day, as an issue
(in the GitOps repository, or the install's one managed GitHub repository). The ranked report and
the sweep's complete findings stay on disk afterwards. This skill answers questions off those files
and the delivery's own record. It scans nothing and ranks nothing.

## The tool is the only route

Call `get_first_run_report`. Nothing else. The report sits on the shell sandbox's data volume and
the delivery's record on the agent pod; the tool reads both, and a file read cannot say whether
the report reached anyone.

- **Never run any command on `/opt/data/INVENTORY*`**: not `cat`, `jq`, `grep`, `head`, `ls`,
  `find`, not `search_files` or `read_file`, not `kubectl exec`. That covers the report files
  (`INVENTORY.md`, `INVENTORY.delivered.md`, `INVENTORY.raw.md`) and the sweep's working files
  beside them (`INVENTORY.items.json`, `INVENTORY.scores.json`, `INVENTORY.shown.json`,
  `INVENTORY.limits.json`), which are not the report and hold nothing the answer needs.
- **Never write a file** to answer: no scratch copy, no notes, no extract.
- If the ask needs more than one call returned, say so in the reply and offer the full read (the
  `full` form below); do not go to the terminal for the rest.
- Never name a path, a file, a field or the tool in the reply: the person asked about the report,
  not about where it lives.

`get_first_run_report(which="ranked")` returns the report the user received (the delivered copy,
else the one still waiting). `which="full"` returns the sweep's complete findings, every cluster
and workload. Each is at most 256 KiB, the most one call returns, so never ask for more or for a
second page; `report.truncated` true means the file was cut at that size. Both carry:

| Field                 | What it says                                                                                                                                                                                                                                                                       |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `state`               | `delivered`, `filed`, `retired`, `pending` or `absent` (below)                                                                                                                                                                                                                     |
| `path_taken`          | `origin` (the chat the onboarding job was linked to) or `conversation` (the conversation recorded under the gateway), both the chat the first person wrote in; `no-human`; `unreadable` (the delivery record could not be read); or null (no claim yet, or an older build's claim) |
| `report`              | `kind` (`ranked` or `full`), `text`, `source` (which file), `bytes`, `truncated`; null when that file is not there                                                                                                                                                                 |
| `filed`               | under `filed`: the issue's `repo`, `number`, `url` and `filed_at` (UTC)                                                                                                                                                                                                            |
| `retired_at`          | under `retired`: when the report was retired (UTC)                                                                                                                                                                                                                                 |
| `findings_registered` | whether the sweep's findings reached the findings queue, where the nudge and the board draw from                                                                                                                                                                                   |

## The questions this gets asked

**"What did onboarding find?"** — the ranked report, as it was written, and nothing else. One
call, `which="ranked"`. Open with one bold line built from the report's own first lines: its
`Scanned N clusters and M workloads.` sentence, when the report carries one, plus how many
critical items it lists (or that it lists none); never a count or a verdict the report does not
carry, such as "clean". Then the critical
items in the report's own words and its roll-up line counting what sits behind them. Then, as one
later line, never the headline, what became of the report (the `state` bullets below). The report
is already prose for a person and already ranked: quote its lines; do not re-rank, re-score,
summarise two items into one, or add a finding from memory, the findings queue, a past audit or a
fresh look at a cluster. Under `absent`, say the sweep has not written a report yet. Under
`pending`, say it is written and not yet delivered, and give it.

**"Show me the full inventory."** — one call, `which="full"`. Open with one bold line that
answers: the sweep's totals or headline as the file states them (how many clusters, how many
findings). Then, when `truncated` is true, one line saying the inventory is longer than one reply
carries and that what follows is its first part. Then the findings as the file wrote them, grouped
as it groups them. When the full file is not there, say the sweep's complete findings are not on
disk and give the ranked report instead, named as such.

**"Was the first-run report delivered? Where did it go?"** — one line from `state` and
`path_taken`:

- `delivered` with `origin` or `conversation`: it was posted into the chat where the first person
  wrote (`origin`: the chat the onboarding job was linked to; `conversation`: the conversation
  recorded under the gateway).
- `delivered` with neither: it was delivered by an earlier build, which did not record where.
- `path_taken` `unreadable`: say the delivery record could not be read just now, and give the
  state from the files alone.
- `filed`: it was filed as an issue, with the link from `filed.url` and the date from
  `filed.filed_at`, because nobody had written to the agent a day after the sweep.
- `retired`: nobody arrived in a week, so the report was retired without a post. When
  `findings_registered` is true, say in words that its findings are still in the findings queue;
  never the field's name. The scheduled audits carry the current picture.
- `pending`: it is written and still waiting to be delivered.
- `absent`: there is no report yet; the sweep has not finished.

When they also want the content, the ranked report follows that line.

## Red lines

- **The tool is the only route.** Never read, list, search or copy an `INVENTORY` file yourself,
  never `kubectl exec` for one, never write a file to answer.
- **Never answer an `ERROR:` from the tool as "no report".** A failed read is one plain line in
  your own words ("I couldn't read the first-run report just now.") with what would work next: ask
  again shortly, or the operator checks that the shell sandbox is up. Never the `ERROR:` text, its
  code or its exception. The reply ends on its own last line with the retry, so it renders as a
  button: `[next: what did onboarding find?]`.
- **Never run a new sweep or audit to answer.** The question is about the report that exists; a
  fresh scan is a different request, and this report describes the fleet as it was then.
- **Never add to the report.** Nothing from the findings queue, a past audit or the clusters
  unless the person asked for it; a workload, a cluster, a count in the report's words, not yours.
- **Never name a path, a file or a field.** The reply says what the report says and where it went.
