"""Tests for the fan-out's infra-lock slot rule in hack/ci-eval-pr.sh (#3384, #3821).

A stack-bearing unit runs under lock-infra, one at a time. Before this rule
`launch_units` gave every unit a parallelism slot as soon as one was free, so a
second stack-bearing unit launched while the first ran sat in its slot waiting
for the lock: on the 2026-10-24 main nightly those waits held 25.7 of ~70
slot-hours and the run timed out. Now at most INFRA_IN_FLIGHT_MAX (two) lock
takers without a state lock are in flight, one holding the lock and one staged
behind it, and the slots the rest would have parked go to other units. Lock
takers with a state lock are not counted, so they cannot fill the limit while
an exempt case holds their state. INFRA_LOCK_DEADLINE is sized in holds
rather than per stack-bearing task.

`launch_units`, `unit_state_lock`, `unit_takes_infra_lock`,
`unit_counts_toward_infra_limit` and `INFRA_IN_FLIGHT_MAX` are extracted from
the script and run with `run_one_unit` stubbed: the stub takes real `mkdir`
locks (lock-infra, and a state lock where the case has one) as the script
does, and appends one line per event (start, parked on lock-infra, got, release,
gaveup, end) to a log with the shell's own `printf`, nothing spawned between
the event and its write. The log's order is therefore the order the events
happened, and every assertion reads that order or the sets it implies, never a
clock: a unit held back for a slot or for the limit launches only after
`jobs -rp` has seen another unit exit, and a unit writes its `end` or `gaveup`
line before it exits, so "launched after" is exact. (Timestamps taken with a
`python3` spawn per event were 0.3-0.6s late
under CPU load, and a window of 0.3s around them miscounted a holder as a
waiter: #4482.) A `sleep` executable first on PATH turns the dispatcher's 3s
polls into a tenth of a second (a shell function named `sleep` breaks
background jobs under bash 3.2). What remains time-shaped is the scenario,
not the measurement: a "launched while X ran" check needs X's hold (1-3s
here) to outlast the dispatcher's reaction, a few polls of a tenth of a
second, and the deadline is counted in tries, so it stretches with load.

The order the dispatcher launched units in is read off the launch sequence
number it hands each unit (`UNIT_SEQ`, logged on the `start` line), not off
the `start` lines' own order, which two back-to-back launches can swap.

The dispatcher launches two lock takers at once, and which of them takes
lock-infra is a scheduling race. A test that needs a particular holder names
it with `held_first_by`: the other lock takers wait until that unit holds its
locks before they contend, up to the stub's deadline, so a dispatcher that
never launches the named unit fails the test rather than hanging it.
"""

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL_PR = _REPO_ROOT / "hack" / "ci-eval-pr.sh"

# The event kinds the stub logs. A unit emits either start, [parked,] got,
# [release,] end, or start, parked, gaveup.
START = "start"
# The unit's first try for lock-infra (and its state lock, when it has one)
# failed: it is waiting. A lock taker with a state lock cycles lock-infra
# while it waits rather than sitting parked on it.
PARKED = "parked"
GOT = "got"  # the unit holds every lock it takes
RELEASE = "release"  # just before the holder gives lock-infra back
GAVEUP = "gaveup"  # the unit stopped waiting for lock-infra at its deadline
END = "end"
# The events that take a unit out of the waiting set it entered at PARKED.
LEAVES_WAITING = (GOT, GAVEUP)
# The events after which a unit is no longer in flight: it exits right after
# either one.
LEAVES_FLIGHT = (END, GAVEUP)


def _function(name):
    text = _CI_EVAL_PR.read_text(encoding="utf-8")
    # A one-line function ends on its own line; a block ends at a bare `}`.
    match = re.search(rf"^{name}\(\) \{{[^\n]*\}}\n|^{name}\(\) \{{.*?^\}}\n", text, re.S | re.M)
    assert match, f"could not find {name}() in hack/ci-eval-pr.sh"
    return match.group(0)


def _extract_line(pattern):
    text = _CI_EVAL_PR.read_text(encoding="utf-8")
    match = re.search(pattern, text, re.M)
    assert match, f"could not find {pattern} in hack/ci-eval-pr.sh"
    return match.group(0)


