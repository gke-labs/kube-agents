"""Tests for deploy/shared/sqlite_journal_migrate.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

The script rewrites the pod's databases, so what these hold is the contract that
makes that safe: it acts only when the managed config pins `delete`, only on the
governed files, only when their header reads WAL, and never on a database another
connection holds open. The rows a crashed writer left in the WAL survive the
conversion, because the checkpoint runs before the switch.
"""

import importlib.util
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "deploy" / "shared" / "sqlite_journal_migrate.py"

_spec = importlib.util.spec_from_file_location("sqlite_journal_migrate", _SCRIPT)
sjm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sjm)

_DELETE_CONFIG = "database:\n  journal_mode: delete\n"
_WAL_CONFIG = "database:\n  journal_mode: wal\n"

# Writes rows into a WAL database and exits without closing, the way a killed
# gateway does, so the rows stay in the -wal file rather than the main file.
_CRASHED_WRITER = """
import sqlite3, sys, os
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
conn.commit()
os._exit(0)
"""


def _make_db(path, mode="WAL"):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA journal_mode={mode}")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path


def _journal_mode(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


def _header(path):
    with path.open("rb") as handle:
        header = handle.read(sjm.HEADER_LENGTH)
    return header[sjm.HEADER_WRITE_VERSION_OFFSET], header[sjm.HEADER_READ_VERSION_OFFSET]


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.home = self.root / "data"
        self.home.mkdir()
        self.config = self.root / "managed" / "config.yaml"
        self.config.parent.mkdir()

    def pin(self, text=_DELETE_CONFIG):
        self.config.write_text(text, encoding="utf-8")

    def run_script(self, *extra):
        return subprocess.run(
            [sys.executable, str(_SCRIPT), "--agent-home", str(self.home), "--managed-config", str(self.config), *extra],
            capture_output=True,
            text=True,
            timeout=60,
        )


class ConversionTest(_Case):
    def test_a_wal_database_is_converted_and_its_sidecars_removed(self):
        db = _make_db(self.home / "state.db")
        self.assertEqual(_header(db), (2, 2))
        self.pin()

        proc = self.run_script()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_header(db), (1, 1))
        self.assertEqual(_journal_mode(db), "delete")
        self.assertFalse(db.with_name("state.db-wal").exists())
        self.assertFalse(db.with_name("state.db-shm").exists())
        self.assertIn("state.db: converted to delete", proc.stderr)

    def test_rows_a_crashed_writer_left_in_the_wal_survive(self):
        """The checkpoint runs before the switch, so nothing committed is lost."""
        db = self.home / "state.db"
        subprocess.run([sys.executable, "-c", _CRASHED_WRITER, str(db)], check=True, timeout=60)
        self.assertTrue(db.with_name("state.db-wal").exists(), "the writer checkpointed on exit; the test proves nothing")
        self.assertGreater(db.with_name("state.db-wal").stat().st_size, 0)
        self.pin()

        proc = self.run_script()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_journal_mode(db), "delete")
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM t").fetchone()[0], 50)
        finally:
            conn.close()
        self.assertFalse(db.with_name("state.db-wal").exists())

    def test_a_database_already_in_delete_is_not_touched(self):
        db = _make_db(self.home / "state.db", mode="DELETE")
        before = db.stat().st_mtime_ns
        self.pin()

        proc = self.run_script()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(db.stat().st_mtime_ns, before)
        self.assertIn("no governed database", proc.stderr)

    def test_a_database_another_connection_holds_is_left_in_wal(self):
        """Leaving WAL needs exclusive access; a reader mid-transaction must block it."""
        db = _make_db(self.home / "state.db")
        self.pin()
        holder = sqlite3.connect(db)
        holder.execute("BEGIN")
        holder.execute("SELECT * FROM t").fetchall()
        try:
            proc = self.run_script()
        finally:
            holder.rollback()
            holder.close()

        self.assertEqual(proc.returncode, 0, "a skipped database must not end start-up")
        self.assertIn("WARN", proc.stderr)
        self.assertIn("state.db", proc.stderr)
        self.assertEqual(_header(db), (2, 2), "the locked database was flipped under its reader")

    def test_a_locked_database_is_retried_by_a_later_run(self):
        db = _make_db(self.home / "state.db")
        self.pin()
        holder = sqlite3.connect(db)
        holder.execute("BEGIN")
        holder.execute("SELECT * FROM t").fetchall()
        try:
            self.run_script()
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(_header(db), (2, 2))

        proc = self.run_script()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_header(db), (1, 1))

    def test_an_unrelated_sqlite_error_leaves_the_file_and_continues(self):
        """One bad database must not stop the others from converting."""
        bad = self.home / "state.db"
        bad.write_bytes(b"SQLite format 3\x00" + bytes([0] * 2) + bytes([2, 2]) + bytes([0] * 80) + b"garbage")
        good = _make_db(self.home / "kanban.db")
        self.pin()

        proc = self.run_script()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WARN", proc.stderr)
        self.assertEqual(_header(good), (1, 1))


