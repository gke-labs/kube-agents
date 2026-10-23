# SOP: Weekly Upgrade Readiness Brief

**Purpose:** one message a person reads in a minute, saying what this week's upgrade readiness
review found, which findings need their decision now, and where the full report is. The review job
did the reading and the registering; the findings queue carries every finding to a decision and
reminds on its own schedule. This brief is the weekly judgement over both: what matters most this
week, said once, in plain words. It changes nothing: it registers no finding, decides nothing on the
user's behalf, and upgrades nothing.

**Data sources:** the review's copy in the sandbox, `/opt/data/upgrade-readiness/weekly/latest.json`
and `latest.md`, and the findings queue as this job's pre-run script reads it (on request, through
`get_ranked_findings` and `get_findings`). Nothing else is read, and nothing is run.

**Cron id:** `upgrade-readiness-weekly-brief` — `0 7 * * 6` (Saturdays, 07:00 UTC), an hour after
`upgrade-readiness-weekly-review` starts. The review grades every cluster in scope and registers what
it finds on the findings queue inside a 45-minute read budget and the ten minutes its sends have after it, so by 07:00 its summary is on disk, or
chat carries one of its lines: the failure line, the partial line, or the line that says its copy
did not land. Step 1 says what each means; nothing else sequences the two.

**Why a model turn.** The review job can rank by rubric; it cannot tell that two criticals on one
cluster are one problem, that a critical on the cluster the agent itself runs on outranks the same
one on a scratch cluster, or that a critical whose clock ends next week matters more than one with
no date.
That is the judgement this turn makes, inside the rules below. The rules bound the length and the
shape; the model chooses the emphasis.

---

## Execution Checklist

### 1. Read the review

1. Read the review's summary, the copy the review job placed where you can read it:

   ```bash
   cat /opt/data/upgrade-readiness/weekly/latest.json
   ```

   It carries `generated_at`, `projects`, `clusters` (reviewed, blocked, ready, unknown, unread),
   `counts` by severity, `findings` (one row per finding: `id`, `severity`, `check`, `entry`,
   `project`, `cluster`, `namespace`, `object`, `title`, `action`, `outcome`), `new_ids`, `suppressed_ids`,
   `no_longer_reported_ids`, `project_failures`, `registration_errors` and `report_path`.

2. If the file is missing, or `generated_at` is older than **48 hours**, the review's record did
   not reach you: the review did not run, the hour cut it short before its record, or its sandbox
   copy failed after its registrations and its record (the review's own chat line, posted before
   07:00, says which). Say so in the headline, name the job (`upgrade-readiness-weekly-review`) and
   the last `generated_at` you found, and compose **Decide now** from the queue alone (step 2): the
   ids are there whatever happened to the copy. Leave out the headline's counts and **Also this
   week**, which only the summary gives. Do not run the readiness report yourself from this job:
   the queue would hold nothing for the findings, and a brief that names findings nobody can decide
   on is noise.
3. Read the full report when a line needs more than the summary row says:

   ```bash
   cat /opt/data/upgrade-readiness/weekly/latest.md
   ```

### 2. Read the queue

The queue is the authority on **state**: the summary says what was found, the queue says what the
user has decided. Only the rows whose `check` starts with `upgrade-` count (every row the review
registers carries that prefix; `source` is `upgrade-readiness`).

- The open backlog, worst first, holds the **undecided** rows (`state` is `queued` or `surfaced`),
  which still need the user, and the `accepted` ones (being worked on).
- The decided rows the backlog never carries are read by state, per project: snoozed (back on
  `snoozed_until`) and dismissed (won't fix; the review will keep finding it and the queue will
  keep it silent). One page holds at most 1000 rows, so a page of exactly 1000 is cut; say "at
  least" before that count.
- A row in the summary with outcome `suppressed` is a dismissed finding seen again. Never name it
  as new.

On the Saturday run the reads are already made. The job's pre-run script
(`upgrade_readiness_brief_queue.py`) reads the queue before your turn starts, and its JSON is above
the prompt under **Script Output**: `open` is the open backlog, `snoozed` and `dismissed` are the
decided rows of every project, all of them `upgrade-` rows. An undecided critical row carries
its `namespace`, `object`, `title` and `action`, for a line composed from the queue alone; every
other row carries its state. `cut` names each page that came back full, and `unread` each
page the script could not read: with any `unread` page, leave **Decided earlier** out, since its
counts are not whole. This turn holds no `platform_control` tool, so it cannot read the queue
again. When the output carries `error` (the open backlog itself was not read), or the scheduler
reports a script error, say in the headline that the queue was not read, and compose from the
summary alone, without **Decided earlier**.

On request, make the reads yourself with two `platform_control` tools: `get_ranked_findings` for
the open backlog, and `get_findings` with `state=snoozed`, then with `state=dismissed`, each with
`limit=1000` and `project=<id>` for each project in the summary's `projects` (the default page is
200 rows across every source, which a mature install exceeds).

### 3. Compose the brief

