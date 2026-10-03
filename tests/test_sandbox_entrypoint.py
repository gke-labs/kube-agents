"""Tests for the home-root sync, the image-tree staging and its read-only
mount gate, and the database tripwire in deploy/sandbox/entrypoint.sh.

    python3 -m unittest discover -s tests -p 'test_*.py'

The script runs as root inside the sandbox and chowns to uid 1000, neither of
which a test host can do. So `chown` and `install` are stubbed onto PATH and the
assertions are about which paths the script hands them — the same technique
tests/test_docker_entrypoint.py uses for the gate next door, and for the same
reason: the interesting behaviour is a decision, not a side effect.

What is being pinned is that every component between $DATA and a nested home root
ends up owned by the sandboxed account, not just the leaf. `install -d -o/-g`
applies the ownership to the last component only, so `profiles/platform` used to
leave `$DATA/profiles` root-owned. That state is readable and traversable, which
is why it survived review and a live upgrade: the platform profile is agent-owned,
the shell works, every skill works. It fails only when something creates a sibling
of `platform` — which is what sandbox_mirror.py does for each of the agent pod's
other profiles, so the migration aborts and the model's files never arrive.

The image trees (skills, scripts, governance) are the opposite case: they must
never be handed to the sandboxed account. So the chown stub records the owner
beside each path, and the tests below say which owner each path gets. Whether a
tree is a read-only mount is read from a mount table, which the tests replace
with a file of their own through SANDBOX_MOUNTINFO.
"""

import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_ENTRYPOINT = _REPO / "deploy" / "sandbox" / "entrypoint.sh"

# The script exits non-zero well after the part under test: step 2 refuses a
# missing authorized_keys, which this test never provides. Everything asserted
# here happens at step 1a or 1b, above that.
#
# One `owner path` line per path. Flags (-R) are skipped; the first operand is
# the owner and every later one is a path it was applied to.
_CHOWN_STUB = """#!/bin/sh
owner=""
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    *)
      if [ -z "$owner" ]; then
        owner="$arg"
      else
        echo "$owner $arg" >>"$CHOWN_LOG"
      fi
      ;;
  esac
done
exit 0
"""

_TREES = ("skills", "scripts", "governance")
_PREPARE = "--prepare-image-trees"

# Drops -o/-g -- the real ones need root -- and keeps the directory creation the
# loop depends on. Deliberately NOT a passthrough to /usr/bin/install: stubbing
# out the ownership is what makes the chown log the only record of it.
#
# -m takes an argument and so has to be consumed like -o/-g rather than ignored
# like -d: dropped from the case below, `install -d -m 0555 x` reads 0555 as a
# second directory to create and the mode silently becomes a path.
_INSTALL_STUB = """#!/bin/sh
dirs=""
mode=""
while [ $# -gt 0 ]; do
  case "$1" in
    -d) ;;
    -m) shift; mode="$1" ;;
    -o|-g) shift ;;
    -*) ;;
    *) dirs="$dirs $1" ;;
  esac
  shift
done
mkdir -p $dirs
if [ -n "$mode" ]; then
  chmod "$mode" $dirs
fi
"""


