"""Tests for check_cron_inventory.py (#5626).

Run: cd scripts && python3 -m unittest test_check_cron_inventory
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_cron_inventory as cci  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def roster(*jobs):
    return json.dumps({"jobs": [dict(j) for j in jobs]}, indent=2)


def job(job_id, expr="0 * * * *", script=None, no_agent=True, deliver="chat"):
    entry = {"id": job_id, "schedule": {"kind": "cron", "expr": expr}, "deliver": deliver, "no_agent": no_agent}
    if script:
        entry["script"] = script
    return entry


def inventory(*rows):
    lines = ["# Inventory", "", "| Job | Schedule (UTC) | Kind | What it does today |", "| --- | --- | --- | --- |"]
    lines += [f"| `{job_id}` | {text} |" for job_id, text in rows]
    return "\n".join(lines) + "\n"


class Repo:
    """A throwaway git repository with the two rosters and the inventory."""

    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def write(self, chat, platform, inv):
        for rel, text in ((cci.CHAT_ROSTER, chat), (cci.PLATFORM_ROSTER, platform), (cci.INVENTORY, inv)):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def commit(self, message="c"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


class StaticCheckTest(unittest.TestCase):
    def test_every_roster_job_has_a_row_and_every_row_a_job(self):
        problems = cci.static_problems(
            {"a": job("a"), "b": job("b")},
            {"a": "| `a` | x |", "c": "| `c` | x |"},
        )
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("`b`" in p and cci.INVENTORY in p for p in problems), problems)
        self.assertTrue(any("`c`" in p and "no roster" in p for p in problems), problems)

    def test_main_passes(self):
        # The appendix (#5628) lists every job on both rosters.
        rosters = cci.read_rosters(REPO)
        rows = cci.inventory_rows((REPO / cci.INVENTORY).read_text(encoding="utf-8"))
        self.assertEqual(cci.static_problems(rosters, rows), [])
        self.assertGreaterEqual(len(rosters), 30)


class RobustnessTest(unittest.TestCase):
    def test_a_duplicate_id_fails_within_or_across_rosters(self):
        repo = Repo(self)
        repo.write(roster(job("a"), job("a", "1 1 * * *")), roster(), inventory(("a", "x")))
        repo.commit()
        self.assertTrue(any("`a`" in p and "twice" in p for p in cci.check(repo.root, None)))
        repo.write(roster(job("a")), roster(job("a", "1 1 * * *")), inventory(("a", "x")))
        self.assertTrue(any("`a`" in p and "twice" in p for p in cci.check(repo.root, None)))

    def test_no_roster_jobs_at_all_fails(self):
        repo = Repo(self)
        (repo.root / "x").write_text("x")
        repo.commit()
        self.assertTrue(any("no roster" in p.lower() for p in cci.check(repo.root, None)))

    def test_a_bad_base_ref_is_a_readable_failure(self):
        repo = Repo(self)
        repo.write(roster(job("a")), roster(), inventory(("a", "x")))
        repo.commit()
        problems = cci.check(repo.root, "origin/nonexistent")
        self.assertTrue(any("origin/nonexistent" in p for p in problems), problems)


class DiffCheckTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)
        self.repo.write(roster(job("tick", "* * * * *")), roster(job("audit", "20 6 * * *", no_agent=False)),
                        inventory(("tick", "every minute"), ("audit", "daily")))
        self.base = self.repo.commit("base")

    def problems(self):
        return cci.check(self.repo.root, self.base)

    def test_an_unchanged_roster_passes(self):
        self.repo.write(roster(job("tick", "* * * * *")), roster(job("audit", "20 6 * * *", no_agent=False)),
                        inventory(("tick", "every minute, reworded"), ("audit", "daily")))
        self.assertEqual(self.problems(), [])

    def test_a_schedule_change_without_its_row_fails_and_names_the_job_and_file(self):
        self.repo.write(roster(job("tick", "*/5 * * * *")), roster(job("audit", "20 6 * * *", no_agent=False)),
                        inventory(("tick", "every minute"), ("audit", "daily")))
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("`tick`", problems[0])
        self.assertIn(cci.INVENTORY, problems[0])

    def test_a_schedule_change_with_its_row_passes(self):
        self.repo.write(roster(job("tick", "*/5 * * * *")), roster(job("audit", "20 6 * * *", no_agent=False)),
                        inventory(("tick", "every five minutes"), ("audit", "daily")))
        self.assertEqual(self.problems(), [])

    def test_script_no_agent_and_deliver_changes_count(self):
        for name, changed in {
            "script": job("audit", "20 6 * * *", script="new.py", no_agent=False),
            "no_agent": job("audit", "20 6 * * *", no_agent=True),
            "deliver": job("audit", "20 6 * * *", no_agent=False, deliver="local"),
        }.items():
            with self.subTest(name):
                self.repo.write(roster(job("tick", "* * * * *")), roster(changed),
                                inventory(("tick", "every minute"), ("audit", "daily")))
                self.assertEqual(len(self.problems()), 1, name)

    def test_a_non_cron_schedule_change_counts(self):
        base = job("audit", "20 6 * * *", no_agent=False)
        base["schedule"] = {"kind": "interval", "minutes": 30, "display": "every 30m"}
        self.repo.write(roster(job("tick", "* * * * *")), roster(base), inventory(("tick", "every minute"), ("audit", "daily")))
        self.base = self.repo.commit("interval base")
        changed = dict(base, schedule={"kind": "interval", "minutes": 60, "display": "every 60m"})
        self.repo.write(roster(job("tick", "* * * * *")), roster(changed), inventory(("tick", "every minute"), ("audit", "daily")))
        self.assertEqual(len(self.problems()), 1)
        # The display string alone isn't a schedule change.
        relabelled = dict(base, schedule={"kind": "interval", "minutes": 30, "display": "half-hourly"})
        self.repo.write(roster(job("tick", "* * * * *")), roster(relabelled), inventory(("tick", "every minute"), ("audit", "daily")))
        self.assertEqual(self.problems(), [])

    def test_another_field_changing_needs_no_row_change(self):
        # A prompt edit isn't one the inventory tracks.
        changed = job("audit", "20 6 * * *", no_agent=False)
        changed["prompt"] = "a new prompt"
        self.repo.write(roster(job("tick", "* * * * *")), roster(changed), inventory(("tick", "every minute"), ("audit", "daily")))
        self.assertEqual(self.problems(), [])

    def test_an_added_job_needs_a_row(self):
        self.repo.write(roster(job("tick", "* * * * *"), job("new")), roster(job("audit", "20 6 * * *", no_agent=False)),
                        inventory(("tick", "every minute"), ("audit", "daily")))
        problems = self.problems()
        self.assertTrue(any("`new`" in p for p in problems), problems)
        self.repo.write(roster(job("tick", "* * * * *"), job("new")), roster(job("audit", "20 6 * * *", no_agent=False)),
                        inventory(("tick", "every minute"), ("audit", "daily"), ("new", "hourly")))
        self.assertEqual(self.problems(), [])

    def test_a_removed_job_needs_its_row_removed(self):
        self.repo.write(roster(), roster(job("audit", "20 6 * * *", no_agent=False)),
                        inventory(("tick", "every minute"), ("audit", "daily")))
        self.assertTrue(any("`tick`" in p for p in self.problems()))
        self.repo.write(roster(), roster(job("audit", "20 6 * * *", no_agent=False)), inventory(("audit", "daily")))
        self.assertEqual(self.problems(), [])

    def test_a_job_moving_between_rosters_counts_as_unchanged(self):
        self.repo.write(roster(job("tick", "* * * * *"), job("audit", "20 6 * * *", no_agent=False)), roster(),
                        inventory(("tick", "every minute"), ("audit", "daily")))
        self.assertEqual(self.problems(), [])


class MainTest(unittest.TestCase):
    def test_main_without_a_base_runs_the_static_check_only(self):
        repo = Repo(self)
        repo.write(roster(job("tick")), roster(), inventory(("tick", "x")))
        repo.commit()
        env = {k: v for k, v in os.environ.items() if k != "BASE_REF"}
        result = subprocess.run([sys.executable, str(Path(cci.__file__)), "--root", str(repo.root)],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_main_fails_with_the_problem_on_stderr(self):
        repo = Repo(self)
        repo.write(roster(job("tick")), roster(), inventory())
        repo.commit()
        result = subprocess.run([sys.executable, str(Path(cci.__file__)), "--root", str(repo.root)],
                                capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "BASE_REF"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("`tick`", result.stderr)


if __name__ == "__main__":
    unittest.main()
