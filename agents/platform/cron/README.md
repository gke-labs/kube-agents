# The Platform Agent's cron roster

`jobs.json` is the Platform Agent's own cron store. This file holds the rules
for editing it, because the store itself cannot: `cron/jobs.py::_save_jobs_unlocked`
writes `json.dump({"jobs": jobs, "updated_at": ...})`, a fresh dict with exactly
two keys, so any top-level `_comment` a shipped roster carries is destroyed by
the first tick. The live pod confirms it — `/opt/data/profiles/platform/cron/jobs.json`
has top-level keys `['jobs', 'updated_at']` and nothing else. An explanation kept
in the JSON survives in git and vanishes everywhere it would actually be read.

Per-**job** keys do survive that rewrite (the job dicts are dumped verbatim), but
the roster does not use them for prose: the reasoning belongs in one place, and
this is it.

## This roster is not inert

The gateway's own ticker is one thread bound to one `HERMES_HOME`, and this image
runs a single gateway homed at `/opt/data`, so the thread only ever ticks the Chat
Agent's store. What reaches this one is `profile-cron-tick`, a `no_agent` job on
that store which runs `hermes cron tick` against every named profile with work due
(see "What fires the schedule" in
[`autonomous-watchdogs.md`](../../../docs/site/src/content/docs/concepts/autonomous-watchdogs.md)).

An enabled entry here therefore fires in its own process, with this profile's
persona, toolsets, `skills`, `model` and `max_turns` — which is the whole reason
the watchdogs live here rather than as kanban cards filed from the Chat Agent's
roster. A card is not a cron run, and that indirection is what stopped `skills`,
`model` and `deliver` reaching the thing that ran.

## When a card _is_ the right shape

Read the paragraph above as being about watchdogs, not as a rule that a cron job
may never file a card. A watchdog fires unconditionally and its product _is_ the
delivery, so routing it through a card loses the run. A poller is the inverse: it
has nothing to deliver on almost every tick, its product goes to GitHub, and it
owes a model turn only when real work exists — which is why `github-repo-watcher`
is a `no_agent` script that costs nothing when idle and files a card when it
finds something. It still names an audible `deliver` for itself — `"chat"` — like
every other report-producing entry here, so a sweep that cannot run still says so. Being
`no_agent` changes what it delivers, not whether it does: a clean tick prints
nothing and relays nothing, and only a sweep that failed produces text.

Adding a sweep is one line in `github_scan_gate.py`'s `SWEEPS` registry —
`SWEEP_ORDER` is derived from it, so there is no second list to keep in step —
not a new cron entry. The consequences of dispatching through a card are in
[`docs/designs/pr-comment-conversation.md`](../../../docs/designs/pr-comment-conversation.md) §2,
and the env knobs that bound a sweep are in §§2 and 4 of the same document.

## `stall-watch` hands a stall to a Cluster Agent card

`stall-watch` is a `no_agent` script: the tick prompts no model. Every thirty
minutes it lists the project's clusters and, for every namespace of every
running or reconciling cluster that has a Cluster Agent profile and is not a
system namespace, runs the Cluster Agent's `stall_report.py` over a bounded list
of controller kinds, keeping a ledger of the rows it has seen. On a new stall
episode it files one kanban card per cluster and namespace, assigned to that
cluster's Cluster Agent profile, telling it to run `gke-stall-detection` on the
namespace and record the finding. That is the same card, diagnosis and chat
thread a user's own question produces, which is the point: one detector and one
experience whether the cron or a person noticed first. The watch follows the
reconciler's roster: a cluster with no profile, one `RECONCILE_EXCLUDE` pruned
or one not yet scaffolded, is neither read nor filed for, because the exclusion
is the operator keeping a model turn off that cluster and a card would hand its
rows to another profile; a cluster that leaves the roster has its rows cleared
and its open card completed with a comment saying so. At most three cards open
per tick (`MAX_CARDS_PER_TICK`, the default of the pull-request poller's
`PR_AGENT_MAX_PER_TICK`), since each is a Cluster Agent turn and the number of
namespaces with a new stall is chosen by whoever can create namespaces; the rest
keep their rows and wait, oldest first sighting first, so a tenant filling three
fresh namespaces every tick cannot keep an older stall from its card, and chat
gets one line saying how many wait. A card the board refused leaves its
namespace waiting the same way. A new object in
a namespace whose card is still open is a comment on that card; when every
object in the namespace has cleared, the card gets a closing comment and is
completed. A `repeating-warnings` or `dangling-reference` row clears only after
two consecutive scans without it, so a warning that recurs hourly or a referent
listing that failed once does not close and reopen a card.

