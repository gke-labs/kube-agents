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

# The runner's own readonly lines fence_live_cards reads, lifted so a rename there fails here.
RUNNER_CONSTANTS = (
    "EVAL_SANDBOX_EXEC_TIMEOUT",
    "EVAL_GATEWAY_CONTAINER",
    "EVAL_GATEWAY_PYTHON",
    "EVAL_GATEWAY_HOME",
    "EVAL_FENCE_CARD_PASSES",
    "EVAL_FENCE_TIMEOUT_SECONDS",
    "EVAL_FENCE_BUDGET_MARGIN_SECONDS",
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
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("still live after 1 passes: t_child (todo)", done.stdout)

    def test_a_refused_archive_exits_non_zero_and_names_the_card(self):
        self._cards([("t_stuck", "running", SINCE + 1)])
        hermes = self.bin / "hermes"
        hermes.write_text('#!/bin/sh\necho "no such task" >&2\nexit 1\n')
        hermes.chmod(0o755)
        done = self._fence()
        self.assertEqual(done.returncode, 1, done.stdout)
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
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual(self.calls.read_text().split(), ["t_a"])
        self.assertIn("not reached: t_b (running), t_c (todo)", done.stdout)
        self.assertIn(f"budget of {ARCHIVE_FLOOR + 2}s", done.stdout)
        self.assertEqual(self._status("t_b"), "running")

    def test_a_budget_below_the_floor_archives_nothing(self):
        self._cards([("t_a", "running", SINCE + 1)])
        self._hermes()
        done = self._fence(budget=ARCHIVE_FLOOR - 1)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertFalse(self.calls.exists())
        self.assertIn("not reached: t_a (running)", done.stdout)

    def test_an_unreadable_board_exits_non_zero(self):
        self.board.write_text("not a database")
        self._hermes()
        done = self._fence()
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("board unreadable", done.stdout)

    # --- the runner's call -----------------------------------------------------

    def _runner(self, kubectl_body: str, context: str = "gke_proj_us_c", since: str = "1000") -> subprocess.CompletedProcess:
        kubectl = self.bin / "kubectl"
        kubectl.write_text(f'#!/bin/sh\necho "$@" > "{self.home}/argv"\ncat > /dev/null\n{kubectl_body}\n')
        kubectl.chmod(0o755)
        body = "\n".join([
            "set -euo pipefail",
            f'SCRIPT_DIR="{REPO / "hack"}"',
            *runner_constants(),
            f"PROJECT_ID=proj AGENT_CLUSTER_CONTEXT={context} TARGET_NAMESPACE=ns AGENT_SERVICE_NAME=platform-agent",
            f"EVAL_FANOUT_STARTED_EPOCH={since}",
            lifted("fence_live_cards"),
            'fence_live_cards "pdb-remediation-pr rep 3"',
            'echo "returned $?"',
        ])
        return subprocess.run(
            ["bash", "-c", body], capture_output=True, text=True, check=False, timeout=60,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"},
        )

    def test_the_runner_execs_the_helper_in_the_gateway_with_the_fan_out_start(self):
        done = self._runner('echo "archived 1 card(s) left live since the fan-out began: t_x (running)"')
        self.assertIn("Card fence (pdb-remediation-pr rep 3): archived 1 card(s)", done.stdout, done.stderr)
        argv = (self.home / "argv").read_text()
        self.assertIn("--context gke_proj_us_c -n ns exec -i deployment/platform-agent-gateway -c platform-agent", argv)
        constants = dict(line.split("=", 1) for line in runner_constants())
        budget = int(constants["EVAL_FENCE_TIMEOUT_SECONDS"]) - int(constants["EVAL_FENCE_BUDGET_MARGIN_SECONDS"])
        self.assertGreater(budget, 0)
        self.assertRegex(argv, rf" - /opt/data 1000 {constants['EVAL_FENCE_CARD_PASSES']} {budget}\n?$")

    def test_a_failed_exec_warns_and_the_unit_goes_ahead(self):
        done = self._runner('echo "error: pod not running"; exit 1')
        self.assertIn("returned 0", done.stdout, done.stderr)
        self.assertIn("WARNING: Card fence (pdb-remediation-pr rep 3)", done.stderr)
        self.assertIn("#4425", done.stderr)

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
        self.assertLess(launch.index('fence_live_cards "${TASK_NAMES[IDX]} rep ${REP}"'), launch.index('sleep "${pause}"'))


if __name__ == "__main__":
    unittest.main()
