#!/usr/bin/env python3
"""Tests for session_retention.py: Hermes' SessionDB is faked at the import, the volume is a
real temporary directory."""

import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_retention  # noqa: E402

DAY = 86400


class FakeSessionDB:
    calls: list = []
    results: dict = {}

    def __init__(self, db_path=None, read_only=False):
        self.db_path = Path(db_path)

    def maybe_auto_prune_and_vacuum(self, retention_days, sessions_dir=None, **kwargs):
        FakeSessionDB.calls.append((self.db_path, retention_days, Path(sessions_dir), kwargs))
        outcome = FakeSessionDB.results.get(self.db_path.parent.name, {"pruned": 0})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        FakeSessionDB.calls = []
        FakeSessionDB.results = {}
        fake = types.ModuleType("hermes_state")
        fake.SessionDB = FakeSessionDB
        modules = patch.dict(sys.modules, {"hermes_state": fake})
        modules.start()
        self.addCleanup(modules.stop)
        self.config = {"sessions": {"auto_prune": True, "retention_days": 7}}
        self.use_config(lambda: self.config)

    def use_config(self, load_config):
        """Hermes' merged config (managed scope included) is faked at the import too."""
        config = types.ModuleType("hermes_cli.config")
        config.load_config = load_config
        package = types.ModuleType("hermes_cli")
        package.config = config
        modules = patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli.config": config})
        modules.start()
        self.addCleanup(modules.stop)

    def store(self, *parts):
        """A real SQLite file with the columns of Hermes' schema the pin guard touches."""
        home = self.home.joinpath(*parts)
        home.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(home / session_retention.STATE_DB)) as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, parent_session_id TEXT,"
                " pinned INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT);"
            )
        return home

    def sessions(self, home, *rows):
        """rows are (id, parent, pinned)."""
        with closing(sqlite3.connect(home / session_retention.STATE_DB)) as conn:
            conn.executemany("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)", rows)
            conn.commit()

    def pinned(self, home):
        with closing(sqlite3.connect(home / session_retention.STATE_DB)) as conn:
            return {r[0] for r in conn.execute("SELECT id FROM sessions WHERE pinned = 1")}

    def delete(self, home, session_id):
        """What Hermes' prune does to a session and its children's link."""
        with closing(sqlite3.connect(home / session_retention.STATE_DB)) as conn:
            conn.execute("UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()

    def log(self, home, name, age_days, now):
        logs = home / session_retention.LOGS_DIR
        logs.mkdir(exist_ok=True)
        path = logs / name
        path.write_text("x")
        stamp = now - age_days * DAY
        os.utime(path, (stamp, stamp))
        return path


class EveryStoreIsPruned(RetentionTest):
    def test_the_machine_home_and_every_profile_get_hermes_own_prune_with_the_period(self):
        self.store()
        self.store("profiles", "platform")
        self.store("profiles", "cluster-a")
        self.assertEqual([], session_retention.tick(self.home))
        pruned = [(c[0].parent, c[1], c[2]) for c in FakeSessionDB.calls]
        # A daily tick must not trip Hermes' own 24-hour throttle, and no store is
        # VACUUMed from here: the gateway serves from the machine home or, as the
        # front door, from profiles/platform, and workers hold the rest.
        for path, _, _, kwargs in FakeSessionDB.calls:
            self.assertLess(kwargs["min_interval_hours"], 24)
            self.assertIs(False, kwargs["vacuum"], path)
        self.assertEqual(
            [
                (self.home, 7, self.home / "sessions"),
                (self.home / "profiles" / "cluster-a", 7, self.home / "profiles" / "cluster-a" / "sessions"),
                (self.home / "profiles" / "platform", 7, self.home / "profiles" / "platform" / "sessions"),
            ],
            pruned,
        )

    def test_a_profile_without_a_state_db_is_skipped(self):
        self.store()
        (self.home / "profiles" / "cluster-new").mkdir(parents=True)
        self.assertEqual([], session_retention.tick(self.home))
        self.assertEqual([self.home], [c[0].parent for c in FakeSessionDB.calls])

    def test_the_tick_reads_the_period_from_hermes_merged_config(self):
        self.store()
        self.config["sessions"]["retention_days"] = 12
        # An environment variable is not a second source: only the managed scope sets it.
        with patch.dict(os.environ, {"SESSION_RETENTION_DAYS": "1"}):
            self.assertEqual([], session_retention.tick(self.home))
        self.assertEqual(12, FakeSessionDB.calls[-1][1])

    def test_an_unreadable_config_prunes_nothing_and_says_so(self):
        self.store()

        def broken():
            raise OSError("managed scope unreadable")

        self.use_config(broken)
        lines = session_retention.tick(self.home)
        self.assertEqual([], FakeSessionDB.calls)
        self.assertEqual(1, len(lines))

    def test_no_usable_period_prunes_nothing_and_says_so(self):
        self.store()
        for bad in (0, -3, "seven", None):
            with self.subTest(value=bad):
                self.config["sessions"]["retention_days"] = bad
                FakeSessionDB.calls = []
                lines = session_retention.tick(self.home)
                self.assertEqual([], FakeSessionDB.calls)
                self.assertEqual(1, len(lines))
                self.assertIn("retention_days", lines[0])


class ParentsOfLiveSessionsAreKept(RetentionTest):
    def test_a_parent_is_pinned_while_its_child_exists_and_released_after(self):
        home = self.store()
        self.sessions(home, ("a2a-root", None, 0), ("tip", "a2a-root", 0), ("alone", None, 0))
        session_retention.tick(self.home)
        self.assertEqual({"a2a-root"}, self.pinned(home))
        self.delete(home, "tip")
        session_retention.tick(self.home)
        self.assertEqual(set(), self.pinned(home))

    def test_a_chain_keeps_every_parent_and_unwinds_from_the_tip(self):
        home = self.store("profiles", "platform")
        self.sessions(home, ("a2a-root", None, 0), ("middle", "a2a-root", 0), ("tip", "middle", 0))
        session_retention.tick(self.home)
        self.assertEqual({"a2a-root", "middle"}, self.pinned(home))
        self.delete(home, "tip")
        session_retention.tick(self.home)
        self.assertEqual({"a2a-root"}, self.pinned(home))

    def test_a_pin_set_by_hand_is_never_cleared(self):
        home = self.store()
        self.sessions(home, ("kept-by-hand", None, 1), ("child", "kept-by-hand", 0))
        session_retention.tick(self.home)
        self.delete(home, "child")
        session_retention.tick(self.home)
        self.assertEqual({"kept-by-hand"}, self.pinned(home))

    def test_a_pin_removed_by_hand_is_set_again_while_the_child_exists(self):
        home = self.store()
        self.sessions(home, ("root", None, 0), ("tip", "root", 0))
        session_retention.tick(self.home)
        self.sessions(home, ("root", None, 0))
        session_retention.tick(self.home)
        self.assertEqual({"root"}, self.pinned(home))

    def test_the_pins_are_set_before_hermes_prunes(self):
        home = self.store()
        self.sessions(home, ("root", None, 0), ("tip", "root", 0))
        seen = []
        FakeSessionDB.results = {}
        original = FakeSessionDB.maybe_auto_prune_and_vacuum

        def prune(db, *args, **kwargs):
            seen.append(self.pinned(home))
            return original(db, *args, **kwargs)

        with patch.object(FakeSessionDB, "maybe_auto_prune_and_vacuum", prune):
            session_retention.tick(self.home)
        self.assertEqual([{"root"}], seen)

    def test_a_store_whose_pins_cannot_be_set_is_not_pruned(self):
        self.store()
        broken = self.store("profiles", "broken")
        (broken / session_retention.STATE_DB).write_text("not a database")
        lines = session_retention.tick(self.home)
        self.assertEqual([self.home], [c[0].parent for c in FakeSessionDB.calls])
        self.assertEqual(1, len(lines))
        self.assertIn("profile `broken`", lines[0])


class FailuresAreSaidAndContained(RetentionTest):
    def test_one_store_failing_does_not_stop_the_rest(self):
        self.store()
        self.store("profiles", "cluster-a")
        self.store("profiles", "cluster-b")
        FakeSessionDB.results = {"cluster-a": RuntimeError("database is locked")}
        lines = session_retention.tick(self.home)
        self.assertEqual(3, len(FakeSessionDB.calls))
        self.assertEqual(1, len(lines))
        self.assertIn("profile `cluster-a`", lines[0])
        self.assertIn("`RuntimeError`", lines[0])

    def test_hermes_reported_error_stays_inside_its_code_span(self):
        self.store()
        FakeSessionDB.results = {Path(self.tmp.name).name: {"pruned": 0, "error": "bad `x`\nline two"}}
        lines = session_retention.tick(self.home)
        self.assertEqual(1, len(lines))
        self.assertIn("the machine home", lines[0])
        self.assertEqual(2, lines[0].count("`"), lines[0])

    def test_a_log_that_cannot_be_removed_is_not_a_failed_prune(self):
        now = time.time()
        home = self.store("profiles", "cluster-a")
        self.log(home, "agent.log.2", 10, now)
        self.store("profiles", "cluster-b")
        with patch.object(Path, "unlink", side_effect=PermissionError("read-only")):
            lines = session_retention.tick(self.home, now=now)
        self.assertEqual(2, len(FakeSessionDB.calls))
        self.assertEqual(1, len(lines))
        self.assertIn("removing rotated logs for profile `cluster-a` failed: `PermissionError`", lines[0])
        self.assertNotIn("pruning", lines[0])

    def test_a_clean_run_prints_nothing(self):
        self.store()
        self.assertEqual([], session_retention.tick(self.home))


class RotatedLogs(RetentionTest):
    def test_only_rotated_hermes_logs_older_than_the_period_are_removed(self):
        now = time.time()
        home = self.store("profiles", "cluster-a")
        old_rotated = self.log(home, "agent.log.2", 10, now)
        new_rotated = self.log(home, "agent.log.1", 2, now)
        live = self.log(home, "agent.log", 30, now)
        audit_rotated = self.log(home, "audit.jsonl.3", 30, now)
        audit = self.log(home, "audit.jsonl", 30, now)
        other = self.log(home, "gateway-exit-diag.log", 30, now)
        session_retention.tick(self.home, now=now)
        self.assertFalse(old_rotated.exists())
        for kept in (new_rotated, live, audit_rotated, audit, other):
            self.assertTrue(kept.exists(), kept.name)

    def test_a_log_rotated_away_under_the_scan_does_not_stop_the_sweep(self):
        now = time.time()
        home = self.store()
        gone = self.log(home, "agent.log.2", 10, now)
        other = self.log(home, "errors.log.3", 10, now)
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == gone:
                raise FileNotFoundError(path)  # Hermes renamed it between the scan and here
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            removed = session_retention.remove_old_rotated_logs(home, 7, now)
        self.assertFalse(other.exists())
        self.assertEqual(1, removed)

    def test_a_symlinked_rotated_log_is_not_followed(self):
        now = time.time()
        home = self.store()
        target = self.home / "elsewhere.txt"
        target.write_text("keep")
        (home / "logs").mkdir()
        link = home / "logs" / "agent.log.1"
        link.symlink_to(target)
        os.utime(target, (now - 30 * DAY, now - 30 * DAY))
        session_retention.tick(self.home, now=now)
        # Skipped outright: a link is not a Hermes rotation, whatever its target's age.
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