class _SandboxEntrypointHarness(unittest.TestCase):
    """Setup shared by the classes below. No tests of its own — a concrete case
    here would be collected once per subclass and reported as several."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.data = self.tmp / "data"
        self.data.mkdir()
        self.defaults = self.tmp / "defaults"
        (self.defaults / "scripts").mkdir(parents=True)
        (self.defaults / "scripts" / "forge.py").write_text("# placeholder\n")
        (self.defaults / "skills" / "fleet-audit" / "scripts").mkdir(parents=True)
        (self.defaults / "skills" / "fleet-audit" / "scripts" / "audit_report.py").write_text(
            "# placeholder\n"
        )
        (self.defaults / "governance").mkdir()
        (self.defaults / "governance" / "compliance_audit_sop.md").write_text("# SOP\n")

        self.chown_log = self.tmp / "chown.log"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("chown", _CHOWN_STUB), ("install", _INSTALL_STUB)):
            stub = bin_dir / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.bin_dir = bin_dir

    def _run(
        self, home_roots: str, *args: str, extra_env: dict[str, str] | None = None
    ) -> list[tuple[str, str]]:
        """Run the entrypoint and return the (owner, path) pairs it chowned.

        The completed process is kept on self.result for the exit code and log.
        """
        env = dict(os.environ)
        # A developer shell that happens to export these must not change the mode.
        env.pop("SANDBOX_IMAGE_TREES", None)
        env.pop("SANDBOX_MOUNTINFO", None)
        env.update(
            {
                "PATH": f"{self.bin_dir}{os.pathsep}{env['PATH']}",
                "CHOWN_LOG": str(self.chown_log),
                "SANDBOX_DATA": str(self.data),
                "SANDBOX_DEFAULTS": str(self.defaults),
                "SANDBOX_HOME_ROOTS": home_roots,
                # Absent on purpose: step 2 and step 3 are past the part under
                # test and are expected to end the run.
                "SANDBOX_SSHD_STATE": str(self.tmp / "absent-sshd"),
                "SANDBOX_AUTHORIZED_KEYS": str(self.tmp / "absent-keys"),
            }
        )
        env.update(extra_env or {})
        self.result = subprocess.run(
            ["bash", str(_ENTRYPOINT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if not self.chown_log.exists():
            return []
        pairs = []
        for line in self.chown_log.read_text().splitlines():
            if line:
                owner, path = line.split(" ", 1)
                pairs.append((owner, path))
        return pairs

    def _tree_paths(self) -> list[pathlib.Path]:
        """Every <home>/<tree> for the operator's two home roots."""
        return [
            home / tree
            for home in (self.data, self.data / "profiles" / "platform")
            for tree in _TREES
        ]

    def _under(self, path: str, root: pathlib.Path) -> bool:
        return path == str(root) or path.startswith(f"{root}/")