_HARNESS = r"""
set -euo pipefail
LOG="$1"; LOCK="$2/lock-infra"
# Task table: index -> name, stack flag, how long the run takes (seconds).
TASK_NAMES=(stack-a stack-b stack-c plain-1 plain-2 plain-3 plain-4 plain-5 plain-6 oobe-first-run-audits stack-stalls bootstrap-discovery-fanout bootstrap-inventory-posture-scope stack-dies)
# As the script builds it: "true" only for a stack off infra_lock_exempt, so the
# exempt oobe-first-run-audits is empty.
TASK_HAS_STACK=(true true true "" "" "" "" "" "" "" true true true true)
RUN_SECONDS=(1.0 1.0 1.0 0.6 0.6 0.6 0.6 0.6 0.6 3.0 0.4 0.3 0.3 0)
# One try for a lock, or for the unit HELD_FIRST_BY names, every TRY_SECONDS;
# a waiter gives up on lock-infra after GIVEUP_TRIES of them (the stub's
# INFRA_LOCK_DEADLINE).
TRY_SECONDS=0.05
GIVEUP_TRIES="${GIVEUP_TRIES:-200}"
# stack-dies holds for at most half that deadline (the tries stretch under
# load, the one sleep does not), derived from it so the two stay in step:
# long enough that the waiter launched when it ends is still waiting when the
# first waiter gives up, short enough that the first waiter is still waiting
# when it launches.
RUN_SECONDS[13]="$(awk "BEGIN { print ${GIVEUP_TRIES} * ${TRY_SECONDS} / 2 }")"
# The unit that must hold its locks before any other lock taker contends for
# lock-infra; empty for no such order. A gated unit waits GIVEUP_TRIES tries
# at most, then contends anyway.
HELD_FIRST_BY="${HELD_FIRST_BY:-}"
STATE_DIR="$2"
TASKS=("${TASK_NAMES[@]}"); TASK_REUSE=(); TASK_STREAMS=()
for i in "${!TASK_NAMES[@]}"; do TASK_REUSE[i]=""; TASK_STREAMS[i]=""; done
EVAL_REPETITIONS=1
# One append per event, under 512 bytes, so concurrent appends stay whole
# lines; printf is a builtin, so nothing runs between the event and its line.
# The optional third field carries the dispatcher's launch sequence number.
event() { printf '%s %s%s\n' "$1" "$2" "${3:+ $3}" >> "${LOG}"; }
# As run_one_unit does: a unit off lock-infra with a state lock waits for the
# state; a lock taker with one takes lock-infra, tries the state once, and
# gives lock-infra back while the state is busy; stack-dies exits holding
# lock-infra, as a unit killed mid-run would.
run_one_unit() { # <task-path> <task-name> <rep> <reuse> <has-stack> <seq> <streams>
  local name="$2" i state tries=0 parked=""
  event "${name}" start "$6"
  for i in "${!TASK_NAMES[@]}"; do
    if [ "${TASK_NAMES[i]}" = "${name}" ]; then break; fi
  done
  state="$(unit_state_lock "${name}")"
  if [ -z "$5" ] && [ -n "${state}" ]; then
    until mkdir "${STATE_DIR}/lock-state-${state}" 2>/dev/null; do /bin/sleep "${TRY_SECONDS}"; done
  fi
  if [ -n "$5" ]; then
    if [ -n "${HELD_FIRST_BY}" ] && [ "${name}" != "${HELD_FIRST_BY}" ]; then
      until [ -d "${STATE_DIR}/held-${HELD_FIRST_BY}" ] || [ "${tries}" -ge "${GIVEUP_TRIES}" ]; do
        /bin/sleep "${TRY_SECONDS}"; tries=$((tries + 1))
      done
      tries=0
    fi
    while :; do
      if mkdir "${LOCK}" 2>/dev/null; then
        if [ -z "${state}" ] || mkdir "${STATE_DIR}/lock-state-${state}" 2>/dev/null; then break; fi
        rmdir "${LOCK}"
      fi
      if [ -z "${parked}" ]; then parked=1; event "${name}" parked; fi
      /bin/sleep "${TRY_SECONDS}"; tries=$((tries + 1))
      if [ "${tries}" -ge "${GIVEUP_TRIES}" ]; then
        event "${name}" gaveup; return 0
      fi
    done
  fi
  event "${name}" got
  # Logged first, then marked: the marker's mkdir is a spawn, and a unit gated
  # on HELD_FIRST_BY waits for the marker, not the log line. Never removed.
  mkdir -p "${STATE_DIR}/held-${name}"
  /bin/sleep "${RUN_SECONDS[i]}"
  if [ -n "$5" ] && [ "${name}" != "stack-dies" ]; then event "${name}" release; rmdir "${LOCK}"; fi
  if [ -n "${state}" ]; then rmdir "${STATE_DIR}/lock-state-${state}"; fi
  event "${name}" end
}
"""


