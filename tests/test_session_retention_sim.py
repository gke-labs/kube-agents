"""hack/session_retention_sim.py's bookkeeping, with Hermes' SessionDB faked at the import.

The script exists to be run against the real SessionDB; what these tests pin is
what it does around that store: one day's cards per simulated day, the prune at
the retention cut-off on simulated time, the derived ceiling, and the verdict.

Run: python3 -m unittest discover -s tests -p 'test_session_retention_sim.py' -v
"""

import importlib.util
import os
import pathlib
import sqlite3
import sys
import time
import types
import unittest
from unittest.mock import patch

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "hack" / "session_retention_sim.py"


def _load():
    spec = importlib.util.spec_from_file_location("session_retention_sim", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSessionDB:
    """The calls the script makes, on a real SQLite file with the columns it touches."""

    pruned_with = []
    vacuumed = 0

    def __init__(self, db_path):
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL,"
            " ended_at REAL, last_activity_at REAL);"
            "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, timestamp REAL);"
        )

    def create_session(self, sid, source):
        # Hermes stamps a new session with the wall clock.
        self._conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)", (sid, source, time.time())
        )

    def append_message(self, sid, role, content, timestamp=None):
        self._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, role, content, timestamp if timestamp is not None else 0),
        )

    def end_session(self, sid, reason):
        self._conn.execute("UPDATE sessions SET ended_at = 0 WHERE id = ?", (sid,))

    def prune_sessions(self, older_than_days, sessions_dir, exclude_active_write_guards, last_active_before):
        FakeSessionDB.pruned_with.append((older_than_days, exclude_active_write_guards, last_active_before))
        # Hermes' own measure of last activity (_LAST_ACTIVE_SQL): the newest message, else started_at.
        ids = [
            r[0]
            for r in self._conn.execute(
                "SELECT s.id FROM sessions s WHERE s.ended_at IS NOT NULL AND COALESCE("
                "(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id), s.started_at) < ?",
                (last_active_before,),
            )
        ]
        for sid in ids:
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        return len(ids)

    def vacuum(self):
        FakeSessionDB.vacuumed += 1
        self._conn.execute("VACUUM")

    def close(self):
        self._conn.close()


