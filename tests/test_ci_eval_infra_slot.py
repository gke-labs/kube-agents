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
does, and records when each unit started, got the lock and ended. A `sleep` executable first on PATH
turns the dispatcher's 3s polls into a tenth of a second (a shell function
named `sleep` breaks background jobs under bash 3.2).
"""

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL_PR = _REPO_ROOT / "hack" / "ci-eval-pr.sh"


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
RUN_SECONDS=(1.0 1.0 1.0 0.6 0.6 0.6 0.6 0.6 0.6 3.0 0.4 0.3 0.3 0.3)
# Before it queues on lock-infra: stack-stalls waits 2s on something else (a
# busy task or stream lock).
PRE_SECONDS=(0 0 0 0 0 0 0 0 0 0 2.0 0 0 0)
# How long a waiter tries for lock-infra before it gives up (the stub's
# INFRA_LOCK_DEADLINE), in 0.05s tries.
GIVEUP_TRIES="${GIVEUP_TRIES:-200}"
STATE_DIR="$2"
TASKS=("${TASK_NAMES[@]}"); TASK_REUSE=(); TASK_STREAMS=()
for i in "${!TASK_NAMES[@]}"; do TASK_REUSE[i]=""; TASK_STREAMS[i]=""; done
EVAL_REPETITIONS=1
now() { python3 -c 'import time; print(f"{time.monotonic():.3f}")'; }
# As run_one_unit does: a unit off lock-infra with a state lock waits for the
# state; a lock taker with one takes lock-infra, tries the state once, and
# gives lock-infra back while the state is busy; stack-dies exits holding
# lock-infra, as a unit killed mid-run would.
run_one_unit() { # <task-path> <task-name> <rep> <reuse> <has-stack> <seq> <streams>
  local name="$2" start got i state tries=0
  start="$(now)"
  for i in "${!TASK_NAMES[@]}"; do
    if [ "${TASK_NAMES[i]}" = "${name}" ]; then break; fi
  done
  state="$(unit_state_lock "${name}")"
  got="${start}"
  /bin/sleep "${PRE_SECONDS[i]}"
  if [ -z "$5" ] && [ -n "${state}" ]; then
    until mkdir "${STATE_DIR}/lock-state-${state}" 2>/dev/null; do /bin/sleep 0.05; done
  fi
  if [ -n "$5" ]; then
    while :; do
      if mkdir "${LOCK}" 2>/dev/null; then
        if [ -z "${state}" ] || mkdir "${STATE_DIR}/lock-state-${state}" 2>/dev/null; then break; fi
        rmdir "${LOCK}"
      fi
      /bin/sleep 0.05; tries=$((tries + 1))
      if [ "${tries}" -ge "${GIVEUP_TRIES}" ]; then
        printf '%s gaveup %s %s\n' "${name}" "${start}" "$(now)" >> "${LOG}"; return 0
      fi
    done
    got="$(now)"
  fi
  /bin/sleep "${RUN_SECONDS[i]}"
  if [ -n "$5" ] && [ "${name}" != "stack-dies" ]; then rmdir "${LOCK}"; fi
  if [ -n "${state}" ]; then rmdir "${STATE_DIR}/lock-state-${state}"; fi
  # One write per unit, under 512 bytes, so concurrent appends stay whole lines.
  printf '%s %s %s %s\n' "${name}" "${start}" "${got}" "$(now)" >> "${LOG}"
}
"""