def _at(events, name, kind):
    """The position of `name`'s one `kind` event."""
    hits = [i for i, (n, k) in enumerate(events) if n == name and k == kind]
    assert len(hits) == 1, f"expected one {kind} event for {name}, got {len(hits)}: {events}"
    return hits[0]


def _waiting_at(events, index):
    """The units waiting for lock-infra once the event at `index` has happened.

    A lock taker with a state lock counts from its first failed try as well,
    though it cycles lock-infra while it waits for the state.
    """
    waiting = set()
    for name, kind in events[: index + 1]:
        if kind == PARKED:
            waiting.add(name)
        elif kind in LEAVES_WAITING:
            waiting.discard(name)
    return waiting


def _peak_in_flight(events, names):
    """The most of `names` that had started and not yet ended or given up at once."""
    running = peak = 0
    for name, kind in events:
        if name not in names:
            continue
        if kind == START:
            running += 1
            peak = max(peak, running)
        elif kind in LEAVES_FLIGHT:
            running -= 1
    return peak


class InfraSlotTest(unittest.TestCase):
    maxDiff = None

    def _run(self, queue_indexes, parallelism, giveup_tries=200, held_first_by="", reps=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            log = tmpdir / "units.log"
            log.write_text("")
            bindir = tmpdir / "bin"
            bindir.mkdir()
            fake_sleep = bindir / "sleep"
            fake_sleep.write_text('#!/bin/sh\nif [ "$1" = "3" ]; then exec /bin/sleep 0.1; fi\nexec /bin/sleep "$@"\n')
            fake_sleep.chmod(0o755)
            script = "\n".join([
                _HARNESS,
                _function("unit_state_lock"),
                _function("unit_takes_infra_lock"),
                _function("unit_counts_toward_infra_limit"),
                _extract_line(r"^INFRA_IN_FLIGHT_MAX=\d+$"),
                _function("launch_units"),
                "QUEUE=\"$(printf '%s 0 %s\\n' " + " ".join(f"{r} {i}" for r, i in zip(reps or [1] * len(queue_indexes), queue_indexes)) + ")\"",
                f'launch_units "${{QUEUE}}" {parallelism} 0 > /dev/null',
                "wait",
            ])
            env = dict(
                os.environ,
                PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                GIVEUP_TRIES=str(giveup_tries),
                HELD_FIRST_BY=held_first_by,
            )
            result = subprocess.run(
                ["bash", "-c", script, "harness", str(log), str(tmpdir)],
                capture_output=True, text=True, timeout=120, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            events = []
            launch_seq = {}
            for line in log.read_text().splitlines():
                name, kind, *seq = line.split()
                events.append((name, kind))
                if kind == START:
                    launch_seq[name] = int(seq[0])
            # The order the dispatcher launched units in.
            self.launched = sorted(launch_seq, key=launch_seq.get)
            return events

    def test_at_most_one_unit_waits_in_a_slot_for_the_infra_lock(self):
        # Three stack-bearing units at the head of the queue, as a cost-sorted
        # queue puts them: before this rule all three took slots and two parked
        # on the lock at once. A holder about to release lock-infra sees every
        # unit parked behind it; with the rule that is the one staged unit,
        # since the third launches only after the holder's job has ended.
        events = self._run([0, 1, 2, 3, 4, 5, 6, 7, 8], parallelism=4)
        releases = [i for i, (name, kind) in enumerate(events) if name.startswith("stack-") and kind == RELEASE]
        self.assertEqual(len(releases), 3, events)
        parked = [_waiting_at(events, i) for i in releases]
        self.assertLessEqual(max(len(p) for p in parked), 1, f"two units were parked on lock-infra at once: {parked}")

    def test_no_more_than_the_limit_of_lock_takers_is_in_flight(self):
        events = self._run([0, 1, 2, 3, 4, 5, 6, 7, 8], parallelism=4)
        stack = {name for name, _ in events if name.startswith("stack-")}
        self.assertLessEqual(_peak_in_flight(events, stack), 2)

    def test_the_freed_slots_go_to_units_that_do_not_take_the_lock(self):
        # Four slots and three stack units at the head of the queue: before
        # this rule the three stack units and one plain unit took the four
        # slots; now the third stack unit waits in the queue and two plain
        # units are among the first four the dispatcher launches.
        self._run([0, 1, 2, 3, 4, 5, 6, 7, 8], parallelism=4)
        at_once = [name for name in self.launched[:4] if name.startswith("plain-")]
        self.assertGreaterEqual(len(at_once), 2, f"only {at_once} launched with the first units: {self.launched}")

    def test_a_holder_waiting_on_another_lock_does_not_stall_the_chain(self):
        # stack-stalls is in flight but waits on something else before it
        # queues on lock-infra (here, until stack-a holds the lock or the
        # deadline passes); the staged stack-a takes lock-infra meanwhile. With
        # one lock taker in flight at a time, stack-a would launch only after
        # stack-stalls had waited out the deadline, held the lock and ended:
        # the start-before-end order is what the limit decides, and the gate
        # makes the got order exact.
        events = self._run([10, 0, 3], parallelism=4, held_first_by="stack-a")
        self.assertLess(_at(events, "stack-a", START), _at(events, "stack-stalls", END), "stack-a was not staged while stack-stalls ran")
        self.assertLess(_at(events, "stack-a", GOT), _at(events, "stack-stalls", GOT), "the chain waited behind a holder that was not on lock-infra")

    def test_a_tasks_next_repetition_waits_for_its_last_and_lends_the_lane(self):
        # The queue is case by case (#3939): plain-1's three repetitions, then
        # plain-2. Launched together, plain-1's later repetitions would sleep
        # on its task lock in two lanes; held back, plain-2 takes a lane at
        # once and plain-1's repetitions never overlap.
        # Read off event order: the three repetitions log under one name, so
        # "never overlap" is a peak of one in flight, and "at once" is plain-2
        # starting before the first repetition has ended.
        events = self._run([3, 3, 3, 4], parallelism=4, reps=[1, 2, 3, 1])
        plain1_starts = [i for i, (name, kind) in enumerate(events) if name == "plain-1" and kind == START]
        plain1_ends = [i for i, (name, kind) in enumerate(events) if name == "plain-1" and kind == END]
        self.assertEqual((len(plain1_starts), len(plain1_ends)), (3, 3), events)
        self.assertEqual(_peak_in_flight(events, {"plain-1"}), 1, "two repetitions of one task were in flight at once")
        self.assertLess(_at(events, "plain-2", START), plain1_ends[0], "plain-2 waited behind plain-1's repetitions")

    def test_parallelism_is_still_the_ceiling(self):
        events = self._run([0, 1, 2, 3, 4, 5, 6, 7, 8], parallelism=3)
        self.assertLessEqual(_peak_in_flight(events, {name for name, _ in events}), 3)

    def test_an_infra_lock_exempt_stack_is_not_held_back(self):
        # oobe-first-run-audits has a stack but is infra_lock_exempt (an empty
        # TASK_HAS_STACK) and has a state lock; either leaves it uncounted, so
        # it launches beside two counted lock takers rather than after one of
        # them has ended. (The launch order alone cannot tell the two apart:
        # oobe is third either way. The exempt-to-empty mapping is built
        # outside the functions extracted here and is not under test.)
        events = self._run([0, 1, 9], parallelism=4)
        oobe_start = _at(events, "oobe-first-run-audits", START)
        self.assertLess(oobe_start, _at(events, "stack-a", END), "the exempt stack launched only after a lock taker ended")
        self.assertLess(oobe_start, _at(events, "stack-b", END), "the exempt stack launched only after a lock taker ended")

    def test_lock_takers_waiting_on_a_busy_state_lock_do_not_hold_up_the_chain(self):
        # oobe-first-run-audits (exempt) holds "onboarding" for 3s; both
        # bootstrap units take lock-infra, find the state busy and give it
        # back. Counted, they would fill the limit and stack-a would launch
        # only after a bootstrap unit ended, which none can before oobe
        # releases the state; uncounted, stack-a launches with them.
        events = self._run([9, 11, 12, 0], parallelism=6, held_first_by="oobe-first-run-audits")
        self.assertEqual([name for name, kind in events if kind == GAVEUP], [])
        stack_a_start = _at(events, "stack-a", START)
        for bootstrap in ("bootstrap-discovery-fanout", "bootstrap-inventory-posture-scope"):
            self.assertLess(stack_a_start, _at(events, bootstrap, END), "the tofu chain waited for oobe's state lock")

    def test_a_dead_holder_costs_one_deadline_per_pair_of_waiters(self):
        # stack-dies takes lock-infra, holds it for half the deadline and
        # exits without releasing it. The three units behind it wait two at a
        # time: stack-a launches with stack-dies, stack-b once stack-dies' job
        # ends, and each gives up at the deadline (60 tries). When the first gives
        # up the other is the one unit parked, and the third launches then,
        # while the other is still waiting: two deadlines in all, not one per
        # waiter.
        events = self._run([13, 0, 1, 2], parallelism=6, giveup_tries=60, held_first_by="stack-dies")
        gave_up = [name for name, kind in events if kind == GAVEUP]
        self.assertEqual(set(gave_up), {"stack-a", "stack-b", "stack-c"}, events)
        first, second, third = gave_up
        first_gaveup = _at(events, first, GAVEUP)
        self.assertEqual(_waiting_at(events, first_gaveup), {second}, "the first two waiters were not in flight together")
        self.assertGreater(_at(events, third, START), first_gaveup, "the third waiter launched before a pair ended")
        self.assertLess(_at(events, third, START), _at(events, second, GAVEUP), "the third waiter did not overlap the second: three deadlines, not two")

    def test_the_infra_lock_deadline_does_not_scale_with_stack_bearing_tasks(self):
        # A counted waiter outlasts two holds plus one per lock taker with a
        # state lock, however many other stack-bearing tasks the run has; a
        # per-task figure would multiply what a dead holder costs (one
        # deadline per pair of waiters).
        text = _CI_EVAL_PR.read_text(encoding="utf-8")
        hold = int(re.search(r"^readonly INFRA_LOCK_LONGEST_HOLD_SECONDS=(\d+)$", text, re.M).group(1))
        block = re.search(r"^STACK_CONTENDERS=0\n.*?^INFRA_LOCK_DEADLINE=.*?\n", text, re.S | re.M).group(0)
        bootstrap = ["bootstrap-discovery-fanout", "bootstrap-inventory-ranking-delivery", "bootstrap-inventory-posture-scope"]

        def deadline(plain_stacks, names_with_state):
            names = [f"stack-{i}" for i in range(plain_stacks)] + names_with_state
            script = "\n".join([
                "set -euo pipefail",
                _function("unit_state_lock"),
                f"INFRA_LOCK_LONGEST_HOLD_SECONDS={hold}",
                "TASK_NAMES=(" + " ".join(names) + ")",
                "TASK_HAS_STACK=(" + " ".join(["true"] * len(names)) + ")",
                block + "echo $INFRA_LOCK_DEADLINE",
            ])
            return int(subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.strip())

        # (At least one stack-bearing task: bash 3.2 under `set -u` rejects an
        # empty array, and the script never builds one.)
        self.assertEqual({deadline(n, []) for n in (1, 2, 25)}, {2 * hold})
        self.assertEqual({deadline(n, bootstrap) for n in (0, 25)}, {5 * hold})
        # The longest lock-infra hold measured (2279s, the #1103 audit rep).
        self.assertGreaterEqual(hold, 2279)

if __name__ == "__main__":
    unittest.main()