The card's progress reaches chat because the script writes the card's
`kanban_notify_subs` row itself: a cron child has no session identity for
`kanban_create` to copy, and a card without a row is invisible to the gateway
notifier. The row targets every shipped chat platform with a home channel in the agent home's `config.yaml` (`platforms.<p>.home_channel.chat_id`, the field the tick spawner reads, because Hermes strips every `*_HOME_CHANNEL` from a `no_agent` child's environment), with the same `notify+wake` delivery a user-filed card gets; `<PLATFORM>_HOME_CHANNEL` in the environment is read only for a platform the file does not settle, which is a run started by hand. A row the board refused is written on a later tick while the card is open. `deliver: chat` then carries three one-liners, "stall noticed in
`<cluster>` / `<namespace>`: `<objects>`; card `<id>` opened", "stall cleared
...; card `<id>` closed" and "stall noticed in `<n>` more namespaces; cards
follow on later ticks", each naming at most eight objects, plus the sweep-failed
and sweep-recovered lines every roster entry owes. A clean tick prints nothing. Anything a tick could not read (a
cluster that timed out, a namespace whose scan failed, the rows of a kind a scan skipped or the repeating-warnings rows of one that could not read the events, a listing gcloud called incomplete, a sweep that hit its
25-minute budget) keeps its rows and is recorded in the ledger, not posted, and
an exhausted sweep resumes where it stopped.

Every `gcloud`, `kubectl` and `stall_report.py` call runs in the shell sandbox
through `sandbox_exec`, because the agent container carries no kubectl (with the
sandbox switched off the calls run locally and fail with `No such file or
directory: 'gcloud'`, the same failure every `sandbox_exec` caller sees in that
state). The script itself travels on the command's stdin, read from the agent
image's copy in `/opt/defaults/scripts` and run with `python3 -I -`: what the
`hermes` login executes is never a file under the sandbox's agent-owned
`/opt/data`, and isolated mode keeps that directory, which is the command's
working directory, off the module path, so a `json.py` the model dropped there is
not the code that runs. The per-cluster kubeconfigs stay in
`/home/hermes/.kubeconfigs`, where `platform_mcp_server.py` keeps its own, under a
prefix of their own. The kind list is `DEFAULT_KINDS` in the script, cut per
cluster to what `kubectl api-resources` says it serves, and is not
operator-configurable on this release: `STALL_WATCH_KINDS` replaces it for a run
started by hand in the pod (`all` hands `stall_report.py` its every-kind
default), but the operator's env allowlist does not carry it, so a value on the
CR's `spec.deployment.env` never reaches the script. The k8s-event-watcher and
this job split the work by signal: a Warning whose reason is on the watcher's
list is the watcher's within seconds; a condition, a reference or an event the
list never names is this job's within the half hour. It declares `risk: high`
because every card it files starts a Cluster Agent turn over every namespace of
every cluster on the Cluster Agent roster, the management cluster included
unless `RECONCILE_EXCLUDE` names it. The card body and chat lines carry object
names, heuristics and durations only, never a row's detail, since condition
reasons, spec paths and event messages are text a tenant writes; the Cluster
Agent reads that text again when it runs the skill, and its read-only skill and
preflight bound what it does with it.

## `kanban-workspace-gc` is neither a watchdog nor a poller

