# fleet-audit — Running a stream on demand

Part of the `fleet-audit` skill (`/opt/data/profiles/platform/skills/fleet-audit/SKILL.md`), which says when to open this file.

## Running a stream on demand

An audit request arrives in one of three distinct forms, distinguished by the request itself:

### 1. Asked to run an audit stream per its SOP: Run the audit directly

When you are delegated a task or kanban card to execute an audit stream following its standard operating procedure (e.g. _"Run the security and RBAC posture audit, following the compliance-audit SOP"_ or a card naming the stream and SOP):

- **You are the audit worker.** You have been given a dedicated worker session and turn budget for this specific audit stream. That budget is the kanban worker's, the profile's `agent.max_turns` (150), not the 250 the stream's roster entry gives its scheduled run (form 2 below). On a large fleet a cluster-by-cluster sweep can exhaust it; the worker then dies with the run still in flight, and the retry's `start` is refused as already running.
- **Execute the audit following its SOP (mapped in `AUDITS` at the top of `audit_report.py`, e.g. `/opt/data/profiles/platform/governance/compliance_audit_sop.md` for `compliance-audit`) directly.** Use the two-command lifecycle ([lifecycle.md](lifecycle.md), [finish.md](finish.md)):
  1. `/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py start --audit <stream> [--repo "<owner>/<repo>"]`
  2. Read the SOP in full — `start` prints its path as `sop` — then enumerate clusters and run the checks as it says. Do not work the audit out from this skill and a script's `--help`: where the SOP runs a collector, it is the SOP that says how to invoke it and that every candidate in the manifest is a verified finding. A candidate your document leaves out is not published: `finish` reports it under `unpublished_candidates`, and holds it on the ledger if an earlier run already carried it.
  3. `/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py finish --audit <stream> ...`
