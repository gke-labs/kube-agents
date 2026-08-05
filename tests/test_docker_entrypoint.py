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
"""

import pathlib
import subprocess
import tempfile
import unittest

_ENTRYPOINT = (
    pathlib.Path(__file__).resolve().parents[1] / "deploy" / "shared" / "docker-entrypoint.sh"
)


class SharedStateGateTest(unittest.TestCase):
    def _run(self, argv, env=None):
        """Run the entrypoint with `argv` as the command it would exec.

        `echo` stands in for the real binary: it is on every PATH, and its output proves
        the entrypoint reached `exec "$@"` rather than dying partway.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "data"
            full_env = {"PATH": "/usr/bin:/bin", "PLATFORM_AGENT_HOME": str(home)}
            full_env.update(env or {})
            proc = subprocess.run(
                ["sh", str(_ENTRYPOINT), "echo", *argv],
                capture_output=True,
                text=True,
                env=full_env,
                timeout=60,
            )
            # step 5 is the last thing the setup does, and the only one that leaves a mark
            # outside the image.
            return proc, (home / "logs").is_dir()

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
        self.assertIn("skipping shared-state setup", proc.stderr)

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


if __name__ == "__main__":
    unittest.main()