The third shape, and the reason it is here rather than anywhere else: it is
housekeeping that needs the board DB, and the board DB is on the agent pod.
`kanban_workspace_gc.py` removes the scratch workspaces Hermes leaves behind —
`kanban_db._cleanup_workspace` runs from `complete_task` and nowhere else, so a
card that reaches a terminal state any other way keeps its directory forever, and
under `terminal.backend: ssh` the sandbox's copy is never removed even on the
path that works. `docs/designs/agent-shell-sandboxing.md` has the account.

It reports nothing on a clean run and only on a run it could not finish, which
is the same contract `github-repo-watcher` keeps. What it removed goes to
stderr, where the scheduler logs it: a job announcing its own housekeeping every
night is a job the room learns to skip, and the message that must not be skipped
is the failure.

Daily is deliberate rather than conservative. The first install to run this had
accumulated 34 directories and 3.9 MB on the agent pod and 9 directories and
68 KB in the sandbox; the first sweep removed 21 and 9 of them, leaving 164 KB
and nothing. A tighter interval buys nothing against that rate and spends an SSH
round trip per tick.

## `kanban-board-health` asks whether the board is wedged

The same shape as the collector above: housekeeping that needs the board, which
is on the agent pod at `<agent home>/kanban.db`. `kanban_board_health.py` reads
that file read-only, asks `hermes kanban diagnostics` for the shipped rule
engine's findings, and prints only when something is wrong. One finding is
reported whatever the severity floor says: a card `blocked` for more than a day
with no comment or unblock since. A worker's or operator's block is sticky by
design, nothing retries it, and #656 was a finished report parked that way for
weeks because nothing periodically asked. Each such line carries the card's
kind, reason and age, read from the board, and the `hermes kanban unblock` and
`archive` commands, because the engine's JSON has neither the kind nor the
reason and a line that names a stuck card without saying how to move it is a
line the room learns to skip.

Daily, and it repeats: the rule is stateless, so a card still blocked is named
every morning until someone unblocks, comments on, or archives it. That is the
intended nag, the same rule `findings-morning-nudge` keeps, and the reason a
finer cadence buys nothing: the engine's threshold is a day. It fires at 12:35
UTC, half an hour after that nudge, for the same reason the nudge sits at 12:00:
an earlier UTC hour is the middle of the night in the US, not a morning.

It sits here rather than on the Chat Agent's roster because that roster delivers
`local`. `PLATFORM_AGENT_HOME`, not `HERMES_HOME`, is how it finds the board:
under this roster `HERMES_HOME` is `profiles/platform`, which holds no board.

## `feedback-prompt` asks once, a week after it first runs

The one entry here whose product is a question to the operator rather than a
report on the fleet. Every feedback channel kube-agents has is
reporter-initiated: the tracker, the public form behind the short link, and the
agent handing out both when asked. `feedback_prompt.py` is the one place the
product asks, and it does so once per install: a fixed message in the home
chat channel carrying the form's short link, the form's disclosure that a
submission becomes a public issue, and a line saying a reply in the thread
reaches the agent. `deliver: "chat"` is what makes that last line true; the
Chat Agent posts the message and owns the thread.

It is a daily cron entry that fires once, not a Hermes one-shot, and the
reason is this roster's merge. A shipped entry cannot carry an absolute
`run_at`, and a completed one-shot is pruned from the store after seven days,
at which point `merge_cron_store` sees an id the volume lacks and re-adds it,
re-arming the prompt. So the once-only state lives outside the store, in two
marker files in the profile home (`HERMES_HOME` under this roster is
`profiles/platform`): `.feedback_prompt_armed`, created with `O_EXCL` on the
first tick and holding the anchor time, and `.feedback_prompt_sent`, claimed
with `O_EXCL` before anything reaches stdout, the same claim
`bootstrap_delivery.py` makes and for the same reason, holding the time of the
claim. Both sit on the data volume, which survives
restarts and image rolls and dies only with an uninstall, so an upgrade is not
a new install.