class SandboxEntrypointHomeRootsTest(_SandboxEntrypointHarness):
    def test_intermediate_directories_are_chowned_with_the_leaf(self) -> None:
        """`profiles/platform` must leave $DATA/profiles agent-owned too."""
        chowned = self._run(". profiles/platform")
        self.assertIn(
            ("agent:agent", str(self.data / "profiles")),
            chowned,
            "the parent of a nested home root was left with the entrypoint's own "
            "ownership; sandbox_mirror.py cannot create the other profiles' homes "
            "beside it and the migration aborts",
        )

    def test_the_data_root_itself_is_not_walked_past(self) -> None:
        """The walk stops at $DATA, which step 1 already owns."""
        chowned = self._run(". profiles/platform")
        parent = str(self.data.parent)
        self.assertNotIn(
            parent,
            [path for _, path in chowned],
            "the walk escaped $DATA and chowned its parent, which belongs to the "
            "image rather than to the model",
        )

    def test_a_deeper_root_chowns_every_component(self) -> None:
        """Nothing here is special-cased to one level of nesting."""
        chowned = self._run("profiles/a/b/c")
        for component in ("profiles", "profiles/a", "profiles/a/b"):
            self.assertIn(("agent:agent", str(self.data / component)), chowned, component)

    def _displaced(self, path: pathlib.Path) -> list[pathlib.Path]:
        return sorted(p for p in path.parent.iterdir() if p.name.startswith(f"{path.name}.displaced"))

    def test_a_file_where_a_home_root_belongs_is_moved_aside(self) -> None:
        """Everything under $DATA is uid 1000's, including the home roots.

        `install -d` exits 71 on a path that exists and is not a directory, and
        `set -e` takes the container with it, so `rm -rf profiles/platform &&
        touch profiles/platform` from a sandbox shell used to stop this pod
        starting for good -- with no way back, because the pod you would exec
        into to repair it is the one that is down. The symlink pass does not
        reach this: it removes links and leaves plain files alone.
        """
        (self.data / "profiles").mkdir()
        planted = self.data / "profiles" / "platform"
        planted.write_text("not a directory")

        self._run(". profiles/platform")

        self.assertTrue(planted.is_dir(), "the home root was not recreated")
        moved = self._displaced(planted)
        self.assertEqual(1, len(moved), f"expected the file to be moved aside, found {moved}")
        # Renamed, not deleted: it is broken state either way, but it is the
        # model's own byte and the entrypoint is not what decides it is worthless.
        self.assertEqual("not a directory", moved[0].read_text())

    def test_a_file_at_an_intermediate_component_is_moved_aside_too(self) -> None:
        """`install -d` creates the parents, so a file at one fails the same way."""
        planted = self.data / "profiles"
        planted.write_text("not a directory either")

        self._run(". profiles/platform")

        self.assertTrue((self.data / "profiles" / "platform").is_dir())
        self.assertEqual(1, len(self._displaced(planted)))

    def test_the_sandbox_marker_is_not_displaced_on_every_start(self) -> None:
        """$DATA/.sandbox is a regular file on purpose.

        It shares the symlink walk with the home roots, so displacing every
        non-directory the walk sees would move the marker aside once per start
        and leave a new copy behind each time.
        """
        self._run(". profiles/platform")
        marker = self.data / ".sandbox"
        self.assertTrue(marker.is_file(), "the marker should be a plain file")
        self.assertEqual([], self._displaced(marker))

    def test_a_directory_where_the_marker_belongs_is_moved_aside(self) -> None:
        """`mkdir /opt/data/.sandbox` is the same wedge by the opposite input.

        `cat >` fails with EISDIR against a directory, and `set -euo pipefail`
        ends the run before sshd starts. The symlink pass does not reach it and
        the home-root displacement deliberately does not either, so this path
        needs the narrower check of its own.
        """
        planted = self.data / ".sandbox"
        planted.mkdir()
        (planted / "kept").write_text("the model put this here")

        self._run(". profiles/platform")

        self.assertTrue(planted.is_file(), "the marker was not rewritten as a file")
        self.assertIn("shell sandbox's /opt/data", planted.read_text())
        moved = self._displaced(planted)
        self.assertEqual(1, len(moved), f"expected the directory to be moved aside, found {moved}")
        self.assertEqual("the model put this here", (moved[0] / "kept").read_text())


