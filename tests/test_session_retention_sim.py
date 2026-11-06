"""hack/session_retention_sim.py's bookkeeping, with Hermes' SessionDB faked at the import.

The script exists to be run against the real SessionDB; what these tests pin is
what it does around that store: one day's cards per simulated day, the prune at
the retention cut-off on simulated time, the derived ceiling, and the verdict.

Run: python3 -m unittest discover -s tests -p 'test_session_retention_sim.py' -v
"""

import importlib.util
import pathlib
import sqlite3
import sys
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
        self._conn.execute("INSERT INTO sessions (id, source, started_at) VALUES (?, ?, 0)", (sid, source))

    def append_message(self, sid, role, content):
        self._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, 0)", (sid, role, content)
        )

    def end_session(self, sid, reason):
        self._conn.execute("UPDATE sessions SET ended_at = 0 WHERE id = ?", (sid,))

    def prune_sessions(self, older_than_days, sessions_dir, exclude_active_write_guards, last_active_before):
        FakeSessionDB.pruned_with.append((older_than_days, exclude_active_write_guards, last_active_before))
        ids = [
            r[0]
            for r in self._conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NOT NULL AND last_activity_at < ?", (last_active_before,)
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
        self.assertTrue(all(c[0] is None and c[1] is True for c in FakeSessionDB.pruned_with))

    def test_the_default_run_is_four_retentions_and_a_month(self):
        _, out = self.run_sim("--retention-days", "2")
        self.assertEqual(4 * 2 + 30, out["assumptions"]["days_simulated"])

    def test_the_ceiling_is_derived_from_the_stated_inputs(self):
        _, out = self.run_sim("--retention-days", "3", "--days", "40", "--slack", "2.5")
        window = 3 * 3 * out["stored_bytes_per_card"]
        self.assertAlmostEqual(out["window_bytes"], window, delta=3)
        self.assertAlmostEqual(out["ceiling_bytes"], out["overhead_bytes"] + window * 2.5, delta=8)
        self.assertIn(out["verdict"], ("under the ceiling", "OVER the ceiling"))

    def test_a_plateau_over_the_ceiling_fails_the_run(self):
        code, out = self.run_sim("--retention-days", "3", "--days", "40", "--slack", "0.01")
        self.assertEqual("OVER the ceiling", out["verdict"])
        self.assertEqual(1, code)

    def test_vacuum_runs_only_when_asked(self):
        self.run_sim("--retention-days", "2", "--days", "40")
        self.assertEqual(0, FakeSessionDB.vacuumed)
        _, out = self.run_sim("--retention-days", "2", "--days", "40", "--vacuum-over", "0.0")
        self.assertGreater(out["vacuums"], 0)
        self.assertEqual(out["vacuums"], FakeSessionDB.vacuumed)


if __name__ == "__main__":
    unittest.main()