Printing is not delivering: the relay runs after the script, and a claim taken
on a day the relay, the Session KV server or the chat platform was down, or on
an install that had bound no chat platform yet, spends the one message on
nothing. The script does not read the scheduler's record of that delivery
(`last_delivery_error` on its entry in this profile's `cron/jobs.json`, the
field `chat_delivery_watch.py` grades) to post again, because the record
cannot tell a post that landed from one that did not: a `hermes send` that
posts, exits 0 and prints no readable message id is recorded as `composed but
not delivered`, the same words as a send that failed, and so is a relay that
raises or times out after the post has gone out. A retry on that record posts
the request twice, and a message in a channel cannot be taken back. So the
job fails in the direction of at most once: a post the scheduler recorded as
undelivered is a lost request, which `chat-delivery-watch` counts like any
other failed delivery, and which an operator sends again by removing
`.feedback_prompt_sent` from the profile home, after which the next tick
claims afresh. One Chat Agent composition is all the job ever costs, on the
one tick that prints. The clock starts at the first
tick, not at any record of when the install finished: nothing in the operator
status carries a ready-since time, and the Chat Agent's onboarding markers
live in a different home. An install that predates the entry therefore gets
the message a week after the upgrade that brings it.

Every other tick prints nothing and relays nothing, so once the message has
landed the job costs one silent subprocess a day, like `github-repo-watcher`'s idle
ticks. Two environment variables, set per install through the CR's
`spec.deployment.env` and passed to the agent container by the operator's
allowlist, are the whole configuration surface: `FEEDBACK_PROMPT_ENABLED`
(default `true`; `false` neither arms nor claims, so an install that turns it
on later still gets exactly one) and `FEEDBACK_PROMPT_DELAY` (default `7d`;
`<n>d`, `<n>h` or `<n>m`). A delay that does not parse is a failed run, exit 1
with the reason on stderr, which the scheduler reports in chat like any other
script failure until the value is fixed; it does not fall back, because the
scheduler keeps a zero-exit script's stderr nowhere and a silent fallback
would leave no trace but the message arriving a week early. The schedule is
daily, so whatever the delay, the message lands on the first 13:00 UTC tick
at or after it; a delay of a day or more is compared with ten minutes of
slack, because the tick's own time drifts by seconds from one day to the next
and a strict week would otherwise land on day eight. The form URL is a
constant, never a knob: the short link is the only address the maintainers
publish. `enabled: false` on the entry stays the fleet-wide switch, and
retiring it follows the two-step path below like any other id; deleting the
entry outright would leave the volume's copy firing against a script the next
image no longer ships.

## Never put an id on both rosters

Do not add any id here to `agents/chat/defaults/cron/jobs.json` as well. Two
rosters both carrying one id is that audit running twice per schedule,
concurrently with itself, writing its ledger issue twice. The per-job lock
(`cron/.job-<id>.lock`) is per profile directory, so it does not stop this.

## `deliver` is `"local"` on exactly one job

Every enabled job here sets `deliver` to `"chat"` or `"all"`, the two audible
values, with one exception below. `cron/scheduler_delivery.py::_resolve_delivery_targets`
returns an **empty target list** for `"local"` — the outcome is written to
`last_output` and delivered nowhere. A watchdog whose run failed would then be
indistinguishable from a quiet fleet. Both audible values carry a failure: the
scheduler builds one with `_summarize_cron_failure_for_delivery` and delivers it
on the same leg.

Silence is still cheap: a run with no findings returns `[SILENT]` and the
scheduler skips delivery, so a steadily clean fleet generates no chat traffic.

The exception is `chat-delivery-watch`, whose job is to notice that the chat leg
itself is down. Its product is a GitHub ledger issue and an `ALERT` line in
`logs/chat_delivery_watch.log` that fluent-bit ships to Cloud Logging, neither of
which passes through chat, and a chat delivery for it would be circular. The
design is in
[`docs/designs/cron-report-relay.md`](../../../docs/designs/cron-report-relay.md)
under "Detecting a broken leg".

