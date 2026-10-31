# Track a rollout across runs

Every full run records its per-member result and compares itself with the previous run for the
same target, so two runs during a rollout show what moved between them. A `--cluster` run does
neither: it prints a note in place of the progress section, and its JSON carries no `rollout`
block and no per-member `progress`, `unchanged_since` or `unchanged_for_seconds`. The record
lives at
`/opt/data/state/fleet-upgrade-verification/<target>.json` (`channel-default.json` for a run
without `--target-version`), on the persistent volume the shell sandbox keeps between turns;
`--state-dir` points it elsewhere. Each target has its own record, so a run against a different
target starts a new baseline rather than comparing across targets.

The first run for a target prints one line saying the baseline was recorded and where. Every later
run prints a "Rollout progress" section after the table, naming the previous run's time and, per
member, the control-plane and lowest-pool versions then and now, the current status, and one of:

- `completed`: the member is `current` or `ahead` after a version change, or its status became
  `current` or `ahead` since the previous run. A `current` member that followed its channel
  default to a new version is `completed`, not `started`.
- `started`: the control plane or the lowest pool changed version without reaching the target,
  or the cluster or a pool is `RECONCILING`/`PROVISIONING`.
- `unchanged`: the same versions as the previous run. A member whose versions are the same but
  whose status changed (the channel default advanced under it) is also `unchanged`, but it starts
  a new observation series: it is not `stalled` in that run, and its elapsed time counts from it.
- `stalled (unchanged for <elapsed>)`: an `unchanged` member that is `lagging` or `patch-behind`
  while a rollout is active, seen at these versions and this status by the previous run too.
  Elapsed counts from the first of the consecutive runs that saw the member so, and keeps growing
  across runs until the member moves.
- `new`: no record of this member in the previous run.

An `unknown` grade (a failed `get-server-config` read, an unparsable version) is not evidence of a
move: the comparison falls back to the versions alone, the record keeps the last graded status
and its clock, and a member that is `current` at the same versions as an ungraded baseline is
`unchanged`, not `completed`.

A rollout is active when at least one member is `started` or `completed` in the same comparison,
or when the operator passed `--rollout-in-progress`, which is for a rollout whose first wave has
not produced a mover yet. Pass the flag when the user says a rollout is under way; when there is
no earlier record to compare with and the user asks which members have stalled, run the report
twice in the turn with the flag and read the second run's section. Without a mover or the flag,
an unmoved member is `unchanged`, never `stalled`, so two quiet runs a week apart do not invent a
stall. A member that is `current`, `ahead` or
`unknown` is never `stalled`.

A member in the previous record with no row this run is listed once under the section: dropped
from the record when its project was read cleanly (the cluster is gone), carried forward when
its project failed to read or was not in this run's `--project` scope, so a failed read never
loses a record or manufactures a stall. A record the script cannot read is reported on stderr and
replaced by a new baseline; a record it cannot write sets exit code 1 with the table still
printed. In a full run's JSON, each member carries `progress`, `unchanged_since` and
`unchanged_for_seconds`, and the top-level `rollout` block has the record path, both run
timestamps, whether the rollout counted as active and why, a count per progress value, and the
members missing this run.
