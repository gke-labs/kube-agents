#!/usr/bin/env python3
"""Tests for the kanban scratch-workspace reconciler.

Two things this job can get wrong are worse than the leak it exists to fix:
deleting a workspace a live card is still using, and deleting something that is
not a workspace at all. Most of what follows is about those, and about the
narrowing step that keeps the sandbox's own listing from ever widening the
delete set.

Run:  python3 agents/platform/scripts/test_kanban_workspace_gc.py
"""

import os
import shutil
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
        def rows(entries):
            return [(n, "1.0", "4096", k) for n, k in sorted(entries.items())]
        self.tree = {
            str(rp): [("platform", "1.0", "1", "d"), ("cluster-a", "1.0", "1", "d"),
                      ("cluster-gone", "1760000000.0", "1", "d"), (".hidden", "1.0", "1", "d"),
                      ("stray-file", "1.0", "1", "f")],
            str(rp / "platform"): rows(gc.PLATFORM_HOME_ENTRIES) + [
                ("dummy-gitops-state", "1760088420.5", "88", "f"),
                ("dummy-git-repo", "1760080140.0", "4096", "d")],
            str(rp / "cluster-a"): rows(gc.CLUSTER_HOME_ENTRIES),
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

    def test_entries_are_expected_per_kind_of_home_and_type(self):
        rp = self.remote / "profiles"
        self.tree[str(rp / "platform")] += [("USER.md", "1.0", "1", "f")]
        self.tree[str(rp / "cluster-a")] += [("kanban.db", "1.0", "0", "f"), ("skills", "1.0", "1", "d")]
        self.tree[str(rp / "platform")] = [
            (n, m, s, "f" if n == "state.db" else k) for n, m, s, k in self.tree[str(rp / "platform")]]
        inv = self.run_inventory(FakeSandbox(self.tree))
        got = {h.rsplit("/", 1)[1]: sorted(r[0] for r in rows) for h, rows in inv.unexpected.items()}
        self.assertEqual(got["platform"], ["USER.md", "dummy-git-repo", "dummy-gitops-state", "state.db"])
        self.assertEqual(got["cluster-a"], ["kanban.db", "skills"])
        self.assertEqual(gc.expected_entries("chat"), gc.SKELETON_ENTRIES)

    def test_a_non_utf8_name_is_reported_and_the_rest_survives(self):
        # The listing decodes with surrogateescape, so one bad byte costs one
        # name, not the home's whole inventory.
        fake = FakeSandbox(self.tree)
        self.run_inventory(fake)
        for _, kwargs in fake.calls:
            self.assertEqual(kwargs.get("errors"), "surrogateescape")
        raw = b"x\xff\t1.0\t1\tf\0dummy-gitops-state\t1.0\t88\tf\0".decode("utf-8", "surrogateescape")
        names = [r[0] for r in gc._parse_listing(raw)]
        self.assertEqual(names, ["dummy-gitops-state", "x\udcff"])
        inv = gc.Inventory()
        inv.unexpected["/h"] = [(names[1], 1.0, 1, "f")]
        gc.append_log(gc._format_inventory(inv), self.home)
        self.assertIn("x\\udcff", (self.home / gc.LOGS_DIR / gc.LOG_FILE_NAME).read_text(encoding="utf-8"))

    def test_every_log_entry_is_one_line(self):
        gc.append_log(["kanban_workspace_gc: inventory skipped: Warning: added host\nclient_loop: send disconnect"], self.home)
        text = (self.home / gc.LOGS_DIR / gc.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("added host client_loop", text)

    def test_a_name_cannot_forge_a_row(self):
        # A newline or a tab inside a name stays inside its NUL-terminated
        # record and is reported whole; neither becomes an expected entry.
        out = _find_output(("evil\nskills", "1.0", "1", "f"), ("evil\tskills", "1.0", "1", "f"))
        self.assertEqual([r[0] for r in gc._parse_listing(out)], ["evil\tskills", "evil\nskills"])

    def test_malformed_records_are_skipped(self):
        out = "garbage\0" + _find_output(("x", "notatime", "1", "f"), ("a/b", "1.0", "1", "f"), ("kept", "1.0", "1", "f"))
        self.assertEqual([r[0] for r in gc._parse_listing(out)], ["kept"])

    def test_only_the_mirrors_displaced_names_are_expected(self):
        def expected(name):
            return gc._is_expected((name, 1.0, 1, "f"), gc.PLATFORM_HOME_ENTRIES)
        self.assertTrue(expected("scratch.displaced-20261010T000000"))
        self.assertFalse(expected("notes.displaced-20261010T000000"))
        self.assertFalse(expected(".displaced"))
        self.assertFalse(expected("scratch.displaced-notes.sh"))
        self.assertFalse(expected("scratch.displaced-20261010T000000\n"))
        # Only the skeleton is ever displaced inside a home.
        self.assertFalse(expected("skills.displaced-20261010T000000"))
        self.assertFalse(gc._is_expected(("kubeconfig.yaml.displaced-20261010T000000", 1.0, 1, "f"),
                                         gc.CLUSTER_HOME_ENTRIES))

    def test_skeleton_matches_the_mirror(self):
        # The mirror creates these in every home; if it grows one, the
        # inventory must not start reporting it as residue.
        shared = Path(__file__).resolve().parents[3] / "deploy" / "shared"
        sys.path.insert(0, str(shared))
        try:
            import sandbox_mirror
        finally:
            sys.path.remove(str(shared))
        self.assertEqual(set(sandbox_mirror.SKELETON_DIRS), set(gc.SKELETON_ENTRIES))
        self.assertLessEqual(sandbox_mirror.IMAGE_OWNED, set(gc.PLATFORM_HOME_ENTRIES))
        self.assertIn(sandbox_mirror.CLUSTER_IDENTITY_FILE, gc.CLUSTER_HOME_ENTRIES)
        self.assertTrue(sandbox_mirror.CLUSTER_PROFILE_PREFIX.endswith(gc.CLUSTER_PROFILE_PREFIX))
        self.assertEqual(sandbox_mirror.DISPLACED_SUFFIX + "-", gc.DISPLACED_MARK)

    def test_databases_match_the_entrypoint(self):
        import re
        entrypoint = (Path(__file__).resolve().parents[3] / "deploy" / "sandbox" / "entrypoint.sh").read_text()
        default = re.search(r'AGENT_POD_DATABASES="\$\{AGENT_POD_DATABASES:-([^}]*)\}"', entrypoint)
        self.assertIsNotNone(default)
        self.assertEqual({n for n in default.group(1).split()},
                         {n for n, k in gc.PLATFORM_HOME_ENTRIES.items() if n.endswith(".db")})
        roots = re.search(r'SANDBOX_HOME_ROOTS="\$\{SANDBOX_HOME_ROOTS:-([^}]*)\}"', entrypoint)
        self.assertIn(f"profiles/{gc.PLATFORM_PROFILE}", roots.group(1).split())

    def test_a_home_find_cannot_list_is_logged(self):
        # A profile on the agent pod whose sandbox home is missing: find exits
        # non-zero, and that is a logged problem, not a clean home.
        (self.profiles / "cluster-b").mkdir()
        inv = self.run_inventory(FakeSandbox(self.tree))
        self.assertEqual(len(inv.problems), 1)
        self.assertIn("cluster-b in the sandbox exited 1", inv.problems[0])
        self.assertIn(str(self.remote / "profiles" / "platform"), inv.unexpected)

    def test_a_live_home_that_is_not_a_directory_is_flagged(self):
        rp = str(self.remote / "profiles")
        self.tree[rp] = [r if r[0] != "cluster-a" else ("cluster-a", "1.0", "4", "l") for r in self.tree[rp]]
        inv = self.run_inventory(FakeSandbox(self.tree))
        self.assertEqual(len(inv.problems), 1)
        self.assertIn("cluster-a is type 'l'", inv.problems[0])

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

    def run_main(self, fake, sweep=None):
        """`main()` end to end; returns (stdout, log file text) and clears the log."""
        import io
        hermes_cli = type(sys)("hermes_cli")
        hermes_cli.kanban_db = type("K", (), {"list_boards": staticmethod(lambda: [])})
        with patch.dict(sys.modules, {"hermes_cli": hermes_cli}), \
             patch.object(gc, "sweep_boards", return_value=sweep or gc.Sweep()), \
             patch.dict("os.environ", {"PLATFORM_AGENT_HOME": str(self.home)}), \
             patch.object(sandbox_exec, "sandbox_enabled", return_value=True), \
             patch.object(sandbox_exec, "_load_terminal_config", return_value={"workspace_root": str(self.remote)}), \
             patch.object(sandbox_exec, "run", side_effect=fake), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(gc.main(), 0)
        log = self.home / gc.LOGS_DIR / gc.LOG_FILE_NAME
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        log.unlink(missing_ok=True)
        return out.getvalue(), text

    def test_main_logs_the_sweeps_removals(self):
        sweep = gc.Sweep()
        sweep.local_removed = ["t_1", "t_2"]
        sweep.remote_removed = ["t_1"]
        out, log = self.run_main(FakeSandbox(self.tree), sweep)
        self.assertEqual(out, "")
        self.assertIn("removed 2 on the agent pod, 1 in the sandbox", log)

    def test_a_broken_config_skips_the_inventory_not_the_job(self):
        import io
        hermes_cli = type(sys)("hermes_cli")
        hermes_cli.kanban_db = type("K", (), {"list_boards": staticmethod(lambda: [])})
        calls = iter([False, sandbox_exec.SandboxMisconfigured("gone")])

        def enabled():
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.dict(sys.modules, {"hermes_cli": hermes_cli}), \
             patch.dict("os.environ", {"PLATFORM_AGENT_HOME": str(self.home)}), \
             patch.object(sandbox_exec, "sandbox_enabled", side_effect=enabled), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(gc.main(), 0)
        self.assertEqual(out.getvalue(), "")
        log = (self.home / gc.LOGS_DIR / gc.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("inventory skipped: gone", log)

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




class CronRunsTest(unittest.TestCase):
    """#2989 (a): each cron agent run's directory is pruned after CRON_RUNS_MAX_AGE_DAYS."""

    NOW = 1_800_000_000.0
    OLD = NOW - (gc.CRON_RUNS_MAX_AGE_DAYS + 1) * 86400
    NEW = NOW - 3600

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profiles = Path(self.tmp.name) / "profiles"
        self.runs = self.profiles.joinpath("platform", *gc.CRON_RUNS_PARTS)
        self.runs.mkdir(parents=True)

    def make_run(self, name, mtime):
        path = self.runs / name
        path.mkdir()
        (path / "probe.py").write_text("print(1)\n")
        os.utime(path, (mtime, mtime))
        return path

    def sweep(self, remote=None, fake=None):
        sweep = gc.Sweep()
        with patch.object(sandbox_exec, "run", side_effect=fake or (lambda *a, **k: None)):
            gc.sweep_cron_runs(self.profiles, remote, sweep, now=self.NOW)
        return sweep

    def test_old_runs_go_and_recent_ones_stay(self):
        old = self.make_run("compliance-audit_ex1", self.OLD)
        new = self.make_run("compliance-audit_ex2", self.NEW)
        sweep = self.sweep()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(sweep.cron_runs_removed, ["platform/compliance-audit_ex1"])
        self.assertEqual(sweep.local_removed, [], "cron runs are not reported as kanban workspaces")

    def test_a_symlinked_runs_root_is_not_followed(self):
        # The model owns the volume: a runs root swapped for a link to somewhere
        # it wants emptied must not be pruned through.
        target = Path(self.tmp.name) / "precious"
        target.mkdir()
        keep = target / "old-but-not-ours"
        keep.mkdir()
        os.utime(keep, (self.OLD, self.OLD))
        shutil.rmtree(self.runs)
        self.runs.symlink_to(target)
        sweep = self.sweep()
        self.assertTrue(keep.exists())
        self.assertTrue(any("symlink" in w for w in sweep.warnings), sweep.warnings)

    def test_a_symlinked_cron_dir_is_not_followed(self):
        target = Path(self.tmp.name) / "elsewhere"
        (target / "runs" / "x").mkdir(parents=True)
        os.utime(target / "runs" / "x", (self.OLD, self.OLD))
        shutil.rmtree(self.runs.parent)
        self.runs.parent.symlink_to(target)
        self.sweep()
        self.assertTrue((target / "runs" / "x").exists())

    def test_a_stray_file_in_the_runs_root_goes_too(self):
        stray = self.runs / "note.txt"
        stray.write_text("x")
        os.utime(stray, (self.OLD, self.OLD))
        self.sweep()
        self.assertFalse(stray.exists())

    def test_an_unreachable_sandbox_is_tried_once_and_reported_once(self):
        (self.profiles / "cluster-a").mkdir()
        (self.profiles / "cluster-b").mkdir()
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            raise sandbox_exec.SandboxUnavailable("no route to host")

        sweep = self.sweep(Path("/sandbox-root"), fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([w for w in sweep.warnings if "sandbox" in w]), 1, sweep.warnings)

    def test_the_nightly_job_prunes_and_logs_it(self):
        old = self.make_run("compliance-audit_ex1", self.OLD)
        import io
        home = self.profiles.parent
        hermes_cli = type(sys)("hermes_cli")
        hermes_cli.kanban_db = type("K", (), {"list_boards": staticmethod(lambda: [])})
        with patch.dict(sys.modules, {"hermes_cli": hermes_cli}), \
             patch.dict("os.environ", {"PLATFORM_AGENT_HOME": str(home)}), \
             patch.object(sandbox_exec, "sandbox_enabled", return_value=False), \
             patch.object(gc.time, "time", return_value=self.NOW), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(gc.main(), 0)
        self.assertFalse(old.exists())
        log = (home / gc.LOGS_DIR / gc.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("removed 1 cron run directories", log)

    def test_a_homes_cron_dir_is_expected_by_the_inventory(self):
        # session-command.sh creates <home>/cron/runs in the sandbox; the nightly
        # inventory must not report it as residue every night.
        self.assertEqual(gc.expected_entries("platform").get("cron"), "d")
        self.assertEqual(gc.expected_entries("cluster-a").get("cron"), "d")

    def test_the_sandbox_half_removes_old_names_only_under_a_real_root(self):
        remote = Path("/sandbox-root")
        rruns = remote.joinpath("profiles", "platform", *gc.CRON_RUNS_PARTS)
        calls = []
        kinds = {"real": "d\nd\nd\n", "linked": "d\nl\nd\n"}
        state = {"kinds": "real"}

        def fake(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "-P":
                return subprocess.CompletedProcess(argv, 0, kinds[state["kinds"]], "")
            if argv[0] == gc.REMOTE_FIND:
                return subprocess.CompletedProcess(argv, 0, _find_output(
                    ("cron-a_ex1", str(self.OLD), "4096", "d"),
                    ("cron-a_ex2", str(self.NEW), "4096", "d"),
                    ("..", str(self.OLD), "4096", "d")), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        sweep = self.sweep(remote, fake)
        rms = [a for a in calls if a[0] == gc.REMOTE_RM]
        self.assertEqual(rms, [[gc.REMOTE_RM, "-rf", "--", str(rruns / "cron-a_ex1")]])
        self.assertEqual(sweep.cron_runs_removed, ["sandbox platform/cron-a_ex1"])

        calls.clear()
        state["kinds"] = "linked"
        sweep = self.sweep(remote, fake)
        self.assertEqual([a for a in calls if a[0] == gc.REMOTE_RM], [])
        self.assertTrue(any("symlink" in w for w in sweep.warnings), sweep.warnings)


class OverridesTest(unittest.TestCase):
    """#3551: a worker's *_override copies of shipped trees are removed from every profile home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profiles = Path(self.tmp.name) / "profiles"
        self.home = self.profiles / "platform"
        (self.home / "skills" / "fleet-audit").mkdir(parents=True)

    def make_override(self, name, home=None):
        path = (home or self.home) / name
        (path / "fleet-audit" / "scripts").mkdir(parents=True)
        (path / "fleet-audit" / "scripts" / "audit_report.py").write_text("print('patched')\n")
        return path

    def sweep(self, remote=None, fake=None):
        sweep = gc.Sweep()
        with patch.object(sandbox_exec, "run", side_effect=fake or (lambda *a, **k: None)):
            gc.sweep_overrides(self.profiles, remote, sweep)
        return sweep

    def test_override_trees_go_and_shipped_trees_stay(self):
        skills = self.make_override("skills_override")
        scripts = self.make_override("scripts_override")
        underscored = self.make_override("fleet_audit_override")
        cluster = self.make_override("skills_override", self.profiles / "cluster-a")
        sweep = self.sweep()
        self.assertFalse(skills.exists())
        self.assertFalse(scripts.exists())
        self.assertFalse(underscored.exists())
        self.assertFalse(cluster.exists())
        self.assertTrue((self.home / "skills" / "fleet-audit").is_dir())
        self.assertEqual(sweep.overrides_removed,
                         ["cluster-a/skills_override", "platform/fleet_audit_override",
                          "platform/scripts_override", "platform/skills_override"])
        self.assertEqual(sweep.local_removed, [], "override trees are not reported as kanban workspaces")

    def test_only_the_suffix_at_the_top_of_a_home_counts(self):
        keep = [self.home / "override", self.home / "skills_overrides", self.home / "_override",
                self.home / "scratch" / "skills_override"]
        for path in keep:
            path.mkdir(parents=True)
        note = self.home / "notes_override"
        note.write_text("a file, not a tree")
        self.sweep()
        for path in keep:
            self.assertTrue(path.exists(), path)
        self.assertTrue(note.exists(), "a plain file is left to the inventory")

    def test_a_symlinked_override_is_unlinked_not_followed(self):
        target = Path(self.tmp.name) / "precious"
        (target / "keep").mkdir(parents=True)
        (self.home / "scripts_override").symlink_to(target)
        sweep = self.sweep()
        self.assertFalse(os.path.lexists(self.home / "scripts_override"))
        self.assertTrue((target / "keep").is_dir())
        self.assertEqual(sweep.overrides_removed, ["platform/scripts_override"])

    def test_a_symlinked_home_is_not_swept(self):
        target = Path(self.tmp.name) / "elsewhere"
        self.make_override("skills_override", target)
        (self.profiles / "cluster-b").symlink_to(target)
        sweep = self.sweep()
        self.assertTrue((target / "skills_override").exists())
        self.assertTrue(any("symlink" in w for w in sweep.warnings), sweep.warnings)

    def test_an_empty_symlinked_home_says_nothing(self):
        # Nothing to remove behind the link, so nothing for chat either.
        target = Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        (self.profiles / "cluster-b").symlink_to(target)
        sweep = self.sweep()
        self.assertEqual(sweep.warnings, [])

    def test_a_home_the_sandbox_cannot_list_is_reported(self):
        def fake(argv, **kwargs):
            if argv[0] == gc.REMOTE_FIND:
                return subprocess.CompletedProcess(argv, 1, "", "find: Permission denied")
            return subprocess.CompletedProcess(argv, 0, "", "")

        sweep = self.sweep(Path("/sandbox-root"), fake)
        self.assertTrue(any("exited 1" in w and "Permission denied" in w for w in sweep.warnings), sweep.warnings)

    def test_a_home_the_sandbox_has_not_received_says_nothing(self):
        def fake(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "/usr/bin/find: '/sandbox-root/profiles/platform': No such file or directory")

        sweep = self.sweep(Path("/sandbox-root"), fake)
        self.assertEqual(sweep.warnings, [])

    def test_a_vanished_entry_mid_listing_still_reports_and_sweeps(self):
        rhome = Path("/sandbox-root") / "profiles" / "platform"
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "-P":
                return subprocess.CompletedProcess(argv, 0, "d\nd\n", "")
            if argv[0] == gc.REMOTE_FIND:
                return subprocess.CompletedProcess(argv, 1, _find_output(("skills_override", "1", "4096", "d")),
                                                   f"find: '{rhome}/tmp': No such file or directory")
            return subprocess.CompletedProcess(argv, 0, "", "")

        sweep = self.sweep(Path("/sandbox-root"), fake)
        self.assertEqual(sweep.overrides_removed, ["sandbox platform/skills_override"])
        self.assertTrue(any("exited 1" in w for w in sweep.warnings), sweep.warnings)

    def test_thousands_of_planted_trees_cost_one_extra_rm(self):
        names = [f"x{i:05d}_override" for i in range(gc.INVENTORY_LIMIT + 500)]
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "-P":
                return subprocess.CompletedProcess(argv, 0, "d\nd\n", "")
            if argv[0] == gc.REMOTE_FIND:
                return subprocess.CompletedProcess(argv, 0, _find_output(*[(n, "1", "4096", "d") for n in names]), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        sweep = self.sweep(Path("/sandbox-root"), fake)
        self.assertEqual(len([a for a in calls if a[0] == gc.REMOTE_RM]), gc.INVENTORY_LIMIT + 1)
        self.assertEqual(len(sweep.overrides_removed), len(names))

    def test_one_unremovable_tree_does_not_hide_the_others(self):
        rhome = Path("/sandbox-root") / "profiles" / "platform"

        def fake(argv, **kwargs):
            if argv[1] == "-P":
                return subprocess.CompletedProcess(argv, 0, "d\nd\n", "")
            if argv[0] == gc.REMOTE_FIND:
                return subprocess.CompletedProcess(argv, 0, _find_output(
                    ("scripts_override", "1", "4096", "d"), ("skills_override", "1", "4096", "d")), "")
            if str(rhome / "skills_override") in argv:
                return subprocess.CompletedProcess(argv, 1, "", "rm: cannot remove: Permission denied")
            return subprocess.CompletedProcess(argv, 0, "", "")

        sweep = self.sweep(Path("/sandbox-root"), fake)
        self.assertEqual(sweep.overrides_removed, ["sandbox platform/scripts_override"])
        self.assertTrue(any("Permission denied" in w for w in sweep.warnings), sweep.warnings)

    def test_a_down_sandbox_is_tried_once_across_both_sweeps(self):
        runs = self.profiles.joinpath("platform", *gc.CRON_RUNS_PARTS)
        runs.mkdir(parents=True)
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            raise sandbox_exec.SandboxUnavailable("no route to host")

        sweep = gc.Sweep()
        with patch.object(sandbox_exec, "run", side_effect=fake):
            remote = gc.sweep_cron_runs(self.profiles, Path("/sandbox-root"), sweep)
            remote = gc.sweep_overrides(self.profiles, remote, sweep)
        self.assertIsNone(remote)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([w for w in sweep.warnings if "did not answer" in w]), 1, sweep.warnings)

    def test_the_sandbox_half_removes_overrides_only_under_a_real_home(self):
        remote = Path("/sandbox-root")
        rhome = remote / "profiles" / "platform"
        calls = []
        kinds = {"real": "d\nd\n", "linked": "d\nl\n"}
        state = {"kinds": "real"}

        def fake(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "-P":
                return subprocess.CompletedProcess(argv, 0, kinds[state["kinds"]], "")
            if argv[0] == gc.REMOTE_FIND:
                return subprocess.CompletedProcess(argv, 0, _find_output(
                    ("skills_override", "1", "4096", "d"),
                    ("scripts_override", "1", "4096", "l"),
                    ("notes_override", "1", "10", "f"),
                    ("skills", "1", "4096", "d"),
                    ("..", "1", "4096", "d")), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        sweep = self.sweep(remote, fake)
        rms = [a for a in calls if a[0] == gc.REMOTE_RM]
        self.assertEqual(rms, [[gc.REMOTE_RM, "-rf", "--", str(rhome / "scripts_override")],
                               [gc.REMOTE_RM, "-rf", "--", str(rhome / "skills_override")]])
        self.assertEqual(sweep.overrides_removed, ["sandbox platform/scripts_override", "sandbox platform/skills_override"])

        calls.clear()
        state["kinds"] = "linked"
        sweep = self.sweep(remote, fake)
        self.assertEqual([a for a in calls if a[0] == gc.REMOTE_RM], [])
        self.assertTrue(any("symlink" in w for w in sweep.warnings), sweep.warnings)

    def test_an_unreachable_sandbox_is_tried_once(self):
        (self.profiles / "cluster-a").mkdir()
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            raise sandbox_exec.SandboxUnavailable("no route to host")

        sweep = self.sweep(Path("/sandbox-root"), fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([w for w in sweep.warnings if "sandbox" in w]), 1, sweep.warnings)

    def test_the_nightly_job_removes_and_logs_them(self):
        override = self.make_override("skills_override")
        import io
        home = self.profiles.parent
        hermes_cli = type(sys)("hermes_cli")
        hermes_cli.kanban_db = type("K", (), {"list_boards": staticmethod(lambda: [])})
        with patch.dict(sys.modules, {"hermes_cli": hermes_cli}), \
             patch.dict("os.environ", {"PLATFORM_AGENT_HOME": str(home)}), \
             patch.object(sandbox_exec, "sandbox_enabled", return_value=False), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(gc.main(), 0)
        self.assertFalse(override.exists())
        log = (home / gc.LOGS_DIR / gc.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("removed 1 override trees: platform/skills_override", log)

    def test_a_flood_of_overrides_is_logged_by_count_past_the_limit(self):
        import io
        home = self.profiles.parent
        for i in range(gc.INVENTORY_LIMIT + 3):
            (self.home / f"a{i:03d}_override").mkdir()
        hermes_cli = type(sys)("hermes_cli")
        hermes_cli.kanban_db = type("K", (), {"list_boards": staticmethod(lambda: [])})
        with patch.dict(sys.modules, {"hermes_cli": hermes_cli}), \
             patch.dict("os.environ", {"PLATFORM_AGENT_HOME": str(home)}), \
             patch.object(sandbox_exec, "sandbox_enabled", return_value=False), \
             patch("sys.stdout", new_callable=io.StringIO):
            gc.main()
        log = (home / gc.LOGS_DIR / gc.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn(" and 3 more", log)

    def test_no_expected_home_entry_carries_the_suffix(self):
        for profile in ("platform", "cluster-a", "other"):
            for name in gc.expected_entries(profile):
                self.assertIsNone(gc.OVERRIDE_NAME.fullmatch(name), (profile, name))

    def test_a_trailing_newline_does_not_match(self):
        self.assertIsNone(gc.OVERRIDE_NAME.fullmatch("skills_override\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