class SimulationTest(unittest.TestCase):
    def setUp(self):
        self.sim = _load()
        FakeSessionDB.pruned_with = []
        FakeSessionDB.vacuumed = 0
        fake = types.ModuleType("hermes_state")
        fake.SessionDB = FakeSessionDB
        modules = patch.dict(sys.modules, {"hermes_state": fake})
        modules.start()
        self.addCleanup(modules.stop)

    def run_sim(self, *extra):
        args = ["--cards-per-day", "3", "--bytes-per-card", "2000", "--messages-per-card", "4", "--series", *extra]
        with patch("builtins.print") as printed:
            code = self.sim.main(args)
        return code, self.sim.json.loads(printed.call_args.args[0])

    def test_rows_level_off_at_retention_times_cards_once_the_prune_runs(self):
        _, out = self.run_sim("--retention-days", "3", "--days", "10")
        sessions = [day["sessions"] for day in out["series"]]
        self.assertEqual([3, 6, 9], sessions[:3])
        self.assertEqual({9}, set(sessions[3:]))
        self.assertEqual(10, len(FakeSessionDB.pruned_with))

    def test_the_prune_cut_off_is_retention_before_the_end_of_each_simulated_day(self):
        self.run_sim("--retention-days", "3", "--days", "6")
        cutoffs = [c[2] for c in FakeSessionDB.pruned_with]
        self.assertEqual({86400.0}, {round(b - a, 3) for a, b in zip(cutoffs, cutoffs[1:])})
        # The last simulated day ends at the run's wall clock, so its cut-off is three days before now.
        self.assertAlmostEqual(cutoffs[-1], time.time() - 3 * 86400, delta=60)
        self.assertTrue(all(c[0] is None and c[1] is True for c in FakeSessionDB.pruned_with))

    def test_the_default_run_is_four_retentions_and_a_month(self):
        _, out = self.run_sim("--retention-days", "2")
        self.assertEqual(4 * 2 + 30, out["assumptions"]["days_simulated"])

    def test_the_ceiling_is_derived_from_the_stated_inputs(self):
        _, out = self.run_sim("--retention-days", "3", "--days", "90", "--slack", "2.5")
        window = 3 * 3 * out["stored_bytes_per_card"]
        # stored_bytes_per_card is rounded to the byte, so nine of them can be off by 4.5.
        self.assertAlmostEqual(out["window_bytes"], window, delta=5)
        self.assertAlmostEqual(out["ceiling_bytes"], out["overhead_bytes"] + window * 2.5, delta=12)
        self.assertIn(out["verdict"], ("under the ceiling", "OVER the ceiling"))

    def test_a_plateau_over_the_ceiling_fails_the_run(self):
        code, out = self.run_sim("--retention-days", "3", "--days", "90", "--slack", "0.01")
        self.assertEqual("OVER the ceiling", out["verdict"])
        self.assertEqual(1, code)

    def test_a_run_shorter_than_the_settling_time_gives_no_verdict(self):
        code, out = self.run_sim("--retention-days", "3", "--days", "2")
        self.assertEqual("too short to settle", out["verdict"])
        self.assertEqual(0, code)

    def test_a_store_still_growing_at_the_end_gives_no_verdict(self):
        with patch.object(FakeSessionDB, "prune_sessions", lambda self, *a, **k: 0):
            code, out = self.run_sim("--retention-days", "3", "--days", "45")
        self.assertEqual("not settled", out["verdict"])
        self.assertEqual(0, code)

    def test_every_session_is_moved_into_the_simulated_past_the_day_it_is_written(self):
        sim = self.sim
        seen = []
        original = FakeSessionDB.prune_sessions

        def prune(db, *args, **kwargs):
            seen.append(db._conn.execute("SELECT MAX(started_at) FROM sessions").fetchone()[0])
            return original(db, *args, **kwargs)

        with patch.object(FakeSessionDB, "prune_sessions", prune):
            self.run_sim("--retention-days", "3", "--days", "6")
        self.assertTrue(all(stamp < sim.time.time() - sim.DAY / 4 for stamp in seen), seen)

    def test_a_slack_or_out_path_that_would_fail_after_the_run_is_refused_first(self):
        for bad in (["--slack", "nan"], ["--slack", "inf"], ["--slack", "0"], ["--out", "/nonexistent-dir/x.json"]):
            with self.subTest(bad=bad), self.assertRaises(SystemExit), patch("sys.stderr"):
                self.sim.main(["--cards-per-day", "3", "--bytes-per-card", "2000", *bad])
        self.assertEqual([], FakeSessionDB.pruned_with)

    def test_counts_below_one_are_refused_at_the_parser(self):
        for flag in ("--cards-per-day", "--messages-per-card", "--retention-days", "--bytes-per-card"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit), patch("sys.stderr"):
                self.sim.main(["--cards-per-day", "3", "--bytes-per-card", "2000", flag, "0"])

    def test_a_warm_up_that_adds_no_page_gives_no_verdict(self):
        with patch.object(self.sim, "store_bytes", lambda conn, home: {"total": 4096, "live": 4096,
                                                                       "freelist_ratio": 0.0, "wal": 0}):
            code, out = self.run_sim("--retention-days", "3", "--days", "45")
        self.assertIn("no per-card growth measured", out["verdict"])
        self.assertIsNone(out["plateau_factor"])
        self.assertEqual(0, code)

    def test_vacuum_runs_only_when_asked(self):
        self.run_sim("--retention-days", "2", "--days", "40")
        self.assertEqual(0, FakeSessionDB.vacuumed)
        _, out = self.run_sim("--retention-days", "2", "--days", "40", "--vacuum-over", "0.0")
        self.assertGreater(out["vacuums"], 0)
        self.assertEqual(out["vacuums"], FakeSessionDB.vacuumed)


@unittest.skipUnless(os.environ.get("HERMES_SRC"), "set HERMES_SRC to a checkout of the pinned Hermes tag")
class AgainstRealHermesTest(unittest.TestCase):
    """Opt-in: the same run against Hermes' real SessionDB. CI has no Hermes, so this is for
    whoever re-runs the measurement, to see the script still drives the store it models."""

    def test_rows_level_off_and_hermes_rows_keep_their_display_identity(self):
        sim = _load()
        home = {}
        real_rmtree = sim.shutil.rmtree
        with patch.object(sim.shutil, "rmtree", lambda path, ignore_errors=False: home.setdefault("path", path)):
            with patch("builtins.print") as printed:
                sim.main(["--hermes-src", os.environ["HERMES_SRC"], "--retention-days", "3", "--cards-per-day", "3",
                          "--bytes-per-card", "2000", "--messages-per-card", "4", "--days", "12", "--series"])
        self.addCleanup(real_rmtree, home["path"], True)
        out = sim.json.loads(printed.call_args.args[0])
        self.assertEqual({9}, {day["sessions"] for day in out["series"][3:]})
        conn = sqlite3.connect(pathlib.Path(home["path"]) / "state.db")
        self.addCleanup(conn.close)
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM messages WHERE display_identity IS NULL").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
