# Eval dashboard: the scenarios view

> **STATUS — implemented.** `scripts/eval_dashboard/render.py` derives the fields,
> `template/pages.js` renders the view as the Cases page's second view
> (`cases.html#view=scenarios`), and [`SCHEMA.md`](../../scripts/eval_dashboard/SCHEMA.md) is the
> field contract.

**Scope:** what the view shows, the confidence rule behind its badge, and why it renders from fields
the cases already carry.

## What it is

The dashboard pages read the eval record from the team's side: is the gate healthy, which case
failed, how long runs take. The scenarios view reads the same record from the customer's side: what
kube-agents can do, scenario by scenario, by domain, with how sure we are of each. It is the Cases
page's second view, one chip away from the reliability table (both are one entry per case), over
data the dashboard already has and refreshed by the same job; the other pages gain a footer link and
nothing else.

A section is a domain, in `docs/designs/domains.yaml` order, headed by the domain's `meaning`
sentence; a chip row filters to one domain. A card is a case, folded to its title line (confidence badge, roster pill, title, the two times)
until the reader opens it for the task's `prompt` as the situation, its `expected_output` as what a
good answer does, the record in words and the newest passing transcript. A case the checkout has no task for (renamed or deleted, still on the
record) has no card; the foot counts them.

## Existing fields only

Every card renders from three fields the case format already reads and almost every task carries
(three of 234 have no `expected_output`; the card says so), so a new case appears complete the first
time the dashboard sees it and nobody writes a second description of a case. The texts are cut short: whitespace collapsed, at most `SUMMARY_MAX_CHARS` (220) at a word, with an
ellipsis. An `expected_output` written as a checklist (`critical requirements:` over `- ` items)
shows its first item: the lead-in says nothing about the scenario and the first requirement usually
does. Test-fleet names (`seeded-b`, `{{CLUSTER_NAME}}`) stay; one caption says the scenarios run
against the seeded test fleet. A hand-written story per case, or a `showcase:` field, is a later pass.

## The confidence rule

The badge is a level read from the case's nightly record over the last
`CONFIDENCE_WINDOW_DAYS` (7) at its current version key, infra repetitions excluded. Admission pools
to 20 repetitions whatever their age; this page measures a repository that changes daily, so it reads
a week, which the nightly's halves cover for every case at least twice (measured 2026-10-28: the
median case had 6 repetitions in it and none had 0), and asks for `CONFIDENCE_MIN_RUNS` (6) of them
before a reading counts. The key rule is admission's: when the agent model, the judge, the scoring
version, the fleet, the verifiers or the door changes, the window holds only repetitions on the new
software, and the card says how many more it needs.

- **Proven**: at least 6 repetitions in the window, at or above the admission rate
  (`trend.ADMISSION_RATE`, 0.90). The same rate lets a case guard every pull request.
- **Proving**: fewer than 6 repetitions in the window, or at least 6 between `PROVING_MIN_RATE`
  (0.70) and the bar. A new case, or a new build, starts here.
- **In development**: at least 6 repetitions below 0.70; or, whatever the window says, a case the
  roster has not admitted (held out, demoted, in no matrix) or whose task carries
  `expected_fail: true`. A demoted case keeps its card and its record, so a capability never
  disappears quietly.

Each card's record line says how many repetitions passed in the window and, for a case that could
reach it, how many more a Proven reading needs; while nothing is Proven the view says how far back
the cards' current records reach.

## The two times

Each card carries two medians over the window's repetitions, each named for what it measures:
**first reply**, until the agent's first chat message comes back (`turn1_s`), and **task finished**,
the whole run from the request to the final report, delegations included (`agent_s`). They come
from `data.json`'s nightly runs, which cover every case; the presubmit `cases[].latency` block covers
the cases the presubmit runs, the roster and its held-out seat. The store writes a record as a case
is graded and the collector lists a build once it finished, so the window's newest night is often
not on record yet; the card then shows the last recorded night's times and says which night. A
record whose repetitions carry no first-reply time (the inject door recorded none from 2026-10-24 to
10-27) says so and shows the task time alone. Neither time has a target yet, so both are shown and not
coloured; a target goes here and in the view's glossary when one exists.

## Status pill

From the rosters the pages already read, in the customer's words: "guards every pull request" for
the blocking roster, "nightly" for a nightly-only case, "in development" for a held-out, demoted or
retired one. The reliability view keeps the team's words for the same statuses.
