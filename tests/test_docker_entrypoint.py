"""Tests for the shared-state gate in deploy/shared/docker-entrypoint.sh.

    python3 -m unittest discover -s tests -p 'test_*.py'

The Deployment runs this image twice against ONE data PVC — the gateway
(`hermes gateway run`) and the dashboard (`hermes dashboard`) — but the operator mounts
the plugin image volumes and the operator-rendered config overlays into the gateway
container only. Everything the entrypoint does below step 1.5 writes to that shared tree,
so the two containers must not both run it: the dashboard's pass reads the gateway's fresh
plugin links as dangling and unlinks them, and reverts the overlay it finds no source for.

That failure is silent where it happens and loud somewhere else — a kanban worker exits 1
with "Unknown skill(s)", retries twice, and the board fills with blocked tasks while the
AgentPlugin still reports Ready. Nothing downstream of the gate can catch it, so the gate
is tested here directly.

The setup steps are all guarded on paths that exist only inside the image (/opt/defaults,
/opt/hermes), so running the real script on a host is safe: the one observable thing it
does is create $PLATFORM_AGENT_HOME/logs at step 5. That directory is the probe for
"did the setup run".

That probe is valid ON A HOST ONLY, and the reason is the same absent /opt/hermes. Inside
the image, step 1 runs upstream's stage2-hook.sh above the gate and lays down the Hermes
skeleton — logs/ included — in EVERY container, so there logs/ proves nothing. Anything
re-checking this against a real container wants scripts/ or profiles/platform/profile.yaml
instead, which only the gated steps below create.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

_ENTRYPOINT = (
    pathlib.Path(__file__).resolve().parents[1] / "deploy" / "shared" / "docker-entrypoint.sh"
)

# The gate announces its decision on stderr in both directions. Asserting on that rather
# than on a filesystem side effect is what makes these tests mean the same thing here and
# inside the image — see the module docstring for why the side effect does not.
_OWNS = "owns the shared state"
_DISOWNS = "does not own the shared state"


class SharedStateGateTest(unittest.TestCase):
    def _run(self, argv, env=None, echo=True):
        """Run the entrypoint with `argv` as the command it would exec.

        `echo` stands in for the real binary: it is on every PATH, and its output proves
        the entrypoint reached `exec "$@"` rather than dying partway. Pass `echo=False`
        to hand the entrypoint `argv` verbatim — the only way to reach an empty one.

        Returns `(proc, owns)`, where `owns` is the gate's own announced decision.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "data"
            full_env = {"PATH": "/usr/bin:/bin", "PLATFORM_AGENT_HOME": str(home)}
            full_env.update(env or {})
            proc = subprocess.run(
                ["sh", str(_ENTRYPOINT), *(["echo"] if echo else []), *argv],
                capture_output=True,
                text=True,
                env=full_env,
                timeout=60,
            )
            # `_DISOWNS` contains "own the", not "owns the", so the two never both match.
            disowns = _DISOWNS in proc.stderr
            owns = _OWNS in proc.stderr
            if owns == disowns:
                self.fail(
                    "the gate must announce exactly one decision; a silent branch is one "
                    f"nothing downstream can check. stderr was:\n{proc.stderr}"
                )
            # Corroborate the announcement against the only side effect observable on a
            # host, so the log line cannot drift into lying about what the script did.
            # Valid HERE ONLY, for the reason the module docstring gives.
            self.assertEqual(
                owns,
                (home / "logs").is_dir(),
                "the gate's announced decision disagrees with whether the setup ran",
            )
            return proc, owns

    def test_gateway_container_runs_the_setup(self):
        proc, ran_setup = self._run(["hermes", "gateway", "run"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(ran_setup, "the gateway container must build the shared tree")

    def test_dashboard_sidecar_skips_the_setup(self):
        proc, ran_setup = self._run(["hermes", "dashboard"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(
            ran_setup,
            "the dashboard sidecar shares the PVC but not the plugin/overlay mounts; "
            "letting it run the setup is what unlinks the gateway's plugins",
        )
        self.assertIn("does not own the shared state", proc.stderr)

    def test_the_sidecar_still_execs_its_command(self):
        """Skipping the setup must not skip the process the container exists to run."""
        proc, _ = self._run(["hermes", "dashboard"])
        self.assertIn("hermes dashboard", proc.stdout)

    def test_an_unrecognised_sidecar_is_excluded_by_default(self):
        """A new sidecar is opted out until someone decides otherwise.

        The alternative default — run the setup unless the command is known to be a
        sidecar — makes every future container an unnoticed corruption of the shared tree.
        """
        _, ran_setup = self._run(["hermes", "some-future-subcommand"])
        self.assertFalse(ran_setup)

    def test_a_command_that_merely_mentions_gateway_is_not_the_gateway(self):
        """The match is on a whole argument, not a substring of the command line.

        `*gateway*` would hand shared-state ownership to anything that happens to name one
        — a kanban board, a namespace, a log file — which is the same corruption this gate
        exists to stop, arriving from a direction nobody would look in.
        """
        _, ran_setup = self._run(["hermes", "kanban", "ls", "--board", "gateway-migration"])
        self.assertFalse(ran_setup)

    def test_the_gateway_is_recognised_when_invoked_by_absolute_path(self):
        _, ran_setup = self._run(["/opt/hermes/.venv/bin/hermes", "gateway", "run"])
        self.assertTrue(ran_setup)

    def test_the_override_forces_the_setup_on(self):
        _, ran_setup = self._run(
            ["hermes", "dashboard"], env={"AGENT_SHARED_STATE_SETUP": "owner"}
        )
        self.assertTrue(ran_setup)

    def test_the_override_forces_the_setup_off(self):
        _, ran_setup = self._run(
            ["hermes", "gateway", "run"], env={"AGENT_SHARED_STATE_SETUP": "skip"}
        )
        self.assertFalse(ran_setup)

    def test_an_unrecognised_override_warns_and_falls_back_to_detection(self):
        """A typo in the escape hatch must not pass silently.

        Falling back to auto-detection is the safe behaviour, and on its own it is also
        the invisible one: an operator who wrote `Owner` gets exactly what they would
        have got by setting nothing, and believes they forced the setup on. The value
        here differs from a valid one only in case.
        """
        proc, ran_setup = self._run(
            ["hermes", "dashboard"], env={"AGENT_SHARED_STATE_SETUP": "Owner"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(ran_setup, "an unrecognised value must not force the setup on")
        self.assertIn("unrecognised AGENT_SHARED_STATE_SETUP", proc.stderr)

    def test_the_documented_default_is_not_reported_as_a_typo(self):
        """`auto` is the documented default; naming it explicitly must stay silent."""
        proc, _ = self._run(
            ["hermes", "gateway", "run"], env={"AGENT_SHARED_STATE_SETUP": "auto"}
        )
        self.assertNotIn("unrecognised AGENT_SHARED_STATE_SETUP", proc.stderr)

    def test_the_leader_election_gateway_is_not_detectable_from_its_argv(self):
        """Why the operator sets the variable instead of trusting auto-detection.

        Above one replica the gateway container runs the leader-election wrapper, which
        starts `hermes gateway run` as a CHILD. Its own argv never says `gateway`, so it
        reads as a sidecar. This test pins the limitation rather than a desired
        behaviour — if a future change makes argv detection cover this case, the guard in
        the operator becomes belt-and-braces rather than the only thing standing between
        an HA deployment and an unpopulated HERMES_HOME.
        """
        _, ran_setup = self._run(
            ["/opt/hermes/.venv/bin/python3", "/opt/data/leader_elect.py"]
        )
        self.assertFalse(ran_setup)

    def test_the_leader_election_gateway_runs_the_setup_when_declared_the_owner(self):
        """The operator's HA container spec, end to end.

        `Args: [python3, <home>/leader_elect.py]` with no `Command`, so the image
        ENTRYPOINT still runs, plus AGENT_SHARED_STATE_SETUP=owner. Setting `Command`
        instead is what removed the entrypoint from the chain entirely and left an HA pod
        with no container building the tree.
        """
        proc, ran_setup = self._run(
            ["/opt/hermes/.venv/bin/python3", "/opt/data/leader_elect.py"],
            env={"AGENT_SHARED_STATE_SETUP": "owner"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(ran_setup)
        # and it still execs the wrapper it was given
        self.assertIn("leader_elect.py", proc.stdout)

    def test_an_explicit_skip_still_execs_its_command(self):
        """The dashboard's operator-set path: excluded from the setup, not from running."""
        proc, ran_setup = self._run(
            ["hermes", "dashboard"], env={"AGENT_SHARED_STATE_SETUP": "skip"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(ran_setup)
        self.assertIn("hermes dashboard", proc.stdout)

    def test_an_explicit_skip_with_no_command_does_not_run_the_setup(self):
        """`skip` must not be able to mean `owner`.

        `exec` with no operands returns instead of replacing the shell, so an empty argv
        used to fall out of the skip branch and run every step below it — the one value
        that exists to stop the setup producing the setup, then exiting 0 on the second
        no-op `exec` as though a process had been started and had finished cleanly.
        """
        proc, ran_setup = self._run([], env={"AGENT_SHARED_STATE_SETUP": "skip"}, echo=False)
        self.assertFalse(ran_setup, "an explicit skip must never build the shared tree")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no command to exec", proc.stderr)

    def test_no_command_at_all_is_a_setup_only_invocation(self):
        """The other half of an empty argv: with no `skip`, it still owns the tree.

        This is the shape an initContainer would use — do the setup, exec nothing. The
        gate must not read "no arguments" as "not the gateway".
        """
        proc, ran_setup = self._run([], echo=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(ran_setup)


def _extract_shell_function(name):
    """Return the source of one shell function from the entrypoint.

    Step 2.6a's helper is the only part of the script that can be exercised in
    isolation: it takes its two directories as arguments and reads no globals. Lifting
    it out is what makes the failure paths testable at all — reaching them through the
    whole script would mean arranging for a `mv` to fail inside a container image.

    Brace-counting rather than a regex because the body contains `}` inside strings
    would break a lazy match, and a stale extraction that silently returned the wrong
    function would make every test below pass against nothing.
    """
    lines = _ENTRYPOINT.read_text(encoding="utf-8").splitlines()
    for start, line in enumerate(lines):
        if line.startswith(f"{name}() {{"):
            break
    else:
        raise AssertionError(f"{name}() not found in {_ENTRYPOINT}")
    for end in range(start, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[start : end + 1])
    raise AssertionError(f"{name}() has no closing brace")


class SyncProfileSkillsTest(unittest.TestCase):
    """Step 2.6a replaces a profile's skills/ wholesale, and must never abort start-up.

    The function runs as a bare command under `set -e`, so any command in it that fails
    without a guard does not degrade to a stale skills directory — it kills the
    container before `exec "$@"`, which is a CrashLoopBackOff caused by the step that
    exists to keep skills fresh. The PVC it writes to can fail for reasons that have
    nothing to do with this script, so "the write failed" has to be an ordinary outcome.

    Each test asserts on both halves: the exit status (start-up survives) and the
    contents of skills/ (the profile is never left without one).
    """

    # The staging paths are suffixed with the pod name, so a test that plants a
    # leftover has to plant it under the same name the function will look for.
    # Pinning HOSTNAME rather than reading the real one keeps the expected paths
    # spellable and keeps the suite from depending on the machine it runs on.
    POD = "test-pod-0"
    NEW = f"skills.new.{POD}"
    OLD = f"skills.old.{POD}"

    def _sync(self, src_parent, dst_parent, preamble="", pod=None):
        """Run the real function under `set -e`, returning the completed process.

        `preamble` is shell injected between the function definition and the call.
        It exists for one job: shadowing a command the function uses, so a test can
        interleave a second writer at an exact point. Some of the guards here are
        reachable only when another process acts between two of this function's own
        statements, and a test that cannot produce that state asserts nothing —
        which is not a hypothetical, it is what the first version of the rollback
        test did, silently passing against the very bug it named.

        `pod` sets the HOSTNAME the function derives its staging names from, so a
        test can run two of them against one profile the way two replicas share one
        ReadWriteMany volume.
        """
        script = f"set -e\n{_extract_shell_function('sync_profile_skills')}\n"
        script += preamble
        script += f'sync_profile_skills "{src_parent}" "{dst_parent}"\necho DONE\n'
        return subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "HOSTNAME": pod or self.POD},
        )

    def _tree(self, root, **files):
        root = pathlib.Path(root)
        root.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (root / name).write_text(body, encoding="utf-8")
        return root

    def _restore_modes(self, root):
        """Make every directory under `root` writable again, wherever it ended up.

        The tests that deny a write do it with a mode bit, and TemporaryDirectory
        cannot clean up behind them. Restoring by walking rather than by remembered
        path matters: the function under test may legitimately have MOVED the
        directory, and a teardown that insists on the old path turns an assertion
        failure into a FileNotFoundError from the `finally`.
        """
        root = pathlib.Path(root)
        if not root.exists():
            return
        for path in [root, *root.rglob("*")]:
            if path.is_dir():
                path.chmod(0o700)

    def test_the_image_copy_replaces_the_volume_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self._tree(tmp / "template" / "skills", **{"kept.md": "new"})
            self._tree(tmp / "profile" / "skills", **{"kept.md": "old", "retired.md": "x"})

            proc = self._sync(tmp / "template", tmp / "profile")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            skills = tmp / "profile" / "skills"
            self.assertEqual((skills / "kept.md").read_text(), "new")
            self.assertFalse(
                (skills / "retired.md").exists(),
                "a whole-directory replace is the point: a skill dropped from the image "
                "has to actually disappear, or a retired procedure stays loadable",
            )

    def test_no_staging_directories_are_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self._tree(tmp / "template" / "skills", **{"a.md": "a"})
            self._tree(tmp / "profile" / "skills", **{"a.md": "old"})

            self._sync(tmp / "template", tmp / "profile")

            names = sorted(p.name for p in (tmp / "profile").iterdir())
            self.assertEqual(names, ["skills"], "no staging directory may survive")

    def test_a_template_without_skills_is_not_an_error(self):
        """A template that ships no skills must leave the profile's alone, not empty it."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "template").mkdir()
            self._tree(tmp / "profile" / "skills", **{"local.md": "keep"})

            proc = self._sync(tmp / "template", tmp / "profile")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((tmp / "profile" / "skills" / "local.md").read_text(), "keep")

    def test_a_profile_with_no_skills_yet_gets_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self._tree(tmp / "template" / "skills", **{"a.md": "a"})
            (tmp / "profile").mkdir()

            proc = self._sync(tmp / "template", tmp / "profile")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((tmp / "profile" / "skills" / "a.md").read_text(), "a")

    def test_an_unwritable_profile_warns_instead_of_killing_start_up(self):
        """The plainest failure: the swap cannot happen, and start-up goes on regardless.

        A read-only profile directory fails the staging copy, which is the first thing
        that touches the destination — the one failure path that was already handled, and
        the baseline the rest of them now match.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self._tree(tmp / "template" / "skills", **{"a.md": "a"})
            profile = self._tree(tmp / "profile" / "skills", **{"old.md": "keep"}).parent
            profile.chmod(0o500)
            try:
                proc = self._sync(tmp / "template", profile)
            finally:
                profile.chmod(0o700)

            self.assertEqual(
                proc.returncode,
                0,
                "a failed skills sync must not abort the entrypoint:\n" + proc.stderr,
            )
            self.assertIn("DONE", proc.stdout, "execution must continue past the helper")
            self.assertIn("WARN", proc.stderr, "a silent skip is the bug, not the fix")
            self.assertEqual(
                (profile / "skills" / "old.md").read_text(),
                "keep",
                "a profile that cannot be refreshed keeps the skills it had",
            )

    @unittest.skipIf(os.geteuid() == 0, "root ignores the mode bits this test relies on")
    def test_an_unremovable_leftover_does_not_kill_start_up(self):
        """The regression this guards: `rm -rf` of the staging dirs was unguarded.

        A boot killed mid-swap can leave a `skills.old` the next boot cannot delete —
        here a read-only directory with a file in it, which `rm -rf` cannot empty. Under
        `set -e` that non-zero exit used to be the last thing the entrypoint did.

        Every later step then fails too (`mv skills skills.old` onto a surviving
        directory would move it *inside*, and cannot, because that directory is
        read-only), so the sync does not happen. That is the whole contract: it degrades
        to the profile keeping the skills it had, and says so, rather than to a container
        that never starts.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self._tree(tmp / "template" / "skills", **{"a.md": "new"})
            self._tree(tmp / "profile" / "skills", **{"a.md": "old"})
            stuck = self._tree(tmp / "profile" / self.OLD, **{"junk.md": "junk"})
            stuck.chmod(0o500)
            try:
                proc = self._sync(tmp / "template", tmp / "profile")
            finally:
                stuck.chmod(0o700)

            self.assertEqual(
                proc.returncode,
                0,
                "an undeletable leftover must not abort the entrypoint:\n" + proc.stderr,
            )
            self.assertIn("DONE", proc.stdout, "execution must continue past the helper")
            self.assertIn("WARN", proc.stderr, "a silent skip is the bug, not the fix")
            self.assertEqual(
                (tmp / "profile" / "skills" / "a.md").read_text(),
                "old",
                "a profile that cannot be refreshed keeps the skills it had",
            )
            self.assertFalse(
                (tmp / "profile" / self.NEW).exists(),
                "the abandoned staging copy must not be left where the next boot "
                "could mistake it for the profile's own",
            )

    @unittest.skipIf(os.geteuid() == 0, "root ignores the mode bits this test relies on")
    def test_an_unclearable_staging_copy_does_not_nest_the_new_skills(self):
        """The destructive half of the hazard the third guard covers for `mv`.

        `cp -a src dst` nests INSIDE dst when dst exists, exactly as `mv` does, and
        the opening `rm -rf` is best-effort — so a `skills.new` that survives it
        makes the staging copy land at skills.new/skills. Every command then exits
        0: `mv skills skills.old` succeeds, `mv skills.new skills` finds $_dst free
        and succeeds, and the closing `rm -rf skills.old` deletes the only real
        copy. The test for the sibling case above asserts the `mv` version fails
        SAFE; this one exists because the `cp` version failed destructive and
        silent — a profile with no loadable skills, on a start-up that reported
        success.

        The leftover here is a read-only subdirectory holding a file, which
        `rm -rf` cannot empty while leaving its writable parent in place — the
        shape an interrupted boot leaves behind on a volume whose ownership
        changed under it, or that an NFS silly-rename left a `.nfsXXXX` entry in.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self._tree(tmp / "template" / "skills", **{"a.md": "new"})
            self._tree(tmp / "profile" / "skills", **{"a.md": "old"})
            self._tree(tmp / "profile" / self.NEW / "sub", **{"junk.md": "junk"}).chmod(0o500)
            try:
                proc = self._sync(tmp / "template", tmp / "profile")
            finally:
                # By path, not by the handle taken above: against the unguarded
                # function the read-only directory is MOVED (to profile/skills/sub),
                # so restoring a captured path raises FileNotFoundError out of the
                # `finally` and buries the assertion that was the point of the test.
                self._restore_modes(tmp / "profile")

            self.assertEqual(
                proc.returncode,
                0,
                "an unclearable staging copy must not abort the entrypoint:\n" + proc.stderr,
            )
            self.assertIn("DONE", proc.stdout, "execution must continue past the helper")
            self.assertIn("WARN", proc.stderr, "a silent skip is the bug, not the fix")
            self.assertFalse(
                (tmp / "profile" / "skills" / "skills").exists(),
                "the staged copy must never install one level deep: nothing loads "
                "from skills/skills and nothing prunes it",
            )
            self.assertEqual(
                (tmp / "profile" / "skills" / "a.md").read_text(),
                "old",
                "a profile that cannot be refreshed keeps the skills it had, rather "
                "than losing them to the closing rm -rf",
            )

    def test_the_rollback_does_not_nest_the_previous_skills(self):
        """The third instance of the nesting hazard, in the arm that recovers from it.

        The install guard's left arm fires precisely BECAUSE `$_dst` exists — and
        that is the one condition under which `mv "$_dst.old" "$_dst"` nests instead
        of restoring. Unguarded, the rollback buries the profile's previous skills
        at `skills/skills.old`: invisible to the loader, never pruned, and reported
        as a clean warning while the profile silently runs on whatever occupied
        `$_dst`.

        Reaching that arm needs `$_dst` to reappear BETWEEN the aside-move and the
        install, which one process cannot do to itself: the opening `rm -rf` clears
        any staged `skills.old`, and after `mv skills skills.old` succeeds nothing
        single-threaded recreates `skills`. Staging the directories up front
        therefore tests nothing — the first version of this test did exactly that
        and passed against the unguarded function.

        So the second writer is real. Shadowing `mv` lets the test recreate `$_dst`
        the instant the aside-move completes, which is precisely what another pod
        does on the one ReadWriteMany volume the operator hands the replicas at
        `availability.replicas > 1`.
        """
        # Only the aside-move has a $2 under skills.old; the install and the
        # rollback both target $_dst itself, so this fires once and leaves them be.
        # The glob is loose enough to match both the tagged name and the fixed one a
        # mutation would restore, so the mutation check still reaches this arm.
        intruder = (
            "mv() {\n"
            "    _rc=0\n"
            '    command mv "$@" || _rc=$?\n'
            '    case "$2" in\n'
            '        *skills.old*) mkdir -p "$1" 2>/dev/null && echo intruder > "$1/intruder.md" ;;\n'
            "    esac\n"
            '    return "$_rc"\n'
            "}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self._tree(tmp / "template" / "skills", **{"a.md": "new"})
            self._tree(tmp / "profile" / "skills", **{"a.md": "previous"})

            proc = self._sync(tmp / "template", tmp / "profile", preamble=intruder)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("DONE", proc.stdout, "execution must continue past the helper")
            self.assertTrue(
                (tmp / "profile" / "skills" / "intruder.md").exists(),
                "the interleave did not happen; the test would assert nothing",
            )
            self.assertFalse(
                any((tmp / "profile" / "skills").glob("skills.old*")),
                "the rollback must never nest the previous skills inside the live "
                "directory: nothing loads from there and nothing prunes it",
            )

    def test_one_pod_does_not_clear_another_pods_swap(self):
        """The opening `rm -rf` used to reach into a second replica's swap.

        `$_dst` is on the PVC, and at `availability.replicas > 1` every replica gets
        the same one, so staging paths named `skills.new` and `skills.old` were
        shared names on a shared volume. The function opens by removing both, before
        any guard. That is a pod deleting whatever another pod has staged — and,
        worse, the aside-moved directory that is the profile's ONLY copy of its
        previous skills during the window between the two renames. The victim's
        install then fails with nothing to restore, and it reports "the profile keeps
        its existing copy" over a profile that has no skills at all.

        Staged as two sequential runs rather than a live race, because the damage
        does not need them to overlap in time — only in namespace. The first pod is
        stopped mid-swap by a shim that fails any `mv` onto `skills` itself, which
        leaves exactly the state the window consists of: no `skills`, and the
        previous copy parked under that pod's aside name. The second pod then runs
        clean. The assertion is that the first pod's parked copy is still there.

        Restore the fixed names and this fails on that assertion: the second pod's
        opening `rm -rf` takes it.
        """
        stuck_mid_swap = (
            "mv() {\n"
            '    case "$2" in\n'
            "        */skills) return 1 ;;\n"
            "    esac\n"
            '    command mv "$@"\n'
            "}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self._tree(tmp / "template" / "skills", **{"a.md": "new"})
            self._tree(tmp / "profile" / "skills", **{"a.md": "the only previous copy"})

            first = self._sync(
                tmp / "template", tmp / "profile", preamble=stuck_mid_swap, pod="other-pod-1"
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            # The canary globs rather than naming the path, so that it reports a
            # broken setup and only that. Asserting the tagged name here would make
            # the mutation fail on the canary instead of on the consequence, which
            # is the assertion worth reading.
            aside = list((tmp / "profile").glob("skills.old*"))
            self.assertEqual(
                len(aside), 1, "the first pod did not end up mid-swap; nothing is under test"
            )
            self.assertFalse(
                (tmp / "profile" / "skills").exists(),
                "mid-swap means skills/ is absent; nothing is under test",
            )
            parked = aside[0]

            second = self._sync(tmp / "template", tmp / "profile")

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue(
                (parked / "a.md").exists(),
                f"the second pod deleted {parked.name}, which was another pod's only "
                "copy of the previous skills: staging paths must be private to a pod",
            )
            self.assertEqual((tmp / "profile" / "skills" / "a.md").read_text(), "new")
            self.assertEqual(
                parked.name,
                "skills.old.other-pod-1",
                "the staging name is what makes it private; it must carry the pod name",
            )

    def test_a_leftover_staging_directory_does_not_wedge_the_next_start(self):
        """A boot killed mid-swap leaves skills.new/skills.old; the next one must recover."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self._tree(tmp / "template" / "skills", **{"a.md": "new"})
            self._tree(tmp / "profile" / "skills", **{"a.md": "old"})
            self._tree(tmp / "profile" / self.NEW, **{"junk.md": "junk"})
            self._tree(tmp / "profile" / self.OLD, **{"junk.md": "junk"})

            proc = self._sync(tmp / "template", tmp / "profile")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((tmp / "profile" / "skills" / "a.md").read_text(), "new")
            self.assertFalse(
                (tmp / "profile" / "skills" / "junk.md").exists(),
                "a stale skills.new must be cleared, not moved into place or nested",
            )
            self.assertEqual(sorted(p.name for p in (tmp / "profile").iterdir()), ["skills"])


if __name__ == "__main__":
    unittest.main()