class SandboxEntrypointDatabaseTripwireTest(_SandboxEntrypointHarness):
    """Step 1b, which makes the agent pod's databases fail to open rather than
    open empty.

    The defect is that sqlite3 creates a database it cannot find. A worker that
    reaches for the board from the sandbox shell -- which both SOUL.md files
    forbid, and which a stuck worker does anyway -- gets no error, no tables and
    exit 0, and an empty board is a plausible enough answer to act on. One did,
    on 2026-09-04: 25 minutes, then `kanban_block` with "local direct DB access
    to kanban.db returns empty tables", and a 0-byte file left on the volume so
    the next worker saw the same thing.
    """

    def _boards(self) -> list[pathlib.Path]:
        return [
            self.data / "kanban.db",
            self.data / "state.db",
            self.data / "profiles" / "platform" / "kanban.db",
            self.data / "profiles" / "platform" / "state.db",
        ]

    def test_opening_a_board_from_the_sandbox_fails_instead_of_returning_empty(
        self,
    ) -> None:
        """The property that matters, asserted through sqlite3 rather than stat.

        A directory is the mechanism, not the requirement -- what the sandbox
        owes the model is that the call raises.

        Connected the way a model would, with no `mode=rw`: that is the call
        that creates the file when nothing is there, and creating it is the
        defect. An assertion that passes because the path is simply absent would
        pass today, against the behaviour this is here to change.
        """
        self._run(". profiles/platform")
        for board in self._boards():
            with self.subTest(board=str(board)):
                self.assertTrue(board.is_dir(), "no tripwire at the board's path")
                with self.assertRaises(sqlite3.OperationalError):
                    sqlite3.connect(str(board)).execute(
                        "select name from sqlite_master"
                    )

    def test_the_tripwire_says_where_the_board_actually_is(self) -> None:
        """An error the model cannot act on just moves where it gets stuck."""
        self._run(". profiles/platform")
        note = self.data / "kanban.db" / "NOT-THE-AGENT-POD-DATABASE.txt"
        self.assertTrue(note.is_file(), "no explanation beside the tripwire")
        self.assertIn("kanban_show", note.read_text())

    def test_a_fabricated_empty_database_is_cleared_off_the_volume(self) -> None:
        """The 0-byte file outlives the worker that created it.

        $DATA is a PVC, so without this the board reads as empty for every
        worker on that volume until someone deletes the file by hand.
        """
        planted = self.data / "kanban.db"
        planted.write_bytes(b"")

        self._run(". profiles/platform")

        self.assertTrue(planted.is_dir(), "the fabricated database was left in place")

    def test_the_model_can_still_clear_its_own_home(self) -> None:
        """Nothing step 1b makes is undeletable, in the fallback run.

        This run has SANDBOX_IMAGE_TREES unset, so nothing is mounted and the
        trees are plain root-owned copies; the test host owns every file, so
        rmtree succeeding shows only that the tripwire is an ordinary directory
        with a mode its owner can remove. That is the property: root-owned and
        0555 would also make sqlite3 raise, and would add an undeletable name
        to the model's home for nothing. The directory is the mechanism; the
        mode never was.

        Under the operator the home roots and the trees are mount points, and
        `rm -rf /opt/data/profiles` fails partway on purpose. That is for
        deploy/sandbox/smoke-test.sh to show, not this test.
        """
        self._run(". profiles/platform")
        shutil.rmtree(self.data / "profiles")
        self.assertFalse((self.data / "profiles").exists())

    def test_the_note_is_handed_to_the_sandboxed_account(self) -> None:
        """The other half of the same invariant, which rmtree cannot see.

        The test host is not root, so `chown` is stubbed and ownership is only
        observable as the paths the script hands it -- the technique this
        module's docstring describes.
        """
        chowned = self._run(". profiles/platform")
        for board in self._boards():
            with self.subTest(board=str(board)):
                self.assertIn(
                    ("agent:agent", str(board / "NOT-THE-AGENT-POD-DATABASE.txt")),
                    chowned,
                )

    def test_the_tripwire_is_not_rebuilt_on_every_start(self) -> None:
        """A pod recycle must not churn the volume it is protecting."""
        self._run(". profiles/platform")
        note = self.data / "kanban.db" / "NOT-THE-AGENT-POD-DATABASE.txt"
        marker = note.parent / "witness"
        marker.write_text("survived")

        self._run(". profiles/platform")

        self.assertTrue(
            marker.is_file(),
            "step 1b tore down a tripwire it had already put there",
        )