class InfraSlotTest(unittest.TestCase):
    maxDiff = None

    def _run(self, queue_indexes, parallelism, giveup_tries=200):
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
                "QUEUE=\"$(printf '1 0 %s\\n' " + " ".join(str(i) for i in queue_indexes) + ")\"",
                f'launch_units "${{QUEUE}}" {parallelism} 0 > /dev/null',
                "wait",
            ])
            env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}", GIVEUP_TRIES=str(giveup_tries))
            result = subprocess.run(
                ["bash", "-c", script, "harness", str(log), str(tmpdir)],
                capture_output=True, text=True, timeout=120, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = []
            self.gave_up = {}
            for line in log.read_text().splitlines():
                name, start, got, end = line.split()
                if start == "gaveup":
                    self.gave_up[name] = (float(got), float(end))
                    continue
                rows.append((name, float(start), float(got), float(end)))
            return rows

    def test_at_most_one_unit_waits_in_a_slot_for_the_infra_lock(self):
        # Three stack-bearing units at the head of the queue, as a cost-sorted
        # queue puts them: before this rule all three took slots and two parked
        # on the lock at once.
        rows = self._run([0, 1, 2, 3, 4, 5, 6, 7, 8], parallelism=4)
        stack = [(start, got) for name, start, got, _ in rows if name.startswith("stack-")]
        self.assertEqual(len(stack), 3)
        events = sorted([(start, 1) for start, got in stack if got - start > 0.3] + [(got, -1) for start, got in stack if got - start > 0.3])
        waiting = peak = 0
        for _, delta in events:
            waiting += delta
            peak = max(peak, waiting)
        self.assertLessEqual(peak, 1, "two units were parked on lock-infra at once")

    def test_no_more_than_the_limit_of_lock_takers_is_in_flight(self):
        rows = self._run([0, 1, 2, 3, 4, 5, 6, 7, 8], parallelism=4)
        stack = [(start, end) for name, start, _, end in rows if name.startswith("stack-")]
        events = sorted([(s_, 1) for s_, _ in stack] + [(e - 0.05, -1) for _, e in stack])
        running = peak = 0
        for _, delta in events:
            running += delta
            peak = max(peak, running)
        self.assertLessEqual(peak, 2)

    def test_the_freed_slots_go_to_units_that_do_not_take_the_lock(self):
        # Four slots and three stack units at the head of the queue: before
        # this rule the three stack units and one plain unit took the four
        # slots; now the third stack unit waits in the queue and two plain
        # units start at once.
        rows = self._run([0, 1, 2, 3, 4, 5, 6, 7, 8], parallelism=4)
        first = min(start for _, start, _, _ in rows)
        at_once = [name for name, start, _, _ in rows if name.startswith("plain-") and start < first + 0.3]
        self.assertGreaterEqual(len(at_once), 2, f"only {at_once} started with the first units")

    def test_a_holder_waiting_on_another_lock_does_not_stall_the_chain(self):
        # stack-stalls is in flight but waits 2s on something else before it
        # queues on lock-infra; the staged stack-a takes lock-infra meanwhile.
        rows = self._run([10, 0, 3], parallelism=4)
        got = {name: g for name, _, g, _ in rows}
        self.assertLess(got["stack-a"], got["stack-stalls"], "the chain waited behind a holder that was not on lock-infra")

    def test_parallelism_is_still_the_ceiling(self):
        rows = self._run([0, 1, 2, 3, 4, 5, 6, 7, 8], parallelism=3)
        events = sorted([(start, 1) for _, start, _, _ in rows] + [(end, -1) for *_, end in rows])
        running = peak = 0
        for _, delta in events:
            running += delta
            peak = max(peak, running)
        self.assertLessEqual(peak, 3)

    def test_an_infra_lock_exempt_stack_is_not_held_back(self):
        # oobe-first-run-audits has a stack but is infra_lock_exempt, so its
        # TASK_HAS_STACK is empty: it launches beside two lock takers.
        rows = self._run([0, 1, 9], parallelism=4)
        starts = {name: start for name, start, _, _ in rows}
        ends = {name: end for name, _, _, end in rows}
        self.assertLess(starts["oobe-first-run-audits"], ends["stack-a"])


    def test_lock_takers_waiting_on_a_busy_state_lock_do_not_hold_up_the_chain(self):
        # oobe-first-run-audits (exempt) holds "onboarding" for 3s; both
        # bootstrap units take lock-infra, find the state busy and give it
        # back. Counted, they would fill the limit and stack-a would wait for
        # oobe; uncounted, stack-a runs at once.
        rows = self._run([9, 11, 12, 0], parallelism=6)
        start = {name: s for name, s, _, _ in rows}
        got = {name: g for name, _, g, _ in rows}
        end = {name: e for name, _, _, e in rows}
        self.assertEqual(self.gave_up, {})
        self.assertLess(got["stack-a"], end["oobe-first-run-audits"], "the tofu chain waited for oobe's state lock")
        self.assertLess(start["stack-a"] - start["oobe-first-run-audits"], 1.0)

    def test_a_dead_holder_costs_one_deadline_per_pair_of_waiters(self):
        # stack-dies exits holding lock-infra and nothing releases it. The
        # three units behind it wait two at a time (the second launches once
        # stack-dies' job ends), each gives up after the stub's deadline (~1s),
        # and the third launches as one of the pair ends: two deadlines in
        # all, not one per waiter.
        self._run([13, 0, 1, 2], parallelism=6, giveup_tries=20)
        self.assertEqual(set(self.gave_up), {"stack-a", "stack-b", "stack-c"})
        starts = sorted(s for s, _ in self.gave_up.values())
        ends = sorted(e for _, e in self.gave_up.values())
        self.assertLess(starts[1], ends[0], "the first two waiters were not in flight together")
        self.assertGreater(starts[2], ends[0] - 0.2, "the third waiter launched before a pair ended")
        self.assertLess(ends[2] - starts[0], 3.5)

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
