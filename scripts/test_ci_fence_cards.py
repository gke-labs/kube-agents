# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""hack/ci_fence_cards.py and fence_live_cards in hack/ci-eval-pr.sh (#4425).

A card an earlier unit left running wrote into the next second-phase unit's exclusive
window and failed it at rung 1 (pull-kube-agents-smoke-test 2116235582324609024 on #4323,
2117358437963665408 on #3846). Pinned: which cards the helper archives and which it leaves,
that it reads the board again for a child filed during the pass, that a refusal or an
unreadable board exits non-zero, that the runner's call warns and goes on when the exec
fails, and that only the second phase fences, before each unit's settle.
"""

import os
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "hack" / "ci_fence_cards.py"
RUNNER = REPO / "hack" / "ci-eval-pr.sh"
SINCE = 1_000
PASSES = 3
BUDGET = 60
# The helper's floor for starting an archive, read from source so the test follows it.
ARCHIVE_FLOOR = int(re.search(r"^ARCHIVE_FLOOR_SECONDS = (\d+)$", SCRIPT.read_text(), re.M).group(1))
# The helper's own verdict exit, which the runner tells apart from kubectl's.
LEFT_LIVE = int(re.search(r"^EXIT_LEFT_LIVE = (\d+)$", SCRIPT.read_text(), re.M).group(1))

# The runner's own readonly lines fence_live_cards reads, lifted so a rename there fails here.
RUNNER_CONSTANTS = (
    "EVAL_SANDBOX_EXEC_TIMEOUT",
    "EVAL_GATEWAY_CONTAINER",
    "EVAL_GATEWAY_PYTHON",
    "EVAL_GATEWAY_HOME",
    "EVAL_FENCE_CARD_PASSES",
    "EVAL_FENCE_TIMEOUT_SECONDS",
    "EVAL_FENCE_BUDGET_MARGIN_SECONDS",
    "EVAL_FENCE_DECIDED_EXIT",
    "EVAL_FENCE_ATTEMPTS",
    "EVAL_FENCE_POD_WAIT_SECONDS",
    "EVAL_FENCE_GATEWAY_AWAY_LATCH",
    "EVAL_PLATFORM_RUN_POLL_SECONDS",
)


def runner_constants() -> list[str]:
    text = RUNNER.read_text()
    return [re.search(rf"^readonly {name}=.*$", text, re.M).group(0).replace("readonly ", "", 1) for name in RUNNER_CONSTANTS]


def lifted(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{.*?^\}}$", RUNNER.read_text(), re.S | re.M)
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"{name}() not found in {RUNNER}")
    return match.group(0)


class FenceCardsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name)
        self.board = self.home / "kanban.db"
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.calls = self.home / "calls"

    def tearDown(self):
        self._tmp.cleanup()

    def _cards(self, rows):
        with sqlite3.connect(self.board) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT, created_at INTEGER)")
            conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)

    def _status(self, card):
        with sqlite3.connect(self.board) as conn:
            return conn.execute("SELECT status FROM tasks WHERE id = ?", (card,)).fetchone()[0]

    def _hermes(self, body: str = ""):
        """A fake `hermes kanban archive <id>`: records the call, runs `body`, archives the row."""
        hermes = self.bin / "hermes"
        hermes.write_text(
            "#!/bin/sh\n"
            f'echo "$3" >> "{self.calls}"\n'
            f"{body}\n"
            f"python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE tasks SET status=? WHERE id=?\", (\"archived\", sys.argv[2])); c.commit()' \"{self.board}\" \"$3\"\n"
            'echo "Archived $3"\n'
        )
        hermes.chmod(0o755)

    def _fence(self, passes: int = PASSES, budget: int = BUDGET) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", "-", str(self.home), str(SINCE), str(passes), str(budget)],
            stdin=SCRIPT.open(), capture_output=True, text=True, check=False, timeout=60,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"},
        )

    def test_a_live_card_from_the_fan_out_is_archived_and_the_rest_is_left(self):
        # #4323: vcs-spent-branch-reuse rep 2's card, filed at 21:27:20Z, opened #844 and #845
        # after its unit was graded. Settled cards and the install's own earlier card stay.
        self._cards([
            ("t_orphan", "running", SINCE + 60),
            ("t_queued", "ready", SINCE + 61),
            ("t_todo", "todo", SINCE + 62),
            ("t_done", "done", SINCE + 10),
            ("t_blocked", "blocked", SINCE + 11),
            ("t_install", "running", SINCE - 1),
        ])
        self._hermes()
        done = self._fence()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.calls.read_text().split(), ["t_orphan", "t_queued", "t_todo"])
        self.assertIn("archived 3 card(s) left live since the fan-out began: t_orphan (running), t_queued (ready), t_todo (todo)", done.stdout)
        self.assertEqual(self._status("t_install"), "running")
        self.assertEqual(self._status("t_blocked"), "blocked")

    def test_nothing_live_archives_nothing(self):
        self._cards([("t_done", "done", SINCE + 1)])
        self._hermes()
        done = self._fence()
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "nothing live"))
        self.assertFalse(self.calls.exists())

    def test_no_board_is_nothing_live(self):
        done = self._fence()
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "no board, nothing live"))

    def test_a_child_filed_during_the_pass_is_archived_by_the_next(self):
        self._cards([("t_parent", "running", SINCE + 1)])
        add_child = (
            f'[ "$3" = t_parent ] && python3 -c \'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); '
            f'c.execute("INSERT INTO tasks VALUES (?, ?, ?)", ("t_child", "todo", {SINCE + 2})); c.commit()\' "{self.board}"'
        )
        self._hermes(add_child)
        done = self._fence()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.calls.read_text().split(), ["t_parent", "t_child"])

    def test_a_card_still_live_after_the_last_pass_exits_non_zero(self):
        self._cards([("t_parent", "running", SINCE + 1)])
        add_child = (
            f'[ "$3" = t_parent ] && python3 -c \'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); '
            f'c.execute("INSERT INTO tasks VALUES (?, ?, ?)", ("t_child", "todo", {SINCE + 2})); c.commit()\' "{self.board}"'
        )
        self._hermes(add_child)
        done = self._fence(passes=1)
        self.assertEqual(done.returncode, LEFT_LIVE, done.stdout)
        self.assertIn("still live after 1 passes: t_child (todo)", done.stdout)

    def test_a_refused_archive_exits_non_zero_and_names_the_card(self):
        self._cards([("t_stuck", "running", SINCE + 1)])
        hermes = self.bin / "hermes"
        hermes.write_text('#!/bin/sh\necho "no such task" >&2\nexit 1\n')
        hermes.chmod(0o755)
        done = self._fence()
        self.assertEqual(done.returncode, LEFT_LIVE, done.stdout)
        self.assertIn("would not archive: t_stuck (running): no such task", done.stdout)

    def test_a_card_that_settled_before_its_archive_is_not_reported(self):
        # Its worker finished between the read and the archive, which hermes then refuses.
        self._cards([("t_late", "running", SINCE + 1)])
        hermes = self.bin / "hermes"
        hermes.write_text(
            "#!/bin/sh\n"
            f"python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE tasks SET status=? WHERE id=?\", (\"done\", sys.argv[2])); c.commit()' \"{self.board}\" \"$3\"\n"
            'echo "task is done" >&2\nexit 1\n'
        )
        hermes.chmod(0o755)
        done = self._fence()
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "nothing live"), done.stdout)

    def test_a_budget_spent_mid_list_stops_and_names_the_cards_not_reached(self):
        # The runner's timeout stops kubectl, not the helper in the pod; a helper that kept
        # going could archive the card of the unit launched after it (#4702 review).
        self._cards([("t_a", "running", SINCE + 1), ("t_b", "running", SINCE + 2), ("t_c", "todo", SINCE + 3)])
        self._hermes(f"echo start >> \"{self.home}/starts\"; /bin/sleep 3")
        done = self._fence(budget=ARCHIVE_FLOOR + 2)
        self.assertEqual(done.returncode, LEFT_LIVE, done.stdout + done.stderr)
        self.assertEqual(self.calls.read_text().split(), ["t_a"])
        self.assertIn("not reached: t_b (running), t_c (todo)", done.stdout)
        self.assertIn(f"budget of {ARCHIVE_FLOOR + 2}s", done.stdout)
        self.assertEqual(self._status("t_b"), "running")

    def test_a_budget_below_the_floor_archives_nothing(self):
        self._cards([("t_a", "running", SINCE + 1)])
        self._hermes()
        done = self._fence(budget=ARCHIVE_FLOOR - 1)
        self.assertEqual(done.returncode, LEFT_LIVE, done.stdout)
        self.assertFalse(self.calls.exists())
        self.assertIn("not reached: t_a (running)", done.stdout)

    def test_an_unreadable_board_exits_non_zero(self):
        self.board.write_text("not a database")
        self._hermes()
        done = self._fence()
        self.assertEqual(done.returncode, LEFT_LIVE, done.stdout)
        self.assertIn("board unreadable", done.stdout)

    # --- the runner's call -----------------------------------------------------

    def _runner(
        self, kubectl_body: str, context: str = "gke_proj_us_c", since: str = "1000",
        gateway_body: str = "echo pod/platform-agent-gateway-0", unit: str = ' "pdb-remediation-pr" 3',
        calls: int = 1,
    ) -> subprocess.CompletedProcess:
        """Run the runner's fence under stubs. ``kubectl_body`` answers every exec
        and appends its argv to ``argv`` (one line per call); ``gateway_body`` is
        the ``ready_gateway_pod`` stub, which prints the pod or exits 1."""
        kubectl = self.bin / "kubectl"
        kubectl.write_text(f'#!/bin/sh\necho "$@" >> "{self.home}/argv"\ncat > /dev/null\n{kubectl_body}\n')
        kubectl.chmod(0o755)
        self.unfenced = self.home / "unfenced"
        body = "\n".join([
            "set -euo pipefail",
            f'SCRIPT_DIR="{REPO / "hack"}"',
            *runner_constants(),
            # The pause between attempts is a constant the function reads; the test does not wait it out.
            "EVAL_PLATFORM_RUN_POLL_SECONDS=0",
            f"PROJECT_ID=proj AGENT_CLUSTER_CONTEXT={context} TARGET_NAMESPACE=ns AGENT_SERVICE_NAME=platform-agent",
            f"EVAL_FANOUT_STARTED_EPOCH={since}",
            f'FENCE_UNFENCED_DIR="{self.unfenced}"',
            f"ready_gateway_pod() {{ echo \"$1 $2 $3\" >> \"{self.home}/waits\"; {gateway_body}; }}",
            lifted("fence_live_cards"),
            *[f'fence_live_cards "pdb-remediation-pr rep 3"{unit}; echo "returned $?"' for _ in range(calls)],
        ])
        return subprocess.run(
            ["bash", "-c", body], capture_output=True, text=True, check=False, timeout=60,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"},
        )

    def test_the_runner_execs_the_helper_in_the_gateway_with_the_fan_out_start(self):
        done = self._runner('echo "archived 1 card(s) left live since the fan-out began: t_x (running)"')
        self.assertIn("Card fence (pdb-remediation-pr rep 3): archived 1 card(s)", done.stdout, done.stderr)
        argv = (self.home / "argv").read_text()
        self.assertIn("--context gke_proj_us_c -n ns exec -i pod/platform-agent-gateway-0 -c platform-agent", argv)
        # One wait for a Ready pod per attempt, in the fence's context and
        # namespace, as long as the constant says.
        constants_all = dict(line.split("=", 1) for line in runner_constants())
        self.assertEqual((self.home / "waits").read_text().splitlines(), [f"gke_proj_us_c ns {constants_all['EVAL_FENCE_POD_WAIT_SECONDS']}"])
        self.assertFalse(self.unfenced.exists())
        constants = dict(line.split("=", 1) for line in runner_constants())
        budget = int(constants["EVAL_FENCE_TIMEOUT_SECONDS"]) - int(constants["EVAL_FENCE_BUDGET_MARGIN_SECONDS"])
        self.assertGreater(budget, 0)
        self.assertRegex(argv, rf" - /opt/data 1000 {constants['EVAL_FENCE_CARD_PASSES']} {budget}\n?$")

    def test_a_failed_exec_is_tried_again_once_the_gateway_is_ready(self):
        # The first exec meets a gateway with no container (a preemption mid-phase,
        # #5508); the second, after the wait, archives. The unit launches.
        done = self._runner(
            f'n=$(wc -l < "{self.home}/argv"); [ "$n" -ge 2 ] && {{ echo "archived 1 card(s) left live since the fan-out began: t_x (running)"; exit 0; }}; '
            'echo "error: Internal error occurred: unable to upgrade connection: container not found (\"platform-agent\")"; exit 1'
        )
        self.assertIn("returned 0", done.stdout, done.stderr)
        self.assertIn("Card fence (pdb-remediation-pr rep 3): archived 1 card(s)", done.stdout)
        self.assertIn("exited 1 on attempt 1 of", done.stderr)
        self.assertEqual(len((self.home / "argv").read_text().splitlines()), 2)
        self.assertEqual(len((self.home / "waits").read_text().splitlines()), 2)
        self.assertFalse(self.unfenced.exists())

    def test_a_gateway_that_stays_away_marks_the_unit_unfenced_and_execs_nothing(self):
        done = self._runner('echo "should not run"; exit 1', gateway_body="exit 1")
        self.assertIn("returned 0", done.stdout, done.stderr)
        self.assertFalse((self.home / "argv").exists())
        constants = dict(line.split("=", 1) for line in runner_constants())
        self.assertEqual(len((self.home / "waits").read_text().splitlines()), int(constants["EVAL_FENCE_ATTEMPTS"]))
        self.assertIn(f"could not run after {constants['EVAL_FENCE_ATTEMPTS']} attempt(s) (no Ready gateway pod within", done.stderr)
        self.assertTrue((self.unfenced / constants["EVAL_FENCE_GATEWAY_AWAY_LATCH"].strip('"')).exists())
        self.assertIn("so the unit is not launched (#4425, #5508)", done.stderr)
        note = (self.unfenced / "pdb-remediation-pr.rep3").read_text()
        self.assertIn("the card fence could not run before this unit", note)
        self.assertIn("#5508", note)

    def test_an_exec_that_fails_every_attempt_marks_the_unit_unfenced(self):
        done = self._runner('echo "error: pod not running"; exit 1')
        self.assertIn("returned 0", done.stdout, done.stderr)
        constants = dict(line.split("=", 1) for line in runner_constants())
        self.assertEqual(len((self.home / "argv").read_text().splitlines()), int(constants["EVAL_FENCE_ATTEMPTS"]))
        self.assertTrue((self.unfenced / "pdb-remediation-pr.rep3").exists())

    def test_a_timed_out_fence_says_so_and_is_tried_again(self):
        """124 is `timeout` cutting the exec: a fence that did not run to its
        verdict, so it is retried, and the message names the bound."""
        done = self._runner(f'n=$(wc -l < "{self.home}/argv"); [ "$n" -ge 2 ] && {{ echo "nothing live"; exit 0; }}; exit 124')
        self.assertIn("returned 0", done.stdout, done.stderr)
        constants = dict(line.split("=", 1) for line in runner_constants())
        self.assertIn(f"timed out after {constants['EVAL_FENCE_TIMEOUT_SECONDS']}s", done.stderr)
        self.assertIn("exited 124 on attempt 1 of", done.stderr)
        self.assertIn("Card fence (pdb-remediation-pr rep 3): nothing live", done.stdout)
        self.assertFalse(self.unfenced.exists())

    def test_an_exit_that_is_neither_success_nor_the_verdict_is_tried_again(self):
        """A helper that died before its verdict (here exit 2) is retried like
        an exec that never reached the container, and the note that follows
        says the fence could not run, not that the gateway was away."""
        done = self._runner('echo "Traceback (most recent call last)"; exit 2')
        self.assertIn("returned 0", done.stdout, done.stderr)
        self.assertIn("exited 2 on attempt 1 of", done.stderr)
        self.assertIn("could not run after", done.stderr)
        self.assertNotIn("reach the gateway", done.stderr)
        self.assertTrue((self.unfenced / "pdb-remediation-pr.rep3").exists())

    def test_the_helpers_own_verdict_is_a_warning_and_the_unit_launches(self):
        """The helper reached the board and says what it could not archive: that
        is the fence's result, not a failure to run it, so there is no retry
        and no note, and the unit runs as it did before the fence existed."""
        done = self._runner(f'echo "1 card(s) still live after 3 pass(es): t_x (running)"; exit {LEFT_LIVE}')
        self.assertIn("returned 0", done.stdout, done.stderr)
        self.assertIn("WARNING: Card fence (pdb-remediation-pr rep 3): 1 card(s) still live", done.stderr)
        self.assertIn("#4425", done.stderr)
        self.assertNotIn("attempt", done.stderr)
        self.assertEqual(len((self.home / "argv").read_text().splitlines()), 1)
        self.assertFalse(self.unfenced.exists())

    def test_the_runners_decided_exit_is_the_helpers(self):
        constants = dict(line.split("=", 1) for line in runner_constants())
        self.assertEqual(int(constants["EVAL_FENCE_DECIDED_EXIT"]), LEFT_LIVE)
        self.assertNotEqual(LEFT_LIVE, 1, "kubectl's own failures exit 1; the helper's verdict must not")

    def test_after_an_unfenced_unit_the_next_fence_makes_one_attempt_until_one_succeeds(self):
        """An outage that outlasts the phase costs one wait per unit, not three:
        the first unit pays the attempts and sets the latch, the next pays one,
        and a fence that succeeds clears it."""
        constants = dict(line.split("=", 1) for line in runner_constants())
        attempts = int(constants["EVAL_FENCE_ATTEMPTS"])
        done = self._runner(
            f'n=$(wc -l < "{self.home}/argv"); [ "$n" -ge {attempts + 2} ] && {{ echo "nothing live"; exit 0; }}; echo "error: container not found"; exit 1',
            calls=3,
        )
        self.assertEqual(done.stdout.count("returned 0"), 3, done.stderr)
        # Call 1: every attempt fails and the latch is set. Call 2: one attempt,
        # still failing, latch kept. Call 3: one attempt that succeeds, latch gone.
        self.assertEqual(len((self.home / "argv").read_text().splitlines()), attempts + 2)
        self.assertEqual(done.stderr.count("could not run after"), 2)
        self.assertIn(f"after {attempts} attempt(s)", done.stderr)
        self.assertIn("after 1 attempt(s)", done.stderr)
        self.assertIn("Card fence (pdb-remediation-pr rep 3): nothing live", done.stdout)
        self.assertFalse((self.unfenced / constants["EVAL_FENCE_GATEWAY_AWAY_LATCH"].strip('"')).exists())
        self.assertTrue((self.unfenced / "pdb-remediation-pr.rep3").exists())

    def test_a_caller_that_names_no_unit_gets_the_warning_alone(self):
        done = self._runner('exit 1', unit="")
        self.assertIn("returned 0", done.stdout, done.stderr)
        self.assertIn("could not run after", done.stderr)
        self.assertFalse(self.unfenced.exists())

    def test_the_unit_an_unfenced_note_names_is_recorded_as_infrastructure_and_not_launched(self):
        """run_one_unit reads the note the fence left, beside the fixture-drift one."""
        run_one_unit = lifted("run_one_unit")
        drift = run_one_unit.index('drift_file="${FIXTURE_DRIFT_DIR:-}/${name}"')
        unfenced = run_one_unit.index('unfenced_file="${FENCE_UNFENCED_DIR:-}/${name}.rep${rep}"')
        self.assertLess(drift, unfenced)
        branch = run_one_unit[unfenced:unfenced + 900]
        self.assertIn('record_unit_not_run "${name}" "${rep}" "$(head -n 1 "${unfenced_file}")"', branch)
        self.assertIn('lock_release "${STATE_DIR}/lock-task-${name}"', branch)
        self.assertIn('unfenced_reps="$(finished_rep_count "${name}")"', branch)
        self.assertIn('finish_case "${task}" "${name}"', branch)
        self.assertIn("did not run: its card fence could not run before it", branch)
        # Before any stream, state or infra lock is taken: only the task lock is unwound.
        self.assertLess(unfenced, run_one_unit.index("wait_platform_runs"))
        src = RUNNER.read_text()
        self.assertIn('FENCE_UNFENCED_DIR="${STATE_DIR}/${EVAL_FENCE_UNFENCED_DIR}"', src)

    def test_a_context_that_does_not_name_the_project_is_refused(self):
        done = self._runner('echo "should not run"', context="gke_other_us_c")
        self.assertIn("returned 0", done.stdout)
        self.assertFalse((self.home / "argv").exists())
        self.assertIn("does not name PROJECT_ID=proj", done.stderr)

    def test_only_the_second_phase_fences_and_before_the_settle(self):
        src = RUNNER.read_text()
        self.assertIn('launch_units "${UNIT_QUEUE}" "${EVAL_TASK_PARALLELISM}" "${EVAL_UNIT_LAUNCH_STAGGER_SECONDS}"\n', src)
        self.assertIn('launch_units "${UNIT_QUEUE_WRITERS}" 1 "${WRITER_LAUNCH_PAUSE:-${EVAL_GITHUB_WRITE_SETTLE_SECONDS}}" fence\n', src)
        self.assertLess(src.index('EVAL_FANOUT_STARTED_EPOCH="$(date -u +%s)"'), src.index('launch_units "${UNIT_QUEUE}"'))
        launch = lifted("launch_units")
        self.assertLess(launch.index('fence_live_cards "${TASK_NAMES[IDX]} rep ${REP}" "${TASK_NAMES[IDX]}" "${REP}"'), launch.index('sleep "${pause}"'))
        # An unfenced unit makes no write, so it skips the writer pause too.
        self.assertIn('[ ! -s "${FENCE_UNFENCED_DIR}/${TASK_NAMES[IDX]}.rep${REP}" ]', launch[:launch.index('sleep "${pause}"')])


if __name__ == "__main__":
    unittest.main()