Write one message, in this shape and order, at most **25 lines**. Plain sentences; no table; no
heading markup beyond the bold labels shown; the ids verbatim as the queue spells them, in
backticks, because the user replies with them.

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
<for a live brief, instead: "No weekly record yet: the Saturday review registers these on <next Saturday>, and each gets an id you can decide with. Reply "show me the upgrade readiness report" for the table.">
```

Rules for the judgement:

- **Lead with what needs a decision.** The lines under **Decide now** are the undecided
  criticals, worst first, one line per cluster and at most ten lines: every critical on one cluster
  is one problem, so the cluster's line names each of its ids and objects, and its worst finding
  sets its place. The queue's rank order is the default. Depart from it, among the criticals and
  only among them, when one of these holds: a critical on the cluster the agent runs on outranks
  the same one elsewhere; a critical whose clock is short (a target or a date within thirty days)
  outranks one with no date; a critical whose evidence says a serving workload goes down outranks
  one on a cluster the summary lists as `unknown` or whose evidence is inferred. No major or minor
  moves up: they have their own line below.
- **One clause of why, one action.** "Budget `pinned-batch-runner` refuses eviction of its only
  pod, so the drain kills it" is the clause; the action is the row's `action`, shortened to the
  verb and the object. Never paste the row's full text.
- **No critical undecided** → the section reads `**Decide now:** nothing; every critical finding
has a decision.` and the brief is shorter.
- **Nothing found at all** → two lines: the headline, then the report line. Never `[SILENT]`: the
  week's brief is owed whether or not there is news.
- **Majors and minors stay out of Decide now.** They go in **Also this week** as counts by
  catalogue entry (the row's `entry` and the first part of its `title`), so the user sees the shape
  of the week without a list. The queue names them at its own pace.
- **Nothing decided yet** → the line is `**Decided earlier:** none yet.`
- **Dismissed is silent.** A `suppressed` row is never counted as new and never named; it was the
  user's decision.
- **A project the review could not read is said, once, in its own line.** The headline's counts
  cover the clusters that were read; `project_failures` names the rest with the review's reason.
  Without the line a reader takes "6 clusters reviewed" for the fleet.
- **Findings that did not reach the queue are said.** When a row's `outcome` is `failed`, the
  headline says how many findings are in the report but not on the queue, and a critical among them
  goes under **Decide now** by its object (it has no id to reply with), with "not on the queue"
  after it: a user who dismisses it in chat has nothing to dismiss. A `registration_errors` entry
  that says a completeness claim did not reach the queue is a cluster whose last week's rows stand
  at full confidence; count those clusters in the headline, not as findings.
- **Never invent.** Every id comes from the summary or the queue; every count comes from the
  summary. If the queue and the summary disagree on a row's state, the queue wins and you say
  nothing about the disagreement in the brief.
- **No CVEs, no version advice.** The review reads budgets, webhooks, pools, exclusions and the
  readiness rules; it has no vulnerability feed. Name no CVE and recommend no version.

### 4. Deliver

Your final response is the brief and nothing else: no preamble, no restatement of these rules, no
closing offer. The scheduler posts it to chat as written and stores it against the thread, so a
reply in that thread reaches the agent with the brief attached.

---

## On request

A user may ask in chat for the brief or the report, at any time. The `upgrade-readiness-brief` skill
(`/opt/data/profiles/platform/skills/upgrade-readiness-brief/SKILL.md`) is this section as the agent loads it for a chat request;
its shape is the one in section 3, kept identical by a test.

- **"Show me this week's upgrade readiness report"**, or any ask for the full report → print
  `/opt/data/upgrade-readiness/weekly/latest.md` as it is. Google Chat splits anything past 4,000
  characters across messages; that is expected. If the file is missing, say the review's copy is not
  there (the weekly review has not run, or its copy did not land; the review's own chat line, if
  any, says which) and offer the on-request readiness report (`fleet-upgrade-verification`,
  `--readiness`).
- **"Give me the upgrade readiness brief"**, or any ask for what needs a decision → steps 1 to 3
  above, and the same shape. If the summary is older than 48 hours, say so in the headline and
  compose from it anyway: a stale brief with its date is better than none. If there is no summary
  at all, read the queue first: when it holds `upgrade-` rows, the review ran and its copy did not
  land, so compose from the queue alone with a headline that says the summary is unavailable.
  Only when the queue holds no `upgrade-` rows either (the review has never run on this install)
  compose a **live brief**: run the readiness
  report yourself over every project in scope (`fleet-upgrade-verification`, `--readiness`, as
  "Check upgrade readiness" in its `SKILL.md` says), take the blockers and risks it names as the
  findings (a drain-blocking budget over a single replica, a pool past the skew ceiling and a
  fail-closed webhook in the upgrade's path are critical; a budget over several replicas, a webhook
  refusing only what it matches today and a pool at the ceiling are major; a covering exclusion is
  minor; a rule's blocker is critical, and its risk is minor for catalogue entries 9, 10, 11, 13, 16 and 25 and major for every other entry), read the queue
  for any decision already recorded, and compose the same shape. Say "live brief, composed now; the
  weekly review has not run" in the headline, use the object's name where the queue would have an
  id, close with the live form of the last line (there is no report file and no id to reply with
  until the Saturday review registers them), and register nothing: the Saturday review owns the
  queue.
- **A decision in reply** ("`<id>` dismiss", "snooze until 2026-11-01", "accept") is not this
  SOP's to apply: the Chat Agent files it as a card and the `update_finding` tool records it. Do
  not answer a decision with a new brief.

## Red Lines

- **Never register, verify or decide a finding from this job.** The review job registers; the user
  decides; the queue remembers.
- **Never run the readiness report from this job.** The on-request route offers it; the scheduled
  brief reads the review.
- **Never pad the brief.** Twenty-five lines is the ceiling, not the target; a quiet week is three lines.
- **Never name a dismissed finding.**
- **Never mutate.** No `gcloud ... upgrade`, no `kubectl apply/patch/delete`, nothing through the
  `gke` MCP server that writes.
