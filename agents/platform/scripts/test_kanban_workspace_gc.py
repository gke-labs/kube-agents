#!/usr/bin/env python3
"""Tests for the kanban scratch-workspace reconciler.

Two things this job can get wrong are worse than the leak it exists to fix:
deleting a workspace a live card is still using, and deleting something that is
not a workspace at all. Most of what follows is about those, and about the
narrowing step that keeps the sandbox's own listing from ever widening the
delete set.

Run:  python3 agents/platform/scripts/test_kanban_workspace_gc.py
"""

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import kanban_workspace_gc as gc
import sandbox_exec

SCHEMA = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  status TEXT,
  workspace_kind TEXT,
  workspace_path TEXT
);
CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
"""


class FakeBoard:
    """A board on disk, with the two paths the sweep resolves through it."""

    def __init__(self, root: Path, slug: str = "default"):
        self.slug = slug
        self.db = root / f"{slug}.db"
        self.workspaces = root / slug / "workspaces"
        self.workspaces.mkdir(parents=True)
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def add(self, task_id, status, kind="scratch", path=None, make_dir=True):
        target = self.workspaces / task_id if path is None else Path(path)
        if make_dir:
            target.mkdir(parents=True, exist_ok=True)
            (target / "note.txt").write_text("output", encoding="utf-8")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO tasks (id, status, workspace_kind, workspace_path) "
            "VALUES (?, ?, ?, ?)",
            (task_id, status, kind, str(target)),
        )
        conn.commit()
        conn.close()
        return target

    def link(self, parent_id, child_id):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (parent_id, child_id),
        )
        conn.commit()
        conn.close()

    def as_module(self):
        """A stand-in for `hermes_cli.kanban_db` covering the three calls used."""
        board, slug = self, self.slug

        class Module:
            @staticmethod
            def list_boards():
                return [{"slug": slug}]

            @staticmethod
            def kanban_db_path(_slug=None):
                return board.db

            @staticmethod
            def workspaces_root(_slug=None):
                return board.workspaces

        return Module


class RemovableTest(unittest.TestCase):
    """What the DB query alone decides, before either filesystem is touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.board = FakeBoard(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def removable(self):
        return {p.name for p in gc._removable(self.board.db, self.board.workspaces)}

    def test_terminal_scratch_tasks_are_removable(self):
        for status in ("done", "archived", "failed", "cancelled"):
            self.board.add(f"t_{status[:6].encode().hex()[:8]}", status)
        self.assertEqual(len(self.removable()), 4)

    def test_live_statuses_are_left_alone(self):
        for index, status in enumerate(("todo", "ready", "running", "blocked")):
            self.board.add(f"t_1000000{index}", status)
        self.assertEqual(self.removable(), set())

    def test_blocked_is_not_terminal(self):
        """The status the live install had nine of. A blocked card resumes."""
        self.board.add("t_aaaaaaaa", "blocked")
        self.assertEqual(self.removable(), set())

    def test_worktree_and_dir_workspaces_are_never_removed(self):
        self.board.add("t_bbbbbbbb", "done", kind="worktree")
        self.board.add("t_cccccccc", "done", kind="dir")
        self.assertEqual(self.removable(), set())

    def test_deferred_while_a_child_is_live(self):
        self.board.add("t_dddddddd", "done")
        self.board.add("t_eeeeeeee", "running")
        self.board.link("t_dddddddd", "t_eeeeeeee")
        self.assertEqual(self.removable(), set())

    def test_removable_once_every_child_is_terminal(self):
        self.board.add("t_dddddddd", "done")
        self.board.add("t_eeeeeeee", "failed")
        self.board.link("t_dddddddd", "t_eeeeeeee")
        self.assertEqual(self.removable(), {"t_dddddddd", "t_eeeeeeee"})

    def test_a_path_outside_the_workspaces_root_is_refused(self):
        """The #28818 shape: `scratch` pointing at a real source tree."""
        outside = Path(self.tmp.name) / "src"
        outside.mkdir()
        self.board.add("t_ffffffff", "done", path=outside)
        self.assertEqual(self.removable(), set())
        self.assertTrue(outside.is_dir())

    def test_the_workspaces_root_itself_is_refused(self):
        self.board.add("t_12121212", "done", path=self.board.workspaces)
        self.assertEqual(self.removable(), set())
        self.assertTrue(self.board.workspaces.is_dir())

    def test_a_nested_path_is_refused(self):
        nested = self.board.workspaces / "t_13131313" / "inner"
        self.board.add("t_13131313", "done", path=nested)
        self.assertEqual(self.removable(), set())

    def test_a_name_that_is_not_a_task_id_is_refused(self):
        target = self.board.workspaces / "scratch-notes"
        self.board.add("t_14141414", "done", path=target)
        self.assertEqual(self.removable(), set())
        self.assertTrue(target.is_dir())

    def test_a_null_workspace_path_is_skipped(self):
        conn = sqlite3.connect(self.board.db)
        conn.execute(
            "INSERT INTO tasks (id, status, workspace_kind, workspace_path) "
            "VALUES ('t_15151515', 'done', 'scratch', NULL)"
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.removable(), set())

    def test_the_db_is_opened_read_only(self):
        """The dispatcher writes this file continuously; housekeeping does not."""
        self.board.add("t_16161616", "done")
        with patch.object(gc.sqlite3, "connect", wraps=gc.sqlite3.connect) as spy:
            gc._removable(self.board.db, self.board.workspaces)
        self.assertIn("mode=ro", spy.call_args[0][0])


class LocalRemovalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.board = FakeBoard(Path(self.tmp.name))
        self.sweep = gc.Sweep()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_tree_goes(self):
        target = self.board.add("t_17171717", "done")
        gc._remove_local(target, self.sweep)
        self.assertFalse(target.exists())
        self.assertEqual(self.sweep.local_removed, ["t_17171717"])

    def test_a_symlink_is_unlinked_and_not_followed(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep.txt").write_text("keep", encoding="utf-8")
        link = self.board.workspaces / "t_18181818"
        link.symlink_to(elsewhere)
        gc._remove_local(link, self.sweep)
        self.assertFalse(link.exists())
        self.assertTrue((elsewhere / "keep.txt").is_file())

    def test_an_absent_path_is_not_reported_as_removed(self):
        gc._remove_local(self.board.workspaces / "t_19191919", self.sweep)
        self.assertEqual(self.sweep.local_removed, [])
        self.assertEqual(self.sweep.warnings, [])


class SandboxTest(unittest.TestCase):
    """The remote half: which login it uses, and what it is allowed to delete."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.board = FakeBoard(Path(self.tmp.name))
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, listing="", returncode=0):
        def runner(argv, **kwargs):
            self.calls.append((argv, kwargs))
            out = listing if argv[0] == gc.REMOTE_LS else ""
            return subprocess.CompletedProcess(argv, returncode, out, "")

        return runner

    def sweep(self, listing):
        with patch.object(sandbox_exec, "sandbox_enabled", return_value=True), \
             patch.object(sandbox_exec, "run", side_effect=self.fake_run(listing)):
            return gc.sweep_boards(self.board.as_module())

    def rm_calls(self):
        return [argv for argv, _ in self.calls if argv[0] == gc.REMOTE_RM]

    def test_it_connects_as_the_model_account(self):
        self.board.add("t_20202020", "done")
        self.sweep("t_20202020\n")
        self.assertTrue(self.calls)
        for _argv, kwargs in self.calls:
            self.assertEqual(kwargs["principal"], sandbox_exec.TERMINAL_PRINCIPAL)

    def test_absolute_paths_for_the_remote_binaries(self):
        """A ~/.bashrc cannot shadow either: no slash in a bash function name."""
        self.board.add("t_21212121", "done")
        self.sweep("t_21212121\n")
        for argv, _kwargs in self.calls:
            self.assertTrue(argv[0].startswith("/"), argv)

    def test_only_the_intersection_is_removed(self):
        self.board.add("t_22222222", "done")
        self.board.add("t_23232323", "done")
        self.sweep("t_22222222\n")
        removed = self.rm_calls()[0]
        self.assertIn(str(self.board.workspaces / "t_22222222"), removed)
        self.assertNotIn(str(self.board.workspaces / "t_23232323"), removed)

    def test_a_forged_listing_cannot_widen_the_delete_set(self):
        """The listing comes from the account the model owns. It only narrows."""
        self.board.add("t_24242424", "done")
        self.board.add("t_25252525", "running")
        self.sweep("t_24242424\nt_25252525\n/etc/passwd\n../../opt/data\n")
        removed = self.rm_calls()[0]
        targets = removed[removed.index("--") + 1:]
        self.assertEqual(targets, [str(self.board.workspaces / "t_24242424")])

    def test_no_remote_call_when_nothing_is_removable(self):
        self.board.add("t_26262626", "running")
        self.sweep("t_26262626\n")
        self.assertEqual(self.calls, [])

    def test_no_rm_when_the_sandbox_holds_none_of_them(self):
        self.board.add("t_27272727", "done")
        self.sweep("")
        self.assertEqual(self.rm_calls(), [])

    def test_one_call_for_the_whole_board(self):
        for index in range(5):
            self.board.add(f"t_3000000{index}", "done")
        self.sweep("".join(f"t_3000000{i}\n" for i in range(5)))
        self.assertEqual(len(self.rm_calls()), 1)

    def test_the_local_half_still_runs_with_the_sandbox_off(self):
        target = self.board.add("t_28282828", "done")
        with patch.object(sandbox_exec, "sandbox_enabled", return_value=False), \
             patch.object(sandbox_exec, "run") as remote:
            sweep = gc.sweep_boards(self.board.as_module())
        remote.assert_not_called()
        self.assertFalse(target.exists())
        self.assertEqual(sweep.local_removed, ["t_28282828"])

    def test_an_unreachable_sandbox_is_reported_not_swallowed(self):
        self.board.add("t_29292929", "done")

        def unreachable(argv, **kwargs):
            raise sandbox_exec.SandboxUnavailable("connection refused")

        with patch.object(sandbox_exec, "sandbox_enabled", return_value=True), \
             patch.object(sandbox_exec, "run", side_effect=unreachable):
            sweep = gc.sweep_boards(self.board.as_module())
        self.assertTrue(sweep.warnings)
        self.assertIn("connection refused", sweep.warnings[0])

    def test_a_missing_remote_root_is_quiet(self):
        """A board no card has run on yet is not a fault worth delivering."""
        self.board.add("t_31313131", "done")
        with patch.object(sandbox_exec, "sandbox_enabled", return_value=True), \
             patch.object(sandbox_exec, "run",
                          side_effect=self.fake_run("", returncode=2)):
            sweep = gc.sweep_boards(self.board.as_module())
        self.assertEqual(sweep.warnings, [])
        self.assertEqual(self.rm_calls(), [])

    def test_a_failing_remote_rm_is_reported(self):
        self.board.add("t_32323232", "done")

        def rm_fails(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if argv[0] == gc.REMOTE_LS:
                return subprocess.CompletedProcess(argv, 0, "t_32323232\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "Permission denied")

        with patch.object(sandbox_exec, "sandbox_enabled", return_value=True), \
             patch.object(sandbox_exec, "run", side_effect=rm_fails):
            sweep = gc.sweep_boards(self.board.as_module())
        self.assertTrue(sweep.warnings)
        self.assertIn("Permission denied", sweep.warnings[0])
        self.assertEqual(sweep.remote_removed, [])


class PrincipalTest(unittest.TestCase):
    """`sandbox_exec` keeps the default it had; the override is bounded."""

    def test_the_default_login_is_unchanged(self):
        self.assertEqual(sandbox_exec.SANDBOX_PRINCIPAL, "hermes")

    def test_an_arbitrary_login_is_refused(self):
        config = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        config.write("terminal:\n  backend: ssh\n  ssh_host: sandbox\n")
        config.close()
        with self.assertRaises(ValueError):
            sandbox_exec.ssh_argv(["true"], path=config.name, principal="root")

    def test_both_sandbox_logins_are_accepted(self):
        config = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        config.write("terminal:\n  backend: ssh\n  ssh_host: sandbox\n")
        config.close()
        for principal in (sandbox_exec.SANDBOX_PRINCIPAL,
                          sandbox_exec.TERMINAL_PRINCIPAL):
            argv = sandbox_exec.ssh_argv(["true"], path=config.name,
                                         principal=principal)
            self.assertIn(f"{principal}@sandbox", argv)


def _find_output(*rows):
    """`FIND_FORMAT` output for (name, mtime, size, type) rows."""
    return "".join(f"{n}\t{m}\t{sz}\t{k}\0" for n, m, sz, k in rows)


class FakeSandbox:
    """`sandbox_exec.run` over a dict of directory -> listing rows."""

    def __init__(self, tree, fail=None):
        self.tree = tree
        self.fail = fail or {}
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        path = argv[1]
        if path in self.fail:
            raise self.fail[path]
        if path not in self.tree:
            return subprocess.CompletedProcess(argv, 1, "", f"find: '{path}': No such file or directory")
        return subprocess.CompletedProcess(argv, 0, _find_output(*self.tree[path]), "")


class HomeInventoryTest(unittest.TestCase):
    """The #2989 inventory: report what is in a profile home, never touch it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "agent"
        self.profiles = self.home / "profiles"
        (self.profiles / "platform").mkdir(parents=True)
        (self.profiles / "cluster-a").mkdir()
        (self.profiles / ".hidden").mkdir()
        self.remote = Path("/sandbox-root")
        rp = self.remote / "profiles"
        self.expected_rows = [(n, "1.0", "4096", "d") for n in sorted(gc.EXPECTED_HOME_ENTRIES)]
        self.tree = {
            str(rp): [("platform", "1.0", "1", "d"), ("cluster-a", "1.0", "1", "d"),
                      ("cluster-gone", "1760000000.0", "1", "d"), (".hidden", "1.0", "1", "d"),
                      ("stray-file", "1.0", "1", "f")],
            str(rp / "platform"): self.expected_rows + [
                ("dummy-gitops-state", "1760088420.5", "88", "f"),
                ("dummy-git-repo", "1760080140.0", "4096", "d")],
            str(rp / "cluster-a"): self.expected_rows,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_inventory(self, fake):
        with patch.object(sandbox_exec, "run", side_effect=fake):
            return gc.take_inventory(self.profiles, self.remote)

    def test_reports_unexpected_entries_and_stale_homes_only(self):
        inv = self.run_inventory(FakeSandbox(self.tree))
        rp = self.remote / "profiles"
        self.assertEqual(
            {h: [r[0] for r in rows] for h, rows in inv.unexpected.items()},
            {str(rp / "platform"): ["dummy-git-repo", "dummy-gitops-state"]},
        )
        # A home the agent pod has no profile for; files and dot-dirs are not homes.
        self.assertEqual([r[0] for r in inv.stale_homes], ["cluster-gone"])
        self.assertEqual(inv.problems, [])

    def test_lists_in_the_sandbox_root_read_only_as_the_default_principal(self):
        fake = FakeSandbox(self.tree)
        self.run_inventory(fake)
        listed = [argv[1] for argv, _ in fake.calls]
        rp = self.remote / "profiles"
        self.assertEqual(listed, [str(rp), str(rp / "cluster-a"), str(rp / "platform")])
        for argv, kwargs in fake.calls:
            self.assertEqual(argv[0], gc.REMOTE_FIND)
            self.assertNotIn("principal", kwargs)
            # subprocess refuses an argv with a NUL in it; find expands its
            # own escapes, so they travel as backslash text.
            self.assertFalse(any("\0" in a or "\t" in a for a in argv), argv)
            self.assertIn(r"\0", argv[-1])

    def test_a_name_cannot_forge_a_row(self):
        # A newline or a tab inside a name stays inside its NUL-terminated
        # record and is reported whole; neither becomes an expected entry.
        out = _find_output(("evil\nskills", "1.0", "1", "f"), ("evil\tskills", "1.0", "1", "f"))
        self.assertEqual([r[0] for r in gc._parse_listing(out)], ["evil\tskills", "evil\nskills"])

    def test_malformed_records_are_skipped(self):
        out = "garbage\0" + _find_output(("x", "notatime", "1", "f"), ("a/b", "1.0", "1", "f"), ("kept", "1.0", "1", "f"))
        self.assertEqual([r[0] for r in gc._parse_listing(out)], ["kept"])

    def test_only_the_mirrors_displaced_names_are_expected(self):
        self.assertTrue(gc._is_expected("scratch.displaced-20261010T000000"))
        self.assertFalse(gc._is_expected("notes.displaced-20261010T000000"))
        self.assertFalse(gc._is_expected(".displaced"))

    def test_skeleton_matches_the_mirror(self):
        # The mirror creates these in every home; if it grows one, the
        # inventory must not start reporting it as residue.
        shared = Path(__file__).resolve().parents[3] / "deploy" / "shared"
        sys.path.insert(0, str(shared))
        try:
            import sandbox_mirror
        finally:
            sys.path.remove(str(shared))
        self.assertLessEqual(set(sandbox_mirror.SKELETON_DIRS), gc.EXPECTED_HOME_ENTRIES)
        self.assertIn(sandbox_mirror.CLUSTER_IDENTITY_FILE, gc.EXPECTED_HOME_ENTRIES)
        self.assertEqual(sandbox_mirror.DISPLACED_SUFFIX + "-", gc.DISPLACED_MARK)

    def test_an_unreadable_home_is_a_problem_not_a_stop(self):
        rp = self.remote / "profiles"
        fake = FakeSandbox(self.tree, fail={str(rp / "cluster-a"): UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")})
        inv = self.run_inventory(fake)
        self.assertIn(str(rp / "platform"), inv.unexpected)
        self.assertEqual(len(inv.problems), 1)

    def test_an_unreachable_or_hung_sandbox_stops_after_one_try(self):
        for exc in (sandbox_exec.SandboxUnavailable("down"), subprocess.TimeoutExpired("ssh", 60)):
            fake = FakeSandbox(self.tree, fail={str(self.remote / "profiles"): exc})
            inv = self.run_inventory(fake)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(len(inv.problems), 1)
            self.assertEqual(inv.unexpected, {})

    def test_report_is_capped(self):
        inv = gc.Inventory()
        inv.unexpected["/h"] = [(f"probe_{i:03}.py", 1760088420.0, 10, "f") for i in range(gc.INVENTORY_LIMIT + 5)]
        lines = gc._format_inventory(inv)
        self.assertIn("not moved", lines[0])
        self.assertEqual(len(lines), gc.INVENTORY_LIMIT + 2)
        self.assertIn("and 5 more", lines[-1])

    def run_main(self, fake):
        """`main()` end to end, with no boards; returns (stdout, log file text)."""
        import io
        hermes_cli = type(sys)("hermes_cli")
        hermes_cli.kanban_db = type("K", (), {"list_boards": staticmethod(lambda: [])})
        with patch.dict(sys.modules, {"hermes_cli": hermes_cli}), \
             patch.dict("os.environ", {"PLATFORM_AGENT_HOME": str(self.home)}), \
             patch.object(sandbox_exec, "sandbox_enabled", return_value=True), \
             patch.object(sandbox_exec, "_load_terminal_config", return_value={"workspace_root": str(self.remote)}), \
             patch.object(sandbox_exec, "run", side_effect=fake), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(gc.main(), 0)
        log = self.home / gc.LOGS_DIR / gc.LOG_FILE_NAME
        return out.getvalue(), log.read_text(encoding="utf-8") if log.exists() else ""

    def test_main_logs_to_the_file_and_never_to_chat(self):
        out, log = self.run_main(FakeSandbox(self.tree))
        self.assertEqual(out, "")
        self.assertIn("dummy-gitops-state", log)
        self.assertIn("'cluster-gone' has no profile", log)

    def test_main_failure_paths_stay_out_of_chat(self):
        rp = str(self.remote / "profiles")
        for exc in (sandbox_exec.SandboxUnavailable("down"), subprocess.TimeoutExpired("ssh", 60),
                    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
            out, log = self.run_main(FakeSandbox(self.tree, fail={rp: exc}))
            self.assertEqual(out, "", exc)
            self.assertIn(gc.LOG_PREFIX, log)

    def test_the_agent_home_root_is_not_the_sandbox_root(self):
        # agentHome moves the agent pod's root only: listings must use the
        # sandbox's published workspace root.
        fake = FakeSandbox(self.tree)
        self.run_main(fake)
        self.assertTrue(all(argv[1].startswith(str(self.remote)) for argv, _ in fake.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
