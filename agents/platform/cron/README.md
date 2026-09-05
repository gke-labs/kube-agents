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
finds something. It still names an audible `deliver` for itself — `"chat"`, like
every other entry here — so a sweep that cannot run still says so. Being
`no_agent` changes what it delivers, not whether it does: a clean tick prints
nothing and relays nothing, and only a sweep that failed produces text.

Adding a sweep is one line in `github_scan_gate.py`'s `SWEEPS` registry —
`SWEEP_ORDER` is derived from it, so there is no second list to keep in step —
not a new cron entry. The consequences of dispatching through a card are in
[`docs/designs/pr-comment-conversation.md`](../../../docs/designs/pr-comment-conversation.md) §2,
and the env knobs that bound a sweep are in §§2 and 4 of the same document.

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

## Never put an id on both rosters

Do not add any id here to `agents/chat/defaults/cron/jobs.json` as well. Two
rosters both carrying one id is that audit running twice per schedule,
concurrently with itself, writing its ledger issue twice. The per-job lock
(`cron/.job-<id>.lock`) is per profile directory, so it does not stop this.

## `deliver` is never `"local"`

Every enabled job here sets `deliver` to `"chat"` (`"all"` is the other audible
value). `cron/scheduler.py::_resolve_delivery_targets` returns an **empty target list**
for `"local"` — the outcome is written to `last_output` and delivered nowhere. A
watchdog whose run failed would then be indistinguishable from a quiet fleet.
Both audible values carry a failure: the scheduler builds one with
`_summarize_cron_failure_for_delivery` and delivers it on the same leg.

Silence is still cheap: a run with no findings returns `[SILENT]` and the
scheduler skips delivery, so a steadily clean fleet generates no chat traffic.

`test_every_watchdog_declares_all_delivery` in
`../skills/fleet-audit/scripts/test_audit_report.py` enforces this.

## `deliver: "chat"` — reporting through the Chat Agent

`"all"` gets the words into a channel. It does not make them answerable: the
process that produced them has exited, and the Chat Agent — which is who the
user replies to — never saw the finding. `"all"` now expands to include the
relay as well as the channel, so a job left on it is heard twice rather than
not at all.

`deliver: "chat"` hands the run's report to the Chat Agent instead, which posts
it and thereby owns the thread the user replies in. It is a delivery mode, not a
prompt contract: **the job's prompt says nothing about it**, because the
scheduler applies `[SILENT]` and builds the failure summary before delivery is
reached.

The relay itself posts to every chat platform the install has enabled, so on a
dual-platform install a job left on `"all"` is now heard twice on _each_ of them
rather than twice in one place. Every entry here names `"chat"`, so this reaches
only a runtime-created job or a hand-edited roster.

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

Every job here names `"chat"`, and getting there was an edit to this file alone
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