`test_every_watchdog_declares_all_delivery` in
`../skills/fleet-audit/scripts/test_audit_report.py` enforces this, and carries
the exemption by name, pinned to a `no_agent` entry whose script exists.

A misspelled `deliver` part next to one that resolves is dropped by the scheduler
with the run still recording `ok`. `check_cron_delivery` in
`scripts/check_prompt_assets.py` (`make prompt-check`) refuses a value outside the
`CRON_DELIVER_VALUES` set in that file, bare or as a `platform:chat_id` prefix, so
the typo fails the pull request instead.

## `deliver: "chat"` — reporting through the Chat Agent

`"all"` gets the words into a channel. It does not make them answerable: the
process that produced them has exited, and the Chat Agent — which is who the
user replies to — never saw the finding. `"all"` now expands to include the
relay as well as the channel, so a job left on it is heard twice rather than
not at all. The relay skips a platform the scheduler already posted to only
where that leaves it another platform to post to; on an install where the cron
child can address every enabled channel itself, both copies land
([design](../../../docs/designs/cron-report-relay.md)).

`deliver: "chat"` hands the run's report to the Chat Agent instead, which posts
it and thereby owns the thread the user replies in. It is a delivery mode, not a
prompt contract: **the job's prompt says nothing about it**, because the
scheduler applies `[SILENT]` and builds the failure summary before delivery is
reached.

The relay itself posts to every chat platform the install has enabled, so on a
dual-platform install whose cron child can address both, a job left on `"all"`
is heard twice on _each_ of them rather than twice in one place. Two entries here name `"all"`
(`gcp-networking-fabric-audit` and `gce-compute-fleet-audit`) and accept that;
the rest name `"chat"`.

The mode is a bundled platform plugin, not a patch: `chat` is a delivery-only
platform ([`deploy/docker/plugins/chat/`](../../../deploy/docker/plugins/chat/))
that Hermes registers like any other. So it is one target among several — a job
on `"chat,slack"` relays _and_ posts, and an unreachable relay records
`last_delivery_error` rather than falling back. Note that `"all"` now expands to
include the relay, so `"all"` and `"chat"` together deliver once, not twice, but
`"all"` alone relays too. The full rationale — why the Chat Agent composes but
does not send, why the session is per job per day, why a mode rather than an
instruction, and what the plugin route costs — is
[`docs/designs/cron-report-relay.md`](../../../docs/designs/cron-report-relay.md).

## Moving the roster onto `"chat"` needed no migration

Every report-producing job here names `"chat"` or `"all"`, and getting there was an edit to this file alone
— no script, no one-off Job, nothing run against a live volume. `deliver` is an
image-owned key on this profile: `merge_cron_store` gives the image every key it
ships and leaves the volume only the keys it does not, so the next pod start
rewrites `"all"` to `"chat"` on stores this repo can no longer reach by any other
means. `test_the_image_decides_where_a_report_is_delivered` in
`../scripts/test_profile_scaffold.py` pins that.

It does not generalise. The Chat Agent's roster is reconciled by
`cron_jobs_sync.py`, which lists `deliver` in `RUNTIME_WINS` because onboarding
rewrites it to `origin` on the delivery job — there the volume's value stands and
an image edit is ignored. And a job the agent creates at runtime is not in the
image at all, so nothing rewrites it; that one is answered in `../AGENTS.md`, by
telling the agent to pass `deliver='chat'` in the first place.

## `schedule.display` mirrors `schedule.expr`

For `kind: "cron"`, `cron/jobs.py` sets `display` to the raw expression
(`"display": schedule`); the `every {minutes}m` form is what it generates for
`kind: "interval"`. Nothing validates `display` against `expr`, and
`scripts/generate_docs.py` reads `expr` and its own `CRON_CADENCE` table, falling
back to `display` only for interval jobs — which neither roster has. So `display`
is a second copy of `expr` that can rot silently. Keep the two identical.

## Retiring a watchdog

`profile_scaffold.merge_cron_store` adds and overwrites but never prunes.
Deleting an entry only ends this image's ability to hold the job off: the
volume's copy goes on firing. The sequence is therefore:

1. Ship the entry with `enabled: false`. That is what actually stops it.
2. Delete the id only once no live volume can still be carrying an enabled copy
   — and name it in `--cron-retire` in the same release, or the volume keeps a
   disabled entry no later image can reach.

Step 2 is not optional bookkeeping. A deleted entry the volume still holds is
invisible to every future image: the merge is silent about it, so nothing can
re-enable it, disable it, or remove it, and `cronjob(action='list')` reports it
forever. That is why this roster has no tombstones left — the five retired
watchdogs (`blueprint-sync`, `policy-propagation`,
`global-capacity-orchestrator`, `standardization-validator`,
`lifecycle-deprecation-manager`) were deleted here _and_ named in
`--cron-retire` on the platform force-sync.

`retire_cron_jobs` (`--cron-retire` in `deploy/shared/docker-entrypoint.sh`) is
also the escape hatch for the case step 1 cannot cover — an id that has to stop
firing in one release, as when the seven governance jobs moved back here from
the Chat Agent's roster. It deletes the named ids outright, and the entrypoint
names them explicitly.

`github-issue-resolver` took that route too: its replacement polls the same
repository through the same `resolver.py poll`, so leaving it enabled for a
release would keep paying the 48 daily model turns the replacement exists to
stop.

Their SOPs under `../governance/` are deliberately left in place: an SOP is
inert without a job to run it, and keeping them makes reviving a watchdog a
roster edit rather than an archaeology exercise.

## Adding a watchdog: the repository steps

The site's [Autonomous watchdogs](../../../docs/site/src/content/docs/concepts/autonomous-watchdogs.md#adding-a-watchdog)
page lists what an entry needs. Two steps belong to this repository rather than
to an install:

- Run `make docs-generate` after editing either roster. The site's cron
  reference table is generated from both, and a cron expression missing from
  `CRON_CADENCE` in `scripts/generate_docs.py` renders its cadence as `—`. The
  `compliance-audit` entry is also rendered in full as the job-schema example
  on the watchdogs, skills and cron-jobs pages, so an edit to it changes those
  three pages too.
- For a dev workspace, `scripts/dev/dev_rebuild_agent.sh` rebuilds and restarts
  the agent image without a release; `./upgrade.sh --upgrade-mode=harness
--image-tag=<ref>` is the path for an installed cluster.

## How the Planning Agent's roster reaches the volume

The Planning Agent is the `default` profile, which is not scaffolded: it lives
at `$HERMES_HOME` directly and the entrypoint seeds it with
`cp -ru /opt/defaults/. "$TARGET_DIR/"` (`deploy/shared/docker-entrypoint.sh`,
step 2). `cron/` is in neither force-sync list — step 2a covers `SOUL.md`,
`AGENTS.md`, `CAPABILITIES.md` and `hindsight/config.json`, step 2b covers
`scripts/` — and since the
scheduler writes `last_run` into the volume's copy on every tick, that copy's
timestamp is permanently ahead of the image's, and `cp -u` skips it for good.

Step 2c-bis closes that gap: `cron_jobs_sync.py` reconciles
`$HERMES_HOME/cron/jobs.json` against the shipped roster by job id, per key,
under the rule `merge_cron_store` applies on this roster — the image wins every
key it ships, `enabled` among them, and a key it ships nothing for stays as the
volume had it. Two rosters obeying opposite merge rules would be a trap for
whoever edits either. The cron-retirement step 2c (the entrypoint labels two
steps `2c`; this is the one immediately before 2c-bis) forces exactly one id
(`--cron-jobs "profile-cron-tick"`); that narrowness is a deliberate subset of
the same rule rather than a second policy for the same file, because 2c is the
call that also carries `--cron-retire` and an unfiltered merge there would
resurrect the two onboarding jobs `bootstrap_delivery.py` deletes once the
first-run report lands. What stops 2c-bis resurrecting them is a ledger instead:
`$HERMES_HOME/.cron_jobs_installed` records every id the script has installed,
so an id missing from the volume that the ledger already knows about was removed
on purpose, not shipped new, and is never reinstalled.