class SandboxEntrypointPrepareModeTest(_SandboxEntrypointHarness):
    """--prepare-image-trees, which the operator's init container runs.

    It stages every image tree into every home root, root-owned, heals what
    would otherwise become a mount point through a planted link, and exits
    before anything that writes outside $DATA.
    """

    def _assert_same_tree(self, expected: pathlib.Path, actual: pathlib.Path) -> None:
        self.assertTrue(actual.is_dir() and not actual.is_symlink(), f"{actual} is not a directory")
        want = sorted(p.relative_to(expected) for p in expected.rglob("*"))
        got = sorted(p.relative_to(actual) for p in actual.rglob("*"))
        self.assertEqual(want, got, f"{actual} does not match {expected}")
        for rel in want:
            if (expected / rel).is_file():
                self.assertEqual((expected / rel).read_bytes(), (actual / rel).read_bytes(), str(rel))

    def test_every_tree_is_staged_in_both_homes_and_owned_by_root(self) -> None:
        chowned = self._run(". profiles/platform", _PREPARE)
        self.assertEqual(0, self.result.returncode, self.result.stderr)
        for tree in self._tree_paths():
            with self.subTest(tree=str(tree)):
                self._assert_same_tree(self.defaults / tree.name, tree)
                self.assertIn(("root:root", str(tree)), chowned)
                handed = [p for owner, p in chowned if owner != "root:root" and self._under(p, tree)]
                self.assertEqual([], handed, "part of an image tree was handed to the model")
                for path in (tree, *tree.rglob("*")):
                    self.assertEqual(0, path.stat().st_mode & 0o022, f"{path} is group- or other-writable")

    def test_the_home_roots_are_still_the_models(self) -> None:
        """Only the trees are root's; the model writes everything else in its home."""
        chowned = self._run(". profiles/platform", _PREPARE)
        for home in (self.data, self.data / "profiles", self.data / "profiles" / "platform"):
            self.assertIn(("agent:agent", str(home)), chowned, str(home))

    def test_an_old_copy_is_replaced_not_merged(self) -> None:
        """A volume from before this change holds agent-owned trees the model may have edited."""
        for tree in (self.data / "scripts", self.data / "profiles" / "platform" / "scripts"):
            tree.mkdir(parents=True)
            (tree / "forge.py").write_text("# planted\n")
            (tree / "witness.py").write_text("# planted helper\n")

        self._run(". profiles/platform", _PREPARE)

        for tree in (self.data / "scripts", self.data / "profiles" / "platform" / "scripts"):
            with self.subTest(tree=str(tree)):
                self.assertFalse((tree / "witness.py").exists(), "the planted helper survived")
                self.assertEqual("# placeholder\n", (tree / "forge.py").read_text())

    def _assert_link_healed(self, relative: str) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("not the sandbox's")
        link = self.data / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)

        chowned = self._run(". profiles/platform", _PREPARE)

        self.assertEqual(0, self.result.returncode, self.result.stderr)
        self.assertFalse(link.is_symlink(), f"the symlink at {relative} survived")
        self.assertTrue(link.is_dir(), f"{relative} was not recreated as a directory")
        self.assertIn(f"removed a symlink at {link}", self.result.stderr)
        self.assertEqual(["sentinel"], sorted(p.name for p in outside.iterdir()))
        self.assertEqual("not the sandbox's", (outside / "sentinel").read_text())
        escaped = [p for _, p in chowned if not self._under(p, self.data)]
        self.assertEqual([], escaped, "something outside $DATA was chowned")
        for tree in self._tree_paths():
            self._assert_same_tree(self.defaults / tree.name, tree)

    def test_a_symlink_at_profiles_is_removed(self) -> None:
        self._assert_link_healed("profiles")

    def test_a_symlink_at_a_home_root_is_removed(self) -> None:
        self._assert_link_healed("profiles/platform")

    def test_a_symlink_at_a_tree_path_is_removed(self) -> None:
        """The tree path itself becomes a mount point, so a link there would be followed."""
        self._assert_link_healed("profiles/platform/scripts")

    def test_a_file_where_a_home_root_belongs_is_moved_aside(self) -> None:
        (self.data / "profiles").mkdir()
        planted = self.data / "profiles" / "platform"
        planted.write_text("not a directory")

        self._run(". profiles/platform", _PREPARE)

        self.assertEqual(0, self.result.returncode, self.result.stderr)
        self.assertTrue(planted.is_dir(), "the home root was not recreated")
        moved = [p for p in planted.parent.iterdir() if p.name.startswith("platform.displaced")]
        self.assertEqual(1, len(moved), moved)
        self._assert_same_tree(self.defaults / "scripts", planted / "scripts")

    def test_it_exits_before_writing_anything_outside_data(self) -> None:
        """The init container has a read-only root filesystem and no key mounted.

        Reaching step 2 would fail on the missing authorized_keys; exiting 0
        with no complaint about it is how this shows it stopped before.
        """
        self._run(". profiles/platform", _PREPARE)
        self.assertEqual(0, self.result.returncode, self.result.stderr)
        self.assertNotIn("authorized_keys", self.result.stderr)
        self.assertFalse((self.data / ".sandbox").exists(), "the marker is the shell's to write")
        self.assertFalse((self.data / "kanban.db").exists(), "the tripwire is the shell's to write")

    def test_it_refuses_an_image_with_no_trees(self) -> None:
        """Exiting 0 here would leave the shell's read-only mounts empty."""
        shutil.rmtree(self.defaults)
        self._run(". profiles/platform", _PREPARE)
        self.assertNotEqual(0, self.result.returncode)

    def test_it_refuses_an_image_whose_defaults_are_empty(self) -> None:
        """An empty /opt/defaults stages nothing: as bad as a missing one."""
        shutil.rmtree(self.defaults)
        self.defaults.mkdir()
        self._run(". profiles/platform", _PREPARE)
        self.assertNotEqual(0, self.result.returncode)
        self.assertIn("no image trees under", self.result.stderr)


