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


if __name__ == "__main__":
    unittest.main()
