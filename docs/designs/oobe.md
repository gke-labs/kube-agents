# Out-of-Box Experience: the `oobe` Job

A new install shows its first inventory report soon after someone messages it, then nothing more
until the scheduled audits run: 06:20 UTC the next morning for security, the next Monday for cost.
This design adds `oobe`, one no-model cron job on the Planning Agent's roster that owns everything
an install does once, on first boot. Its first stage starts the fleet audits as soon as the
inventory scan finishes, so an operator sees cost, security, reliability and capacity findings
within about two hours of install. Later it takes over the two bootstrap jobs, so first-run work
lives in one place.

> **Status:** §4, the first-run audits, is implemented, with the entrypoint's `--assume-retired` entry
> from §5. The rest of §5, folding in the bootstrap jobs, is not. §8 is the build order.

## 1. Why

The operator who ran the installer wants to know, on day one, whether kube-agents is worth
keeping; the first-run audit request, [#1866 "First-Run Quick Value Audit"](https://github.com/gke-labs/kube-agents/issues/1866), calls it
the "wow moment that justifies the installation". What they get today:

| When                     | What happens                                                        | Source                                                |
| ------------------------ | ------------------------------------------------------------------- | ----------------------------------------------------- |
| Install                  | Pods come up; a first boot can take over fifteen minutes            | INSTALL.md "What to Expect After Installation"        |
| First message to the bot | Greeting; the inventory scan has been running since boot            | `agents/chat/defaults/onboarding/scan_in_progress.md` |
| Scan finishes            | Ranked report posted into the thread of the first message           | `bootstrap_delivery.py`                               |
| Next 06:20 UTC           | First scheduled audit (compliance); first remediation pull requests | `agents/platform/cron/jobs.json`                      |
| Next Monday 07:50 UTC    | First cost audit                                                    | same                                                  |

The gap between the report and the first audit is up to a day, and up to a week for cost. The
only way to close it today is to know to start an audit by hand inside the pod
(`agents/platform/AGENTS.md`, "Run the `<x>` cron job now"). The inventory report covers most of
what the compliance and reliability audits check (`inventory-findings-queue.md` §10), but nothing
on day one covers cost or capacity, and no pull request opens until the first audit.

## 2. What `oobe` is

One `no_agent` job in `agents/chat/defaults/cron/jobs.json`, ticking every minute, running
`agents/chat/scripts/oobe.py`. Each stage is gated by a marker on the Chat Agent's volume, does its
work once, and then the job removes itself. It lives on the Planning Agent's roster, not the
Platform Agent's, for the reason `bootstrap_scan_gate.py` gives: the onboarding markers are in the
Chat Agent's home, and a job on another profile would gate itself on a different directory.

| Stage                                                           | Done when                                    | Marker                                                     | Built in    |
| --------------------------------------------------------------- | -------------------------------------------- | ---------------------------------------------------------- | ----------- |
| Inventory scan: file the sweep, hand off, file the ranking card | Ranking card filed                           | `.bootstrap_scan_filed`, `.bootstrap_handoff_filed` (kept) | Step 2 (§5) |
| First-run audits                                                | All audits started, or skipped with a reason | `.oobe_audits_fired` (new)                                 | Step 1 (§4) |
| Delivery: post the report to the first chat                     | Report claimed                               | `.bootstrap_completed` (kept)                              | Step 2 (§5) |

Stages this job takes over keep their `.bootstrap_*` markers, so an install upgraded mid-onboarding
carries on from where it was. New stages use `.oobe_*`. The `bootstrap_onboarding` plugin keeps the
first message: it greets, and links the delivery to that chat.

Order within a tick: scan, then audits, then delivery. The audits wait for the scan rather than
starting at boot for two reasons. The report is the operator's first result and should not compete
for the model quota with four audits at once: on an API-key install, three workers running
together have been enough to hit per-minute 429s. And the report should land before the audit
summaries, which assume a fleet the operator has already seen.

## 3. Today's onboarding, for reference

`bootstrap-inventory-scan` (`bootstrap_scan_gate.py`) files the sweep card to `platform`, which
lists the fleet, and records it in `.bootstrap_scan_filed`. On the same job's ticks the hand-off
(`bootstrap_handoff.py`) files one card per Cluster Agent and, once those settle or its deadline of
an hour plus five minutes per cluster card passes (`deadline`), files the ranking card, key
`bootstrap-inventory-prioritize`, recorded in `.bootstrap_handoff_filed`. It files one ranking card
per sweep, and Hermes retries that card in place; a re-run by hand uses a suffixed key
(`bootstrap_onboarding/README.md`). When no cluster was audited the hand-off writes the
report itself and files no ranking card. The ranking worker writes `/opt/data/INVENTORY.md`, on the
sandbox's volume when the shell sandbox is on. `bootstrap-inventory-delivery`
(`bootstrap_delivery.py`) posts it once a human has spoken (`.user_aligned`) and claims
`.bootstrap_completed` with `O_CREAT | O_EXCL`; a run five minutes later removes both jobs.

## 4. First-run audits

### 4.1 Trigger

The stage fires when the scan has settled. The hand-off records the ranking card it filed for this
sweep in `.bootstrap_handoff_filed`; when that card is `done`, `failed`, `cancelled` or `archived`
(the statuses the hand-off itself counts as settled, less `blocked` and `triage`), the stage fires.
When the record says no cluster was audited (`task_id=none`), the hand-off has written the report
itself and the stage fires at once. Before the hand-off has recorded anything, every card under the
ranking key (or the suffixed key of a re-run by hand) filed after the sweep card counts instead. A
card a person may still unblock waits for the fallback below. The marker files are read with the
hand-off's own parser and the card status from the board's SQLite file in the agent pod; neither
costs a call into the sandbox.

Two things the trigger must not be:

- **Delivery** (`.bootstrap_completed`). It waits for a human message, so an install nobody talks
  to would never audit.
- **The report file.** `INVENTORY.md` and `INVENTORY.raw.md` are on the sandbox's volume when the
  sandbox is on, the default. A gate that tests for them on the Chat Agent's volume never fires,
  and testing across the sandbox every minute costs an ssh call per tick.

**Fallback.** If the scan has not settled by the hand-off's deadline for this sweep plus
`RANKING_ALLOWANCE_SECONDS` (30 minutes), counted from `.bootstrap_scan_filed`, fire anyway: 90
minutes for a sweep with no cluster cards, longer by five minutes a card. A stuck sweep, a blocked
ranking card or one never filed must not hold the audits back forever, and a shorter wait would
start them beside a large fleet's ranking card.

**Not a new install.** If, before the stage has started anything, it finds a sweep filed more than
`NEW_INSTALL_SECONDS` (24 hours) earlier, the install onboarded before this job existed but never
reached delivery, so the entrypoint's `--assume-retired` entry (§5) could not tell it apart from a
new one. The stage records the skip and starts nothing; the audits run on their schedules.

### 4.2 Which audits

The four audits [#1866](https://github.com/gke-labs/kube-agents/issues/1866) names, in the order it
chains them (cost, security, reliability, capacity), by their job ids in
`agents/platform/cron/jobs.json`:

| Order | Job id                     | Audit                            | Normal schedule (UTC) |
| ----- | -------------------------- | -------------------------------- | --------------------- |
| 1     | `fleet-wide-cost-analysis` | Fleet waste                      | Mondays 07:50         |
| 2     | `compliance-audit`         | Security and RBAC posture        | Daily 06:20           |
| 3     | `obtainability-audit`      | Workload reliability             | Daily 06:50           |
| 4     | `stockout-prevention`      | Stockout prevention and capacity | Daily 09:20           |

The other five governance jobs keep their schedules.

### 4.3 Firing

The audits run as a chain, back to back: the next is marked due only once the previous one's run
has ended, whatever its outcome. The shipped schedule staggers the audits for the same reason
(`autonomous-watchdogs.md`: "Stagger start minutes so two audits never contend for the same
session"). Four started in the same minute put a fresh CI install's gateway pod under memory
pressure for the best part of an hour and broke the cluster reads of a case running beside them.
For the same reason the chain waits while any of the four is running on its schedule, before the
first mark and between marks; a mark that finds its audit already running counts that run as its
own.
A mark the scheduler has not claimed after `START_LIMIT_SECONDS` (10 minutes) is made again, as a
failed attempt; a run still going after `RUN_LIMIT_SECONDS` (an hour), or a row a gateway restart
left at running, stops holding the chain. The stage is done once the last audit's run has started.

For each audit in turn, `oobe.py` calls Hermes' `cron.jobs.trigger_job(<id>)` in a subprocess of the
gateway's own interpreter with `HERMES_HOME=<agent home>/profiles/platform`, which is where
`cron.jobs` finds the Platform Agent's store. That sets the job's `next_run_at` to now; the next
`profile-cron-tick` runs it within a minute through the schedule's own path, with its prompt,
skills and `deliver: chat`, and the per-job lock keeps a run already in flight from starting twice.

Two routes that look the same are traps. `hermes cron run` runs the whole job synchronously in the
calling process (`hermes_cli/cron.py`, `_job_action` forces it), so a per-minute script would hold
a model run open, outside the tick's environment, until it times out. `cronjob(action='run')` does
the same on some runtimes (`agents/platform/AGENTS.md`).

`.oobe_audits_fired` records each id once it is marked due, and the one in flight with when it was
marked. A failed mark is retried on the next tick, and only that one: marking an audit due again
after it has run starts a second full run.

`trigger_job` also sets the job's `enabled` back to true and clears a pause, so an
audit an operator has disabled or paused, or one missing from the Platform Agent's roster, is
recorded as held and not started. A start that fails is tried five times in all; after that the
audit is left to its schedule, so the stage always finishes and the job always leaves.

### 4.4 No GitOps repository

Every audit opens its run against the GitOps repository before it reads anything
(`audit_report.py start`, `resolve_repo`), so on an install with none configured all four fail
(INSTALL.md: "every run fails before it audits anything"). `oobe.py` reads the managed repositories
from `/etc/gitops/managed_repos`, which the operator mounts into the gateway container
(`platformagent_manifests.go`, `gitopsStateDir`), through `gitops_workspace.get_managed_github_repos`.
With none, it writes the marker with the reason and starts nothing.

It does not exit non-zero. No repository is a configuration the installer offers, not a fault, and
a failing per-minute job repeats until something changes. The operator is told instead: the
interactive installer's "GitOps repository connection skipped" line says the scheduled audits fail
without one,
and once delivery is a stage of `oobe` (§5) the delivered report is to carry one line saying the
first-run audits did not run and why.

### 4.5 What the operator sees

Each audit's usual output, expected within about two hours of install and not yet measured: a ledger issue in the GitOps
repository, a one-line summary in the home channel when one is set, and remediation pull requests
for the findings that auto-promote, at most five per audit run (`audit_report.py`,
`AUTO_PROMOTION_CAP`): critical findings whose fix is a manifest, and major ones on the checks the
collector vouches for (`MAJOR_SWEEP_CHECKS`). Each audit's SOP lists its own in §5. A scheduled run with nothing new is silent
(same section), so the morning after brings no second burst. The four run one after another
(§4.3), so the last starts once the first three have ended (§9, chain length); each
takes 9–15 minutes on its own, most of it inside the SOP
([#985 "Fleet audit SOPs cost 9–15 min per run"](https://github.com/gke-labs/kube-agents/issues/985)).

## 5. Folding in the bootstrap jobs

The second step moves the scan and delivery into `oobe` as stages, behind the audits.

- **Code.** `bootstrap_scan_gate.py` and `bootstrap_delivery.py` stay as modules `oobe.py` calls;
  their markers and claim logic do not change.
- **The old entries.** Both stay in `jobs.json` with `enabled: false`. `cron_jobs_sync.py` lets the
  image win `enabled`, before the scheduler starts, so an upgraded install has them off from its
  first minute. Dropping the ids comes in a later change, once every live install has booted with
  the disabled form; the sync never prunes.
- **The chat link.** The plugin links `oobe` to the first chat instead of
  `bootstrap-inventory-delivery`. An install upgraded after its first message has the link on the
  old job only, and the plugin will not run again, so `cron_jobs_sync.py` copies `deliver` and
  `origin` from the old delivery job to `oobe` when `oobe` is first added. Without that, the report
  goes out with `deliver: local` and reaches nobody.
- **Scan output stays out of chat.** Once linked, anything `oobe` prints is posted to the operator,
  and a non-zero exit is posted as a failure. The scan stage writes to stderr only, including its
  subprocesses (fd 1 redirected for the stage), and an exception in it is caught so it neither
  posts nor blocks delivery.
- **Removal.** Five minutes after `.bootstrap_completed`, `oobe` removes itself and the two disabled
  entries, as `bootstrap_delivery._retire_jobs` does today.
- **Finished installs.** Already in step 1: the entrypoint passes `oobe` in `--assume-retired`
  when `.bootstrap_completed` exists, beside the two bootstrap ids, so an install that onboarded
  before the job existed never gets it.

The fold has no behaviour of its own to show: the operator sees the same messages at the same
times. It rides with the audits stage, whose eval case covers both.

## 6. Failure modes

| Situation              | What the operator sees                                                                                   | Recovery                                                                                              |
| ---------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Audits running         | Nothing in chat until each posts its summary; `hermes cron list` in the platform profile shows them      | None needed                                                                                           |
| Marking an audit fails | Nothing; `oobe` logs and retries the missing ids each minute, five tries per audit                       | Automatic                                                                                             |
| An audit run fails     | The audit's own failure path; `chat-delivery-watch` opens a GitHub issue when reports stop reaching chat | As today                                                                                              |
| No GitOps repository   | One line in the interactive installer's output, later one line in the report                             | Re-run the installer with `--gitops-org` and `--gitops-repo` (INSTALL.md), then wait for the schedule |
| No home channel        | Ledger issues and pull requests appear in the repository with no chat summary                            | Set one (`/sethome`)                                                                                  |
| Sweep stuck            | Audits start at the fallback                                                                             | As today for the report                                                                               |
| Ranking card blocked   | Audits start at the fallback, unless someone unblocks the card first                                     | As today for the report                                                                               |

## 7. Not in this design

- **A combined, prioritized first summary.** Each audit posts its own. How many findings surface,
  and when, follows the pacing [#2451 "pace surfaced findings to the first-24-hour spec"](https://github.com/gke-labs/kube-agents/issues/2451)
  sets out (two criticals in the first report, at most two a day).
- **[#1866](https://github.com/gke-labs/kube-agents/issues/1866)'s 30, 45 and 60 minute targets for cost, security and reliability findings.** The
  chain starts at scan completion in #1866's order, so each audit lands as soon as the ones before
  it end; nothing holds a finding back to meet a target.
- **Reaching an install with no home channel.** Audit summaries go to the home channel only.
  Posting the report there at T+0 when one is set is a separate change.
- **Audits without a GitOps repository.** They would need a chat-only mode in the shared fleet-audit
  script.
- **An install self-check.** Not asked for by any issue.
- **A "first audits are running" line in the report.** A small follow-up once delivery is a stage.

## 8. Build order and evaluation

1. **First-run audits stage**, with `oobe` running beside the two bootstrap jobs, and the
   entrypoint entry that keeps it off finished installs. This covers the
   part of [#1866](https://github.com/gke-labs/kube-agents/issues/1866) that removes the wait; §7 lists what it leaves.
2. **The fold** (§5), in a later change.
3. **Later:** drop the disabled ids; the report line for a skipped audit; T+0 delivery to the home
   channel.

Eval case `oobe-first-run-audits` (domain `fleet-audits`, in `hack/eval/nightly-cases.txt`: each
repetition waits for the previous one's four audits to finish). The stack
(`bench/tf/prebuilt/oobe-first-run-audits`) first waits for the install's own first-run stage to
finish, so it never cuts across a fresh install's real scan, then re-arms the stage: it files an
archived stand-in sweep card and an archived ranking card after it, points `.bootstrap_scan_filed`
at the sweep, clears `.oobe_audits_fired`, and puts back the `oobe` job when the image ships one;
the teardown restores both markers and the job as it found them.
The stack then waits, up to an hour, for the stage to finish its chain, so the verifier's two-minute window opens after the last audit has started. The verifier reads the Platform Agent's cron run records and
passes when the stage's `.oobe_audits_fired` lists all four audits as marked due and each has a run claimed since the stage marked it that is running or completed (a skipped row is passed over), so a scheduled run that falls in the window does not count, and each started only after the one before it in the chain ended. Red: on
an image without the job, no audit runs. Green: four, in three repetitions. The no-repository skip is unit-tested,
not evaluated: the shared install has a repository, and removing it mid-run would break concurrent
cases.

## 9. Open questions

- **Chain length.** Each audit took one to eight minutes on a four-cluster install with
  `gemini-3.1-flash-lite`, so the chain ends within about half an hour of the scan there. On a large
  fleet the last audit may land an hour or more after the scan.
- **Output volume.** How many pull requests and chat lines a first run produces on a real fleet. If
  it reads as noise, day one narrows to cost and capacity, the two audits the inventory report does
  not overlap.
- **CI installs.** A fresh CI install runs the chain during an eval run, beside the audit cases
  that start the same audits on demand. The first smoke run with all four started together showed
  the cost; the chain keeps it to one audit at a time, as on a scheduled morning.