## Incidents behind the roster's shape

Three rules on the site page came from measured failures, recorded here so the
rule outlives the memory of why:

- **On-demand runs are marked due, never re-enacted in the requesting session.**
  On 2026-08-03 a session asked to run several audits at once crammed them into
  one turn budget and produced five hand-typed empty findings documents and a
  fleet-wide all-clear, having issued no `kubectl` at all. That is why the
  Platform Agent marks the job due for the next tick instead of running the SOP
  itself.
- **Overlap is held per job, not per profile.** Holding the profile lock across
  execution — the upstream default — meant a fleet audit blocked every dispatch
  for its whole run; three `github-issue-resolver` firings were measured 418s,
  179s and 1142s late behind one, each recovering within seconds of the audit
  finishing. The per-job `cron/.job-<id>.lock` is the fix.
- **Pollers are `no_agent` scripts.** As a prompt job, `github-issue-resolver`
  ran a third as often as its replacement and still spent 48 model turns a day
  to be told "nothing to do" 47 times.

## Hard-coded line numbers in prompts

Each governance prompt cites its SOP's total length and the line range of its
checks section. Those numbers are load-bearing — they are what stops a model
reading the first screen and reporting a clean fleet it never looked at — and
they rot the moment an SOP is edited.
`test_cron_prompts_cite_the_real_sop_geography` in
`../skills/fleet-audit/scripts/test_audit_report.py` re-derives both from the
SOP itself, so an edit that skips re-measuring fails there rather than at 06:20
in production. Run it after touching anything in `../governance/`.

No prompt is quoted here on purpose. A copy in prose is one more place for the
same numbers to go stale, and the test above checks the roster against the SOPs
— not this file against the roster.

## `risk` tier contract

Every job entry across both rosters declares an explicit `"risk": "low" | "high"`.
The field is validated by `scripts/check_prompt_assets.py` (`check_cron_risk`) and enforced
across pod restarts by `profile_scaffold.py::merge_cron_store` (an image-owned key).
Runtime-created jobs (`cron.jobs::create_job` and `tools.cronjob_tools::cronjob`) stamp
`"risk": "low"` at creation time unless an explicit `risk` is provided. Existing unannotated
legacy jobs are backfilled to `"risk": "low"` across all profiles:
Platform Agent roster during scaffold merge (`profile_scaffold.py::merge_cron_store`),
Chat Agent roster during reconciliation (`cron_jobs_sync.py`), and cluster profiles both at
profile creation (`cluster_agent_profile.py`) and pod startup (`docker-entrypoint.sh` via
`profile_scaffold.py --backfill-cron`). Unannotated in-flight executions or dispatches with no
tier default fail-closed to `"high"` under `cron_run_scope.py` and `cron_risk_gate.py`.

- `"low"`: Read-only governance watchdogs, audits, and internal scheduler plumbing. Runs under
  the configured `cron_mode` (typically `approve`), protected by the denylist floor (hardline +
  `approvals.deny` + Tirith POSIX shell content scan), terminal escape rejection, lookalike TLD
  blocks, and `execute_code` blocks.
- `"high"`: Workloads with broad operational authority or untrusted input sources (such as
  `github-repo-watcher`), as well as unannotated dispatches. For agentic (prompt-driven) jobs,
  this applies a fail-closed read-only command policy (`cron_command_policy_block`): every command
  segment must be an allowlisted inspection command (`kubectl get/describe/logs/top`, `gcloud … list/describe`,
  read-only text utilities, `--dry-run` validations); mutating, unknown, or unanalyzable commands
  are refused while the run continues. For `no_agent: true` jobs like `github-repo-watcher`, there is
  no agent loop and therefore no tool-approval surface to gate; `"high"` is a threat classification of
  the untrusted input the subprocess ingests (runtime isolation is tracked in #913; today the tier is
  metadata only for those jobs).