class SandboxEntrypointReadOnlyMountGateTest(_SandboxEntrypointHarness):
    """Default mode with SANDBOX_IMAGE_TREES=read-only-mounts, as the operator sets it.

    The trees were staged by the init container and are mounted read-only, so
    step 1a must not touch them. It checks the mount table instead, and a tree
    that is missing from it or mounted writable stops the start.
    """

    def setUp(self) -> None:
        super().setUp()
        # What the init container leaves behind.
        self._run(". profiles/platform", _PREPARE)
        self.assertEqual(0, self.result.returncode, self.result.stderr)
        self.chown_log.unlink()
        self.witness = self.data / "profiles" / "platform" / "scripts" / "witness"
        self.witness.write_text("staged by the init container")
        self.mountinfo = self.tmp / "mountinfo"

    def _write_mountinfo(self, options: dict[pathlib.Path, str], pins: bool = True) -> None:
        lines = [f"30 1 0:30 / {self.data} rw,relatime - ext4 /dev/sdb rw"]
        if pins:
            options = {self.data / "profiles": "rw,relatime", self.data / "profiles" / "platform": "rw,relatime", **options}
        for n, (path, opts) in enumerate(options.items()):
            lines.append(f"{40 + n} 30 0:30 /{path.relative_to(self.data)} {path} {opts} - ext4 /dev/sdb rw")
        self.mountinfo.write_text("\n".join(lines) + "\n")

    def _run_gated(self) -> list[tuple[str, str]]:
        return self._run(
            ". profiles/platform",
            extra_env={
                "SANDBOX_IMAGE_TREES": "read-only-mounts",
                "SANDBOX_MOUNTINFO": str(self.mountinfo),
            },
        )

    def test_read_only_trees_are_left_alone(self) -> None:
        self._write_mountinfo({tree: "ro,relatime" for tree in self._tree_paths()})
        chowned = self._run_gated()
        self.assertTrue(self.witness.is_file(), "step 1a restaged a tree that is a read-only mount")
        for tree in self._tree_paths():
            touched = [pair for pair in chowned if self._under(pair[1], tree)]
            self.assertEqual([], touched, f"{tree} was chowned")
        # Past the gate: the run went on to step 2 and stopped at the missing key.
        self.assertIn("no authorized_keys", self.result.stderr)

    def test_a_writable_tree_stops_the_start(self) -> None:
        trees = self._tree_paths()
        options = {tree: "ro,relatime" for tree in trees}
        options[trees[4]] = "rw,relatime"
        self._write_mountinfo(options)
        self._run_gated()
        self.assertNotEqual(0, self.result.returncode)
        self.assertIn(f"{trees[4]} is not a read-only mount", self.result.stderr)
        self.assertNotIn("no authorized_keys", self.result.stderr)

    def test_a_missing_tree_mount_stops_the_start(self) -> None:
        """No mount at all is as bad as a writable one."""
        trees = self._tree_paths()
        self._write_mountinfo({tree: "ro,relatime" for tree in trees[:-1]})
        self._run_gated()
        self.assertNotEqual(0, self.result.returncode)
        self.assertIn(f"{trees[-1]} is not a read-only mount", self.result.stderr)

    def test_a_missing_pin_stops_the_start(self) -> None:
        """Read-only trees under a profiles/ that is not a mount point can be renamed away with it."""
        options = {tree: "ro,relatime" for tree in self._tree_paths()}
        options[self.data / "profiles" / "platform"] = "rw,relatime"
        self._write_mountinfo(options, pins=False)
        self._run_gated()
        self.assertNotEqual(0, self.result.returncode)
        self.assertIn(f"{self.data / 'profiles'} is not a mount point", self.result.stderr)
        self.assertNotIn("no authorized_keys", self.result.stderr)

    def test_ro_is_matched_as_an_option_not_a_substring(self) -> None:
        trees = self._tree_paths()
        options = {tree: "ro,relatime" for tree in trees}
        options[trees[0]] = "rw,errors=remount-ro"
        self._write_mountinfo(options)
        self._run_gated()
        self.assertNotEqual(0, self.result.returncode)
        self.assertIn(f"{trees[0]} is not a read-only mount", self.result.stderr)

    def test_a_missing_defaults_stops_the_start(self) -> None:
        """No /opt/defaults at all is as vacuous as an empty one."""
        self._write_mountinfo({tree: "ro,relatime" for tree in self._tree_paths()})
        shutil.rmtree(self.defaults)
        self._run_gated()
        self.assertNotEqual(0, self.result.returncode)
        self.assertIn("no image trees under", self.result.stderr)
        self.assertNotIn("no authorized_keys", self.result.stderr)

    def test_an_empty_defaults_stops_the_start(self) -> None:
        """With no trees to iterate, the per-tree check would pass vacuously."""
        self._write_mountinfo({tree: "ro,relatime" for tree in self._tree_paths()})
        for entry in self.defaults.iterdir():
            shutil.rmtree(entry)
        self._run_gated()
        self.assertNotEqual(0, self.result.returncode)
        self.assertIn("no image trees under", self.result.stderr)
        self.assertNotIn("no authorized_keys", self.result.stderr)

    def test_an_unknown_mode_stops_the_start(self) -> None:
        self._run(". profiles/platform", extra_env={"SANDBOX_IMAGE_TREES": "read-only"})
        self.assertNotEqual(0, self.result.returncode)
        self.assertIn("not a mode this image knows", self.result.stderr)

    def test_with_the_mode_unset_the_trees_are_root_owned_copies(self) -> None:
        """The fallback outside the operator's StatefulSet: restage, root-owned."""
        chowned = self._run(". profiles/platform")
        self.assertFalse(self.witness.exists(), "the fallback did not restage the tree")
        for tree in self._tree_paths():
            with self.subTest(tree=str(tree)):
                self.assertIn(("root:root", str(tree)), chowned)
                handed = [p for owner, p in chowned if owner != "root:root" and self._under(p, tree)]
                self.assertEqual([], handed, "the fallback handed an image tree to the model")
        self.assertIn("rename a tree aside", self.result.stderr)


class SandboxEntrypointForwardedEnvTest(unittest.TestCase):
    def test_forwarded_env_names_include_context_and_gke_variables(self) -> None:
        """KUBE_CONTEXT_NAME and GKE_* variables must be forwarded to ssh sessions."""
        content = _ENTRYPOINT.read_text()
        match = re.search(r'SANDBOX_FORWARDED_ENV_NAMES=["\']([^"\']+)["\']', content)
        self.assertIsNotNone(match, "SANDBOX_FORWARDED_ENV_NAMES definition not found")
        assert match is not None
        names = set(match.group(1).split())
        required = {
            "CREDENTIAL_PROXY_URL",
            "CREDENTIAL_PROXY_TOKEN_FILE",
            "KUBE_CONTEXT_NAME",
            "GKE_PROJECT_ID",
            "GKE_CLUSTER_NAME",
            "GKE_LOCATION",
        }
        self.assertTrue(
            required.issubset(names),
            f"expected {required} to be subset of {names}",
        )


if __name__ == "__main__":
    unittest.main()