- **Run `start` first, by its full path, and read the SOP at the full path it prints as `sop`. Do not search for the SOP.** On a kanban card your working directory is the card's own workspace, not the profile home, so a relative path such as `governance/…` or `./skills/…`, or a `search_files`, finds nothing there.
- **Do not reach for `hermes cron run` or say "queued for the next cron tick":** This request is an explicit on-demand audit execution, not a request to trigger the scheduled cron job. Execute the SOP directly and report the ledger issue URL in your result.
- **`start` refuses while a run of that stream is in flight, a scheduled tick's or another session's:**
  it exits 2 with a `START REFUSED` line that names the run (not a `FINDINGS REJECTED` line; there is
  no document to fix). If it refuses, say the stream is already running and stop; there is no override
  for you, and the refusal is not a problem to work around. A `BROKER UNAVAILABLE` line is a different
  refusal: the credential proxy did not answer, so check the credential-proxy pod and re-run the same
  command once it answers (see the exit codes in [finish.md](finish.md#step-3--finish) for what a refused `finish` leaves in flight). One refusal is your own: the note does not
  know sessions, so if your `start` for that stream already succeeded in this session, a second `start`
  is refused like anyone's and your first run is untouched — do not run `start` again; continue the
  sweep from the first `start`'s output to `finish`. That holds only while no `finish` for that stream
  has released the lease since: a `finish` that published (exit 0) or died (exit 1) released it, and a
  refusal after one is someone else's run — stop and report the sweep as partial. A `finish` that
  exited 2 (`FINDINGS REJECTED`) kept the lease, so after it the run is still yours and the next step
  is to fix the document and run `finish` again, never `start`.
- **Alignment with `AGENTS.md`:** `AGENTS.md` ("'Run the `<x>` cron job now' → trigger the schedule, do not re-enact it") addresses requests asking to trigger the background cron job or to run multiple/all audits in a single session. When delegated a task to execute a single audit stream per its SOP, you are the dedicated worker session for that stream; execute the audit directly.
- **A card whose result is "queued for later" must NEVER be marked `done` (#1876):** If an audit cannot be run in this session due to missing credentials or infrastructure failure, call `kanban_block` (or ask for input); **never** call `kanban_complete` claiming `done` when zero findings or ledger were produced. `kanban_complete` refuses a call that has no `finish` result line after your last `start`. In that case, run `finish`, or call `kanban_block` with the reason. Another `kanban_complete` within 15 minutes only closes the card marked "Not published".

### 2. Asked to run or trigger the scheduled cron job, or to run multiple/all audits

When a request asks to trigger or run the scheduled cron job (e.g. _"Run the `compliance-audit` cron job now"_, _"Trigger the compliance-audit schedule"_), or asks to run multiple or all scheduled audits in one request (e.g. _"Run all scheduled audits"_):

Each stream's cron job id **is** its audit id, so an operator asking for a run off-schedule is asking
for one command per stream:

```
HERMES_HOME=/opt/data/profiles/platform /opt/hermes/.venv/bin/hermes cron run compliance-audit
```

Every stream's cron job lives in this profile's own roster, ticked once a minute by the Chat Agent's
`profile-cron-tick`. `hermes cron run` marks the job due rather than running it here; the next tick
picks it up within a minute and runs it through the identical path the 06:20 tick uses, with the
stream's prompt verbatim, its `skills` preloaded, and the entry's own `max_turns`.

`cronjob(action='run')` is not the route. Where the session cannot take a detached result — a
one-shot `hermes -z`, a stateless HTTP turn, a Kanban worker, a nested cron run — or where the
dispatch pool is full, it
executes the job synchronously inside the session that calls it, which is the re-enactment the next
paragraph exists to prevent. Elsewhere it hands the run to the background delegation executor and
returns a handle; that is closer to what you want, but `hermes cron run` is the one route that
behaves identically on every runtime and always runs in a fresh process.

**Your shell cannot reach that command in the sandbox pod, and there is no substitute yet.** It runs on the gateway pod,
where `hermes` and `/opt/data/profiles` are; your shell runs in the sandbox pod, which has neither, so
`command not found` there is the split working as designed rather than a broken install. When you hit
it, say the on-demand trigger is unavailable and that the stream will run on its 06:20 schedule. That
does not license either fallback: not `cronjob(action='run')`, and not running the audit yourself inline.

**Never do the audit in the session that received the request when asked to trigger the cron job or run all audits.** A triggered run gets its
own process and its own turn budget. A session that improvises multiple audits instead has neither — and
when the request is "run them all", it has one turn budget for work the schedule spreads across
every stream and two days. That is not a hypothetical failure mode: on 2026-08-03 a single worker
asked to run all five streams that existed then issued zero `kubectl` commands, hand-typed five
empty findings documents, and published a fleet-wide all-clear.

The scheduler holds a per-job lock for the length of a run, so a stream already in flight is not
started a second time and cannot write its ledger issue twice. `cronjob(action='runs')` shows what
is running and what each attempt did.

A stream run as a delegated worker (form 1 above) holds no such lock, and the scheduler's ledger
never sees it, so the guard lives in the script: `start` leaves an in-flight note for the stream and
refuses while one younger than two hours exists, whichever side wrote it, and `finish` removes it
when that `start`'s run is over, published (exit 0) or died (exit 1, whatever the cause), so a `finish` that died does not
refuse the stream's next run or your own retry. The note spans one `start`-`finish` pair, and a run
publishes to one repository, the one with `role: gitops`: do not loop over the managed
repositories. If a person asked for several repositories, each with its own `--repo`, a `start`
refused between two of them means that another run took the stream; stop there and report the
repositories not yet audited. Two exits keep it: `--dry-run`, a preview mid-run, and exit
2, a rejected document you are about to fix and resubmit, which is still the run in flight. If
`start` cannot take the guard at all it exits 2 too, rather than run unguarded, also as
`START REFUSED`. A run that died before `finish` is forgotten after those two hours, so a dead run
costs the stream at most the ticks that fall inside them; releasing it sooner is an operator's action
from outside the session.

**Each run reports on itself. Your own answer is a roll-up, not a copy.** When triggering cron jobs, answer with one line per
stream — the stream, and that it is queued for the next tick (or that the on-demand trigger is unavailable). The reports arrive through each run's
own `deliver` setting; repeating them here sends the same content twice.

### 3. Asked what an audit found

A question about a run that already happened — what a stream last found, what changed since
yesterday, which findings the ledger omitted for space, whether a stream is stuck — is not a request
to run anything. Every `finish` that publishes (exits 0, not `--dry-run`) keeps its report on the
volume, best-effort, and
[`fleet-audit-reports`](../../fleet-audit-reports/SKILL.md) answers from it without a `start`, a lease
or a GitHub call. Do not re-run a stream to answer a question its last report already answers.