class GateTest(_Case):
    def test_no_pin_means_no_conversion(self):
        db = _make_db(self.home / "state.db")
        for text in (_WAL_CONFIG, "model:\n  default: x\n", "database: delete\n", "database:\n  journal_mode: 7\n", ""):
            with self.subTest(config=text):
                self.pin(text)
                proc = self.run_script()
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(_header(db), (2, 2), f"converted under config {text!r}")

    def test_a_missing_managed_config_means_no_conversion(self):
        db = _make_db(self.home / "state.db")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_header(db), (2, 2))

    def test_an_unparseable_managed_config_means_no_conversion(self):
        db = _make_db(self.home / "state.db")
        self.pin("database: [\n")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WARN", proc.stderr)
        self.assertEqual(_header(db), (2, 2))

    def test_the_pin_is_read_case_insensitively(self):
        db = _make_db(self.home / "state.db")
        self.pin("database:\n  journal_mode: DELETE\n")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_header(db), (1, 1))

    def test_no_yaml_parser_means_no_conversion(self):
        with mock.patch.dict(sys.modules, {"yaml": None}):
            self.pin()
            self.assertIsNone(sjm.configured_journal_mode(self.config))


class DiscoveryTest(_Case):
    def test_the_governed_files_are_found_at_every_home(self):
        expected = [_make_db(self.home / "kanban.db"), _make_db(self.home / "kanban" / "boards" / "ops" / "kanban.db")]
        for home in (self.home, self.home / "profiles" / "alpha", self.home / "profiles" / "beta"):
            expected.extend(_make_db(home / relative) for relative in sjm.HOME_DATABASES)
        self.assertEqual(set(sjm.governed_databases(self.home)), set(expected))
        self.assertEqual(len(expected), 2 + 3 * len(sjm.HOME_DATABASES))

    def test_every_hermes_opener_of_the_pin_is_governed(self):
        """The pin is read only by apply_wal_with_fallback; these are its callers at the base.

        A file the pin reaches but the conversion skips stays in WAL on the gofer mount
        for as long as it predates the pin, which is the gap #610 was filed for.
        """
        self.assertEqual(
            set(sjm.HOME_DATABASES),
            {
                "state.db",
                "cron/executions.db",
                "cron/notepad.db",
                "projects.db",
                "verification_evidence.db",
                "response_store.db",
                "memory_store.db",
                "gateway/discord_message_recovery.db",
            },
        )

    def test_other_databases_on_the_volume_are_not_governed(self):
        """Files whose opener sets its own journal mode are their owners', not this script's."""
        others = [
            _make_db(self.home / "session_kv.db"),
            _make_db(self.home / "otel" / "live.db"),
            _make_db(self.home / "notepad.db"),
            _make_db(self.home / "profiles" / "alpha" / "kanban.db"),
            _make_db(self.home / "kanban" / "boards" / "ops" / "state.db"),
            _make_db(self.home / "profiles" / "alpha" / "nested" / "state.db"),
        ]
        self.pin()
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for path in others:
            self.assertEqual(_header(path), (2, 2), f"{path} is not governed and was converted")

    def test_a_missing_or_short_file_is_not_a_wal_database(self):
        empty = self.home / "state.db"
        empty.write_bytes(b"")
        self.assertIsNone(sjm.header_journal_mode(empty))
        self.assertIsNone(sjm.header_journal_mode(self.home / "absent.db"))

    def test_a_directory_named_like_a_database_is_skipped(self):
        (self.home / "state.db").mkdir()
        self.assertEqual(sjm.governed_databases(self.home), [])


class CheckTest(_Case):
    def test_check_says_a_conversion_is_owed_while_a_governed_header_reads_wal(self):
        _make_db(self.home / "profiles" / "alpha" / "state.db")
        self.pin()
        proc = self.run_script("--check")
        self.assertEqual(proc.returncode, sjm.EXIT_WAL_REMAINS)
        self.assertIn("still reads WAL", proc.stderr)

    def test_check_is_satisfied_once_every_governed_database_left_wal(self):
        _make_db(self.home / "state.db")
        self.pin()
        self.run_script()
        proc = self.run_script("--check")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_check_is_satisfied_when_nothing_is_pinned(self):
        """No pin, no conversion coming: a waiter must not sit out its whole budget."""
        _make_db(self.home / "state.db")
        for text in (None, _WAL_CONFIG):
            with self.subTest(config=text):
                if text is None:
                    self.config.unlink(missing_ok=True)
                else:
                    self.pin(text)
                proc = self.run_script("--check")
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_check_ignores_databases_that_are_not_governed(self):
        _make_db(self.home / "notepad.db")
        self.pin()
        proc = self.run_script("--check")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_check_never_writes(self):
        db = _make_db(self.home / "state.db")
        before = db.stat().st_mtime_ns
        self.pin()
        self.run_script("--check")
        self.assertEqual(db.stat().st_mtime_ns, before)
        self.assertEqual(_header(db), (2, 2))


class ContractTest(unittest.TestCase):
    def test_the_layout_matches_hermes(self):
        """kanban_db.py: default board at <root>/kanban.db, others under kanban/boards/<slug>/."""
        self.assertEqual(sjm.KANBAN_DB_NAME, "kanban.db")
        self.assertNotIn(sjm.KANBAN_DB_NAME, sjm.HOME_DATABASES)
        self.assertEqual(str(sjm.BOARDS_DIR), os.path.join("kanban", "boards"))
        self.assertEqual(sjm.PROFILES_DIR, "profiles")

    def test_the_pin_is_the_key_hermes_reads(self):
        """hermes_state.resolve_journal_mode reads database.journal_mode."""
        self.assertEqual((sjm.CONFIG_SECTION, sjm.CONFIG_KEY), ("database", "journal_mode"))
        self.assertEqual(sjm.JOURNAL_MODE_DELETE, "delete")


if __name__ == "__main__":
    unittest.main()
