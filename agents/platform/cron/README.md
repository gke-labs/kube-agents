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

## Never put an id on both rosters

Do not add any id here to `agents/chat/defaults/cron/jobs.json` as well. Two
rosters both carrying one id is that audit running twice per schedule,
concurrently with itself, writing its ledger issue twice. The per-job lock
(`cron/.job-<id>.lock`) is per profile directory, so it does not stop this.

## `deliver` is `"all"`, deliberately

Every enabled job here sets `deliver: "all"`. `cron/scheduler.py::_resolve_delivery_targets`
returns an **empty target list** for `"local"` — the outcome is written to
`last_output` and delivered nowhere. A watchdog whose run failed would then be
indistinguishable from a quiet fleet. `"all"` expands at fire time to every
platform with a configured home channel, so a failure is audible.

Silence is still cheap: a run with no findings returns `[SILENT]` and the
scheduler skips delivery, so a steadily clean fleet generates no chat traffic.

`test_every_watchdog_declares_all_delivery` in
`../skills/fleet-audit/scripts/test_audit_report.py` enforces this.

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
