---
name: upgrade-readiness-brief
description: Compose the upgrade readiness brief on request — which upgrade readiness findings need the user's decision now, what else the week found, and where the full report is — from the Saturday review's record and the findings queue, in the brief's fixed shape. Never the readiness table, never a summary of the report, never "nothing needs a decision" while a critical row is queued or surfaced.
---

# upgrade-readiness-brief — The Week's Decisions, On Request

The Saturday job `upgrade-readiness-weekly-review` grades every cluster and registers each blocker
and risk on the findings queue; the job `upgrade-readiness-weekly-brief` composes the week's brief
from both under `/opt/data/profiles/platform/governance/upgrade_readiness_brief_sop.md`. This skill is that SOP's "On request"
route: a user asks for the brief, or for what needs their decision, or for this week's report, and
gets the same shape in chat. Read only. Register nothing, decide nothing, upgrade nothing.

## Steps

1. Read the review's copy:
   - `cat /opt/data/upgrade-readiness/weekly/latest.json` — the summary: `generated_at`, `projects`,
     `clusters` (reviewed, blocked, ready, unknown, unread), `counts`, `findings` (rows with `id`,
     `severity`, `check`, `entry`, `project`, `cluster`, `namespace`, `object`, `title`, `action`,
     `outcome`), `new_ids`, `suppressed_ids`, `no_longer_reported_ids`, `project_failures`.
   - `cat /opt/data/upgrade-readiness/weekly/latest.md` when a line needs more than the row says.
   - If the asked-for thing is **the report**, print `latest.md` as it is and stop; when the file is
     missing, say the weekly review has not run yet and offer the on-request readiness report
     (`fleet-upgrade-verification`, `--readiness`) instead of composing anything.
   - If `generated_at` is older than 48 hours, compose from the summary anyway and say its date and
     age in the headline: a stale brief with its date beats none.
   - If the file is missing, compose a **live brief**: run the `fleet-upgrade-verification` skill
     with `--readiness` once over every project in scope, treat its blockers and risks as the
     findings (a budget over one replica, a pool past the skew ceiling and a webhook in the upgrade's
     path are critical; a budget over several replicas, a webhook refusing only what it matches
     today and a pool at the ceiling are major; a covering exclusion is minor; a readiness rule's
     risk takes the severity the review's rubric gives its catalogue entry), name objects where the
     queue would give ids, and say "live brief, composed now; the weekly review has not run" in the
     headline.
2. Read the queue, with the `platform_control` tools:
   - `get_ranked_findings`: the open backlog. Keep the rows whose `check` starts with `upgrade-`.
     A row in state `queued` **or** `surfaced` is **undecided**: nobody has decided on it, whether or
     not the hourly nudge has named it yet. `accepted` is decided (being worked on).
   - `get_findings` with `state=snoozed`, then `state=dismissed`: the other decided rows; the backlog
     never carries them. Keep the `upgrade-` rows of these too. Pass `limit=1000` and `project=<id>`
     for each project in the summary's `projects` (the default page is 200 rows across every
     source); a page of exactly 1000 rows is cut, so say "at least" before that count.
3. Compose the brief in exactly this shape, at most twenty-five lines, ids verbatim in backticks:

   ```
   Upgrade readiness, week of <YYYY-MM-DD>: <n> clusters reviewed, <b> blocked, <r> ready<, u not read>. <new> new findings since last week, <gone> no longer reported.

   **Decide now** (critical, nobody has decided yet):
   1. `<id>` — <cluster>: <what blocks and what it costs, one clause>. Do: <the first action>.
   2. ...
   <one line per cluster, at most ten; then one line: "and <k> more critical findings await a decision, listed in the report.">

   **Also this week:** <one or two lines: counts by catalogue entry on which clusters, majors only; minors as one count>.

   **Decided earlier:** <a> accepted, <s> snoozed, <d> dismissed; nothing changes there.

   <when project_failures is not empty: "**Not read this week:** <project>: <reason>; last week's findings there stand.">

   Full report: /opt/data/upgrade-readiness/weekly/latest.md on the agent. Reply "show me this week's upgrade readiness report" to read it here, or reply with an id and `dismiss`, `snooze until <date>` or `accept` to decide.
   ```

   - **Decide now** lists the undecided criticals, worst first (the queue's order), one line per
     cluster and at most ten lines: every critical on one cluster goes in that cluster's line, with
     each id and object, and its worst finding sets its place. A finding on the cluster the agent
     runs on, or one whose clock ends within thirty days, may move up; so may a major whose evidence
     says a serving workload goes down, over a critical on a cluster the summary lists as `unknown`.
     Never leave the section empty while an undecided critical exists; when every critical row is
     `accepted`, `snoozed` or `dismissed`, the section reads `**Decide now:** nothing; every critical
finding has a decision.` When nothing was found at all, the brief is the headline and the
     report line, two lines.
   - One clause of why ("budget `pinned-batch-runner` refuses eviction of its only pod, so the drain
     kills it"), one action (the row's `action`, shortened to the verb and the object).
   - Majors and minors go under **Also this week** as counts by catalogue entry; a `suppressed` row is
     never named. With nothing decided yet, the line is `**Decided earlier:** none yet.`
   - When `registration_errors` is not empty, or a row's `outcome` is `failed`, the headline says
     how many findings are in the report but not on the queue, and a critical among them goes under
     **Decide now** by its object with "not on the queue" after it: there is no id to reply with.
   - Every id and every count comes from the summary or the queue; invent nothing, name no CVE,
     recommend no version.

4. Answer with the brief and nothing else. When a card's report format asks for a bold opening
   sentence, bold the headline line; keep every other line as the shape gives it.

## Red lines

- A table, a prose summary of the report, a link to a GitHub ledger, or "no decision needed" with
  queued critical rows is the wrong answer.
- Never call `register_findings` or `update_finding` from this skill; the review registers, the user
  decides, the queue remembers.
